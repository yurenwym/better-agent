"""Owner-scoped snapshot export and frozen Research replay.

Offline executions use the production engine and control plane in a fresh
evaluation database. Only the provider and retriever are scripted. Public
reports contain digests/identities, never fixture or response bodies.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any
import uuid

from .model_input_snapshot import ModelInputSnapshot, SnapshotError, SnapshotProvenance, canonical_json, deserialize_envelope, freeze_model_input
from .model_input_snapshot_store import ModelInputSnapshotStore
from .model_control import ModelCallContext, ModelControlStore, RoutedModelGateway, new_logical_call
from .model_gateway import GatewayError, ModelRequest, ModelResponse, Timing, UsageBuckets
from .real_evaluation import ResearchRoleReplayEvaluator, LiveEvaluationRunner, QUALITY_JUDGE_PROMPT, SAFETY_JUDGE_PROMPT
from .research.engine import ResearchEngine, ResearchCancelled, UnknownCitation, InsufficientEvidence
from .research.live import LiveResearchModel, build_research_write_messages, DEFAULT_EVIDENCE_STATEMENT
from .research.models import ResearchRequest, ResearchLimits, Source

SCHEMA = "research-snapshot-replay-v1"
CONVERSION = "research-write-sidecar-v1"
JUDGE_VERSION = "research-replay-judge-v2"
SEED = "research-replay-blind-v1"
REQUEST_FIELDS = ("messages", "tools", "temperature", "max_tokens", "thinking", "response_format", "single_attempt")


class ReplayError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class MissingFixture(GatewayError):
    def __init__(self):
        super().__init__("MISSING_FIXTURE", "configuration")


class ResponseLost(BaseException):
    """An interrupted process has no response disposition. Never auto resend."""


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def request_payload(request: ModelRequest) -> dict:
    return {field: getattr(request, field) for field in REQUEST_FIELDS}


def evidence_id(item) -> str:
    return "evidence_" + digest([item.source_id, item.text])[:24]


def _write(path: Path, value: Any) -> None:
    path.write_text(canonical_json(value) + "\n", encoding="utf-8", newline="\n")


def _path(root: Path, reference: str) -> Path:
    if not isinstance(reference, str) or not reference or Path(reference).is_absolute():
        raise ReplayError("FIXTURE_PATH_OUTSIDE_SUITE")
    resolved = (root / reference).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ReplayError("FIXTURE_PATH_OUTSIDE_SUITE")
    return resolved


@contextmanager
def readonly_source(target: str):
    """A store-compatible read connection without Database initialization/migrations."""
    if target.startswith(("postgresql://", "postgres://")):
        import psycopg
        from .db import _postgres_cursor_factory, _compat_row_factory
        connection = psycopg.connect(target, cursor_factory=_postgres_cursor_factory(), row_factory=_compat_row_factory)
        connection.execute("SET TRANSACTION READ ONLY")
    else:
        path = Path(target).resolve()
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
    class ReadStore:
        @contextmanager
        def connection(self):
            yield connection
    try:
        yield ReadStore()
    finally:
        connection.close()


def load_history(store: ModelInputSnapshotStore, owner: str, invocation: str) -> dict:
    if not isinstance(owner, str) or not owner.strip():
        raise ReplayError("TRUSTED_OWNER_REQUIRED")
    try:
        snapshot = store.load_for_invocation(owner, invocation)
    except SnapshotError as exc:
        return {"status": exc.code}
    if snapshot.role != "researcher" or snapshot.purpose != "write_research_section" or snapshot.to_request().tools:
        return {"status": "UNSUPPORTED_ROLE_PURPOSE_OR_TOOLS"}
    return {"status": "READY", "snapshot": snapshot}


def convert_write(snapshot: ModelInputSnapshot, sidecar: dict | None, manifest: dict) -> dict:
    if sidecar is None or sidecar.get("version") != CONVERSION:
        return {"status": "UNSUPPORTED_MISSING_BUSINESS_SIDECAR", "version": CONVERSION}
    if any(key not in sidecar for key in ("heading", "thesis", "evidence", "prior_summary")):
        return {"status": "UNSUPPORTED_INCOMPLETE_SIDECAR", "version": CONVERSION}
    messages = build_research_write_messages(manifest.get("prompts", manifest.get("prompt")),
        sidecar["heading"], sidecar["thesis"], [tuple(item) for item in sidecar["evidence"]], sidecar["prior_summary"])
    rendered = ModelRequest(messages, temperature=0, max_tokens=4096, thinking=False)
    mismatches = [field for field in REQUEST_FIELDS if getattr(rendered, field) != getattr(snapshot.to_request(), field)]
    return {"status": "BASELINE_INPUT_MISMATCH" if mismatches else "EXACT", "version": CONVERSION,
            "mismatched_fields": mismatches, "request_digest": digest(request_payload(snapshot.to_request()))}


def freeze_suite(root: Path, cases: list[dict], fixtures: dict[str, dict]) -> dict:
    """Create an immutable manifest; identical repeated exports are byte-stable."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    normalized = []
    for case in cases:
        fixture = fixtures[case["case_id"]]
        ref = case.get("fixture_ref", f"cases/{case['case_id']}.json")
        path = _path(root, ref)
        payload = (canonical_json(fixture) + "\n").encode("utf-8")
        row = {**case, "fixture_ref": ref, "fixture_digest": digest(fixture),
               "fixture_sha256": hashlib.sha256(payload).hexdigest()}
        normalized.append(row)
    suite = {"schema_version": SCHEMA, "cases": sorted(normalized, key=lambda item: item["case_id"])}
    suite["suite_digest"] = digest(suite)
    manifest = root / "case-manifest.json"
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != suite:
            raise ReplayError("SUITE_IMMUTABLE")
        load_suite(manifest)
        return suite
    # Validate all paths/identities before creating files.
    _validate_cases(suite["cases"])
    for row in normalized:
        path = _path(root, row["fixture_ref"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise ReplayError("FIXTURE_ALREADY_EXISTS")
        _write(path, fixtures[row["case_id"]])
    _write(manifest, suite)
    return suite


def _validate_cases(cases: list[dict]) -> None:
    if not cases:
        raise ReplayError("EMPTY_SUITE")
    ids, lineages = set(), {}
    references = set()
    for case in cases:
        identity, lineage = case.get("case_id"), case.get("lineage_id")
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ReplayError("DUPLICATE_OR_INVALID_CASE")
        if not isinstance(lineage, str) or not lineage:
            raise ReplayError("LINEAGE_REQUIRED")
        partition = case.get("partition")
        if partition not in {"DEV", "HOLDOUT", "SAFETY"}:
            raise ReplayError("INVALID_PARTITION")
        if lineage in lineages:
            raise ReplayError("DUPLICATE_LINEAGE")
        if case.get("replay_unit") not in {"request", "scenario"}:
            raise ReplayError("UNREPLAYABLE_UNIT")
        if case.get("fixture_ref") in references:
            raise ReplayError("DUPLICATE_FIXTURE_PATH")
        references.add(case.get("fixture_ref"))
        ids.add(identity)
        lineages[lineage] = partition


def load_suite(path: Path) -> tuple[dict, dict[str, dict]]:
    path = Path(path)
    suite = json.loads(path.read_text(encoding="utf-8"))
    if suite.get("schema_version") != SCHEMA:
        raise ReplayError("UNKNOWN_SUITE_SCHEMA")
    if suite.get("suite_digest") != digest({k: v for k, v in suite.items() if k != "suite_digest"}):
        raise ReplayError("SUITE_DIGEST_MISMATCH")
    _validate_cases(suite["cases"])
    fixtures = {}
    for case in suite["cases"]:
        raw = _path(path.parent, case["fixture_ref"]).read_bytes()
        value = json.loads(raw)
        if hashlib.sha256(raw).hexdigest() != case["fixture_sha256"] or digest(value) != case["fixture_digest"]:
            raise ReplayError("FIXTURE_DIGEST_MISMATCH")
        if case["replay_unit"] == "request":
            envelope = deserialize_envelope(canonical_json(value["envelope"]))
            source = case["source"]
            if any(source.get(key) != envelope["binding"][field] for key, field in (
                ("bundle_id", "runtime_bundle_id"), ("role", "role"), ("purpose", "purpose"))):
                raise ReplayError("HISTORICAL_SOURCE_BINDING_MISMATCH")
            if not case.get("derived") and digest(envelope) != source["snapshot_digest"]:
                raise ReplayError("HISTORICAL_SOURCE_DIGEST_MISMATCH")
        fixtures[case["case_id"]] = value
    return suite, fixtures


def export_case(store: ModelInputSnapshotStore, owner: str, invocation: str, out: Path, *,
                lineage: str | None = None, sidecar: dict | None = None, manifest: dict | None = None,
                replacements: dict[str, str] | None = None) -> dict:
    loaded = load_history(store, owner, invocation)
    if loaded["status"] != "READY":
        return loaded
    snapshot = loaded["snapshot"]
    envelope = snapshot.envelope()
    derived = bool(replacements)
    if derived:
        # Explicit authorized local transformation; original digest stays separate.
        for message in envelope["request"]["messages"]:
            if isinstance(message.get("content"), str):
                for old, new in replacements.items():
                    message["content"] = message["content"].replace(old, new)
        sidecar = None
    case = {"case_id": "request-" + digest(invocation)[:16], "lineage_id": lineage or "invocation:" + invocation,
            "lineage_status": "task" if lineage else "unknown_task_request_only",
            "partition": "DEV", "replay_unit": "request", "derived": derived,
            "transformation_version": "explicit-replacements-v1" if derived else None,
            "lost_information": ["original message text", "business sidecar"] if derived else [],
            "source": {"invocation_id": invocation, "snapshot_id": snapshot.id, "snapshot_digest": snapshot.content_digest,
                       "bundle_id": snapshot.runtime_bundle_id, "role": snapshot.role, "purpose": snapshot.purpose},
            "conversion": convert_write(snapshot, sidecar, manifest or {}), "expected": {"outcome": "success", "checks": []}}
    fixture = {"envelope": envelope, "sidecar": sidecar, "baseline_manifest": manifest or {},
               "date": "2026-10-09", "scripts": {}, "date_status": "diagnostic_default_not_historical_clock"}
    suite = freeze_suite(Path(out), [case], {case["case_id"]: fixture})
    return {"status": "EXPORTED", "suite_digest": suite["suite_digest"], "conversion": case["conversion"]}


class ScriptedProvider:
    def __init__(self, db, owner: str, script: list[dict], cancel_event: asyncio.Event):
        self.db, self.owner, self.script, self.cancel = db, owner, list(script), cancel_event
        self.trace = []

    async def __call__(self, profile, request, **kwargs):
        # A separate connection verifies commit visibility at every provider send.
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT a.id AS attempt_id,a.ordinal,i.id AS invocation_id,i.context_snapshot_id,i.root_budget_id,"
                "i.execution_context_digest FROM model_attempts a JOIN model_invocations i ON i.id=a.invocation_id "
                "WHERE a.status='STARTED' AND i.owner_id=? AND a.profile_version_id=?",
                (self.owner, profile.registered_profile_version_id)).fetchall()
        matches = []
        for row in rows:
            snapshot = ModelInputSnapshotStore(self.db).load_for_invocation(self.owner, row["invocation_id"])
            if snapshot.role == request.role and snapshot.purpose == request.purpose and request_payload(snapshot.to_request()) == request_payload(request):
                matches.append((row, snapshot))
        if len(matches) != 1:
            raise ReplayError("INVALID_COMMITTED_BINDING")
        row, snapshot = matches[0]
        observation = {**dict(row), "snapshot_digest": snapshot.content_digest,
            "input_digest": digest(request_payload(request)), "role": request.role, "purpose": request.purpose,
            "profile_version_id": profile.registered_profile_version_id, "committed_binding": True}
        self.trace.append(observation)
        if not self.script:
            observation["status"] = "MISSING_FIXTURE"
            raise MissingFixture()
        step = self.script.pop(0)
        if step.get("purpose") != request.purpose or (step.get("input_digest") and step["input_digest"] != observation["input_digest"]):
            observation["status"] = "MISSING_FIXTURE"
            raise MissingFixture()
        observation["step"] = step["step"]
        if step.get("error") == "response_lost":
            observation["status"] = "UNKNOWN"
            raise ResponseLost()
        if step.get("error"):
            observation["status"] = step["error"]
            if step["error"] == "cancelled":
                self.cancel.set()
            raise GatewayError("fixture failure", step["error"])
        observation["status"] = "succeeded"
        observation["output_digest"] = digest(step["text"])
        return ModelResponse(step["text"], [], step.get("finish_reason", "stop"),
                             UsageBuckets(**step.get("usage", {})), Timing(0, None, None), 1)


class FixtureRetriever:
    def __init__(self, fixture: dict):
        self.fixture = fixture
        self.trace = []

    async def retrieve(self, query, request):
        if query not in self.fixture:
            raise MissingFixture()
        sources = [Source(**value) for value in self.fixture[query]]
        self.trace.append({"query_digest": digest(query), "sources_digest": digest([asdict(item) for item in sources])})
        return sources


class SourceBoundControl:
    """Call-scoped store view: shared gateway checks include the historical source.

    The source reference is frozen in each new invocation's snapshot provenance.
    No shared store method is replaced, and concurrent unrelated calls are free
    of this dependency. Both direct and routed gateways use this send gate.
    """
    def __init__(self, control, source_store, owner, reference):
        self.control, self.source_store, self.owner = control, source_store, owner
        self.reference_json = canonical_json(reference)

    def __getattr__(self, name):
        return getattr(self.control, name)

    def assert_request_active(self, invocation_id, context):
        snapshot = self.snapshots.load_for_invocation(self.owner, invocation_id)
        reference = (snapshot.provenance().assembly or {}).get("research_replay_source")
        if context.owner_id != self.owner or canonical_json(reference) != self.reference_json:
            raise ReplayError("HISTORICAL_SOURCE_BINDING_MISMATCH")
        source = self.source_store.snapshots.load_for_invocation(self.owner, reference["invocation_id"])
        if source.content_digest != reference["snapshot_digest"]:
            raise ReplayError("HISTORICAL_SOURCE_BINDING_MISMATCH")
        self.source_store.assert_request_active(reference["invocation_id"],
            ModelCallContext(source.role, source.purpose, owner_id=self.owner, runtime_bundle_id=source.runtime_bundle_id))
        self.control.assert_request_active(invocation_id, context)


class SourceBoundGateway:
    def __init__(self, gateway, source_store, owner, reference):
        self.gateway = gateway
        self.reference_json = canonical_json(reference)
        gateway.control_store = SourceBoundControl(gateway.control_store, source_store, owner, reference)

    def __getattr__(self, name):
        return getattr(self.gateway, name)

    async def complete(self, request, *, provenance=None, **kwargs):
        original = provenance or SnapshotProvenance()
        provenance = replace(original, assembly={**(original.assembly or {}),
                                                 "research_replay_source": json.loads(self.reference_json)})
        return await self.gateway.complete(request, provenance=provenance, **kwargs)


class ResearchEvaluationRunner(LiveEvaluationRunner):
    """Research write + dual blind Judge seam, using the existing paid control plane.

    The caller must supply an already authorized, frozen evaluation config.
    An injected gateway factory is for offline tests only. This runner does not
    create a live evaluation budget or start a paid evaluation from the CLI.
    """
    def __init__(self, model_admin, control_store, *, gateway_factory=None, mode="controlled", source_store=None):
        super().__init__(model_admin, control_store)
        self.gateway_factory, self.mode, self.source_store = gateway_factory, mode, source_store
        self.bindings = []
        self.blind_records = []

    def _gateway(self, version_id):
        return self.gateway_factory(version_id) if self.gateway_factory else super()._gateway(version_id)

    def _preflight(self, config):
        required = ("owner_id", "root_budget_id", "baseline_bundle_id", "candidate_bundle_id", "evaluator_bundle_id",
                    "baseline_model_id", "candidate_model_id", "quality_judge_model_id", "safety_judge_model_id", "price_snapshot_ids")
        if any(not config.get(key) for key in required):
            raise ReplayError("CONTROLLED_CONFIG_INCOMPLETE")
        if config["baseline_model_id"] != config["candidate_model_id"]:
            raise ReplayError("RESEARCH_ARMS_REQUIRE_SAME_PROFILE")
        if config["price_snapshot_ids"].get("baseline") != config["price_snapshot_ids"].get("candidate"):
            raise ReplayError("RESEARCH_ARMS_REQUIRE_SAME_PRICE")
        prices = config["price_snapshot_ids"]
        if set(prices) != {"baseline", "candidate", "quality_judge", "safety_judge"}:
            raise ReplayError("PRICE_BINDINGS_INCOMPLETE")
        validate_pair(self._manifest(config["baseline_bundle_id"]), self._manifest(config["candidate_bundle_id"]),
                      {"heading": "preflight", "thesis": "preflight", "evidence": [], "prior_summary": ""})
        if self.mode == "controlled":
            if self.control_store.db.backend != "postgresql" or self.control_store.costs is None:
                raise ReplayError("CONTROLLED_REQUIRES_AUTHORITATIVE_ROOT_BUDGET")
            with self.control_store.db.connection() as connection:
                root = connection.execute("SELECT 1 FROM task_budget_roots WHERE id=? AND owner_id=? AND root_kind='evaluation'",
                    (config["root_budget_id"], config["owner_id"])).fetchone()
            if root is None:
                raise ReplayError("AUTHORIZED_EVALUATION_ROOT_REQUIRED")
        for label in ("baseline", "candidate", "quality_judge", "safety_judge"):
            self._reservation(config[label + "_model_id"], prices[label])
            role = "researcher" if label in {"baseline", "candidate"} else "judge_quality" if label == "quality_judge" else "judge_safety"
            bundle_id = config[label + "_bundle_id"] if label in {"baseline", "candidate"} else config["evaluator_bundle_id"]
            context = self._context(role, "research_evaluation_preflight", "preflight", bundle_id, prices[label],
                                    owner_id=config["owner_id"], root_budget_id=config["root_budget_id"])
            _, profiles, _ = RoutedModelGateway(self.control_store.db, self.control_store)._route(context)
            if len(profiles) != 1 or profiles[0].registered_profile_version_id != config[label + "_model_id"]:
                raise ReplayError("FROZEN_EVALUATION_PROFILE_MISMATCH")

    def runner(self, config):
        self._preflight(config)
        def invoke(messages, bundle_id, case):
            arm = "baseline" if bundle_id == config["baseline_bundle_id"] else "candidate"
            if bundle_id != config[arm + "_bundle_id"]:
                raise ReplayError("UNBOUND_ARM_BUNDLE")
            if case.get("source"):
                if self.source_store is None:
                    raise ReplayError("CURRENT_SOURCE_AUTHORIZATION_REQUIRED")
                source = self.source_store.snapshots.load_for_invocation(config["owner_id"], case["source"]["invocation_id"])
                if case["source"].get("snapshot_digest") != source.content_digest:
                    raise ReplayError("HISTORICAL_SOURCE_BINDING_MISMATCH")
                self.source_store.assert_request_active(case["source"]["invocation_id"],
                    ModelCallContext(source.role, source.purpose, owner_id=config["owner_id"], runtime_bundle_id=source.runtime_bundle_id))
                conversion = convert_write(source, {"version": CONVERSION, **{key: case[key] for key in ("heading", "thesis", "evidence", "prior_summary")}},
                    self._manifest(config["baseline_bundle_id"]))
                if conversion["status"] != "EXACT":
                    raise ReplayError("UNSUPPORTED_NONEXACT_COMPARISON")
            parent = self._context("researcher", "research_replay", "evaluation_" + uuid.uuid4().hex, bundle_id,
                config["price_snapshot_ids"][arm], owner_id=config["owner_id"], root_budget_id=config["root_budget_id"])
            gateway = self._gateway(config[arm + "_model_id"])
            gateway = self._bind_source(gateway, config, case)
            from .research.models import Evidence
            evidence = [Evidence("fixture-" + str(index), item[1], item[0], None, 1) for index, item in enumerate(case["evidence"])]
            with self.control_store.db.connection() as connection:
                bundle = json.loads(connection.execute("SELECT manifest_json FROM runtime_bundles WHERE id=?", (bundle_id,)).fetchone()[0])
            model = LiveResearchModel(gateway)
            model.runtime_prompt_policy = lambda: bundle["prompts"]
            if model.render_write_messages(case["heading"], case["thesis"], evidence, case["prior_summary"]) != messages:
                raise ReplayError("RENDERED_RESEARCH_INPUT_MISMATCH")
            before = self._identities(config)
            token = gateway.set_call_context(parent)
            try:
                text, _ = asyncio.run(model.write(case["heading"], case["thesis"], evidence, case["prior_summary"]))
            finally:
                gateway.reset_call_context(token)
            identities = self._identities(config) - before
            self._record_bindings(identities)
            values = [self._cost(identity) for identity in identities]
            cost = sum(values) if values and all(value is not None for value in values) else None
            return {"text": text, "cost_microusd": cost if self.mode == "controlled" else None,
                    "ttft_seconds": None, "prompt_digest": _prompt_digest(messages[0]["content"]),
                    "model_identity": config[arm + "_model_id"], "profile_version_id": config[arm + "_model_id"]}
        return invoke

    def _bind_source(self, gateway, config, case):
        if not case.get("source"):
            return gateway
        if self.source_store is None:
            raise ReplayError("CURRENT_SOURCE_AUTHORIZATION_REQUIRED")
        reference = {key: case["source"].get(key) for key in ("invocation_id", "snapshot_digest")}
        return SourceBoundGateway(gateway, self.source_store, config["owner_id"], reference)

    def _manifest(self, bundle_id):
        with self.control_store.db.connection() as connection:
            return json.loads(connection.execute("SELECT manifest_json FROM runtime_bundles WHERE id=?", (bundle_id,)).fetchone()[0])

    def _identities(self, config):
        with self.control_store.db.connection() as connection:
            return {row[0] for row in connection.execute("SELECT id FROM model_invocations WHERE owner_id=? AND root_budget_id=?",
                    (config["owner_id"], config["root_budget_id"])).fetchall()}

    def _record_bindings(self, identities):
        with self.control_store.db.connection() as connection:
            for identity in sorted(identities):
                self.bindings.append(dict(connection.execute("SELECT id,owner_id,role,purpose,context_snapshot_id,"
                    "context_snapshot_digest,runtime_bundle_id,root_budget_id,execution_context_digest "
                    "FROM model_invocations WHERE id=?", (identity,)).fetchone()))
                self.bindings[-1]["attempt_prices"] = [dict(row) for row in connection.execute(
                    "SELECT id,price_snapshot_id,cost_microusd,cost_status FROM model_attempts WHERE invocation_id=? ORDER BY ordinal", (identity,)).fetchall()]

    def judge(self, config):
        self._preflight(config)
        def invoke(payload):
            left_is_baseline = int(digest([SEED, payload["case"]["id"]])[:8], 16) % 2 == 0
            blind = {"left": payload["baseline"] if left_is_baseline else payload["candidate"],
                     "right": payload["candidate"] if left_is_baseline else payload["baseline"]}
            # Only frozen business fields are included. Arm/bundle/model labels
            # and candidate prompt never enter the Judge's task context.
            case = payload["case"]
            context = json.loads(canonical_json({
                "task": {key: case.get(key) for key in ("input", "topic", "heading", "thesis", "prior_summary")},
                "evidence": case["evidence"], "rubric": case.get("rubric", {}),
            }))
            blind["context"] = context
            decisions, costs = [], []
            for label, role, instruction in (("quality_judge", "judge_quality", QUALITY_JUDGE_PROMPT),
                                             ("safety_judge", "judge_safety", SAFETY_JUDGE_PROMPT)):
                gateway = self._gateway(config[label + "_model_id"])
                gateway = self._bind_source(gateway, config, case)
                identity = "model_invocation_eval_" + uuid.uuid4().hex
                response = asyncio.run(gateway.complete(ModelRequest(
                    [{"role": "system", "content": instruction}, {"role": "user", "content": canonical_json(blind)}],
                    temperature=0, max_tokens=300, role=role, purpose="paired_evaluation_judgment"),
                    context=self._context(role, "paired_evaluation_judgment", identity, config["evaluator_bundle_id"],
                        config["price_snapshot_ids"][label], owner_id=config["owner_id"], root_budget_id=config["root_budget_id"])))
                decisions.append(json.loads(response.message))
                costs.append(self._cost(identity))
                self._record_bindings({identity})
            quality, safety = decisions
            if quality.get("winner") not in {"left", "right", "tie"} or any(type(safety.get(key)) is not bool for key in ("left_safe", "right_safe")):
                raise ReplayError("INVALID_BLIND_JUDGE")
            winner = "tie" if quality["winner"] == "tie" else "baseline" if (quality["winner"] == "left") == left_is_baseline else "candidate"
            self.blind_records.append({"case_id": payload["case"]["id"], "left_is_baseline": left_is_baseline,
                "seed_digest": digest(SEED), "rubric_digest": digest(context["rubric"]),
                "task_digest": digest(context["task"]), "evidence_digest": digest(context["evidence"]),
                "judge_prompt_digest": digest([QUALITY_JUDGE_PROMPT, SAFETY_JUDGE_PROMPT]), "judge_version": JUDGE_VERSION,
                "blind_input_digest": digest(blind)})
            return {"winner": winner, "baseline_safe": safety["left_safe" if left_is_baseline else "right_safe"],
                    "candidate_safe": safety["right_safe" if left_is_baseline else "left_safe"],
                    "cost_microusd": sum(costs) if self.mode == "controlled" and all(cost is not None for cost in costs) else None,
                    "judge_profiles": [config["quality_judge_model_id"], config["safety_judge_model_id"]]}
        return invoke


def _prompt_digest(text):
    # Existing ResearchRoleReplayEvaluator uses raw string SHA256 for prompts.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def offline_plane(db, owner: str):
    """Register synthetic versions only in an isolated evaluation database."""
    from .model_admin import ModelAdminService
    from .behavior import BehaviorBundleService
    from .costs import CostService, PriceSnapshot
    admin = ModelAdminService(db, owner_id=owner)
    versions = {}
    for name in ("researcher", "judge_quality", "judge_safety"):
        versions[name] = admin.create_profile({"name": "replay-" + name, "provider_protocol": "openai_compatible",
            "provider_name": "fixture", "base_url": "https://fixture.invalid/v1", "model_name": "fixture-" + name,
            "credential_env_ref": "REPLAY_OFFLINE_UNUSED", "capabilities": {"text": True, "json_object": True, "streaming": True},
            "context_window": 131072, "max_output_tokens": 4096, "timeout_seconds": 30, "max_attempts": 2})["versions"][0]["id"]
    policy = admin.create_policy("replay", {role: {"primary": version, "fallback": []} for role, version in versions.items()})
    routing = {"model_routing": {"policy_id": policy["id"], "digest": policy["policy_digest"]}}
    costs = CostService(db)
    for role, version in versions.items():
        costs.register_price(version, PriceSnapshot("fixture-price-" + role, 1, 1, 1, 1, 1), "2026-01-01T00:00:00+00:00")
    # SQLite has no authoritative root budget tables. Keep its offline root an
    # identity reference, never feed it into the PostgreSQL budget SQL.
    return ModelControlStore(db, costs=costs if db.backend == "postgresql" else None), BehaviorBundleService(db), routing, versions


def policy(fragment: str) -> dict:
    if not isinstance(fragment, str) or not fragment.strip():
        raise ReplayError("EMPTY_CANDIDATE_FRAGMENT")
    return {"prompts": {"researcher": {"write_research_section": {"evidence_statement": fragment.strip()}}}}


def validate_pair(base: dict, candidate: dict, business: dict) -> dict:
    # Validate the complete manifest too, not only the rendered prompt.
    import copy
    left, right = copy.deepcopy(base), copy.deepcopy(candidate)
    for value in (left, right):
        try:
            value["prompts"]["researcher"]["write_research_section"]["evidence_statement"] = "<ALLOWED_FRAGMENT>"
        except (KeyError, TypeError):
            raise ReplayError("INVALID_PROMPT_MANIFEST") from None
    if left != right:
        raise ReplayError("CHANGE_OUTSIDE_ALLOWED_PATH")
    pair = ResearchRoleReplayEvaluator.render_pair(base, candidate, business)
    if not pair["single_allowed_fragment"]:
        raise ReplayError("CHANGE_OUTSIDE_ALLOWED_PATH")
    return pair


def authoritative_cost(db, trace: list[dict], *, mode: str) -> dict:
    attempts = []
    with db.connection() as connection:
        for identity in sorted({row["invocation_id"] for row in trace}):
            attempts.extend(dict(row) for row in connection.execute(
                "SELECT id,cost_microusd FROM model_attempts WHERE invocation_id=? ORDER BY ordinal", (identity,)).fetchall())
    known = bool(attempts) and all(type(row["cost_microusd"]) is int for row in attempts)
    return {"cost_microusd": sum(row["cost_microusd"] for row in attempts) if known and mode == "controlled" else None,
            "status": "known" if known and mode == "controlled" else "unknown",
            "source": "authoritative_ledger" if mode == "controlled" else "fixture/simulated",
            "simulated_microusd": sum(row["cost_microusd"] for row in attempts) if known and mode == "offline" else None,
            "attempt_count": len(attempts),
            "known_cost_microusd": sum(row["cost_microusd"] for row in attempts if type(row["cost_microusd"]) is int) if mode == "controlled" else None,
            "unknown_cost_attempts": sum(row["cost_microusd"] is None for row in attempts)}


async def execute_case(case: dict, fixture: dict, gateway, parent: ModelCallContext, *,
                       arm: str, manifest: dict, provider: ScriptedProvider | None = None, mode="offline") -> dict:
    started = time.perf_counter()
    cancel = provider.cancel if provider else asyncio.Event()
    model = LiveResearchModel(gateway, today=lambda: date.fromisoformat(fixture["date"]))
    model.runtime_prompt_policy = lambda: manifest["prompts"]
    events, outputs = [], []
    outcome, reason, failure_stage = "success", None, None
    token = gateway.set_call_context(parent)
    try:
        if case["replay_unit"] == "request":
            raw = canonical_json(fixture["envelope"])
            envelope = deserialize_envelope(raw)
            binding = envelope["binding"]
            snapshot = ModelInputSnapshot(content_json=raw, content_digest=hashlib.sha256(raw.encode()).hexdigest(), **binding)
            if binding["role"] != "researcher" or binding["purpose"] != "write_research_section" or snapshot.to_request().tools:
                raise ReplayError("UNSUPPORTED_ROLE_PURPOSE_OR_TOOLS")
            request = snapshot.to_request()
            if arm == "candidate":
                if case["derived"] or case["conversion"]["status"] != "EXACT":
                    raise ReplayError("UNSUPPORTED_NONEXACT_COMPARISON")
                sidecar = fixture["sidecar"]
                request = replace(request, messages=build_research_write_messages(manifest["prompts"], sidecar["heading"],
                    sidecar["thesis"], [tuple(item) for item in sidecar["evidence"]], sidecar["prior_summary"]))
            response = await gateway.complete(request, context=new_logical_call(parent, role=request.role, purpose=request.purpose))
            from .research.delivery import deliver_section
            outputs.append(deliver_section(response.message, (fixture.get("sidecar") or {}).get("heading", "Research"), response.finish_reason)["delivered"])
        else:
            retriever = FixtureRetriever(fixture["retrieval"])
            engine = ResearchEngine(model, retriever, today=model.today, evidence_identity=evidence_id)
            request = ResearchRequest(parent.invocation_id, fixture["topic"], tuple(fixture["source_scopes"]),
                                      ResearchLimits(**fixture["limits"]), cancel_event=cancel)
            async for event in engine.run_research(request):
                failure_stage = event.phase
                events.append({"type": event.type, "phase": event.phase, "data_digest": digest(_event_value(event.data))})
                if event.type == "section":
                    outputs.append(event.data["markdown"])
                if event.type == "report" and event.data["completion_status"] != "COMPLETED":
                    outcome, reason = "rejected", "incomplete_delivery"
            events.extend({"type": "retrieval", **row} for row in retriever.trace)
    except ResponseLost:
        outcome, reason = "UNKNOWN", "response_lost_no_automatic_resume"
    except (ResearchCancelled, asyncio.CancelledError):
        outcome, reason = "cancelled", "cancelled"
    except InsufficientEvidence:
        outcome, reason = "insufficient_evidence", "insufficient_evidence"
    except UnknownCitation:
        outcome, reason = "rejected", "unknown_citation"
    except MissingFixture:
        outcome, reason = "invalid", "MISSING_FIXTURE"
    except (ReplayError, GatewayError) as exc:
        outcome, reason = "invalid", getattr(exc, "code", getattr(exc, "kind", "gateway_error"))
        if provider and any(row.get("status") == "MISSING_FIXTURE" for row in provider.trace):
            reason = "MISSING_FIXTURE"
    finally:
        gateway.reset_call_context(token)
    text = "\n".join(outputs)
    checks = case["expected"].get("checks", [])
    failures = []
    if "known_citations" in checks:
        from .research.citations import CITATION
        ids = set(fixture.get("source_ids", []))
        markers = CITATION.findall(text)
        if outcome == "success" and (not markers or not set(markers) <= ids):
            failures.append("unknown_or_missing_citation")
    for forbidden in fixture.get("safety_forbidden", []):
        if forbidden.casefold() in text.casefold():
            failures.append("source_instruction_violation")
    if failures:
        outcome, reason = "rejected", failures[0]
    if provider and provider.script and outcome == "success":
        outcome, reason = "invalid", "UNUSED_MODEL_FIXTURE"
    passed = outcome == case["expected"]["outcome"] and (not case["expected"].get("reason") or reason == case["expected"]["reason"])
    trace = provider.trace if provider else []
    semantic = {"case_id": case["case_id"], "lineage_id": case["lineage_id"], "partition": case["partition"],
                "outcome": outcome, "reason": reason, "contract_pass": passed, "failure_stage": failure_stage if outcome != "success" else None,
                "score": int(passed), "safety_pass": "source_instruction_violation" not in failures,
                "output_digest": digest(text), "events": events, "input_digests": [row["input_digest"] for row in trace],
                "logical_calls": len({row["invocation_id"] for row in trace}), "attempts": len(trace)}
    return {**semantic, "semantic_digest": digest(semantic), "bindings": trace,
            "cost": authoritative_cost(gateway.control_store.db, trace, mode=mode),
            "offline_elapsed_seconds": time.perf_counter() - started if mode == "offline" else None,
            "_text": text}


def _event_value(value):
    if hasattr(value, "__dataclass_fields__"):
        return _event_value(asdict(value))
    if isinstance(value, dict):
        return {key: _event_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_event_value(item) for item in value]
    return value


async def run_offline(suite_path: Path, out: Path, *, arm="baseline", fragment: str | None = None) -> dict:
    from .db import Database
    suite, fixtures = load_suite(suite_path)
    if any(case["partition"] != "DEV" for case in suite["cases"]):
        raise ReplayError("SEALED_PARTITION_REQUIRES_MANAGED_EVALUATION")
    manifest = policy(fragment or DEFAULT_EVIDENCE_STATEMENT)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)  # Ambiguous partial batches never auto resume.
    _write(out / "batch-state.json", {"status": "RUNNING", "suite_digest": suite["suite_digest"]})
    db = Database(out / "evaluation.db")
    try:
        owner = "replay-offline-operator"
        control, bundles, routing, versions = offline_plane(db, owner)
        bundle = bundles.ensure({**routing, **manifest})
        gateway = RoutedModelGateway(db, control)
        records = []
        for case in suite["cases"]:
            fixture = fixtures[case["case_id"]]
            if arm == "candidate":
                business = fixture.get("sidecar") or fixture.get("write_input", {})
                validate_pair(fixture.get("baseline_manifest", policy(DEFAULT_EVIDENCE_STATEMENT)), manifest, business)
            script_arm = "regression" if arm == "candidate" and fixture.get("regression_manifest_digest") == digest(manifest) else arm
            script = fixture.get("scripts", {}).get(script_arm)
            if not isinstance(script, list):
                raise ReplayError("MISSING_MODEL_FIXTURE")
            provider = ScriptedProvider(db, owner, script, asyncio.Event())
            gateway._execute_attempt = provider
            identity = "evaluation_" + uuid.uuid4().hex
            root_id = "evaluation_root_" + uuid.uuid4().hex
            if db.backend == "postgresql":
                root_id = control.costs.create_default_root_budget(owner, "evaluation", identity)["id"]
            from .execution_context import create_root_context
            parent = ModelCallContext.from_harness(create_root_context(owner_id=owner, runtime_bundle_id=bundle.id, root_budget_id=root_id),
                                                   role="researcher", purpose="research_replay", invocation_id=identity)
            result = await execute_case(case, fixture, gateway, parent, arm=arm, manifest=manifest, provider=provider)
            result.pop("_text")
            records.append(result)
            _write(out / f"case-{len(records):03}.json", result)
        report = {"schema_version": SCHEMA, "kind": "offline_research_dev_replay", "mode": "offline", "arm": arm,
            "suite_digest": suite["suite_digest"], "manifest_digest": digest(manifest), "bundle_id": bundle.id,
            "profile_config_digest": digest({"model": "fixture", "context_window": 131072, "max_output_tokens": 4096, "max_attempts": 2}),
            "model_parameters_digest": digest({"temperature": 0, "max_tokens": 4096, "thinking": False, "tools": None}),
            "records": records, "planned_cases": len(suite["cases"]), "passed": sum(row["contract_pass"] for row in records),
            "cost_source": "fixture/simulated", "cost_microusd": None, "release_eligible": False,
            "engineering_status": "PASS" if all(row["contract_pass"] for row in records) else "FAIL"}
        report["report_digest"] = digest(report)
        _write(out / "report.json", report)
        _write(out / "batch-state.json", {"status": "COMPLETED", "report_digest": report["report_digest"]})
        return report
    except BaseException:
        _write(out / "batch-state.json", {"status": "STOPPED", "reason": "no_automatic_resume"})
        raise
    finally:
        db.close()


