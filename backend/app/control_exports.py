from __future__ import annotations

import json
from typing import Any

from .db import Database


def cost_export(db: Database, owner_id: str = "local-user") -> str:
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT id,period_kind,period_key,invocation_id,attempt_id,price_snapshot_id,entry_type,amount_microusd,"
            "cost_status,reason,created_at FROM cost_ledger WHERE owner_id=? ORDER BY row_id", (owner_id,),
        ).fetchall()
    return _jsonl(rows)


def invocation_export(db: Database, owner_id: str = "local-user") -> str:
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT i.id,i.run_id,i.thread_id,i.role,i.purpose,i.runtime_bundle_id,i.routing_policy_id,i.routing_policy_digest,"
            "i.request_digest,i.tool_schema_digest,i.context_snapshot_digest,i.status,i.selected_attempt_id,i.created_at,i.finished_at,"
            "a.id attempt_id,a.ordinal,a.reason,a.profile_version_id,a.provider_protocol,a.status attempt_status,a.error_kind,"
            "a.uncached_input_tokens,a.cache_read_tokens,a.cache_write_tokens,a.output_tokens,a.reasoning_tokens,a.usage_status,"
            "a.cost_status,a.cost_microusd,a.started_at,a.first_token_at,a.finished_at attempt_finished_at "
            "FROM model_invocations i LEFT JOIN model_attempts a ON a.invocation_id=i.id WHERE i.owner_id=? ORDER BY i.created_at,a.ordinal",
            (owner_id,),
        ).fetchall()
    return _jsonl(rows)


def skill_audit_export(db: Database, owner_id: str = "local-user") -> str:
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT event_id,skill_id,skill_version_id,type,actor,data_json,occurred_at FROM skill_events "
            "WHERE owner_id=? ORDER BY row_id", (owner_id,),
        ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        data = json.loads(item.pop("data_json") or "{}")
        item["data"] = {key: value for key, value in data.items() if key in {
            "version_id", "package_digest", "manifest_digest", "request_digest", "connector_id",
        }}
        output.append(item)
    return "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in output) + ("\n" if output else "")


def _jsonl(rows: list[Any]) -> str:
    return "\n".join(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else "")
