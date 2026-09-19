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

    def create_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = _text(payload, "name")
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

    def add_version(self, profile_id: str, payload: dict[str, Any]) -> dict[str, Any]:
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
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT v.*,p.name AS profile_name FROM model_profile_versions v JOIN model_profiles p ON p.id=v.profile_id "
                "WHERE v.id=? AND p.owner_id=?", (version_id, self.owner_id),
            ).fetchone()
        if row is None:
            raise KeyError(version_id)
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
        unknown = configured - MODEL_CAPABILITIES
        if unknown:
            raise ModelAdminError(f"unknown {capabilities_env}: {', '.join(sorted(unknown))}")
        capabilities = {name: name == "text" or name in configured for name in MODEL_CAPABILITIES}
        payload = {
            "provider_protocol": profile.provider_protocol, "provider_name": profile.provider_name,
            "base_url": profile.base_url, "model_name": profile.model,
            "credential_env_ref": profile.api_key_env, "capabilities": capabilities,
            "context_window": max(int(profile.context_window), 1),
            "max_output_tokens": max(int(profile.max_output_tokens), 1),
            "timeout_seconds": profile.timeout_seconds, "max_attempts": profile.max_attempts,
        }
        config = {
            "provider_protocol": payload["provider_protocol"], "provider_name": payload["provider_name"],
            "base_url": payload["base_url"].rstrip("/"), "model_name": payload["model_name"],
            "credential_env_ref": payload["credential_env_ref"], "capabilities": capabilities,
            "context_window": payload["context_window"], "max_output_tokens": payload["max_output_tokens"],
            "timeout_seconds": float(payload["timeout_seconds"]), "max_attempts": payload["max_attempts"],
        }
        digest = _digest(config)
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
                created = self.create_profile({"name": base_name, **payload})
                version_id = created["versions"][0]["id"]
            else:
                version_id = self.add_version(existing["id"], payload)["id"]
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
        protocol = _text(payload, "provider_protocol")
        if protocol not in PROTOCOLS:
            raise ModelAdminError("unsupported provider protocol")
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, dict) or any(not isinstance(k, str) or not isinstance(v, bool) for k, v in capabilities.items()):
            raise ModelAdminError("capabilities must be a boolean object")
        config = {
            "provider_protocol": protocol, "provider_name": _text(payload, "provider_name"),
            "base_url": _text(payload, "base_url").rstrip("/"), "model_name": _text(payload, "model_name"),
            "credential_env_ref": _text(payload, "credential_env_ref"), "capabilities": capabilities,
            "context_window": _positive_int(payload, "context_window"), "max_output_tokens": _positive_int(payload, "max_output_tokens"),
            "timeout_seconds": float(payload.get("timeout_seconds", 60)), "max_attempts": _positive_int(payload, "max_attempts"),
        }
        if config["timeout_seconds"] <= 0 or not config["credential_env_ref"].replace("_", "A").isalnum():
            raise ModelAdminError("invalid model configuration")
        digest, version_id = _digest(config), f"model_profile_version_{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO model_profile_versions(id,profile_id,version,provider_protocol,provider_name,base_url,model_name,"
            "credential_env_ref,capabilities_json,context_window,max_output_tokens,timeout_seconds,max_attempts,config_digest,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, profile_id, version, protocol, config["provider_name"], config["base_url"], config["model_name"],
             config["credential_env_ref"], _json(capabilities), config["context_window"], config["max_output_tokens"],
             config["timeout_seconds"], config["max_attempts"], digest, _now()),
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
