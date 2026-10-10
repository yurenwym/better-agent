from __future__ import annotations

import asyncio

import hashlib
import json
import os
import uuid
from contextvars import ContextVar
from dataclasses import replace
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .execution_context import (
    HarnessContextError,
    HarnessExecutionContext,
    check_context_alignment,
    rebind,
)
from .model_input_snapshot import (
    ModelInputSnapshot,
    SnapshotBindingConflict,
    SnapshotError,
    SnapshotIntegrityError,
    freeze_model_input,
)
from .model_input_snapshot_store import SNAPSHOT_ID_PREFIX, ModelInputSnapshotStore


class RoutingError(ValueError):
    pass


class InvocationReplayError(RuntimeError):
    def __init__(self, invocation_id: str, status: str) -> None:
        super().__init__(f"model invocation already exists: {invocation_id} ({status})")
        self.invocation_id = invocation_id
        self.status = status


class InvocationIdempotencyConflict(ValueError):
    pass


@dataclass(frozen=True)
class ModelCandidate:
    profile_version_id: str
    priority: int
    capabilities: dict[str, bool]


class ModelRouter:
    """Deterministic capability filter; policy construction stays outside the router."""

    @staticmethod
    def select(candidates: list[ModelCandidate], required_capabilities: set[str]) -> ModelCandidate:
        eligible = [
            candidate for candidate in candidates
            if all(candidate.capabilities.get(capability) is True for capability in required_capabilities)
        ]
        if not eligible:
            raise RoutingError("no model satisfies required capabilities")
        return min(eligible, key=lambda candidate: (candidate.priority, candidate.profile_version_id))


@dataclass(frozen=True)
class ModelCallContext:
    role: str
    purpose: str
    # Deliberately unset by default. A production gateway must receive an owner
    # from an authorized execution record or explicit service configuration.
    owner_id: str | None = None
    run_id: str | None = None
    goal_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    agent_task_id: str | None = None
    runtime_bundle_id: str | None = None
    routing_policy_id: str | None = None
    routing_policy_digest: str = "direct"
    context_snapshot_digest: str = ""
    # The frozen input this logical call is bound to.  A caller may point at an
    # already-frozen snapshot, but the value is never trusted: it is read back and
    # verified against this call's identity before it is used.
    input_snapshot_id: str | None = None
    idempotency_key: str | None = None
    invocation_id: str | None = None
    price_snapshot_id: str | None = None
    root_budget_id: str | None = None
    # Span identity of the logical call.  A model call that shares a span with
    # another is the *same* logical call (a retry, or a role/purpose adaptation
    # inside one invocation), never a second independent call.
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    task_id: str | None = None
    parent_task_id: str | None = None
    root_task_id: str | None = None
    # The harness this context was converted from, when it came from one.
    harness: HarnessExecutionContext | None = None

    def __post_init__(self) -> None:
        check_context_alignment(self)

    @classmethod
    def from_harness(
        cls,
        harness: HarnessExecutionContext,
        *,
        role: str,
        purpose: str,
        invocation_id: str | None = None,
        idempotency_key: str | None = None,
        goal_id: str | None = None,
        routing_policy_id: str | None = None,
        routing_policy_digest: str = "direct",
        context_snapshot_digest: str = "",
        input_snapshot_id: str | None = None,
        price_snapshot_id: str | None = None,
    ) -> "ModelCallContext":
        """Convert a harness context into a model call identity.

        A conversion, not a factory: it neither mints a trace/span nor creates a
        task or a budget.  Owner, budget root, bundle and trace are taken from
        the harness and cannot be overridden here.
        """
        if not isinstance(harness, HarnessExecutionContext):
            raise HarnessContextError("harness must be a HarnessExecutionContext")
        return cls(
            role=role,
            purpose=purpose,
            owner_id=harness.owner_id,
            run_id=harness.run_id,
            goal_id=goal_id,
            thread_id=harness.thread_id,
            turn_id=harness.turn_id,
            agent_task_id=harness.task_id,
            runtime_bundle_id=harness.runtime_bundle_id,
            routing_policy_id=routing_policy_id,
            routing_policy_digest=routing_policy_digest,
            context_snapshot_digest=context_snapshot_digest,
            input_snapshot_id=input_snapshot_id,
            idempotency_key=idempotency_key,
            invocation_id=invocation_id,
            price_snapshot_id=price_snapshot_id,
            root_budget_id=harness.root_budget_id,
            trace_id=harness.trace_id,
            span_id=harness.span_id,
            parent_span_id=harness.parent_span_id,
            task_id=harness.task_id,
            parent_task_id=harness.parent_task_id,
            root_task_id=harness.root_task_id,
            harness=harness,
        )


def child_call_context(
    context: ModelCallContext,
    *,
    role: str | None,
    purpose: str | None,
) -> ModelCallContext:
    child_role = role or context.role
    child_purpose = purpose or context.purpose
    nested = child_purpose != context.purpose
    return replace(
        context,
        role=child_role,
        purpose=child_purpose,
        # A nested purpose is a new logical call, so it must freeze its own input
        # rather than inherit the parent's.  A role/purpose adaptation that keeps
        # the same purpose stays inside the *same* logical call — that is the
        # retry case — and keeps the input it was already bound to.
        input_snapshot_id=None if nested else context.input_snapshot_id,
        context_snapshot_digest="" if nested else context.context_snapshot_digest,
        invocation_id=(
            f"{context.invocation_id}:{child_purpose}"
            if nested and context.invocation_id
            else context.invocation_id
        ),
        idempotency_key=(
            f"{context.idempotency_key}:{child_purpose}"
            if nested and context.idempotency_key
            else context.idempotency_key
        ),
    )


def new_logical_call(
    context: ModelCallContext,
    *,
    role: str | None = None,
    purpose: str | None = None,
) -> ModelCallContext:
    """Derive an independent logical call from a parent context.

    A new logical call has its own span, its own invocation and its own frozen
    input.  It inherits the parent's owner, trace, budget root, runtime bundle and
    task association, but never the parent's invocation id, idempotency key or
    bound snapshot: those identify the *parent's* call, and reusing them would
    merge two calls' ledgers.

    The new span hangs directly off the parent context, so two calls derived from
    the same parent are *siblings* rather than a chain.  That is what makes "first
    compile" and "JSON repair" two calls at one boundary instead of one call whose
    identity was assembled out of a previous attempt.

    Callers must pass the **parent** context on every derivation.  Deriving from a
    previously derived context would nest the spans and make the second call look
    like a child of the first — the very confusion this API exists to prevent.

    ``child_call_context`` remains the *same-call* adapter: it keeps the input a
    call was already bound to when only the role is adapted.  This function is the
    explicit statement "this is a different logical call", and it does not depend
    on the purpose changing.
    """
    child_role = role or context.role
    child_purpose = purpose or context.purpose
    harness = context.harness
    if harness is not None:
        from .execution_context import create_child_context

        derived = ModelCallContext.from_harness(
            create_child_context(harness), role=child_role, purpose=child_purpose,
        )
    else:
        # A legacy caller has no span to derive.  It still gets a new logical
        # call: the call-scoped identity below is what makes it independent, and
        # no tool identity is invented on its behalf.
        derived = replace(context, role=child_role, purpose=child_purpose)
    return replace(
        derived,
        goal_id=context.goal_id,
        routing_policy_id=context.routing_policy_id,
        routing_policy_digest=context.routing_policy_digest,
        price_snapshot_id=context.price_snapshot_id,
        invocation_id=None,
        idempotency_key=None,
        input_snapshot_id=None,
        context_snapshot_digest="",
    )


@dataclass(frozen=True)
class ModelCallHandle:
    invocation_id: str
    profile_version_id: str
    context: ModelCallContext
    input_snapshot_id: str | None = None