def compare_reports(baseline: dict, candidate: dict) -> dict:
    for report in (baseline, candidate):
        if report.get("report_digest") != digest({k: v for k, v in report.items() if k != "report_digest"}):
            raise ReplayError("REPORT_DIGEST_MISMATCH")
    for field in ("suite_digest", "profile_config_digest", "model_parameters_digest", "planned_cases", "mode"):
        if baseline[field] != candidate[field]:
            raise ReplayError("UNPAIRED_REPORTS")
    left = {row["case_id"]: row for row in baseline["records"]}
    right = {row["case_id"]: row for row in candidate["records"]}
    if len(left) != len(baseline["records"]) or len(right) != len(candidate["records"]) or left.keys() != right.keys() or len(left) != baseline["planned_cases"]:
        raise ReplayError("MISSING_OR_DUPLICATE_CASE_RESULTS")
    records = []
    for identity in left:
        a, b = left[identity], right[identity]
        invalid = a["outcome"] in {"invalid", "cancelled", "UNKNOWN"} or b["outcome"] in {"invalid", "cancelled", "UNKNOWN"}
        delta = b["score"] - a["score"]
        records.append({"case_id": identity, "delta": delta, "winner": "invalid" if invalid else "candidate" if delta > 0 else "baseline" if delta < 0 else "tie",
            "baseline": {k: a[k] for k in ("outcome", "reason", "score", "failure_stage", "logical_calls", "attempts", "cost", "semantic_digest")},
            "candidate": {k: b[k] for k in ("outcome", "reason", "score", "failure_stage", "logical_calls", "attempts", "cost", "semantic_digest")},
            "candidate_safe": b["safety_pass"]})
    counts = {name: sum(row["winner"] == name for row in records) for name in ("baseline", "candidate", "tie", "invalid")}
    failed = counts["baseline"] > 0 or any(not row["candidate_safe"] or row["delta"] < 0 for row in records)
    report = {"schema_version": SCHEMA, "suite_digest": baseline["suite_digest"], "planned_cases": baseline["planned_cases"],
        "records": records, "counts": counts, "baseline_report_digest": baseline["report_digest"], "candidate_report_digest": candidate["report_digest"],
        "cost_source": candidate["cost_source"], "baseline_cost_microusd": baseline["cost_microusd"], "candidate_cost_microusd": candidate["cost_microusd"],
        "release_eligible": False, "outcome": "FAIL" if failed else "INSUFFICIENT_EVIDENCE"}
    report["report_digest"] = digest(report)
    return report


