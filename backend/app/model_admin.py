from __future__ import annotations

import hashlib
import inspect
import json
import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from .db import Database


ROLES = {
    "conversation", "ask", "planner", "executor", "reflector", "researcher",
    "expert", "coordinator", "judge_quality", "judge_safety",
}
ROLE_CAPABILITIES = {
    "conversation": {"text", "streaming"}, "ask": {"text", "tool_calling"},
    "planner": {"text", "json_object"}, "executor": {"text", "tool_calling"},
    "reflector": {"text", "json_object"}, "researcher": {"text", "streaming"},
    "expert": {"text"}, "coordinator": {"text", "json_object"},
    "judge_quality": {"text", "json_object"}, "judge_safety": {"text", "json_object"},
}
PROTOCOLS = {"openai_compatible", "anthropic", "gemini"}
MODEL_CAPABILITIES = {
    "text", "streaming", "tool_calling", "json_object", "json_schema",
    "vision", "cache_usage", "reasoning_usage",
}


class ModelAdminError(ValueError):
    pass


class ExternalCredentialMissing(ModelAdminError):
    pass


class ModelAdminService:
    def __init__(self, db: Database, *, owner_id: str = "local-user", verifier: Callable | None = None) -> None:
        self.db = db
        self.owner_id = owner_id
        self.verifier = verifier or self._verify_live

    def create_profile(self, payload: dict[str, Any], *, validate_capacity: bool = True) -> dict[str, Any]:
        name = _text(payload, "name")
        if validate_capacity:
            payload = _resolved_capacity_payload(payload)
            _validate_capacity_contract(payload)
        with self.db.transaction() as connection:
            prior = connection.execute(
                "SELECT id FROM model_profiles WHERE owner_id=? AND name=?", (self.owner_id, name)
            ).fetchone()
            if prior:
                raise ModelAdminError("model profile name already exists")
            profile_id, now = f"model_profile_{uuid.uuid4().hex}", _now()
            connection.execute(
                "INSERT INTO model_profiles(id,owner_id,name,status,created_at,updated_at) VALUES (?,?,?,'ACTIVE',?,?)",
                (profile_id, self.owner_id, name, now, now),
            )
            self._insert_version(connection, profile_id, payload, 1)
        return self.get_profile(profile_id)

    def add_version(self, profile_id: str, payload: dict[str, Any], *, validate_capacity: bool = True) -> dict[str, Any]:
        if validate_capacity:
            payload = _resolved_capacity_payload(payload)
            _validate_capacity_contract(payload)
        with self.db.transaction() as connection:
            profile = connection.execute(
                "SELECT * FROM model_profiles WHERE id=? AND owner_id=?", (profile_id, self.owner_id)
            ).fetchone()
            if profile is None:
                raise KeyError(profile_id)
            version = int(connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM model_profile_versions WHERE profile_id=?", (profile_id,)
            ).fetchone()[0])
            version_id = self._insert_version(connection, profile_id, payload, version)
            connection.execute("UPDATE model_profiles SET status='ACTIVE',updated_at=? WHERE id=?", (_now(), profile_id))
        return self.version(version_id)

    def list_profiles(self) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            ids = [row["id"] for row in connection.execute(
                "SELECT id FROM model_profiles WHERE owner_id=? ORDER BY created_at,id", (self.owner_id,)
            )]
        return [self.get_profile(profile_id) for profile_id in ids]

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM model_profiles WHERE id=? AND owner_id=?", (profile_id, self.owner_id)
            ).fetchone()
            if row is None:
                raise KeyError(profile_id)
            versions = connection.execute(
                "SELECT id FROM model_profile_versions WHERE profile_id=? ORDER BY version DESC", (profile_id,)
            ).fetchall()
        return {
            "id": row["id"], "name": row["name"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "versions": [self.version(item["id"]) for item in versions],
        }

    def version(self, version_id: str) -> dict[str, Any]:
        from .model_capacity import loads_capacity_record

        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT v.*,p.name AS profile_name FROM model_profile_versions v JOIN model_profiles p ON p.id=v.profile_id "
                "WHERE v.id=? AND p.owner_id=?", (version_id, self.owner_id),
            ).fetchone()
        if row is None:
            raise KeyError(version_id)
        capacity_record = loads_capacity_record(row["capacity_evidence"]) or {}
        return {
            "id": row["id"], "profile_id": row["profile_id"], "profile_name": row["profile_name"],
            "version": row["version"], "provider_protocol": row["provider_protocol"],
            "provider_name": row["provider_name"], "base_url": row["base_url"], "model_name": row["model_name"],
            "credential_env_ref": row["credential_env_ref"], "credential_configured": bool(os.getenv(row["credential_env_ref"])),
            "capabilities": json.loads(row["capabilities_json"]), "context_window": row["context_window"],
            "max_output_tokens": row["max_output_tokens"], "timeout_seconds": row["timeout_seconds"],
            "max_attempts": row["max_attempts"], "config_digest": row["config_digest"], "status": row["status"],
            "verified_at": row["verified_at"], "verification_status": row["verification_status"],
            "verification_error_kind": row["verification_error_kind"], "created_at": row["created_at"],
            # Capacity and counter contract. These fields decide the working
            # window and the counting ruler, so they must survive the
            # register -> store -> reload -> route round trip.
            "admitted_context_limit": row["admitted_context_limit"],
            "soft_context_limit": row["soft_context_limit"],
            "context_window_verified": bool(row["context_window_verified"]),
            "validation_tier": row["validation_tier"],
            "counter_id": row["counter_id"],
            "counter_version": row["counter_version"],
            "counter_evidence_version": row["counter_evidence_version"],
            "capacity_evidence": row["capacity_evidence"],
            "capacity": capacity_record,
            "working_window_mode": capacity_record.get("mode"),
            "model_context_limit": capacity_record.get("model_context_limit"),
            "model_max_output_limit": capacity_record.get("model_max_output_limit"),
            "capacity_status": capacity_record.get("status"),
            "capacity_source": capacity_record.get("source"),
            "counter_mode": capacity_record.get("counter_mode"),
            "protocol_budget": json.loads(row["protocol_budget_json"] or "{}"),
            "history_min_turns": row["history_min_turns"],
            "compact_ratio": row["compact_ratio"],
            "recent_window_bytes": row["recent_window_bytes"],
            "recent_window_ratio": row["recent_window_ratio"],
            "archive_trigger_ratio": row["archive_trigger_ratio"],
            "archive_reserve_ratio": row["archive_reserve_ratio"],
            "archive_prefix_reserve": row["archive_prefix_reserve"],
        }

    async def verify(self, version_id: str) -> dict[str, Any]:
        item = self.version(version_id)
        if not os.getenv(item["credential_env_ref"]):
            raise ExternalCredentialMissing(item["credential_env_ref"])
        try:
            result = self.verifier(item)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ModelAdminError("model verification failed")
        except Exception as exc:
            kind = getattr(exc, "kind", "verification_failed")
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE model_profile_versions SET verification_status='FAILED',verification_error_kind=? WHERE id=?",
                    (kind, version_id),
                )
            raise
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE model_profile_versions SET verification_status='VERIFIED',verification_error_kind=NULL,verified_at=? WHERE id=?",
                (_now(), version_id),
            )
        return self.version(version_id)

    def disable(self, version_id: str) -> dict[str, Any]:
        self.version(version_id)
        with self.db.transaction() as connection:
            connection.execute("UPDATE model_profile_versions SET status='DISABLED' WHERE id=?", (version_id,))
        return self.version(version_id)

    def create_policy(self, name: str, roles: dict[str, Any]) -> dict[str, Any]:
        normalized = self._validate_roles(roles)
        with self.db.transaction() as connection:
            version = int(connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM model_routing_policies WHERE owner_id=? AND name=?",
                (self.owner_id, name),
            ).fetchone()[0])
            payload = {"name": name, "version": version, "roles": normalized}
            policy_id = f"model_routing_policy_{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO model_routing_policies(id,owner_id,version,name,roles_json,policy_digest,created_at) VALUES (?,?,?,?,?,?,?)",
                (policy_id, self.owner_id, version, name, _json(normalized), _digest(payload), _now()),
            )
        return self.policy(policy_id)

    def ensure_profile(self, profile: Any, *, capabilities_env: str = "AGENT_MODEL_CAPABILITIES") -> Any:
        """Register a legacy environment profile once and return its immutable version binding."""
        configured = {
            item.strip() for item in os.getenv(capabilities_env, "").split(",") if item.strip()
        }
        if not configured:
            # Provider presets declare their verified capabilities; without this
            # fallback a saved DeepSeek preset would register with no capabilities,
            # drop the conversation/ask routes and leave the runtime NOT_READY.
            configured = set(getattr(profile, "declared_capabilities", ()) or ())
        unknown = configured - MODEL_CAPABILITIES
        if unknown:
            raise ModelAdminError(f"unknown {capabilities_env}: {', '.join(sorted(unknown))}")
        capabilities = {name: name == "text" or name in configured for name in MODEL_CAPABILITIES}
        from .model_capacity import capacity_record_for_profile

        protocol_budget = getattr(profile, "protocol_budget", None)
        if protocol_budget is not None and hasattr(protocol_budget, "public_view"):
            protocol_budget = protocol_budget.public_view()
        payload = {
            "provider_protocol": profile.provider_protocol, "provider_name": profile.provider_name,
            "base_url": profile.base_url, "model_name": profile.model,
            "credential_env_ref": profile.api_key_env, "capabilities": capabilities,
            "context_window": max(int(profile.context_window), 1),
            "max_output_tokens": max(int(profile.max_output_tokens), 1),
            "timeout_seconds": profile.timeout_seconds, "max_attempts": profile.max_attempts,
            # Capacity and counter contract: without these the reloaded profile
            # loses its window provenance and counting strategy.
            "validation_tier": getattr(profile, "validation_tier", None),
            "admitted_context_limit": getattr(profile, "admitted_context_limit", None),
            "soft_context_limit": getattr(profile, "soft_context_limit", None),
            "context_window_verified": bool(getattr(profile, "context_window_verified", False)),
            "counter_id": getattr(profile, "counter_id", None),
            "counter_version": getattr(profile, "counter_version", None),
            "counter_evidence_version": getattr(profile, "counter_evidence_version", None),
            "capacity_evidence": capacity_record_for_profile(profile),
            "protocol_budget": protocol_budget,
            "history_min_turns": getattr(profile, "history_min_turns", None),
            "compact_ratio": getattr(profile, "compact_ratio", None),
            "recent_window_bytes": getattr(profile, "recent_window_bytes", None),
            "recent_window_ratio": getattr(profile, "recent_window_ratio", None),
            "archive_trigger_ratio": getattr(profile, "archive_trigger_ratio", None),
            "archive_reserve_ratio": getattr(profile, "archive_reserve_ratio", None),
            "archive_prefix_reserve": getattr(profile, "archive_prefix_reserve", None),
        }
        digest = _digest(_version_config(payload))
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT v.id FROM model_profile_versions v JOIN model_profiles p ON p.id=v.profile_id "
                "WHERE p.owner_id=? AND v.config_digest=? ORDER BY v.created_at DESC LIMIT 1",
                (self.owner_id, digest),
            ).fetchone()
        if row is None:
            base_name = f"{profile.provider_name}:{profile.model}"
            with self.db.connection() as connection:
                existing = connection.execute(
                    "SELECT id FROM model_profiles WHERE owner_id=? AND name=?", (self.owner_id, base_name)
                ).fetchone()
            if existing is None:
                created = self.create_profile({"name": base_name, **payload}, validate_capacity=False)
                version_id = created["versions"][0]["id"]
            else:
                version_id = self.add_version(existing["id"], payload, validate_capacity=False)["id"]
        else:
            version_id = row["id"]
        return replace(profile, registered_profile_version_id=version_id)

    def ensure_policy(self, name: str, roles: dict[str, Any]) -> dict[str, Any]:
        normalized = self._validate_roles(roles)
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT id,roles_json FROM model_routing_policies WHERE owner_id=? AND name=? ORDER BY version DESC LIMIT 1",
                (self.owner_id, name),
            ).fetchone()
        if row is not None and json.loads(row["roles_json"]) == normalized:
            return self.policy(row["id"])
        return self.create_policy(name, normalized)

    def list_policies(self) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            ids = [row["id"] for row in connection.execute(
                "SELECT id FROM model_routing_policies WHERE owner_id=? ORDER BY created_at,id", (self.owner_id,)
            )]
        return [self.policy(policy_id) for policy_id in ids]

    def policy(self, policy_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM model_routing_policies WHERE id=? AND owner_id=?", (policy_id, self.owner_id)
            ).fetchone()
        if row is None:
            raise KeyError(policy_id)
        return {"id": row["id"], "name": row["name"], "version": row["version"], "roles": json.loads(row["roles_json"]), "policy_digest": row["policy_digest"], "created_at": row["created_at"]}

    def _insert_version(self, connection, profile_id: str, payload: dict[str, Any], version: int) -> str:
        config = _version_config(payload)
        if config["timeout_seconds"] <= 0 or not config["credential_env_ref"].replace("_", "A").isalnum():
            raise ModelAdminError("invalid model configuration")
        digest, version_id = _digest(config), f"model_profile_version_{uuid.uuid4().hex}"
        capacity_evidence = config["capacity_evidence"]
        if capacity_evidence is not None and not isinstance(capacity_evidence, str):
            capacity_evidence = _json(capacity_evidence)
        connection.execute(
            "INSERT INTO model_profile_versions(id,profile_id,version,provider_protocol,provider_name,base_url,model_name,"
            "credential_env_ref,capabilities_json,context_window,max_output_tokens,timeout_seconds,max_attempts,config_digest,created_at,"
            "validation_tier,admitted_context_limit,soft_context_limit,context_window_verified,counter_id,counter_version,"
            "counter_evidence_version,capacity_evidence,protocol_budget_json,history_min_turns,compact_ratio,"
            "recent_window_bytes,recent_window_ratio,archive_trigger_ratio,archive_reserve_ratio,archive_prefix_reserve) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                version_id, profile_id, version, config["provider_protocol"], config["provider_name"],
                config["base_url"], config["model_name"], config["credential_env_ref"],
                _json(config["capabilities"]), config["context_window"], config["max_output_tokens"],
                config["timeout_seconds"], config["max_attempts"], digest, _now(),
                config["validation_tier"], config["admitted_context_limit"], config["soft_context_limit"],
                1 if config["context_window_verified"] else 0, config["counter_id"], config["counter_version"],
                config["counter_evidence_version"], capacity_evidence,
                _json(config["protocol_budget"]), config["history_min_turns"], config["compact_ratio"],
                config["recent_window_bytes"], config["recent_window_ratio"], config["archive_trigger_ratio"],
                config["archive_reserve_ratio"], config["archive_prefix_reserve"],
            ),
        )
        return version_id

    def _validate_roles(self, roles: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(roles, dict) or not roles:
            raise ModelAdminError("routing roles are required")
        normalized = {}
        for role, route in roles.items():
            if role not in ROLES or not isinstance(route, dict):
                raise ModelAdminError("invalid routing role")
            primary, fallback = route.get("primary"), route.get("fallback", [])
            if not isinstance(primary, str) or not isinstance(fallback, list) or any(not isinstance(v, str) for v in fallback):
                raise ModelAdminError("invalid route")
            versions = [self.version(version_id) for version_id in [primary, *fallback]]
            for item in versions:
                if item["status"] != "ACTIVE" or not ROLE_CAPABILITIES[role].issubset({k for k, v in item["capabilities"].items() if v}):
                    raise ModelAdminError(f"model does not satisfy {role} capabilities")
            normalized[role] = {"primary": primary, "fallback": list(dict.fromkeys(fallback))}
        return normalized

    async def _verify_live(self, item: dict[str, Any]) -> dict[str, Any]:
        from .model_gateway import ModelGateway, ModelProfile, ModelRequest
        profile = ModelProfile(
            item["base_url"], item["model_name"], item["credential_env_ref"], item["timeout_seconds"],
            item["max_attempts"], provider_protocol=item["provider_protocol"], provider_name=item["provider_name"],
            context_window=item["context_window"], max_output_tokens=item["max_output_tokens"],
            registered_profile_version_id=item["id"],
        )
        response = await ModelGateway(profile).complete(ModelRequest(messages=[{"role": "user", "content": "只回复 OK"}], max_tokens=8))
        return {"ok": bool(response.message.strip())}


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ModelAdminError(f"{key} is required")
    return value.strip()


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelAdminError("capacity integers must be integers")
    return value


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelAdminError("capacity ratios must be numbers")
    return float(value)


def _resolved_capacity_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve the capacity contract server-side when a working-window mode is set.

    A client may choose *manual* or *auto*, but it can never promote a window
    with its own evidence: auto is resolved from the server catalog, and manual
    only supplies the smaller cap. The resolved record is persisted, so the
    reloaded profile cannot disagree with the catalog it was resolved against.
    """
    from . import model_capacity as capacity_module
    from .model_capacity import CapacityContractError, dumps_capacity_record, resolve_working_window

    mode = payload.get("working_window_mode")
    if mode is None:
        return payload
    base_url = _text(payload, "base_url")
    protocol = _text(payload, "provider_protocol")
    model_name = _text(payload, "model_name")
    max_output = _positive_int(payload, "max_output_tokens")
    admitted = payload.get("admitted_context_limit")
    soft = payload.get("soft_context_limit")
    try:
        if mode == "manual":
            context_window = _positive_int(payload, "context_window")
            resolution = resolve_working_window(
                base_url=base_url, protocol=protocol, model_id=model_name, mode="manual",
                explicit_context_window=context_window,
                admitted_context_limit=admitted, soft_context_limit=soft,
                max_output_tokens=max_output, entries=capacity_module.CATALOG,
            )
        elif mode == "auto":
            resolution = resolve_working_window(
                base_url=base_url, protocol=protocol, model_id=model_name, mode="auto",
                admitted_context_limit=admitted, soft_context_limit=soft,
                max_output_tokens=max_output, entries=capacity_module.CATALOG,
            )
            if resolution.effective_context_limit is None or resolution.status != "verified":
                raise ModelAdminError(
                    "auto working window requires verified capacity evidence "
                    "resolved from the server catalog"
                )
        else:
            raise ModelAdminError("unsupported working window mode")
    except CapacityContractError as exc:
        raise ModelAdminError(str(exc)) from exc
    record = resolution.capacity_record()
    resolved = dict(payload)
    resolved.update({
        "context_window": int(resolution.effective_context_limit),
        "soft_context_limit": resolution.soft_context_limit,
        "admitted_context_limit": resolution.admitted_context_limit,
        "context_window_verified": resolution.status == "verified",
        "counter_id": resolution.counter_id,
        "counter_version": resolution.counter_version,
        "capacity_evidence": dumps_capacity_record(record),
    })
    return resolved


def _validate_capacity_contract(payload: dict[str, Any]) -> None:
    """Reject a public profile write whose capacity contract is not evidenced.

    ``ensure_profile`` bypasses this: the environment loader already resolved
    its window (including the legacy conservative fallback) before registering.
    """
    from .model_capacity import (
        CAPACITY_STATUS_MANUAL,
        CAPACITY_STATUS_OFFICIAL_DEFAULT,
        CAPACITY_STATUS_VERIFIED,
        loads_capacity_record,
    )

    record = loads_capacity_record(payload.get("capacity_evidence"))
    if record is None:
        return
    status = record.get("status")
    if status not in {
        CAPACITY_STATUS_VERIFIED, CAPACITY_STATUS_MANUAL, CAPACITY_STATUS_OFFICIAL_DEFAULT,
    }:
        raise ModelAdminError(
            "capacity evidence must be verified capacity evidence; "
            "unverified capacity cannot be registered through the public API"
        )
    model_output = record.get("model_max_output_limit")
    max_output = payload.get("max_output_tokens")
    if isinstance(model_output, int) and isinstance(max_output, int) and max_output > model_output:
        raise ModelAdminError(
            f"max_output_tokens {max_output} exceeds the model output limit {model_output}"
        )
    model_context = record.get("model_context_limit")
    context_window = payload.get("context_window")
    if (
        record.get("mode") == "manual"
        and isinstance(model_context, int)
        and isinstance(context_window, int)
        and context_window > model_context
    ):
        raise ModelAdminError(
            f"manual context window {context_window} exceeds the verified model capacity {model_context}"
        )


def _version_config(payload: dict[str, Any]) -> dict[str, Any]:
    """The frozen fields of one profile version, including the capacity contract.

    The digest is computed over this dict, so a counter or capacity change
    creates a new immutable version instead of reusing an old one.
    """
    protocol = _text(payload, "provider_protocol")
    if protocol not in PROTOCOLS:
        raise ModelAdminError("unsupported provider protocol")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict) or any(not isinstance(k, str) or not isinstance(v, bool) for k, v in capabilities.items()):
        raise ModelAdminError("capabilities must be a boolean object")
    protocol_budget = payload.get("protocol_budget")
    if protocol_budget is None:
        protocol_budget = {}
    if not isinstance(protocol_budget, dict):
        raise ModelAdminError("protocol_budget must be an object")
    config = {
        "provider_protocol": protocol, "provider_name": _text(payload, "provider_name"),
        "base_url": _text(payload, "base_url").rstrip("/"), "model_name": _text(payload, "model_name"),
        "credential_env_ref": _text(payload, "credential_env_ref"), "capabilities": capabilities,
        "context_window": _positive_int(payload, "context_window"), "max_output_tokens": _positive_int(payload, "max_output_tokens"),
        "timeout_seconds": float(payload.get("timeout_seconds", 60)), "max_attempts": _positive_int(payload, "max_attempts"),
        "validation_tier": payload.get("validation_tier"),
        "admitted_context_limit": _optional_int(payload.get("admitted_context_limit")),
        "soft_context_limit": _optional_int(payload.get("soft_context_limit")),
        "context_window_verified": bool(payload.get("context_window_verified", False)),
        "counter_id": str(payload.get("counter_id") or "utf8-upper-bound"),
        "counter_version": str(payload.get("counter_version") or "utf8-upper-bound-v1"),
        "counter_evidence_version": payload.get("counter_evidence_version"),
        "capacity_evidence": payload.get("capacity_evidence"),
        "protocol_budget": protocol_budget,
        "history_min_turns": _optional_int(payload.get("history_min_turns")),
        "compact_ratio": _optional_float(payload.get("compact_ratio")),
        "recent_window_bytes": _optional_int(payload.get("recent_window_bytes")),
        "recent_window_ratio": _optional_float(payload.get("recent_window_ratio")),
        "archive_trigger_ratio": _optional_float(payload.get("archive_trigger_ratio")),
        "archive_reserve_ratio": _optional_float(payload.get("archive_reserve_ratio")),
        "archive_prefix_reserve": _optional_int(payload.get("archive_prefix_reserve")),
    }
    _validate_budget_config(config)
    return config


def _validate_budget_config(config: dict[str, Any]) -> None:
    """Validate the frozen budget before either public or internal registration."""
    from types import SimpleNamespace
    from .token_budget import ProtocolBudget, effective_input_budget

    tier = config["validation_tier"]
    if tier == "A":
        admitted = config["admitted_context_limit"]
        if admitted is None or admitted <= 0:
            raise ModelAdminError("validation_tier A requires admitted_context_limit")
        evidence = config["counter_evidence_version"]
        if not isinstance(evidence, str) or not evidence.strip():
            raise ModelAdminError("validation_tier A requires counter_evidence_version")
        if not config["protocol_budget"]:
            raise ModelAdminError("validation_tier A requires protocol_budget")
        if not str(config["protocol_budget"].get("evidence") or "").strip():
            raise ModelAdminError("protocol_budget requires recorded evidence")
    soft = config["soft_context_limit"]
    if soft is not None and soft != config["context_window"]:
        raise ModelAdminError("context_window and soft_context_limit disagree")
    protocol = dict(config["protocol_budget"])
    protocol.pop("declared", None)  # Derived by ProtocolBudget, never trusted from callers.
    try:
        if protocol:
            budget = ProtocolBudget(**protocol)
            config["protocol_budget"] = budget.public_view()
        if tier is not None:
            effective_input_budget(SimpleNamespace(**config))
    except (ValueError, TypeError) as exc:
        raise ModelAdminError(str(exc)) from exc


def _positive_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ModelAdminError(f"{key} must be positive")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
