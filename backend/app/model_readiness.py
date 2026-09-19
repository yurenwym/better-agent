from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import resolve_credential
from .costs import monetary_limits_enabled


class ModelReadinessService:
    """No-network checks that prevent predictably doomed model work."""

    def __init__(self, db, behavior, *, owner_id: str = "local-user") -> None:
        self.db = db
        self.behavior = behavior
        self.owner_id = owner_id

    def check(self, required_roles: Iterable[str] = ("conversation", "ask")) -> dict[str, Any]:
        roles = tuple(dict.fromkeys(required_roles))
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        try:
            stable = self.behavior.active("stable")
        except KeyError:
            stable = None
        bindings = (stable.manifest.get("model_role_bindings") or {}) if stable else {}
        version_ids: list[str] = []
        primary_ids: set[str] = set()
        for role in roles:
            route = bindings.get(role)
            if not isinstance(route, dict) or not route.get("primary"):
                errors.append(_error("MODEL_ROUTE_MISSING", f"模型角色 {role} 尚未配置", role=role))
                continue
            version_ids.extend([route["primary"], *route.get("fallback", [])])
            primary_ids.add(route["primary"])

        profiles: dict[str, Any] = {}
        prices: dict[str, Any] = {}
        unique_ids = tuple(dict.fromkeys(version_ids))
        if unique_ids:
            placeholders = ",".join("?" for _ in unique_ids)
            with self.db.connection() as connection:
                profile_rows = connection.execute(
                    f"SELECT id,status,model_name,provider_name,credential_env_ref,verification_status "
                    f"FROM model_profile_versions WHERE id IN ({placeholders})",
                    unique_ids,
                ).fetchall()
                price_rows = connection.execute(
                    f"SELECT id,profile_version_id,effective_at,source_url FROM model_price_snapshots "
                    f"WHERE profile_version_id IN ({placeholders}) AND effective_at<=? "
                    f"ORDER BY effective_at DESC,id DESC",
                    (*unique_ids, datetime.now(timezone.utc).isoformat()),
                ).fetchall()
            profiles = {row["id"]: row for row in profile_rows}
            for row in price_rows:
                prices.setdefault(row["profile_version_id"], row)
        for version_id in unique_ids:
            findings = errors if version_id in primary_ids else warnings
            profile = profiles.get(version_id)
            if profile is None or profile["status"] != "ACTIVE":
                findings.append(_error("MODEL_PROFILE_INACTIVE", "模型版本不存在或未启用", profile_version_id=version_id))
                continue
            if not resolve_credential(profile["credential_env_ref"]):
                findings.append(_error(
                    "MODEL_CREDENTIAL_MISSING", f"模型密钥 {profile['credential_env_ref']} 尚未配置",
                    profile_version_id=version_id, credential_env_ref=profile["credential_env_ref"],
                ))
            if version_id not in prices:
                (findings if monetary_limits_enabled() else warnings).append(_error(
                    "MODEL_PRICE_MISSING", "当前模型版本没有已生效的价格快照",
                    profile_version_id=version_id, model=profile["model_name"],
                ))

        budget_values = {}
        for name, default in (
            ("ROOT_TASK_MAX_ATTEMPTS", 30),
            ("ROOT_TASK_DEADLINE_MINUTES", 30),
            ("ROOT_TASK_MAX_COST_MICROUSD", 1_000_000),
        ):
            if name == "ROOT_TASK_MAX_COST_MICROUSD" and not monetary_limits_enabled():
                continue
            try:
                value = int(os.getenv(name, str(default)))
            except ValueError:
                value = 0
            budget_values[name] = value
            if value <= 0:
                errors.append(_error("MODEL_BUDGET_INVALID", f"{name} 必须是正整数", setting=name))

        price_view = {
            version_id: {
                "snapshot_id": prices[version_id]["id"],
                "effective_at": prices[version_id]["effective_at"],
                "source_url": prices[version_id]["source_url"],
            }
            for version_id in unique_ids if version_id in prices
        }
        verified = bool(unique_ids) and all(
            profiles.get(version_id) is not None
            and profiles[version_id]["verification_status"] == "VERIFIED"
            for version_id in unique_ids
        )
        return {
            "status": "READY" if not errors else "NOT_READY",
            "cost_mode": "enforce" if monetary_limits_enabled() else "observe",
            "ready": not errors,
            "network_verified": verified,
            "required_roles": list(roles),
            "profile_version_ids": list(unique_ids),
            "prices": price_view,
            "budget": budget_values,
            "errors": errors,
            "warnings": warnings,
        }


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": False, **details}
