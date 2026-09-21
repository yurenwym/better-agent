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
    owner_id: str = "local-user"
    run_id: str | None = None
    goal_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    agent_task_id: str | None = None
    runtime_bundle_id: str | None = None
    routing_policy_id: str | None = None
    routing_policy_digest: str = "direct"
    context_snapshot_digest: str = ""
    idempotency_key: str | None = None
    invocation_id: str | None = None
    price_snapshot_id: str | None = None
    root_budget_id: str | None = None


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


@dataclass(frozen=True)
class ModelCallHandle:
    invocation_id: str
    profile_version_id: str
    context: ModelCallContext


class ModelControlStore:
    def __init__(self, db: Database, *, events: Any | None = None, costs: Any | None = None) -> None:
        self.db = db
        self.events = events
        self.costs = costs

    def begin_invocation(
        self, profile: Any, request: Any, context: ModelCallContext,
        route_snapshot: dict[str, Any] | None = None,
    ) -> ModelCallHandle:
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
            existing = connection.execute(
                "SELECT id,request_digest,status FROM model_invocations WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                if existing["request_digest"] != request_digest:
                    raise InvocationIdempotencyConflict(
                        f"idempotency key is already bound to a different request: {key}"
                    )
                raise InvocationReplayError(existing["id"], existing["status"])
            connection.execute(
                "INSERT INTO model_invocations("
                "id,owner_id,run_id,thread_id,turn_id,agent_task_id,role,purpose,runtime_bundle_id,routing_policy_id,"
                "routing_policy_digest,route_snapshot_json,request_digest,tool_schema_digest,context_snapshot_digest,status,"
                "idempotency_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    invocation_id, context.owner_id, context.run_id, context.thread_id, context.turn_id, context.agent_task_id,
                    context.role, context.purpose, context.runtime_bundle_id, context.routing_policy_id,
                    context.routing_policy_digest, _json(route_snapshot), request_digest, tool_schema_digest,
                    context.context_snapshot_digest, "RUNNING", key, now,
                ),
            )
            if context.root_budget_id is not None:
                connection.execute("UPDATE model_invocations SET root_budget_id=? WHERE id=?",
                                   (context.root_budget_id, invocation_id))
            connection.execute("UPDATE model_invocations SET system_prompt_digest=? WHERE id=?", (system_prompt_digest, invocation_id))
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
        return ModelCallHandle(invocation_id, profile_version_id, context)

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
        active = context or self._call_context.get() or ModelCallContext("conversation", "complete", owner_id=owner_id or "local-user")
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
        active = context or self._call_context.get() or ModelCallContext("planner", "compile_goal_program", owner_id=owner_id or "local-user")
        if owner_id is not None and active.owner_id != owner_id:
            active = replace(active, owner_id=owner_id)
        active = child_call_context(active, role=role, purpose=purpose)
        _, profiles, _ = self._route(active)
        return int(profiles[0].max_output_tokens)

    async def complete(
        self, request: Any, cancel_event=None, on_text_delta=None, on_text_reset=None,
        on_attempt_started=None, on_attempt_finished=None, context: ModelCallContext | None = None,
    ) -> Any:
        from .model_gateway import GatewayError, ModelResponse

        context = context or self._call_context.get() or ModelCallContext(
            request.role or "conversation", request.purpose or "complete"
        )
        context = child_call_context(
            context,
            role=request.role,
            purpose=request.purpose,
        )
        route, profiles, context = self._route(context)
        if getattr(request, "single_attempt", False):
            profiles = profiles[:1]
        from .token_budget import ContextOverflow, assert_request_fits
        try:
            assert_request_fits(request.messages, request.tools, profiles[0])
        except ContextOverflow as exc:
            raise GatewayError(str(exc), "context_overflow") from exc
        handle = self.control_store.begin_invocation(profiles[0], request, context, route)
        if cancel_event is not None and cancel_event.is_set():
            self.control_store.finish_invocation(handle, "cancelled")
            raise GatewayError("model request cancelled", "cancelled")
        ordinal = 0
        learning_call = bool(getattr(self, "learning", None) is not None and self.learning.assert_learning_call_allowed(context.owner_id, context.root_budget_id))
        output_started = False
        last_error = None
        for profile_index, profile in enumerate(profiles):
            try:
                assert_request_fits(request.messages, request.tools, profile)
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
            retries = 1 if getattr(request, "single_attempt", False) else max(int(profile.max_attempts), 1)
            for retry in range(retries):
                if getattr(self.control_store, "learning_assets", None) is not None:
                    self.control_store.learning_assets.assert_request_active(handle.invocation_id, context.owner_id)
                if learning_call:
                    self.learning.assert_learning_call_allowed(context.owner_id, context.root_budget_id)
                if context.runtime_bundle_id and getattr(self, "learning", None) is not None:
                    self.learning.assert_pinned_prompt_active(context.owner_id, context.runtime_bundle_id)
                ordinal += 1
                reason = "primary" if ordinal == 1 else ("fallback" if retry == 0 else "retry")
                active = replace(handle, profile_version_id=profile.registered_profile_version_id)
                if ordinal > 1 and on_text_reset is not None:
                    on_text_reset()
                if on_attempt_started is not None:
                    on_attempt_started(ordinal, reason)
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
                    self.control_store.record_request_estimate(active, ordinal, attempt_profile, request)
                    response = await self._execute_attempt(
                        attempt_profile, request, cancel_event=cancel_event,
                        on_text_delta=text_delta, on_output_started=output,
                    )
                    response = ModelResponse(**{**response.__dict__, "attempts": ordinal})
                    if getattr(self.control_store, "learning_assets", None) is not None:
                        self.control_store.learning_assets.assert_request_active(handle.invocation_id, context.owner_id)
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
        return snapshot, profiles, replace(
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