def comparison_markdown(report: dict) -> str:
    lines = ["# Research 快照回放比较", "", f"效果结论：**{report['outcome']}**；发布资格：false。",
             f"计划分母：{report['planned_cases']}；胜/负/平/无效：{report['counts']['candidate']}/{report['counts']['baseline']}/{report['counts']['tie']}/{report['counts']['invalid']}。",
             f"费用来源：{report['cost_source']}；baseline/candidate：{report['baseline_cost_microusd']}/{report['candidate_cost_microusd']}（null 为未知）。", "",
             "| Case | 差值 | 判定 | baseline | candidate | 失败阶段/原因 |", "|---|---:|---|---|---|---|"]
    for row in report["records"]:
        b = row["candidate"]
        # Identifiers are untrusted local metadata. Avoid Markdown injection.
        identity = digest(row["case_id"])[:12]
        lines.append(f"| {identity} | {row['delta']} | {row['winner']} | {row['baseline']['outcome']} | {b['outcome']} | {b['failure_stage'] or '-'} / {b['reason'] or '-'} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--invocation", required=True)
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--lineage")
    export.add_argument("--sidecar", type=Path)
    export.add_argument("--baseline-manifest", type=Path)
    run = commands.add_parser("run")
    run.add_argument("--suite", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--mode", choices=("offline", "controlled"), default="offline")
    run.add_argument("--arm", choices=("baseline", "candidate"), default="baseline")
    run.add_argument("--fragment-file", type=Path)
    compare = commands.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            owner, target = os.getenv("REPLAY_OPERATOR_OWNER"), os.getenv("REPLAY_SOURCE_DATABASE")
            if not owner or not target:
                raise ReplayError("TRUSTED_OPERATOR_AND_READONLY_SOURCE_REQUIRED")
            with readonly_source(target) as db:
                result = export_case(ModelInputSnapshotStore(db), owner, args.invocation, args.out, lineage=args.lineage,
                    sidecar=json.loads(args.sidecar.read_text(encoding="utf-8")) if args.sidecar else None,
                    manifest=json.loads(args.baseline_manifest.read_text(encoding="utf-8")) if args.baseline_manifest else None)
        elif args.command == "run":
            if args.mode == "controlled":
                raise ReplayError("CONTROLLED_REQUIRES_EXISTING_AUTHORIZED_EVALUATION_START")
            fragment = args.fragment_file.read_text(encoding="utf-8") if args.fragment_file else None
            result = asyncio.run(run_offline(args.suite, args.out, arm=args.arm, fragment=fragment))
            result = {key: result[key] for key in ("engineering_status", "planned_cases", "passed", "report_digest")}
        else:
            result = compare_reports(json.loads(args.baseline.read_text(encoding="utf-8")), json.loads(args.candidate.read_text(encoding="utf-8")))
            args.out.mkdir(parents=True, exist_ok=False)
            _write(args.out / "comparison.json", result)
            (args.out / "comparison.md").write_text(comparison_markdown(result), encoding="utf-8")
            result = {key: result[key] for key in ("outcome", "counts", "report_digest")}
        print(canonical_json(result))
        return 0
    except (ReplayError, SnapshotError) as exc:
        print(canonical_json({"status": getattr(exc, "code", "REPLAY_REJECTED")}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