def open_model_invocation(
    control_store: "ModelControlStore",
    profile: Any,
    request: Any,
    context: ModelCallContext,
    route: dict[str, Any] | None = None,
    *,
    admit: Any | None = None,
    provenance: Any | None = None,
) -> tuple[ModelCallHandle, ModelInputSnapshot, Any]:
    """Freeze the final logical input, admit it, then open its invocation.

    The order is the contract: nothing is counted, stored or sent that was not
    frozen first, and the frozen bytes are what admission and the send both read.
    Both gateways go through here so the rule cannot drift between them — the
    routed one for its production funnel, the direct one for a top-level call.

    ``admit`` is the capacity check for the profile that will actually be used.
    It is a callback because the routed gateway admits against every candidate
    profile while the direct gateway has exactly one.

    ``provenance`` is metadata *about* the input, supplied by the call site that
    assembled it.  The gateway never goes looking for sources of its own; it only
    freezes and shape-checks what it was handed.

    Returns ``(handle, snapshot, frozen_request)``.  ``frozen_request`` is a copy
    made from the frozen bytes; callers must send *that*, never their own object.
    """
    snapshot = freeze_model_input(request, context, provenance)
    frozen = snapshot.to_request()
    if admit is not None:
        admit(frozen)
    handle = control_store.begin_invocation(profile, frozen, context, route, snapshot=snapshot)
    return handle, snapshot, frozen


