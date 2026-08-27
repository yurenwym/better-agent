from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .db import Database


class RoutingError(ValueError):
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


@dataclass(frozen=True)
class ModelCallHandle:
    invocation_id: str
    profile_version_id: str
    context: ModelCallContext


class ModelControlStore:
    def __init__(self, db: Database, *, events: Any | None = None) -> None:
        self.db = db
        self.events = events

    def begin_invocation(self, profile: Any, request: Any, context: ModelCallContext) -> ModelCallHandle:
        now = _now()
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
        }
        config_digest = _digest(config)
        profile_id = f"model_profile_{_digest([profile.provider_name, profile.model])[:24]}"
        profile_version_id = f"model_profile_version_{config_digest[:24]}"
        invocation_id = context.invocation_id or f"model_invocation_{uuid.uuid4().hex}"
        request_payload = {
            "messages": request.messages,
            "tools": request.tools or [],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        request_digest = _digest(request_payload)
        tool_schema_digest = _digest(request.tools or [])
        route_snapshot = {
            "profile_version_id": profile_version_id,
            "provider_protocol": profile.provider_protocol,
            "model": profile.model,
        }
        key = context.idempotency_key or invocation_id
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO model_profiles(id,owner_id,name,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (profile_id, context.owner_id, f"{profile.provider_name}:{profile.model}", "ACTIVE", now, now),
            )
            current = connection.execute("SELECT COALESCE(MAX(version),0) FROM model_profile_versions WHERE profile_id=?", (profile_id,)).fetchone()[0]
            connection.execute(
                "INSERT OR IGNORE INTO model_profile_versions("
                "id,profile_id,version,provider_protocol,provider_name,base_url,model_name,credential_env_ref,capabilities_json,"
                "context_window,max_output_tokens,timeout_seconds,max_attempts,config_digest,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    profile_version_id, profile_id, int(current) + 1, profile.provider_protocol, profile.provider_name,
                    profile.base_url.rstrip("/"), profile.model, profile.api_key_env,
                    _json({"text": True, "streaming": True, "tool_calling": True, "json_object": True}),
                    profile.context_window, profile.max_output_tokens, profile.timeout_seconds, profile.max_attempts, config_digest, now,
                ),
            )
            existing = connection.execute("SELECT id FROM model_invocations WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                row = connection.execute("SELECT route_snapshot_json FROM model_invocations WHERE id=?", (existing["id"],)).fetchone()
                existing_profile = json.loads(row["route_snapshot_json"])["profile_version_id"]
                return ModelCallHandle(existing["id"], existing_profile, context)
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
            self._event(connection, context, "model.invocation.created", {"model_invocation_id": invocation_id, "role": context.role})
        return ModelCallHandle(invocation_id, profile_version_id, context)

    def start_attempt(self, handle: ModelCallHandle, ordinal: int, reason: str) -> str:
        attempt_id = f"{handle.invocation_id}_attempt_{ordinal}"
        now = _now()
        with self.db.transaction() as connection:
            row = connection.execute("SELECT request_digest,route_snapshot_json FROM model_invocations WHERE id=?", (handle.invocation_id,)).fetchone()
            route = json.loads(row["route_snapshot_json"])
            connection.execute(
                "INSERT OR IGNORE INTO model_attempts(id,invocation_id,ordinal,reason,profile_version_id,provider_protocol,request_digest,status,started_at) "
                "VALUES (?,?,?,?,?,?,?,'STARTED',?)",
                (attempt_id, handle.invocation_id, ordinal, reason, handle.profile_version_id, route["provider_protocol"], row["request_digest"], now),
            )
            self._event(connection, handle.context, "model.attempt.started", {
                "model_invocation_id": handle.invocation_id, "model_attempt_id": attempt_id, "attempt": ordinal, "reason": reason,
            })
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
            connection.execute(
                "UPDATE model_attempts SET status=?,error_kind=?,uncached_input_tokens=?,cache_read_tokens=?,cache_write_tokens=?,"
                "output_tokens=?,reasoning_tokens=?,finished_at=?,usage_status=?,usage_digest=? WHERE id=? AND status='STARTED'",
                (
                    db_status, error_kind, values["uncached_input_tokens"], values["cache_read_tokens"],
                    values["cache_write_tokens"], values["output_tokens"], values["reasoning_tokens"], now,
                    usage_status, usage_digest, attempt_id,
                ),
            )
            self._event(connection, handle.context, "model.attempt.finished", {
                "model_invocation_id": handle.invocation_id, "model_attempt_id": attempt_id,
                "attempt": ordinal, "status": status, "error_kind": error_kind,
            })

    def finish_invocation(self, handle: ModelCallHandle, status: str, selected_ordinal: int | None = None) -> None:
        db_status = status.upper()
        selected = f"{handle.invocation_id}_attempt_{selected_ordinal}" if selected_ordinal is not None else None
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE model_invocations SET status=?,selected_attempt_id=?,finished_at=? WHERE id=? AND status='RUNNING'",
                (db_status, selected, _now(), handle.invocation_id),
            )
            self._event(connection, handle.context, "model.invocation.finished", {
                "model_invocation_id": handle.invocation_id, "status": status,
            })

    def _event(self, connection: Any, context: ModelCallContext, event_type: str, data: dict[str, Any]) -> None:
        if self.events is not None and context.run_id and context.goal_id:
            self.events.append(context.run_id, context.goal_id, event_type, "runtime", data, connection=connection)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
