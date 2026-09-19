"""T10: migrate existing model profile versions onto the capacity contract.

Existing versions keep their immutable windows. Migration is opt-in and creates
a *new* version:

* default: the old explicit window is treated as a manual cap (no guessing what
  the user intended);
* ``prefer_auto``: only when the catalog has a verified context integer does the
  new version follow it, otherwise the manual window is kept and the report says
  why.

Running it twice changes nothing: a version that already carries a capacity
record is skipped.
"""

from __future__ import annotations

from typing import Any

from .model_capacity import (
    CAPACITY_STATUS_VERIFIED,
    WORKING_WINDOW_AUTO,
    WORKING_WINDOW_MANUAL,
    dumps_capacity_record,
    loads_capacity_record,
    resolve_working_window,
)


def _payload_from_version(version: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider_protocol": version["provider_protocol"],
        "provider_name": version["provider_name"],
        "base_url": version["base_url"],
        "model_name": version["model_name"],
        "credential_env_ref": version["credential_env_ref"],
        "capabilities": version["capabilities"],
        "context_window": int(version["context_window"]),
        "max_output_tokens": int(version["max_output_tokens"]),
        "timeout_seconds": version["timeout_seconds"],
        "max_attempts": int(version["max_attempts"]),
        "validation_tier": version.get("validation_tier"),
        "admitted_context_limit": version.get("admitted_context_limit"),
        "soft_context_limit": version.get("soft_context_limit"),
        "context_window_verified": version.get("context_window_verified"),
        "counter_id": version.get("counter_id"),
        "counter_version": version.get("counter_version"),
        "counter_evidence_version": version.get("counter_evidence_version"),
        # Legacy versions store an empty object; the admin contract treats a
        # provided-but-unevidenced budget as an error, so pass None instead.
        "protocol_budget": version.get("protocol_budget") or None,
        "history_min_turns": version.get("history_min_turns"),
        "compact_ratio": version.get("compact_ratio"),
        "recent_window_bytes": version.get("recent_window_bytes"),
        "recent_window_ratio": version.get("recent_window_ratio"),
        "archive_trigger_ratio": version.get("archive_trigger_ratio"),
        "archive_reserve_ratio": version.get("archive_reserve_ratio"),
        "archive_prefix_reserve": version.get("archive_prefix_reserve"),
    }


def plan_model_capacity_migration(
    db: Any, *, owner_id: str = "local-user", prefer_auto: bool = False,
) -> list[dict[str, Any]]:
    """Build a dry-run report for every latest model profile version."""
    from .model_admin import ModelAdminService

    service = ModelAdminService(db, owner_id=owner_id)
    plans: list[dict[str, Any]] = []
    for profile in service.list_profiles():
        versions = profile.get("versions") or []
        if not versions:
            continue
        latest = versions[0]
        current_record = loads_capacity_record(latest.get("capacity_evidence"))
        item: dict[str, Any] = {
            "profile_id": profile["id"],
            "profile_name": profile["name"],
            "version_id": latest["id"],
            "version": latest["version"],
            "old_context_window": int(latest["context_window"]),
            "already_migrated": current_record is not None and current_record.get("mode") is not None,
        }
        if item["already_migrated"]:
            item.update({
                "action": "skip",
                "reason": "version already carries a capacity record",
                "proposed_mode": current_record.get("mode"),
                "proposed_effective_context_limit": current_record.get("effective_context_limit"),
                "capacity_status": current_record.get("status"),
            })
            plans.append(item)
            continue
        resolution = resolve_working_window(
            base_url=latest["base_url"],
            protocol=latest["provider_protocol"],
            model_id=latest["model_name"],
            mode=WORKING_WINDOW_AUTO if prefer_auto else WORKING_WINDOW_MANUAL,
            explicit_context_window=None if prefer_auto else int(latest["context_window"]),
            admitted_context_limit=latest.get("admitted_context_limit"),
            soft_context_limit=latest.get("soft_context_limit"),
            max_output_tokens=int(latest["max_output_tokens"]),
        )
        if prefer_auto and resolution.status != CAPACITY_STATUS_VERIFIED:
            # Never upgrade to an unverified large window; keep the manual one.
            resolution = resolve_working_window(
                base_url=latest["base_url"],
                protocol=latest["provider_protocol"],
                model_id=latest["model_name"],
                mode=WORKING_WINDOW_MANUAL,
                explicit_context_window=int(latest["context_window"]),
                admitted_context_limit=latest.get("admitted_context_limit"),
                soft_context_limit=latest.get("soft_context_limit"),
                max_output_tokens=int(latest["max_output_tokens"]),
            )
            item["auto_fallback_reason"] = "catalog capacity is not verified"
        item.update({
            "action": "dry-run",
            "proposed_mode": resolution.mode,
            "proposed_effective_context_limit": resolution.effective_context_limit,
            "capacity_status": resolution.status,
            "capacity_source": resolution.source,
            "model_context_limit": resolution.model_context_limit,
            "model_max_output_limit": resolution.model_max_output_limit,
            "capacity_evidence": resolution.capacity_record(),
        })
        plans.append(item)
    return plans


def apply_model_capacity_migration(
    db: Any, plans: list[dict[str, Any]], *, owner_id: str = "local-user", apply: bool = False,
) -> dict[str, Any]:
    """Apply (or simulate) the plan, creating one new version per change."""
    from .model_admin import ModelAdminService

    service = ModelAdminService(db, owner_id=owner_id)
    results: list[dict[str, Any]] = []
    for item in plans:
        if item["action"] == "skip":
            results.append({**item, "apply_result": "skipped"})
            continue
        if not apply:
            results.append({**item, "apply_result": "dry-run"})
            continue
        profile = service.get_profile(item["profile_id"])
        latest = profile["versions"][0]
        payload = _payload_from_version(latest)
        record = dict(item["capacity_evidence"])
        record["effective_context_limit"] = item["proposed_effective_context_limit"]
        payload["working_window_mode"] = item["proposed_mode"]
        payload["context_window"] = int(item["proposed_effective_context_limit"] or latest["context_window"])
        if item["proposed_mode"] == WORKING_WINDOW_MANUAL:
            payload["soft_context_limit"] = payload["context_window"]
        payload["capacity_evidence"] = dumps_capacity_record(record)
        created = service.add_version(item["profile_id"], payload)
        results.append({
            **item,
            "apply_result": "created",
            "new_version_id": created["id"],
            "new_version": created["version"],
        })
    return {
        "owner_id": owner_id,
        "apply": apply,
        "items": results,
        "created": sum(1 for item in results if item["apply_result"] == "created"),
        "skipped": sum(1 for item in results if item["apply_result"] == "skipped"),
    }