class ModelControlStore:
    def __init__(self, db: Database, *, events: Any | None = None, costs: Any | None = None) -> None:
        self.db = db
        self.events = events
        self.costs = costs
        self.snapshots = ModelInputSnapshotStore(db)
        # The optional learning service, wired at startup (see ``startup.py``).
        # It is an attribute rather than a constructor argument because the
        # learning service is built after the stores.  When it is absent the
        # checks that need it are not installed at all, so an installation
        # without learning keeps sending; when it is present, a check that was
        # declared required can never be skipped after it has failed.
        self.learning: Any | None = None

    def assert_request_active(self, invocation_id: str, context: ModelCallContext) -> None:
        """The one pre-send check, shared by the routed and the direct gateway.

        The inputs are the *already bound* invocation, its owner and its pinned
        bundle.  It re-checks what this call was frozen against; it never
        re-resolves the latest stable bundle or the current learning policy, so
        a refusal cannot silently turn into "use the newer version instead".

        Both gateways call this immediately before every send - the first attempt
        and every retry and fallback alike - because an asset can be revoked
        between the freeze and the wire, or between two attempts.

        It raises the asset layer's own domain error.  This is a refusal, not a
        repair: the caller must not retry, must not fall back, and must not
        replace the frozen input.
        """
        from .send_authority import assert_send_authority

        assert_send_authority()
        self.assert_sources_active(invocation_id, context)

    def assert_sources_active(self, invocation_id: str, context: ModelCallContext) -> None:
        """Revalidate frozen source dependencies at send and result delivery."""
        from .learning import LearningConflict
        from .evolution import EvolutionGateError
        from .policy_engine import PolicyAction, PolicyInput, decide

        source_error = None
        try:
            assets = getattr(self, "learning_assets", None)
            if assets is not None:
                assets.assert_request_active(invocation_id, context.owner_id)
            learning = getattr(self, "learning", None)
            if learning is not None and context.runtime_bundle_id:
                learning.assert_pinned_prompt_active(context.owner_id, context.runtime_bundle_id)
        except (LearningConflict, EvolutionGateError) as exc:
            source_error = exc
        decision = decide(PolicyInput(True, True, True, source_valid=source_error is None))
        if decision.action == PolicyAction.DENY:
            raise source_error

    def begin_invocation(
        self, profile: Any, request: Any, context: ModelCallContext,
        route_snapshot: dict[str, Any] | None = None, *, snapshot: Any | None = None,
    ) -> ModelCallHandle:
        """Open one logical call, freezing its input in the same transaction.

        ``snapshot`` is the frozen input the gateway just built.  When it is
        given, every derived digest comes from the frozen content rather than
        from ``request``, and the snapshot row, the invocation row and the
        execution-context binding all commit together — a failure in any of them
        leaves nothing behind and no request is sent.

        When it is not given, a pre-bound ``context.input_snapshot_id`` (or
        digest) is still not trusted: it is read back and verified, or refused.
        """
        now = _now()
        contract = _budget_contract_columns(profile)
        config = {
            "protocol": profile.provider_protocol,
            "provider": profile.provider_name,
            "base_url": profile.base_url.rstrip("/"),
            "model": profile.model,
            "credential_env_ref": profile.api_key_env,
            "timeout_seconds": profile.timeout_seconds,
            "max_attempts": profile.max_attempts,
            "context_window": profile.context_window,
            "max_output_tokens": profile.max_output_tokens,
            "budget_contract": {key: value for key, value in contract.items() if key != "protocol_budget_json"},
            "protocol_budget": contract["protocol_budget_json"],
        }
        config_digest = _digest(config)
        profile_id = f"model_profile_{_digest([profile.provider_name, profile.model])[:24]}"
        profile_version_id = profile.registered_profile_version_id or f"model_profile_version_{config_digest[:24]}"
        invocation_id = context.invocation_id or f"model_invocation_{uuid.uuid4().hex}"
        # Pin the resolved id onto the context so every invocation-scoped event
        # this call emits can be persisted against a known invocation, even when
        # the caller supplied no thread/turn or run/goal ids.
        context = replace(context, invocation_id=invocation_id)
        snapshot = self._resolve_input_snapshot(context, snapshot)
        snapshot_id: str | None = None
        if snapshot is not None:
            snapshot_id = snapshot.id or f"{SNAPSHOT_ID_PREFIX}{uuid.uuid4().hex}"
            context = replace(
                context,
                input_snapshot_id=snapshot_id,
                context_snapshot_digest=snapshot.content_digest,
            )
            # The frozen bytes are the only source of the derived digests, so a
            # later mutation of the caller's request cannot change what is
            # recorded as having been sent.
            request = snapshot.to_request()
        request_payload = {
            "messages": request.messages,
            "tools": request.tools or [],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "thinking": request.thinking,
            "response_format": getattr(request, "response_format", None),
        }
        request_digest = _digest(request_payload)
        system_prompt_digest = hashlib.sha256("".join(message["content"] for message in request.messages if message.get("role") == "system").encode("utf-8")).hexdigest()
        tool_schema_digest = _digest(request.tools or [])
        route_snapshot = route_snapshot or {
            "profile_version_id": profile_version_id,
            "provider_protocol": profile.provider_protocol,
            "model": profile.model,
        }
        profile_version_id = route_snapshot.get("profile_version_id") or route_snapshot.get("profile_sequence", [profile_version_id])[0]
        key = context.idempotency_key or invocation_id
        if context.idempotency_key is None:
            # The key here is *derived* from the call's identity (turn + purpose),
            # not claimed by the caller.  Identity alone must never make two
            # different inputs share one invocation: the same purpose used twice
            # in a turn is two logical calls, so the second one gets its own
            # invocation and its own snapshot instead of replaying or conflicting
            # with the first.  An explicitly supplied idempotency key is a claim,
            # and is handled inside the transaction below.
            invocation_id, key = self._mint_free_identity(invocation_id, key)
            context = replace(context, invocation_id=invocation_id)
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO model_profiles(id,owner_id,name,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (profile_id, context.owner_id, f"{profile.provider_name}:{profile.model}", "ACTIVE", now, now),
            )
            current = connection.execute("SELECT COALESCE(MAX(version),0) FROM model_profile_versions WHERE profile_id=?", (profile_id,)).fetchone()[0]
            known = connection.execute("SELECT id FROM model_profile_versions WHERE id=?", (profile_version_id,)).fetchone()
            if known is None: connection.execute(
                "INSERT OR IGNORE INTO model_profile_versions("
                "id,profile_id,version,provider_protocol,provider_name,base_url,model_name,credential_env_ref,capabilities_json,"
                "context_window,max_output_tokens,timeout_seconds,max_attempts,config_digest,created_at,"
                "admitted_context_limit,soft_context_limit,context_window_verified,validation_tier,"
                "counter_id,counter_version,counter_evidence_version,capacity_evidence,protocol_budget_json,"
                "history_min_turns,compact_ratio,recent_window_bytes,recent_window_ratio,"
                "archive_trigger_ratio,archive_reserve_ratio,archive_prefix_reserve"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    profile_version_id, profile_id, int(current) + 1, profile.provider_protocol, profile.provider_name,
                    profile.base_url.rstrip("/"), profile.model, profile.api_key_env,
                    _json({"text": True}),
                    profile.context_window, profile.max_output_tokens, profile.timeout_seconds, profile.max_attempts, config_digest, now,
                    contract["admitted_context_limit"], contract["soft_context_limit"], contract["context_window_verified"],
                    contract["validation_tier"], contract["counter_id"], contract["counter_version"],
                    contract["counter_evidence_version"], contract["capacity_evidence"], contract["protocol_budget_json"],
                    contract["history_min_turns"], contract["compact_ratio"],
                    contract["recent_window_bytes"], contract["recent_window_ratio"],
                    contract["archive_trigger_ratio"], contract["archive_reserve_ratio"],
                    contract["archive_prefix_reserve"],
                ),
            )
            existing = self._existing_invocation(connection, key)
            if existing:
                self._resolve_existing_invocation(
                    existing, key=key, request_digest=request_digest,
                    context=context, snapshot=snapshot,
                )
            # The frozen input row is written next, on this same transaction,
            # because the invocation's own INSERT carries the binding.  A call is
            # therefore either created together with its input or not created at
            # all: there is no window where it exists unbound, and no path that
            # attaches an input to an existing row afterwards.  A snapshot a
            # caller already persisted is re-read and re-checked here rather than
            # trusted by id.
            if snapshot is not None:
                if snapshot.id is None:
                    self.snapshots.insert(connection, snapshot, snapshot_id=snapshot_id)
                else:
                    stored = self.snapshots.require_bindable(
                        connection, snapshot_id,
                        owner_id=context.owner_id,
                        runtime_bundle_id=context.runtime_bundle_id,
                        role=context.role,
                        purpose=context.purpose,
                    )
                    if stored.content_digest != snapshot.content_digest:
                        raise SnapshotIntegrityError(
                            "a pre-persisted input snapshot does not match the frozen input"
                        )
            # `DO NOTHING` rather than a bare INSERT: two transactions can pass
            # the existence check above and then race the unique key, and the
            # loser must get a domain error, not the driver's constraint
            # violation.  On both backends the conflicting insert waits for the
            # other transaction to settle, so the re-read below sees a committed
            # row rather than nothing.
            inserted = connection.execute(
                "INSERT INTO model_invocations("
                "id,owner_id,run_id,thread_id,turn_id,agent_task_id,role,purpose,runtime_bundle_id,routing_policy_id,"
                "routing_policy_digest,route_snapshot_json,request_digest,tool_schema_digest,context_snapshot_digest,status,"
                "idempotency_key,created_at,context_snapshot_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    invocation_id, context.owner_id, context.run_id, context.thread_id, context.turn_id, context.agent_task_id,
                    context.role, context.purpose, context.runtime_bundle_id, context.routing_policy_id,
                    context.routing_policy_digest, _json(route_snapshot), request_digest, tool_schema_digest,
                    context.context_snapshot_digest, "RUNNING", key, now, snapshot_id,
                ),
            ).rowcount
            if inserted != 1:
                raced = self._existing_invocation(connection, key)
                if raced is None:
                    raise InvocationIdempotencyConflict(
                        f"model invocation {invocation_id} could not be created for key {key}"
                    )
                self._resolve_existing_invocation(
                    raced, key=key, request_digest=request_digest,
                    context=context, snapshot=snapshot,
                )
            if context.root_budget_id is not None:
                connection.execute("UPDATE model_invocations SET root_budget_id=? WHERE id=?",
                                   (context.root_budget_id, invocation_id))
            connection.execute("UPDATE model_invocations SET system_prompt_digest=? WHERE id=?", (system_prompt_digest, invocation_id))
            if context.harness is not None:
                # Same transaction as the INSERT: an invocation row can never be
                # observable without the execution identity it was opened under.
                from .harness_context_store import HarnessContextStore

                HarnessContextStore(self.db).save_invocation_context(connection, invocation_id, context.harness)
            if getattr(self, "learning_assets", None) is not None:
                self.learning_assets.freeze_request(connection, context, request, invocation_id)
            exposure = connection.execute(
                "SELECT e.*,c.owner_id FROM canary_exposures e JOIN canary_deployments d ON d.id=e.deployment_id "
                "JOIN evolution_candidates c ON c.id=d.candidate_id WHERE e.run_id=?", (context.run_id,),
            ).fetchone()
            if exposure and exposure["owner_id"] != context.owner_id:
                raise RoutingError("canary task belongs to another owner")
            if exposure and exposure["target_role"] == context.role and exposure["target_purpose"] == context.purpose:
                from .research.live import build_research_write_messages
                bundle = connection.execute("SELECT manifest_json FROM runtime_bundles WHERE id=?", (exposure["bundle_id"],)).fetchone()
                manifest = json.loads(bundle["manifest_json"])
                expected = build_research_write_messages(manifest.get("prompts", manifest.get("prompt")), "", "", [], "")[0]["content"]
                if exposure["bundle_id"] != context.runtime_bundle_id or system_prompt_digest != hashlib.sha256(expected.encode("utf-8")).hexdigest():
                    raise RoutingError("canary request did not hit the frozen production prompt")
            self._event(connection, context, "model.invocation.created", {"model_invocation_id": invocation_id, "role": context.role})
            self._event(connection, context, "model.route.selected", {
                "model_invocation_id": invocation_id,
                "routing_policy_digest": context.routing_policy_digest,
                "profile_version_ids": route_snapshot.get("profile_sequence", [profile_version_id]),
            })
        return ModelCallHandle(invocation_id, profile_version_id, context, snapshot_id)

    #: The persisted public identity columns, in the order a conflict reports
    #: them.  ``agent_task_id`` is the ledger's name for the harness ``task_id``.
    _IDENTITY_COLUMNS: tuple[tuple[str, str], ...] = (
        ("owner_id", "owner"),
        ("run_id", "run"),
        ("thread_id", "thread"),
        ("turn_id", "turn"),
        ("agent_task_id", "task"),
        ("root_budget_id", "budget"),
        ("runtime_bundle_id", "runtime bundle"),
    )

    def _existing_invocation(self, connection: Any, key: str) -> Any:
        return connection.execute(
            "SELECT id,request_digest,status,owner_id,run_id,thread_id,turn_id,agent_task_id,"
            "root_budget_id,runtime_bundle_id,role,purpose,"
            "context_snapshot_id,context_snapshot_digest,execution_context_digest "
            "FROM model_invocations WHERE idempotency_key=?", (key,)
        ).fetchone()

    def _resolve_existing_invocation(
        self, existing: Any, *, key: str, request_digest: str,
        context: ModelCallContext, snapshot: Any | None,
    ) -> None:
        """Answer a re-used idempotency key: replay the same call, refuse a different one.

        An idempotency key claims exactly *one* logical call, so the key alone is
        not enough to make something a replay.  The request, the frozen input and
        the execution identity all have to agree; a key reused for a different
        input, owner, run, thread, turn, task, budget, bundle or span is a
        conflict, and answering it with the first call's result would silently
        merge two calls' ledgers.  Both outcomes raise — this never returns.

        The comparison is over the *persisted public columns*, field by field and
        including nulls, because a controlled direct or legacy caller legitimately
        has no harness at all.  "No execution identity" is not a reason to skip
        the identity check; it is one of the values being compared.
        """
        conflicts: list[str] = []
        if existing["request_digest"] != request_digest:
            conflicts.append("request")
        for column, label in self._IDENTITY_COLUMNS:
            # Strict equality, nulls included: "no run" and "run A" are different
            # calls, and so are "run A" and "run B".
            if existing[column] != getattr(context, column):
                conflicts.append(label)
        if (existing["role"], existing["purpose"]) != (context.role, context.purpose):
            conflicts.append("role/purpose")
        if snapshot is not None and existing["context_snapshot_digest"] != snapshot.content_digest:
            conflicts.append("frozen input")
        conflicts.extend(_execution_identity_conflicts(existing, context))
        if conflicts:
            raise InvocationIdempotencyConflict(
                f"idempotency key {key} is already bound to a different call: "
                + ", ".join(conflicts)
            )
        raise InvocationReplayError(existing["id"], existing["status"])

    def _mint_free_identity(self, invocation_id: str, key: str) -> tuple[str, str]:
        """An invocation id and ledger key no existing row already claims.

        Only reached when the caller supplied no idempotency key, so nothing is
        being overwritten or replayed - the previous holder of this identity is a
        different logical call that happens to share the turn and purpose.
        """
        with self.db.connection() as connection:
            for _ in range(64):
                taken = connection.execute(
                    "SELECT 1 FROM model_invocations WHERE idempotency_key=? OR id=?",
                    (key, invocation_id),
                ).fetchone()
                if taken is None:
                    return invocation_id, key
                invocation_id = f"{invocation_id}:{uuid.uuid4().hex[:8]}"
                key = invocation_id
        raise InvocationIdempotencyConflict(
            f"could not allocate a free model invocation identity for {invocation_id}"
        )

    def _resolve_input_snapshot(self, context: ModelCallContext, snapshot: Any | None) -> Any | None:
        """Decide which frozen input this call uses, refusing anything unverified.

        Nothing supplied by a caller is trusted as a binding.  A snapshot object is
        checked against this call's identity, and any pre-bound id or digest on the
        context has to agree with it — a caller cannot hand over one frozen input
        while claiming another.  With no snapshot object, a pre-bound reference is
        read back from storage and checked the same way, and a digest with nothing
        behind it is refused outright rather than recorded as if it meant something.
        """
        bound_id = context.input_snapshot_id
        bound_digest = context.context_snapshot_digest
        if snapshot is not None:
            if not isinstance(snapshot, ModelInputSnapshot):
                raise SnapshotError("snapshot must be a ModelInputSnapshot")
            if not self._snapshot_matches_call(snapshot, context):
                raise SnapshotBindingConflict(
                    "input snapshot does not describe this model call"
                )
            if bound_id is not None and bound_id != snapshot.id:
                raise SnapshotBindingConflict(
                    "a pre-bound input snapshot id contradicts the frozen input"
                )
            if bound_digest and bound_digest != snapshot.content_digest:
                raise SnapshotBindingConflict(
                    "a pre-bound digest contradicts the frozen input"
                )
            return snapshot
        if bound_id:
            existing = self.snapshots.load(context.owner_id, bound_id)
            if not self._snapshot_matches_call(existing, context):
                raise SnapshotBindingConflict(
                    "pre-bound input snapshot does not describe this model call"
                )
            if bound_digest and bound_digest != existing.content_digest:
                raise SnapshotBindingConflict(
                    "pre-bound digest does not describe the referenced input snapshot"
                )
            return existing
        if bound_digest:
            raise SnapshotBindingConflict(
                "a pre-bound context_snapshot_digest must reference an input snapshot "
                "that can be verified"
            )
        return None

    @staticmethod
    def _snapshot_matches_call(snapshot: ModelInputSnapshot, context: ModelCallContext) -> bool:
        return snapshot.bound_to(
            context.owner_id,
            runtime_bundle_id=context.runtime_bundle_id,
            role=context.role,
            purpose=context.purpose,
        )

    def start_attempt(self, handle: ModelCallHandle, ordinal: int, reason: str) -> str:
        attempt_id = f"{handle.invocation_id}_attempt_{ordinal}"
        now = _now()
        try:
            with self.db.transaction() as connection:
                row = connection.execute("SELECT request_digest,route_snapshot_json FROM model_invocations WHERE id=?", (handle.invocation_id,)).fetchone()
                profile = connection.execute(
                    "SELECT provider_protocol FROM model_profile_versions WHERE id=?", (handle.profile_version_id,)
                ).fetchone()
                if profile is None:
                    raise KeyError(handle.profile_version_id)
                canary = connection.execute(
                    "SELECT d.id FROM canary_exposures e JOIN canary_deployments d ON d.id=e.deployment_id "
                    "WHERE e.run_id=? AND d.release_contract_version<>''", (handle.context.run_id,),
                ).fetchone()
                if canary and (self.costs is None or ordinal != 1 or reason != "primary"):
                    from .costs import BudgetExceeded
                    raise BudgetExceeded("canary requires a priced first attempt with no retries or fallback")
                connection.execute(
                    "INSERT OR IGNORE INTO model_attempts(id,invocation_id,ordinal,reason,profile_version_id,provider_protocol,request_digest,status,started_at) "
                    "VALUES (?,?,?,?,?,?,?,'STARTED',?)",
                    (attempt_id, handle.invocation_id, ordinal, reason, handle.profile_version_id, profile["provider_protocol"], row["request_digest"], now),
                )
                if self.costs is not None:
                    self.costs.reserve_attempt(connection, handle, attempt_id)
                    reserved = connection.execute(
                        "SELECT COALESCE(SUM(amount_microusd),0) amount FROM cost_ledger "
                        "WHERE attempt_id=? AND entry_type='RESERVE'", (attempt_id,),
                    ).fetchone()["amount"]
                    self._event(connection, handle.context, "cost.budget_reserved", {
                        "model_invocation_id": handle.invocation_id, "model_attempt_id": attempt_id,
                        "amount_microusd": int(reserved),
                    })
                self._event(connection, handle.context, "model.attempt.started", {
                    "model_invocation_id": handle.invocation_id, "model_attempt_id": attempt_id, "attempt": ordinal, "reason": reason,
                })
        except Exception as exc:
            from .costs import BudgetExceeded

            if isinstance(exc, BudgetExceeded):
                with self.db.transaction() as connection:
                    from .canary_budget import stop_deployment
                    deployment = connection.execute(
                        "SELECT d.* FROM canary_deployments d JOIN canary_exposures e ON e.deployment_id=d.id "
                        "WHERE e.run_id=? AND d.release_contract_version<>''", (handle.context.run_id,),
                    ).fetchone()
                    if deployment:
                        stop_deployment(connection, deployment, str(exc))
                    self._event(connection, handle.context, "cost.budget_blocked", {
                        "model_invocation_id": handle.invocation_id, "model_attempt_id": attempt_id,
                        "reason": str(exc),
                    })
                self.finish_invocation(handle, "budget_blocked")
            raise
        return attempt_id

    def mark_output_started(self, handle: ModelCallHandle, ordinal: int) -> None:
        attempt_id = f"{handle.invocation_id}_attempt_{ordinal}"
        now = _now()
        with self.db.transaction() as connection:
            changed = connection.execute(
                "UPDATE model_attempts SET first_token_at=? WHERE id=? AND first_token_at IS NULL",
                (now, attempt_id),
            ).rowcount
            if changed:
                self._event(connection, handle.context, "model.output.started", {
                    "model_invocation_id": handle.invocation_id, "model_attempt_id": attempt_id,
                })

    def finish_attempt(self, handle: ModelCallHandle, ordinal: int, status: str, error_kind: str | None, response: Any | None) -> None:
        attempt_id = f"{handle.invocation_id}_attempt_{ordinal}"
        now = _now()
        usage = getattr(response, "usage", None)
        values = {
            "uncached_input_tokens": getattr(usage, "uncached_input_tokens", None),
            "cache_read_tokens": getattr(usage, "cache_read_tokens", None),
            "cache_write_tokens": getattr(usage, "cache_write_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "reasoning_tokens": getattr(usage, "reasoning_tokens", None),
        }
        usage_status = "COMPLETE" if usage is not None and all(value is not None for value in values.values()) else "UNAVAILABLE"
        usage_digest = _digest(values) if usage is not None else None
        db_status = {"succeeded": "SUCCEEDED", "failed": "FAILED", "cancelled": "CANCELLED"}[status]
        with self.db.transaction() as connection:
            # Serialize settlement and its audit events with the state check.
            # SQLite transactions already use BEGIN IMMEDIATE; PostgreSQL needs
            # a row lock held until the settlement transaction commits/rolls back.
            lock = " FOR UPDATE" if self.db.backend == "postgresql" else ""
            current = connection.execute(
                "SELECT status FROM model_attempts WHERE id=?" + lock, (attempt_id,)
            ).fetchone()
            if current is None or current["status"] != "STARTED":
                return
            cost = self.costs.settle_attempt(connection, handle, attempt_id, usage) if self.costs is not None else None
            connection.execute(
                "UPDATE model_attempts SET status=?,error_kind=?,uncached_input_tokens=?,cache_read_tokens=?,cache_write_tokens=?,"
                "output_tokens=?,reasoning_tokens=?,finished_at=?,usage_status=?,usage_digest=?,price_snapshot_id=?,cost_status=?,cost_microusd=? WHERE id=? AND status='STARTED'",
                (
                    db_status, error_kind, values["uncached_input_tokens"], values["cache_read_tokens"],
                    values["cache_write_tokens"], values["output_tokens"], values["reasoning_tokens"], now,
                    usage_status, usage_digest, getattr(cost, "price_snapshot_id", None), cost.status if cost else "UNAVAILABLE", cost.microusd if cost else None, attempt_id,
                ),
            )
            self._event(connection, handle.context, "model.attempt.finished", {
                "model_invocation_id": handle.invocation_id, "model_attempt_id": attempt_id,
                "attempt": ordinal, "status": status, "error_kind": error_kind,
            })
            if cost is not None:
                self._event(connection, handle.context, "cost.settled", {
                    "model_invocation_id": handle.invocation_id, "model_attempt_id": attempt_id,
                    "cost_status": cost.status, "cost_microusd": cost.microusd,
                })

    def finish_invocation(self, handle: ModelCallHandle, status: str, selected_ordinal: int | None = None) -> None:
        db_status = status.upper()
        selected = f"{handle.invocation_id}_attempt_{selected_ordinal}" if selected_ordinal is not None else None
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE model_invocations SET status=?,selected_attempt_id=?,finished_at=? WHERE id=? AND status='RUNNING'",
                (db_status, selected, _now(), handle.invocation_id),
            )
            if db_status == "SUCCEEDED":
                connection.execute(
                    "UPDATE canary_exposures SET prompt_hit=1,prompt_digest=(SELECT system_prompt_digest FROM model_invocations WHERE id=?) "
                    "WHERE run_id=? AND bundle_id=? AND target_role=? AND target_purpose=?",
                    (handle.invocation_id, handle.context.run_id, handle.context.runtime_bundle_id, handle.context.role, handle.context.purpose),
                )
            self._event(connection, handle.context, "model.invocation.finished", {
                "model_invocation_id": handle.invocation_id, "status": status,
            })

    def record_event(self, handle: ModelCallHandle, event_type: str, data: dict[str, Any]) -> None:
        with self.db.transaction() as connection:
            self._event(connection, handle.context, event_type, data, handle.invocation_id)

    def record_request_estimate(self, handle: ModelCallHandle, ordinal: int, profile: Any, request: Any) -> None:
        """Pair the final adapter request estimate with one provider attempt's usage."""
        from .model_gateway import provider_payload
        from .token_budget import counter_for_profile, effective_input_budget

        payload = provider_payload(profile, request)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        selection = counter_for_profile(profile)
        self.record_event(handle, "model.request.estimated", {
            "model_invocation_id": handle.invocation_id,
            "model_attempt_id": f"{handle.invocation_id}_attempt_{ordinal}",
            "profile_version_id": handle.profile_version_id,
            "counter_id": selection.counter_id,
            "counter_version": selection.counter_version,
            "counter_mode": selection.mode,
            "estimated_input_tokens": selection.counter.count_text(encoded),
            "wire_payload_bytes": len(encoded.encode("utf-8")),
            "wire_payload_digest": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "input_limit": effective_input_budget(profile).input_limit,
        })

    def _event(self, connection: Any, context: ModelCallContext, event_type: str, data: dict[str, Any],
               invocation_id: str | None = None) -> None:
        invocation_id = invocation_id or getattr(context, "invocation_id", None)
        if self.events is not None and context.run_id and context.goal_id:
            self.events.append(context.run_id, context.goal_id, event_type, "runtime", data, connection=connection)
        if context.thread_id and context.turn_id:
            from .events import ThreadEventStore
            ThreadEventStore(self.db).append(
                context.thread_id, context.turn_id, event_type, "runtime", data,
                connection=connection,
            )
        if invocation_id:
            # The invocation is the only identifier every call is guaranteed to
            # have. Persisting here keeps capacity rejections and skipped
            # fallbacks observable even when no thread/turn or run/goal exists.
            connection.execute(
                "INSERT INTO model_invocation_events(id,invocation_id,event_type,data_json,created_at)"
                " VALUES(?,?,?,?,?)",
                (f"model_invocation_event_{uuid.uuid4().hex}", invocation_id, event_type,
                 json.dumps(data, ensure_ascii=False, sort_keys=True), _now()),
            )


