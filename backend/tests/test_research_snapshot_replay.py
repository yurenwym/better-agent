"""RP01–RP18: frozen replay contracts at real Research/gateway boundaries."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from app.db import Database
from app.model_control import ModelCallContext, RoutedModelGateway, InvocationReplayError
from app.model_gateway import ModelRequest, GatewayError
from app.model_input_snapshot import freeze_model_input, canonical_json
from app.model_input_snapshot_store import ModelInputSnapshotStore
from app.real_evaluation import ResearchRoleReplayEvaluator
from app.research_replay import (
    ReplayError, ScriptedProvider, execute_case, export_case, load_history, readonly_source,
    convert_write, freeze_suite, load_suite, run_offline, compare_reports, digest,
    policy, validate_pair, offline_plane, ResearchEvaluationRunner, authoritative_cost,
    comparison_markdown, DEFAULT_EVIDENCE_STATEMENT, CONVERSION, main,
)
from test_model_input_snapshot_store import _invocation
from test_snapshot_gateway import _configured_control_plane, _answer

FIXTURES = Path(__file__).parent / "fixtures/research-snapshot-replay-v3"
CANDIDATE = (FIXTURES / "candidate.txt").read_text(encoding="utf-8")


def historical(tmp_path, *, request=None):
    db = Database(tmp_path / "history.db")
    context = ModelCallContext("researcher", "write_research_section", owner_id="owner-a", runtime_bundle_id=None)
    snapshot = freeze_model_input(request or ModelRequest([{"role": "user", "content": "PRIVATE_BODY https://u:password@host.test"}], max_tokens=None), context)
    with db.transaction() as connection:
        identity = ModelInputSnapshotStore(db).insert(connection, snapshot, snapshot_id="history-snapshot")
    _invocation(db, invocation_id="history-invocation", owner_id="owner-a", role="researcher", purpose="write_research_section",
                runtime_bundle_id=None, snapshot_id=identity, digest=snapshot.content_digest, status="SUCCEEDED")
    return db, ModelInputSnapshotStore(db), snapshot


def single_suite(tmp_path, category="normal"):
    suite, fixtures = load_suite(FIXTURES / "case-manifest.json")
    case = next(copy.deepcopy(case) for case in suite["cases"] if case["case_id"] == category)
    return case, copy.deepcopy(fixtures[category])


async def scenario(tmp_path, category="normal", *, mutate=None):
    db = Database(tmp_path / "evaluation.db")
    control, bundles, routing, versions = offline_plane(db, "operator")
    manifest = policy(DEFAULT_EVIDENCE_STATEMENT)
    bundle = bundles.ensure({**routing, **manifest})
    case, fixture = single_suite(tmp_path, category)
    if mutate:
        mutate(case, fixture)
    provider = ScriptedProvider(db, "operator", fixture["scripts"]["baseline"], asyncio.Event())
    gateway = RoutedModelGateway(db, control, execute_attempt=provider)
    parent = ModelCallContext("researcher", "research_replay", owner_id="operator", runtime_bundle_id=bundle.id,
                              invocation_id="new-evaluation-parent")
    result = await execute_case(case, fixture, gateway, parent, arm="baseline", manifest=manifest, provider=provider)
    assert gateway.current_call_context() is None
    return db, gateway, result, provider


def test_rp01_owner_is_trusted_and_cross_owner_read_is_body_free(tmp_path):
    db, store, _ = historical(tmp_path)
    assert load_history(store, "owner-a", "history-invocation")["status"] == "READY"
    refused = export_case(store, "owner-b", "history-invocation", tmp_path / "export")
    assert refused == {"status": "MODEL_INVOCATION_MISSING"}
    assert "PRIVATE_BODY" not in canonical_json(refused) and not (tmp_path / "export").exists()
    with pytest.raises(ReplayError, match="TRUSTED_OWNER_REQUIRED"):
        load_history(store, "", "history-invocation")
    before = db.path.read_bytes()
    with readonly_source(str(db.path)) as read:
        assert load_history(ModelInputSnapshotStore(read), "owner-a", "history-invocation")["status"] == "READY"
        with read.connection() as connection, pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM model_invocations")
    assert db.path.read_bytes() == before


@pytest.mark.parametrize("kind,code", [("legacy", "SNAPSHOT_BINDING_MISSING"), ("corrupt", "SNAPSHOT_INTEGRITY_ERROR"), ("unknown", "UNKNOWN_SNAPSHOT_VERSION")])
def test_rp02_legacy_corruption_and_unknown_are_classified_without_mutation(tmp_path, kind, code):
    db = Database(tmp_path / "legacy.db")
    sid, sha = None, ""
    if kind != "legacy":
        snapshot = freeze_model_input(ModelRequest([]), ModelCallContext("researcher", "write_research_section", owner_id="owner-a"))
        envelope = snapshot.envelope()
        if kind == "unknown":
            envelope["schema_version"] = "unknown-v99"
        content = canonical_json(envelope)
        sha = "0" * 64 if kind == "corrupt" else hashlib.sha256(content.encode()).hexdigest()
        sid = "bad"
        with db.transaction() as connection:
            connection.execute("INSERT INTO model_input_snapshots(id,owner_id,schema_version,content_json,content_digest,created_at) VALUES (?,?,?,?,?,?)",
                               (sid, "owner-a", "model-input-snapshot-v1", content, sha, "now"))
    _invocation(db, invocation_id="historical", owner_id="owner-a", role="researcher", purpose="write_research_section", runtime_bundle_id=None, snapshot_id=sid, digest=sha)
    before = db.path.read_bytes()
    assert load_history(ModelInputSnapshotStore(db), "owner-a", "historical") == {"status": code}
    assert db.path.read_bytes() == before
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0


def test_rp03_request_copies_are_independent_and_null_survives(tmp_path):
    db, store, original = historical(tmp_path, request=ModelRequest([{"role": "user", "content": [{"type": "text", "text": "frozen"}]}], max_tokens=None))
    snapshot = load_history(store, "owner-a", "history-invocation")["snapshot"]
    first, second = snapshot.to_request(), snapshot.to_request()
    first.messages[0]["content"][0]["text"] = "changed"
    assert second.messages[0]["content"][0]["text"] == "frozen"
    assert first.max_tokens is None and second.max_tokens is None
    assert snapshot.content_json == original.content_json


@pytest.mark.parametrize("tamper", ["fixture", "expected", "partition"])
def test_rp04_export_is_stable_and_every_suite_component_is_frozen(tmp_path, tamper):
    _, store, _ = historical(tmp_path)
    out = tmp_path / "export"
    first = export_case(store, "owner-a", "history-invocation", out)
    bytes_before = (out / "case-manifest.json").read_bytes()
    assert export_case(store, "owner-a", "history-invocation", out) == first
    assert (out / "case-manifest.json").read_bytes() == bytes_before
    suite, fixtures = load_suite(out / "case-manifest.json")
    case = suite["cases"][0]
    if tamper == "fixture":
        path = out / case["fixture_ref"]
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    else:
        case["expected"]["outcome"] = "rejected" if tamper == "expected" else case["expected"]["outcome"]
        if tamper == "partition":
            case["partition"] = "HOLDOUT"
        (out / "case-manifest.json").write_text(canonical_json(suite), encoding="utf-8")
    with pytest.raises(ReplayError, match="DIGEST_MISMATCH"):
        load_suite(out / "case-manifest.json")
    new_suite = freeze_suite(tmp_path / "new", [case], fixtures)
    if tamper != "fixture":
        assert new_suite["suite_digest"] != first["suite_digest"]


def test_rp05_paths_and_derived_cases_do_not_leak_bodies(tmp_path):
    _, store, snapshot = historical(tmp_path)
    exported = export_case(store, "owner-a", "history-invocation", tmp_path / "derived", replacements={"PRIVATE_BODY": "synthetic", "https://u:password@host.test": "synthetic-url"})
    suite, fixtures = load_suite(tmp_path / "derived/case-manifest.json")
    case = suite["cases"][0]
    assert case["derived"] and case["lost_information"] and case["source"]["snapshot_digest"] == snapshot.content_digest
    assert digest(fixtures[case["case_id"]]["envelope"]) != snapshot.content_digest
    assert "PRIVATE_BODY" not in canonical_json(suite) and "password" not in canonical_json(suite)
    assert exported["conversion"]["status"] == "UNSUPPORTED_MISSING_BUSINESS_SIDECAR"
    for reference in ("../outside.json", str(tmp_path / "outside.json")):
        case["fixture_ref"] = reference
        with pytest.raises(ReplayError, match="FIXTURE_PATH_OUTSIDE_SUITE"):
            freeze_suite(tmp_path / "attack", [case], fixtures)


def test_rp06_sidecar_is_versioned_and_mismatch_never_claims_exact(tmp_path):
    from app.research.live import build_research_write_messages
    sidecar = {"version": CONVERSION, "heading": "Setup", "thesis": "Supported", "evidence": [["Evidence", "s1"]], "prior_summary": ""}
    messages = build_research_write_messages(policy(DEFAULT_EVIDENCE_STATEMENT)["prompts"], "Setup", "Supported", [("Evidence", "s1")], "")
    _, _, snapshot = historical(tmp_path, request=ModelRequest(messages, temperature=0, max_tokens=None, thinking=False))
    assert convert_write(snapshot, None, {})["status"] == "UNSUPPORTED_MISSING_BUSINESS_SIDECAR"
    result = convert_write(snapshot, sidecar, policy(DEFAULT_EVIDENCE_STATEMENT))
    assert result["status"] == "BASELINE_INPUT_MISMATCH" and result["mismatched_fields"] == ["max_tokens"]
    exact = freeze_model_input(ModelRequest(messages, temperature=0, max_tokens=4096, thinking=False), ModelCallContext("researcher", "write_research_section", owner_id="owner-a"))
    assert convert_write(exact, sidecar, policy(DEFAULT_EVIDENCE_STATEMENT))["status"] == "EXACT"


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["normal", "multi_section"])
async def test_rp07_real_engine_commits_independent_chapters_and_fixed_inputs(tmp_path, category):
    db, gateway, result, observer = await scenario(tmp_path, category)
    assert result["contract_pass"] and result["outcome"] == "success"
    assert any(row["type"] == "report" for row in result["events"])
    writes = [row for row in observer.trace if row["purpose"] == "write_research_section"]
    assert len(writes) == 2 and len({row["context_snapshot_id"] for row in writes}) == 2
    snapshots = [ModelInputSnapshotStore(db).load_for_invocation("operator", row["invocation_id"]) for row in writes]
    assert "前文摘要：\n" in snapshots[0].to_request().messages[1]["content"]
    assert "前文摘要：Probe" in snapshots[1].to_request().messages[1]["content"]
    plan = ModelInputSnapshotStore(db).load_for_invocation("operator", observer.trace[0]["invocation_id"])
    assert "2026-10-09" in plan.content_json
    assert all(row["committed_binding"] for row in observer.trace)


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["json_repair", "network_retry"])
async def test_rp08_json_repair_and_network_retry_have_distinct_boundaries(tmp_path, category):
    db, _, result, observer = await scenario(tmp_path, category)
    assert result["contract_pass"]
    first, second = observer.trace[:2]
    if category == "json_repair":
        assert first["invocation_id"] != second["invocation_id"] and first["context_snapshot_id"] != second["context_snapshot_id"]
        assert first["input_digest"] != second["input_digest"]
        snapshot = ModelInputSnapshotStore(db).load_for_invocation("operator", first["invocation_id"])
        assert "malformed" not in snapshot.content_json
    else:
        assert first["invocation_id"] == second["invocation_id"] and first["context_snapshot_id"] == second["context_snapshot_id"]
        assert first["attempt_id"] != second["attempt_id"] and first["input_digest"] == second["input_digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize("category,reason", [("insufficient", "insufficient_evidence"), ("unknown_citation", "unknown_citation"), ("source_instruction", "source_instruction_violation")])
async def test_rp09_business_and_safety_rules_reject_violations(tmp_path, category, reason):
    def mutate(case, fixture):
        if category == "source_instruction":
            next(step for step in fixture["scripts"]["baseline"] if step["purpose"] == "write_research_section")["text"] += " SYNTHETIC_SECRET"
            case["expected"] = {"outcome": "rejected", "reason": reason, "checks": ["known_citations"]}
    _, _, result, _ = await scenario(tmp_path, category, mutate=mutate)
    assert result["contract_pass"] and result["reason"] == reason and result["failure_stage"]
    assert result["outcome"] != "success"
    if category == "source_instruction":
        assert not result["safety_pass"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["lost", "cancelled"])
async def test_rp10_interruption_preserves_records_restores_ambient_and_refuses_resend(tmp_path, kind):
    def mutate(case, fixture):
        if kind == "cancelled":
            fixture["scripts"]["baseline"] = [{"step": "cancel-plan", "purpose": "research_structured_step", "error": "cancelled"}]
            case["expected"] = {"outcome": "cancelled", "checks": []}
    db, gateway, result, observer = await scenario(tmp_path, "interruption", mutate=mutate)
    assert result["contract_pass"] and result["outcome"] == ("UNKNOWN" if kind == "lost" else "cancelled")
    row = observer.trace[-1]
    snapshot = ModelInputSnapshotStore(db).load_for_invocation("operator", row["invocation_id"])
    before = snapshot.content_json
    with pytest.raises(InvocationReplayError):
        await gateway.complete(snapshot.to_request(), context=ModelCallContext(snapshot.role, snapshot.purpose, owner_id="operator",
            runtime_bundle_id=snapshot.runtime_bundle_id, invocation_id=row["invocation_id"], idempotency_key=row["invocation_id"]),
            provenance=snapshot.provenance())
    assert snapshot.content_json == before and gateway.current_call_context() is None
    assert len(observer.trace) == result["attempts"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["model", "retrieval"])
async def test_rp11_missing_fixtures_fail_without_external_fallback(tmp_path, monkeypatch, missing):
    import httpx
    async def external(*args, **kwargs):
        pytest.fail("offline must never access external I/O")
    monkeypatch.setattr(httpx.AsyncClient, "send", external)
    def mutate(case, fixture):
        if missing == "model":
            fixture["scripts"]["baseline"] = []
        else:
            fixture["retrieval"] = {}
    _, _, result, observer = await scenario(tmp_path, mutate=mutate)
    assert result["outcome"] == "invalid" and result["reason"] == "MISSING_FIXTURE"
    assert len(observer.trace) == (1 if missing == "model" else 1)


def paired_setup(tmp_path, monkeypatch, *, database=None, controlled=False):
    db, source_bundle, versions = _configured_control_plane(tmp_path, monkeypatch, owner_id="operator", database=database)
    from app.behavior import BehaviorBundleService
    from app.costs import CostService, PriceSnapshot
    from datetime import datetime, timezone, timedelta
    from app.model_control import ModelControlStore
    costs = CostService(db)
    for name in ("chat", "planner", "fallback"):
        costs.register_price(versions[name], PriceSnapshot("price-" + name, 1_000_000, 1_000_000, 1_000_000, 1_000_000, 1_000_000), "2026-01-01T00:00:00+00:00")
    bundles = BehaviorBundleService(db)
    from app.model_admin import ModelAdminService
    roles = {"researcher": {"primary": versions["chat"], "fallback": []},
             "judge_quality": {"primary": versions["planner"], "fallback": []},
             "judge_safety": {"primary": versions["fallback"], "fallback": []}}
    replay_policy = ModelAdminService(db, owner_id="operator").create_policy("research-evaluation", roles)
    routing = {"model_routing": {"policy_id": replay_policy["id"], "digest": replay_policy["policy_digest"]}}
    baseline = bundles.ensure({**routing, **policy(DEFAULT_EVIDENCE_STATEMENT)})
    candidate = bundles.ensure({**routing, **policy(CANDIDATE)})
    evaluator = bundles.ensure({**routing, "judge_version": "fixed-v1"})
    root_id = "new-evaluation-root"
    if controlled:
        root_id = costs.create_root_budget("operator", "evaluation", "new-research-evaluation", max_attempts=50,
            deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), limit_microusd=10_000_000)["id"]
    control = ModelControlStore(db, costs=costs if controlled else None)
    config = {"owner_id": "operator", "root_budget_id": root_id,
              "baseline_bundle_id": baseline.id, "candidate_bundle_id": candidate.id, "evaluator_bundle_id": evaluator.id,
              "baseline_model_id": versions["chat"], "candidate_model_id": versions["chat"],
              "quality_judge_model_id": versions["planner"], "safety_judge_model_id": versions["fallback"],
              "price_snapshot_ids": {"baseline": "price-chat", "candidate": "price-chat", "quality_judge": "price-planner", "safety_judge": "price-fallback"}}
    observers = []
    from snapshot_entrypoint_helpers import CommittedSnapshotTransport
    def factory(version):
        async def execute(profile, request, **kwargs):
            if request.role == "judge_quality":
                text = '{"winner":"tie"}'
            elif request.role == "judge_safety":
                text = '{"left_safe":true,"right_safe":true}'
            else:
                text = "Supported measured result [[source:s1]]"
            observer = CommittedSnapshotTransport(db, "operator", "CS-EV-03", _answer(text))
            observers.append((request, observer))
            return await observer(profile, request, **kwargs)
        return RoutedModelGateway(db, control, execute_attempt=execute)
    runner = ResearchEvaluationRunner(None, control, gateway_factory=factory, mode="controlled" if controlled else "offline")
    case = {"id": "paired-case", "lineage_id": "independent-business-task", "partition": "DEV", "heading": "Setup", "thesis": "Measured result",
            "evidence": [("Supported measured result.", "s1")], "prior_summary": "", "rubric": {"deterministic_required": ["[[source:s1]]"]}}
    return db, baseline, candidate, runner, config, observers, case, source_bundle


def test_rp12_rp13_frozen_model_binding_and_source_request_are_checked(tmp_path, monkeypatch):
    _, base, candidate, runner, config, observers, case, _ = paired_setup(tmp_path, monkeypatch)
    invalid = {**config, "candidate_model_id": config["quality_judge_model_id"]}
    with pytest.raises(ReplayError, match="RESEARCH_ARMS_REQUIRE_SAME_PROFILE"):
        runner.runner(invalid)
    invalid = {**config, "safety_judge_model_id": config["quality_judge_model_id"]}
    invalid["price_snapshot_ids"] = {**config["price_snapshot_ids"], "safety_judge": "price-planner"}
    with pytest.raises(ReplayError, match="FROZEN_EVALUATION_PROFILE_MISMATCH"):
        runner.runner(invalid)
    assert observers == []


@pytest.mark.asyncio
async def test_rp10_nondefault_ambient_is_restored_on_explicit_cancellation(tmp_path):
    db = Database(tmp_path / "eval.db")
    control, bundles, routing, _ = offline_plane(db, "operator")
    manifest = policy(DEFAULT_EVIDENCE_STATEMENT)
    bundle = bundles.ensure({**routing, **manifest})
    case, fixture = single_suite(tmp_path)
    fixture["scripts"]["baseline"] = [{"step": "cancel-plan", "purpose": "research_structured_step", "error": "cancelled"}]
    observer = ScriptedProvider(db, "operator", fixture["scripts"]["baseline"], asyncio.Event())
    gateway = RoutedModelGateway(db, control, execute_attempt=observer)
    sentinel = ModelCallContext("conversation", "unrelated", owner_id="other-owner")
    token = gateway.set_call_context(sentinel)
    try:
        result = await execute_case(case, fixture, gateway, ModelCallContext("researcher", "research_replay", owner_id="operator",
            runtime_bundle_id=bundle.id), arm="baseline", manifest=manifest, provider=observer)
        assert result["outcome"] == "cancelled" and gateway.current_call_context() is sentinel
        assert observer.cancel.is_set() and len(observer.trace) == 1
    finally:
        gateway.reset_call_context(token)


def test_rp12_new_arms_and_dual_judges_preserve_original_state(tmp_path, monkeypatch):
    db, base, candidate, runner, config, observers, case, stable = paired_setup(tmp_path, monkeypatch)
    report = ResearchRoleReplayEvaluator().evaluate(base_manifest=base.manifest, candidate_manifest=candidate.manifest,
        baseline_bundle_id=base.id, candidate_bundle_id=candidate.id, cases=[case], runner=runner.runner(config), judge=runner.judge(config))
    assert report["outcome"] == "INSUFFICIENT_EVIDENCE" and report["cost_microusd"] is None
    assert len(runner.bindings) == 4 and len({row["context_snapshot_id"] for row in runner.bindings}) == 4
    assert {row["root_budget_id"] for row in runner.bindings} == {config["root_budget_id"]}
    assert {row["role"] for row in runner.bindings} == {"researcher", "judge_quality", "judge_safety"}
    with db.connection() as connection:
        assert connection.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()[0] == stable.id
    assert all(observer.observations[0]["valid_committed_binding"] for _, observer in observers)


def test_rp13_only_allowed_fragment_changes_and_judges_are_blind(tmp_path, monkeypatch):
    db, base, candidate, runner, config, observers, case, _ = paired_setup(tmp_path, monkeypatch)
    pair = validate_pair(base.manifest, candidate.manifest, case)
    assert pair["base_messages"][1:] == pair["candidate_messages"][1:]
    ResearchRoleReplayEvaluator().evaluate(base_manifest=base.manifest, candidate_manifest=candidate.manifest,
        baseline_bundle_id=base.id, candidate_bundle_id=candidate.id, cases=[case], runner=runner.runner(config), judge=runner.judge(config))
    writes = [request for request, _ in observers if request.role == "researcher"]
    assert [(x.temperature, x.max_tokens, x.tools, x.thinking) for x in writes] == [(0, 4096, None, False)] * 2
    judges = [request for request, _ in observers if request.role.startswith("judge")]
    assert len(judges) == 2
    for request in judges:
        assert set(json.loads(request.messages[1]["content"])) == {"left", "right", "context"}
        assert CANDIDATE not in request.messages[0]["content"]
    assert runner.blind_records[0]["seed_digest"] and runner.blind_records[0]["judge_prompt_digest"]
    invalid = copy.deepcopy(candidate.manifest)
    invalid["tools"] = "changed"
    with pytest.raises(ReplayError, match="CHANGE_OUTSIDE_ALLOWED_PATH"):
        validate_pair(base.manifest, invalid, case)


def test_rp14_diagnostic_permission_does_not_authorize_new_send(tmp_path, monkeypatch):
    db, store, snapshot = historical(tmp_path / "source")
    assert load_history(store, "owner-a", "history-invocation")["status"] == "READY"
    _, base, candidate, runner, config, observers, case, _ = paired_setup(tmp_path / "eval", monkeypatch)
    case["source"] = {"invocation_id": "history-invocation"}
    with pytest.raises(ReplayError, match="CURRENT_SOURCE_AUTHORIZATION_REQUIRED"):
        runner.runner(config)(validate_pair(base.manifest, candidate.manifest, case)["base_messages"], base.id, case)
    assert observers == []
    invalid = {**config, "root_budget_id": None}
    with pytest.raises(ReplayError, match="CONTROLLED_CONFIG_INCOMPLETE"):
        runner.runner(invalid)
    assert load_history(store, "owner-a", "history-invocation")["snapshot"].content_json == snapshot.content_json


def test_rp14_revoked_frozen_source_is_readable_but_cannot_send_again(tmp_path, monkeypatch):
    from test_snapshot_gateway import _learning_assets
    from app.learning_assets import LearningConflict
    db, base, candidate, runner, config, observers, case, _ = paired_setup(tmp_path, monkeypatch)
    assets = _learning_assets(db, tmp_path)
    runner.control_store.learning_assets = assets
    pair = validate_pair(base.manifest, candidate.manifest, case)
    runner.runner(config)(pair["base_messages"], base.id, case)
    old = runner.bindings[0]
    frozen = ModelInputSnapshotStore(db).load_for_invocation("operator", old["id"])
    assets.revoke("operator", "bundle", base.id, "frozen-source-revoked")
    assert load_history(ModelInputSnapshotStore(db), "operator", old["id"])["status"] == "READY"
    runner.source_store = runner.control_store
    case["source"] = {"invocation_id": old["id"], "snapshot_digest": frozen.content_digest}
    before = len(observers)
    with pytest.raises(LearningConflict, match="revoked"):
        runner.runner(config)(pair["candidate_messages"], candidate.id, case)
    assert len(observers) == before
    assert ModelInputSnapshotStore(db).load_for_invocation("operator", old["id"]).content_json == frozen.content_json


@pytest.mark.parametrize("boundary", ["before_gateway", "retry", "judge"])
def test_rp14_source_revocation_after_precheck_blocks_every_new_send(tmp_path, monkeypatch, boundary):
    assert_source_revocation_blocks_send(tmp_path, monkeypatch, boundary)


def assert_source_revocation_blocks_send(tmp_path, monkeypatch, boundary, *, database=None, gateway_kind="routed"):
    from test_snapshot_gateway import _learning_assets
    from app.learning_assets import LearningConflict
    db, base, candidate, runner, config, observers, case, _ = paired_setup(tmp_path, monkeypatch, database=database, controlled=database is not None)
    assets = _learning_assets(db, tmp_path)
    runner.control_store.learning_assets = assets
    pair = validate_pair(base.manifest, candidate.manifest, case)
    runner.runner(config)(pair["base_messages"], base.id, case)
    old = runner.bindings[0]
    frozen = ModelInputSnapshotStore(db).load_for_invocation("operator", old["id"])
    runner.source_store = runner.control_store
    case["source"] = {"invocation_id": old["id"], "snapshot_digest": frozen.content_digest}
    before = len(observers)
    factory = runner.gateway_factory
    gateways, sends = [], []

    def revoke():
        assets.revoke("operator", "bundle", base.id, "source-revoked-during-replay")

    def create(version):
        gateway = factory(version)
        if gateway_kind == "direct":
            import httpx
            from app.model_gateway import ModelGateway
            from test_snapshot_gateway import _registered_profile
            def handler(request):
                sends.append(request)
                assert len(sends) == 1, "direct retry sent after source revocation"
                revoke()
                raise httpx.ReadTimeout("fixture timeout", request=request)
            async def no_sleep(_):
                return None
            gateway = ModelGateway(replace(_registered_profile(db, version), max_attempts=2, retry_base_seconds=0),
                transport=httpx.MockTransport(handler), control_store=runner.control_store, sleep=no_sleep)
        gateways.append(gateway)
        if boundary in {"before_gateway", "judge"}:
            revoke()
        if boundary == "retry" and gateway_kind == "routed":
            # Registered fixture version is immutable; only this isolated
            # transport's resolved profile gets the extra retry attempt.
            route = gateway._route
            def retry_route(context):
                snapshot, profiles, context = route(context)
                return snapshot, [replace(profile, max_attempts=2) for profile in profiles], context
            gateway._route = retry_route
            async def fail_once(profile, request, **kwargs):
                from snapshot_entrypoint_helpers import CommittedSnapshotTransport
                observer = CommittedSnapshotTransport(db, "operator", "CS-EV-03", _answer())
                observer.record(profile, request)
                sends.append(observer.observations[0])
                assert len(sends) == 1, "retry sent after source revocation"
                dependency = ModelInputSnapshotStore(db).load_for_invocation("operator", sends[-1]["invocation_id"]).provenance().assembly
                assert dependency["research_replay_source"] == case["source"]
                revoke()
                raise GatewayError("fixture timeout", "timeout")
            gateway._execute_attempt = fail_once
        return gateway

    runner.gateway_factory = create
    with pytest.raises(LearningConflict, match="revoked"):
        if boundary == "judge":
            runner.judge(config)({"case": case, "baseline": "answer", "candidate": "answer"})
        else:
            runner.runner(config)(pair["candidate_messages"], candidate.id, case)
    assert len(observers) == before and len(sends) == (1 if boundary == "retry" else 0)
    with db.connection() as connection:
        rows = connection.execute("SELECT id,status FROM model_invocations WHERE id<>?", (old["id"],)).fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "FAILED"
        assert connection.execute("SELECT COUNT(*) FROM model_attempts WHERE invocation_id=?", (rows[0]["id"],)).fetchone()[0] == len(sends)
    dependency = ModelInputSnapshotStore(db).load_for_invocation("operator", rows[0]["id"]).provenance().assembly
    assert dependency["research_replay_source"] == case["source"]
    assert all(gateway.current_call_context() is None for gateway in gateways)
    assert ModelInputSnapshotStore(db).load_for_invocation("operator", old["id"]).content_json == frozen.content_json
    # The source guard is scoped to this call, not attached to the shared store.
    assert runner.control_store.__class__.__name__ == "ModelControlStore"


@pytest.mark.parametrize("boundary", ["before_gateway", "retry"])
def test_rp14_direct_gateway_checks_historical_source_before_send_and_retry(tmp_path, monkeypatch, boundary):
    assert_source_revocation_blocks_send(tmp_path, monkeypatch, boundary, gateway_kind="direct")


@pytest.mark.parametrize("changed", ["task", "evidence", "rubric"])
def test_rp13_judges_receive_frozen_scoring_context_and_digest_it(tmp_path, monkeypatch, changed):
    _, _, _, runner, config, observers, case, _ = paired_setup(tmp_path, monkeypatch)
    judge = runner.judge(config)
    payload = {"case": case, "baseline": "same A", "candidate": "same B"}
    judge(payload)
    if changed == "task":
        case["heading"] = "Different frozen task"
    elif changed == "evidence":
        case["evidence"] = [("Different frozen evidence.", "s2")]
    else:
        case["rubric"] = {"deterministic_required": ["different required result"]}
    judge(payload)
    assert len(observers) == 4
    for index in (0, 1):
        before = json.loads(observers[index][0].messages[1]["content"])
        after = json.loads(observers[index + 2][0].messages[1]["content"])
        assert before != after
        assert (before["left"], before["right"]) == (after["left"], after["right"])
        context = after["context"]
        assert context["task"]["heading"] == case["heading"]
        assert context["evidence"] == json.loads(canonical_json(case["evidence"]))
        assert context["rubric"] == case["rubric"]
        assert set(after) == {"left", "right", "context"}
        assert config["candidate_bundle_id"] not in canonical_json(after) and CANDIDATE not in canonical_json(after)
        record = runner.blind_records[-1]
        assert record["blind_input_digest"] == digest(after)
        assert record["task_digest"] == digest(context["task"])
        assert record["evidence_digest"] == digest(context["evidence"])
        assert record["rubric_digest"] == digest(context["rubric"])
    assert runner.blind_records[0]["blind_input_digest"] != runner.blind_records[1]["blind_input_digest"]


@pytest.mark.parametrize("metric", [None, 1])
def test_rp15_missing_arm_or_judge_cost_stays_unknown(tmp_path, metric):
    case = {"id": "cost", "heading": "Setup", "thesis": "result", "evidence": [], "prior_summary": "", "lineage_id": "cost-task", "partition": "DEV",
            "rubric": {"deterministic_required": ["supported"]}}
    report = ResearchRoleReplayEvaluator().evaluate(base_manifest=policy(DEFAULT_EVIDENCE_STATEMENT), candidate_manifest=policy(CANDIDATE),
        baseline_bundle_id="base", candidate_bundle_id="candidate", cases=[case],
        runner=lambda *args: {"text": "supported", "cost_microusd": metric, "ttft_seconds": None},
        judge=lambda _: {"winner": "tie", "candidate_safe": True, "baseline_safe": True, "cost_microusd": None})
    assert not report["checks"]["cost_known"] and report["cost_microusd"] is None
    assert report["outcome"] != "PASS"


@pytest.mark.asyncio
async def test_rp16_known_regression_is_detected_and_ties_never_release(tmp_path):
    baseline = await run_offline(FIXTURES / "case-manifest.json", tmp_path / "baseline")
    candidate = await run_offline(FIXTURES / "case-manifest.json", tmp_path / "candidate", arm="candidate", fragment=CANDIDATE)
    comparison = compare_reports(baseline, candidate)
    assert comparison["counts"]["tie"] == 7 and comparison["counts"]["invalid"] == 1
    assert comparison["planned_cases"] == 8 and comparison["outcome"] == "INSUFFICIENT_EVIDENCE" and not comparison["release_eligible"]
    regression = await run_offline(FIXTURES / "case-manifest.json", tmp_path / "regression", arm="candidate", fragment=(FIXTURES / "regression.txt").read_text(encoding="utf-8"))
    failed = compare_reports(baseline, regression)
    assert failed["outcome"] == "FAIL" and failed["counts"]["baseline"] >= 1
    assert "null" in comparison_markdown(comparison) and "SYNTHETIC_SECRET" not in comparison_markdown(comparison)
    case, fixture = single_suite(tmp_path)
    freeze_suite(tmp_path / "tie-suite", [case], {case["case_id"]: fixture})
    tie_base = await run_offline(tmp_path / "tie-suite/case-manifest.json", tmp_path / "tie-base")
    tie_candidate = await run_offline(tmp_path / "tie-suite/case-manifest.json", tmp_path / "tie-candidate", arm="candidate", fragment=CANDIDATE)
    ties = compare_reports(tie_base, tie_candidate)
    assert ties["counts"] == {"baseline": 0, "candidate": 0, "tie": 1, "invalid": 0}
    assert ties["outcome"] == "INSUFFICIENT_EVIDENCE"


@pytest.mark.asyncio
async def test_rp17_lineages_and_sealed_partitions_are_enforced(tmp_path):
    suite, fixtures = load_suite(FIXTURES / "case-manifest.json")
    cases = copy.deepcopy(suite["cases"])
    cases[1]["lineage_id"] = cases[0]["lineage_id"]
    with pytest.raises(ReplayError, match="DUPLICATE_LINEAGE"):
        freeze_suite(tmp_path / "duplicates", cases, fixtures)
    cases = copy.deepcopy(suite["cases"])
    cases[0]["partition"] = "HOLDOUT"
    freeze_suite(tmp_path / "sealed", cases, fixtures)
    with pytest.raises(ReplayError, match="SEALED_PARTITION"):
        await run_offline(tmp_path / "sealed/case-manifest.json", tmp_path / "leak", arm="candidate", fragment=CANDIDATE)
    assert not (tmp_path / "leak").exists()


@pytest.mark.asyncio
async def test_rp18_repeated_offline_suites_have_identical_semantics_and_inputs(tmp_path):
    first = await run_offline(FIXTURES / "case-manifest.json", tmp_path / "one")
    second = await run_offline(FIXTURES / "case-manifest.json", tmp_path / "two")
    assert first["engineering_status"] == second["engineering_status"] == "PASS"
    assert [row["semantic_digest"] for row in first["records"]] == [row["semantic_digest"] for row in second["records"]]
    assert [row["input_digests"] for row in first["records"]] == [row["input_digests"] for row in second["records"]]
    assert first["records"][0]["bindings"][0]["context_snapshot_id"] != second["records"][0]["bindings"][0]["context_snapshot_id"]
    with pytest.raises(FileExistsError):
        await run_offline(FIXTURES / "case-manifest.json", tmp_path / "one")


def test_cli_default_and_controlled_refusal_are_zero_send(tmp_path, capsys):
    assert main(["run", "--suite", str(FIXTURES / "case-manifest.json"), "--out", str(tmp_path / "controlled"), "--mode", "controlled"]) == 2
    assert not (tmp_path / "controlled").exists()
    assert "CONTROLLED_REQUIRES_EXISTING_AUTHORIZED_EVALUATION_START" in capsys.readouterr().out