class RoutedModelGateway:
    """Shared data-plane gateway: immutable Bundle route, one Invocation, many Attempts."""

    FALLBACK_ERRORS = {"timeout", "rate_limit", "server", "provider_unavailable"}
    supports_intent_classification = True
    supports_role_routing = True
    ROLE_CAPABILITIES = {
        "conversation": {"text", "streaming"}, "ask": {"text", "tool_calling"},
        "planner": {"text", "json_object"}, "executor": {"text", "tool_calling"},
        "reflector": {"text", "json_object"}, "researcher": {"text", "streaming"},
        "expert": {"text", "json_object"}, "coordinator": {"text", "json_object"},
        "judge_quality": {"text", "json_object"}, "judge_safety": {"text", "json_object"},
        # V3 learning pipeline roles: the generator drafts, the judge scores.
        "learning_generator": {"text", "json_object"}, "learning_judge": {"text", "json_object"},
    }

    def __init__(self, db: Database, control_store: ModelControlStore, *, execute_attempt=None) -> None:
        self.db = db
        self.control_store = control_store
        self._execute_attempt = execute_attempt or self._execute_http_attempt
        self._call_context: ContextVar[ModelCallContext | None] = ContextVar("routed_model_call_context", default=None)

    def set_call_context(self, context: ModelCallContext):
        return self._call_context.set(context)

    def reset_call_context(self, token: Any) -> None:
        self._call_context.reset(token)

    def current_call_context(self) -> ModelCallContext | None:
        """The ambient context, or ``None`` when the caller set none.

        Read-only: a caller that needs a *new* logical call derives one from this
        with :func:`new_logical_call` instead of mutating it in place.
        """
        return self._call_context.get()

    def prompt_policy(self):
        """Read prompt policy from the same pinned bundle as the current call."""
        context = self._call_context.get()
        bundle_id = context.runtime_bundle_id if context is not None else None
        if context is not None and bundle_id and getattr(self, "learning", None) is not None:
            self.learning.assert_pinned_prompt_active(context.owner_id, bundle_id)
        with self.db.connection() as connection:
            if bundle_id is None:
                channel = connection.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()
                if channel is None:
                    raise RoutingError("stable runtime bundle is not configured")
                bundle_id = channel["bundle_id"]
            row = connection.execute("SELECT manifest_json FROM runtime_bundles WHERE id=?", (bundle_id,)).fetchone()
        if row is None:
            raise RoutingError("pinned runtime bundle does not exist")
        manifest = json.loads(row["manifest_json"])
        return manifest.get("prompts", manifest.get("prompt"))

    def resolved_profile(self, context: ModelCallContext | None = None, *, owner_id: str | None = None,
                         role: str | None = None, purpose: str | None = None):
        """The profile the next call in this context will actually use.

        The hot-window selector needs the same profile the send path will route
        to, so the budget, ``min_turns`` and ``compact_ratio`` all come from one
        versioned source instead of a local constant.
        """
        active = context or self._call_context.get()
        if active is None:
            if not owner_id:
                raise RoutingError("model execution owner is required")
            active = ModelCallContext("conversation", "complete", owner_id=owner_id)
        if owner_id is not None and active.owner_id != owner_id:
            active = replace(active, owner_id=owner_id)
        active = child_call_context(active, role=role, purpose=purpose)
        _, profiles, _ = self._route(active)
        return profiles[0]

    def input_limit(self, context: ModelCallContext | None = None, *, owner_id: str | None = None,
                    role: str | None = None, purpose: str | None = None) -> int:
        from .token_budget import effective_input_budget
        return effective_input_budget(
            self.resolved_profile(context, owner_id=owner_id, role=role, purpose=purpose)
        ).input_limit

    def packing_limit(self, context: ModelCallContext | None = None, *, owner_id: str | None = None,
                      role: str | None = None, purpose: str | None = None,
                      message_count: int = 0, tool_count: int = 0) -> int:
        """``input_limit`` minus the adapter overhead for a request this size.

        Callers that bound a request before dispatch must use this, not
        ``input_limit``. Packing against ``input_limit`` fills the canonical
        ``messages``/``tools`` budget and leaves nothing for what the adapter
        adds, so the wire gate refuses a request that is not actually too large.
        """
        from .token_budget import packing_limit as _packing_limit
        return _packing_limit(
            self.resolved_profile(context, owner_id=owner_id, role=role, purpose=purpose),
            message_count=message_count, tool_count=tool_count,
        )

    def request_counter(self, context: ModelCallContext | None = None, *, owner_id: str | None = None,
                        role: str | None = None, purpose: str | None = None):
        """The counter the routed profile will actually use for this request.

        Packing and the per-profile wire gate must share one ruler; a fallback
        to a profile with a different counter is re-resolved at send time.
        """
        from .token_budget import counter_for_profile
        return counter_for_profile(
            self.resolved_profile(context, owner_id=owner_id, role=role, purpose=purpose)
        ).counter

    def output_limit(self, context: ModelCallContext | None = None, *, owner_id: str | None = None,
                     role: str | None = None, purpose: str | None = None) -> int:
        active = context or self._call_context.get()
        if active is None:
            if not owner_id:
                raise RoutingError("model execution owner is required")
            active = ModelCallContext("planner", "compile_goal_program", owner_id=owner_id)
        if owner_id is not None and active.owner_id != owner_id:
            active = rebind(active, owner_id=owner_id)
        active = child_call_context(active, role=role, purpose=purpose)
        _, profiles, _ = self._route(active)
        return int(profiles[0].max_output_tokens)

    async def complete(
        self, request: Any, cancel_event=None, on_text_delta=None, on_text_reset=None,
        on_attempt_started=None, on_attempt_finished=None, context: ModelCallContext | None = None,
        provenance: Any | None = None,
    ) -> Any:
        from .model_gateway import GatewayError, ModelResponse

        context = context or self._call_context.get()
        if context is None or not isinstance(context.owner_id, str) or not context.owner_id.strip():
            raise RoutingError("model execution owner is required")
        context = child_call_context(
            context,
            role=request.role,
            purpose=request.purpose,
        )
        route, profiles, context = self._route(context)
        if getattr(request, "single_attempt", False):
            profiles = profiles[:1]
        from .token_budget import ContextOverflow, assert_request_fits

        def admit(frozen_request: Any) -> None:
            try:
                assert_request_fits(frozen_request.messages, frozen_request.tools, profiles[0])
            except ContextOverflow as exc:
                raise GatewayError(str(exc), "context_overflow") from exc

        # Freeze the final logical input *before* capacity admission, so what is
        # counted, persisted and sent is one and the same content.  From here on
        # the frozen bytes are the only input the call consumes.
        handle, snapshot, frozen = open_model_invocation(
            self.control_store, profiles[0], request, context, route, admit=admit,
            provenance=provenance,
        )
        if cancel_event is not None and cancel_event.is_set():
            self.control_store.finish_invocation(handle, "cancelled")
            raise GatewayError("model request cancelled", "cancelled")
        ordinal = 0
        learning_call = bool(getattr(self, "learning", None) is not None and self.learning.assert_learning_call_allowed(context.owner_id, context.root_budget_id))
        output_started = False
        last_error = None
        for profile_index, profile in enumerate(profiles):
            try:
                assert_request_fits(frozen.messages, frozen.tools, profile)
            except ContextOverflow as exc:
                # H and U_A are recomputed against *this* profile, never reused
                # from the primary. A fallback whose window cannot hold the
                # request is explicitly skipped so a later profile still gets
                # its chance; the request is never silently cropped to fit a
                # hard protection. Only the last remaining profile ends the call.
                if profile_index < len(profiles) - 1:
                    self.control_store.record_event(handle, "model.context.fallback_skipped", {
                        "model_invocation_id": handle.invocation_id,
                        "profile_version_id": profile.registered_profile_version_id,
                        "reason": "context_overflow",
                        "detail": str(exc),
                        "remaining_profiles": len(profiles) - profile_index - 1,
                    })
                    continue
                self.control_store.finish_invocation(handle, "failed")
                raise GatewayError(str(exc), "context_overflow", ordinal) from exc
            retries = 1 if frozen.single_attempt else max(int(profile.max_attempts), 1)
            for retry in range(retries):
                if learning_call:
                    # The "is learning allowed for this call at all" gate is
                    # routed-only; the asset revocation rule itself lives in the
                    # shared store method below.  A refusal here is terminal too,
                    # so the invocation is closed rather than left RUNNING.
                    try:
                        self.learning.assert_learning_call_allowed(context.owner_id, context.root_budget_id)
                    except Exception:
                        self.control_store.finish_invocation(handle, "failed")
                        raise
                ordinal += 1
                reason = "primary" if ordinal == 1 else ("fallback" if retry == 0 else "retry")
                active = replace(handle, profile_version_id=profile.registered_profile_version_id)
                # Each attempt deserialises its own copy of the frozen input, so a
                # retry or a fallback cannot be handed an object a previous
                # attempt mutated — and the snapshot itself stays the one record.
                attempt_request = snapshot.to_request()
                if ordinal > 1 and on_text_reset is not None:
                    on_text_reset()
                if on_attempt_started is not None:
                    on_attempt_started(ordinal, reason)
                from .policy_engine import PolicyAction, PolicyInput, decide
                decision = decide(PolicyInput(True, True, True,
                    cancelled=cancel_event is not None and cancel_event.is_set()))
                if decision.action == PolicyAction.DENY:
                    self.control_store.finish_invocation(handle, "cancelled")
                    raise GatewayError("model request cancelled", "cancelled", ordinal - 1)
                # The shared send-time asset check, run after every caller
                # callback and immediately before the wire, on every attempt.
                try:
                    self.control_store.assert_request_active(handle.invocation_id, context)
                except Exception:
                    self.control_store.finish_invocation(handle, "failed")
                    raise
                try:
                    try:
                        self.control_store.start_attempt(active, ordinal, reason)
                    except Exception as exc:
                        from .costs import BudgetExceeded
                        if isinstance(exc, BudgetExceeded):
                            raise GatewayError(str(exc), "budget", ordinal) from exc
                        raise
                    attempt_profile = profile
                    costs = getattr(self.control_store, "costs", None)
                    if context.root_budget_id is not None and costs is not None:
                        remaining = costs.root_seconds_remaining(context.owner_id, context.root_budget_id)
                        if remaining <= 0:
                            raise GatewayError("root task deadline exceeded", "budget", ordinal)
                        attempt_profile = replace(
                            profile, timeout_seconds=min(float(profile.timeout_seconds), remaining),
                        )
                    def text_delta(value: str) -> None:
                        nonlocal output_started
                        output_started = True
                        self.control_store.mark_output_started(active, ordinal)
                        if on_text_delta is not None:
                            on_text_delta(value)
                    def output(kind: str) -> None:
                        nonlocal output_started
                        output_started = True
                        self.control_store.mark_output_started(active, ordinal)
                    self.control_store.record_request_estimate(active, ordinal, attempt_profile, attempt_request)
                    response = await self._execute_attempt(
                        attempt_profile, attempt_request, cancel_event=cancel_event,
                        on_text_delta=text_delta, on_output_started=output,
                    )
                    response = ModelResponse(**{**response.__dict__, "attempts": ordinal})
                    if getattr(self.control_store, "learning_assets", None) is not None:
                        # The asset was still active when the attempt started; it
                        # may have been revoked while the response was in flight.
                        # The result is not usable and the call is closed rather
                        # than left RUNNING for a later retry to pick up -- but
                        # the provider *did* run and bill this attempt, so the
                        # response is handed to the settlement path: its usage is
                        # persisted and its cost is charged as incurred. Passing
                        # None here would silently drop a real, already-billed
                        # usage record.
                        try:
                            self.control_store.assert_request_active(handle.invocation_id, context)
                        except Exception:
                            self.control_store.finish_attempt(active, ordinal, "failed", "asset_revoked", response)
                            self.control_store.finish_invocation(active, "failed")
                            raise
                    self.control_store.finish_attempt(active, ordinal, "succeeded", None, response)
                    self.control_store.finish_invocation(active, "succeeded", ordinal)
                    if on_attempt_finished is not None:
                        on_attempt_finished(ordinal, "succeeded", None, response)
                    return response
                except asyncio.CancelledError:
                    self.control_store.finish_attempt(active, ordinal, "cancelled", "cancelled", None)
                    self.control_store.finish_invocation(active, "cancelled")
                    raise
                except GatewayError as error:
                    last_error = error
                    status = "cancelled" if error.kind == "cancelled" else "failed"
                    if error.kind != "budget":
                        self.control_store.finish_attempt(active, ordinal, status, error.kind, None)
                    if on_attempt_finished is not None:
                        on_attempt_finished(ordinal, status, error.kind, None)
                    if error.kind == "cancelled":
                        self.control_store.finish_invocation(active, "cancelled")
                        raise GatewayError(str(error), error.kind, ordinal) from error
                    if error.kind == "budget":
                        raise GatewayError(str(error), error.kind, ordinal) from error
                    if learning_call:
                        self.control_store.finish_invocation(active, "failed")
                        raise GatewayError(str(error), error.kind, ordinal) from error
                    if error.kind == "context_overflow":
                        # Same rule as the single-profile gateway: this layer
                        # cannot shrink a payload, so resending an identical
                        # body would burn a retry and mask the real signal.
                        # Record that the admitted capacity evidence no longer
                        # holds and fail closed instead of retrying blindly.
                        from .token_budget import effective_input_budget
                        self.control_store.record_event(active, "model.context.capacity_evidence_invalidated", {
                            "model_invocation_id": active.invocation_id,
                            "after_attempt": ordinal,
                            "error_kind": error.kind,
                            "input_limit": effective_input_budget(profile).input_limit,
                        })
                        self.control_store.finish_invocation(active, "failed")
                        raise GatewayError(str(error), error.kind, ordinal) from error
                    retryable = error.kind in self.FALLBACK_ERRORS or error.kind == "structure"
                    if retry + 1 < retries and retryable:
                        self.control_store.record_event(active, "model.attempt.retry_scheduled", {
                            "model_invocation_id": active.invocation_id, "after_attempt": ordinal,
                            "next_attempt": ordinal + 1, "error_kind": error.kind,
                        })
                        continue
                    can_fallback = (
                        profile_index + 1 < len(profiles)
                        and error.kind in self.FALLBACK_ERRORS
                        and not output_started
                    )
                    if can_fallback:
                        self.control_store.record_event(active, "model.fallback.selected", {
                            "model_invocation_id": active.invocation_id,
                            "from_profile_version_id": active.profile_version_id,
                            "to_profile_version_id": profiles[profile_index + 1].registered_profile_version_id,
                            "error_kind": error.kind,
                        })
                        break
                    self.control_store.finish_invocation(active, "failed")
                    raise GatewayError(str(error), error.kind, ordinal) from error
        self.control_store.finish_invocation(handle, "failed")
        raise GatewayError(str(last_error or "model routing failed"), getattr(last_error, "kind", "unknown"), ordinal)

    def _route(self, context: ModelCallContext):
        bundle_id = context.runtime_bundle_id
        with self.db.connection() as connection:
            if not bundle_id:
                row = connection.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()
                if row is None:
                    raise RoutingError("stable runtime bundle is not configured")
                bundle_id = row["bundle_id"]
            bundle = connection.execute("SELECT manifest_json FROM runtime_bundles WHERE id=?", (bundle_id,)).fetchone()
            if bundle is None:
                raise RoutingError("runtime bundle does not exist")
            manifest = json.loads(bundle["manifest_json"])
            routing = manifest.get("model_routing") or {}
            policy_id, digest = routing.get("policy_id"), routing.get("digest")
            policy = connection.execute(
                "SELECT roles_json,policy_digest FROM model_routing_policies WHERE id=? AND owner_id=?",
                (policy_id, context.owner_id),
            ).fetchone()
            if policy is None or policy["policy_digest"] != digest:
                raise RoutingError("runtime bundle routing policy is missing or changed")
            route = json.loads(policy["roles_json"]).get(context.role)
            if not route:
                raise RoutingError(f"runtime bundle has no route for role {context.role}")
            ids = [route["primary"], *route.get("fallback", [])]
            profiles, eligible_ids = [], []
            required = self.ROLE_CAPABILITIES.get(context.role)
            if required is None:
                raise RoutingError("unsupported model role")
            for version_id in ids:
                row = connection.execute(
                    "SELECT v.*,p.status AS profile_status FROM model_profile_versions v "
                    "JOIN model_profiles p ON p.id=v.profile_id WHERE v.id=? AND p.owner_id=?",
                    (version_id, context.owner_id),
                ).fetchone()
                if row is None or row["status"] != "ACTIVE" or row["profile_status"] != "ACTIVE":
                    continue
                capabilities = json.loads(row["capabilities_json"])
                if not all(capabilities.get(item) is True for item in required):
                    continue
                profiles.append(self._profile(row))
                eligible_ids.append(version_id)
            if not profiles:
                raise RoutingError("no routed model satisfies availability and capability requirements")
        snapshot = {
            "runtime_bundle_id": bundle_id, "routing_policy_id": policy_id,
            "routing_policy_digest": digest, "role": context.role,
            "required_capabilities": sorted(required), "configured_profile_sequence": ids,
            "profile_sequence": eligible_ids, "profile_version_id": eligible_ids[0],
            "provider_protocol": profiles[0].provider_protocol, "fallback_enabled": len(eligible_ids) > 1,
        }
        return snapshot, profiles, rebind(
            context, runtime_bundle_id=bundle_id, routing_policy_id=policy_id, routing_policy_digest=digest
        )

    @staticmethod
    def _profile(row: Any):
        from .model_capacity import loads_capacity_record
        from .model_gateway import ModelProfile
        from .token_budget import ProtocolBudget

        admitted = _row_get(row, "admitted_context_limit")
        soft = _row_get(row, "soft_context_limit")
        protocol_budget = None
        raw_protocol = _row_get(row, "protocol_budget_json")
        if raw_protocol:
            try:
                payload = json.loads(raw_protocol)
            except (TypeError, ValueError):
                payload = {}
            if isinstance(payload, dict) and str(payload.get("evidence") or "").strip():
                protocol_budget = ProtocolBudget(
                    b0=int(payload.get("b0", 0) or 0),
                    per_message=int(payload.get("per_message", 0) or 0),
                    per_tool_definition=int(payload.get("per_tool_definition", 0) or 0),
                    per_tool_call=int(payload.get("per_tool_call", 0) or 0),
                    per_tool_result=int(payload.get("per_tool_result", 0) or 0),
                    evidence=str(payload["evidence"]),
                )
        capacity_record = loads_capacity_record(_row_get(row, "capacity_evidence"))
        return ModelProfile(
            row["base_url"], row["model_name"], row["credential_env_ref"],
            float(row["timeout_seconds"]), int(row["max_attempts"]),
            provider_protocol=row["provider_protocol"], provider_name=row["provider_name"],
            context_window=int(row["context_window"]), max_output_tokens=int(row["max_output_tokens"]),
            registered_profile_version_id=row["id"],
            validation_tier=_row_get(row, "validation_tier"),
            admitted_context_limit=int(admitted) if admitted is not None else None,
            soft_context_limit=int(soft) if soft is not None else None,
            context_window_verified=bool(_row_get(row, "context_window_verified", 0)),
            counter_id=_row_get(row, "counter_id") or "utf8-upper-bound",
            counter_version=_row_get(row, "counter_version") or "utf8-upper-bound-v1",
            counter_evidence_version=_row_get(row, "counter_evidence_version"),
            capacity_evidence=_row_get(row, "capacity_evidence"),
            working_window_mode=(capacity_record or {}).get("mode"),
            model_context_limit=(capacity_record or {}).get("model_context_limit"),
            model_max_output_limit=(capacity_record or {}).get("model_max_output_limit"),
            capacity_status=(capacity_record or {}).get("status"),
            capacity_source=(capacity_record or {}).get("source"),
            counter_mode=(capacity_record or {}).get("counter_mode"),
            protocol_budget=protocol_budget,
            history_min_turns=(
                int(_row_get(row, "history_min_turns"))
                if _row_get(row, "history_min_turns") is not None else None
            ),
            compact_ratio=(
                float(_row_get(row, "compact_ratio"))
                if _row_get(row, "compact_ratio") is not None else None
            ),
            recent_window_bytes=(
                int(_row_get(row, "recent_window_bytes"))
                if _row_get(row, "recent_window_bytes") is not None else None
            ),
            recent_window_ratio=(
                float(_row_get(row, "recent_window_ratio"))
                if _row_get(row, "recent_window_ratio") is not None else None
            ),
            archive_trigger_ratio=(
                float(_row_get(row, "archive_trigger_ratio"))
                if _row_get(row, "archive_trigger_ratio") is not None else None
            ),
            archive_reserve_ratio=(
                float(_row_get(row, "archive_reserve_ratio"))
                if _row_get(row, "archive_reserve_ratio") is not None else None
            ),
            archive_prefix_reserve=(
                int(_row_get(row, "archive_prefix_reserve"))
                if _row_get(row, "archive_prefix_reserve") is not None else None
            ),
        )

    @staticmethod
    async def _execute_http_attempt(profile: Any, request: Any, *, cancel_event=None, on_text_delta=None, on_output_started=None):
        from .model_gateway import GatewayError, ModelGateway
        from .config import resolve_credential
        api_key = resolve_credential(profile.api_key_env)
        if not api_key:
            raise GatewayError("model API key missing", "configuration")
        return await ModelGateway(profile)._attempt(request, api_key, cancel_event, on_text_delta, on_output_started)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_get(row: Any, name: str, default: Any = None) -> Any:
    """Read an optional column so pre-migration rows stay readable."""
    try:
        keys = row.keys()
    except AttributeError:
        return default
    return row[name] if name in keys else default


def _budget_contract_columns(profile: Any) -> dict[str, Any]:
    """Flatten the R1 budget contract into ``model_profile_versions`` columns.

    These values participate in the config digest, so changing the tier, A, or
    the adapter wrapper budget produces a new immutable profile version instead
    of silently reusing the previous budget.
    """
    from .model_capacity import capacity_record_for_profile
    from .token_budget import ProtocolBudget

    protocol_budget = getattr(profile, "protocol_budget", None)
    return {
        "admitted_context_limit": getattr(profile, "admitted_context_limit", None),
        "soft_context_limit": getattr(profile, "soft_context_limit", None),
        "context_window_verified": 1 if getattr(profile, "context_window_verified", False) else 0,
        "validation_tier": getattr(profile, "validation_tier", None),
        "counter_id": getattr(profile, "counter_id", None) or "utf8-upper-bound",
        "counter_version": getattr(profile, "counter_version", None) or "utf8-upper-bound-v1",
        "counter_evidence_version": getattr(profile, "counter_evidence_version", None),
        "capacity_evidence": capacity_record_for_profile(profile),
        "protocol_budget_json": _json(protocol_budget.public_view()) if isinstance(protocol_budget, ProtocolBudget) else "{}",
        # R1 selection policy. These belong to the versioned profile, not to the
        # selector: a policy value that cannot survive a round trip through the
        # database is not a policy, it is a default with extra steps.
        "history_min_turns": getattr(profile, "history_min_turns", None),
        "compact_ratio": getattr(profile, "compact_ratio", None),
        "recent_window_bytes": getattr(profile, "recent_window_bytes", None),
        "recent_window_ratio": getattr(profile, "recent_window_ratio", None),
        # R2-03 static early-archival policy.
        "archive_trigger_ratio": getattr(profile, "archive_trigger_ratio", None),
        "archive_reserve_ratio": getattr(profile, "archive_reserve_ratio", None),
        "archive_prefix_reserve": getattr(profile, "archive_prefix_reserve", None),
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execution_identity_digest(context: ModelCallContext) -> str | None:
    """The first-phase execution identity of this call, when it has one.

    Idempotency is checked against this as well as the request and the frozen
    input: two calls that agree on every byte of input but were opened under
    different execution contexts are different calls, and reusing one key for
    both would silently merge their ledgers.
    """
    if context.harness is None:
        return None
    from .execution_context import execution_context_digest

    return execution_context_digest(context.harness)


def _execution_identity_conflicts(existing: Any, context: ModelCallContext) -> list[str]:
    """Compare the stored execution identity with the claimed one.

    A call opened under a harness and one opened without are different calls even
    when every persisted public column agrees, so a *one-sided* harness is a
    conflict rather than something to skip.  Only when neither side carries an
    identity is there nothing extra to compare — and then the public columns are
    the whole contract, which is why they are compared field by field rather than
    skipped as well.
    """
    stored = existing["execution_context_digest"]
    claimed = _execution_identity_digest(context)
    if bool(stored) != bool(claimed):
        return ["execution identity"]
    if stored and claimed and stored != claimed:
        return ["execution identity"]
    return []
