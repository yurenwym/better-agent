from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .db import Database


SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "env_value",
    "environment_value",
}
TEXT_KEYS = {"content", "message", "body", "note", "prompt", "text", "raw", "delta"}
SECRET_PATTERNS = (
    (re.compile(r"Bearer\s+\S+", re.IGNORECASE), "Bearer <redacted>"),
    (re.compile(r"sk-[A-Za-z0-9_-]+"), "<redacted-key>"),
    (re.compile(r"ghp_[A-Za-z0-9]+"), "<redacted-token>"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Event:
    schema_version: int
    event_id: str
    seq: int
    run_id: str
    goal_id: str
    type: str
    occurred_at: str
    actor: str
    correlation: dict[str, Any]
    data: dict[str, Any]


class EventStore:
    def __init__(self, db: Database, workspace: str | Path | None = None) -> None:
        self.db = db
        self.workspace = Path(workspace) if workspace else db.workspace
        self.projector = None

    def append(
        self,
        run_id: str,
        goal_id: str,
        event_type: str,
        actor: str,
        data: dict[str, Any],
        correlation: dict[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> Event:
        event = Event(
            schema_version=1,
            event_id=f"evt_{uuid.uuid4().hex}",
            seq=0,
            run_id=run_id,
            goal_id=goal_id,
            type=event_type,
            occurred_at=occurred_at or utc_now(),
            actor=actor,
            correlation=correlation or {},
            data=data,
        )
        with self.db.transaction() as connection:
            next_seq = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO events(
                    schema_version, event_id, seq, run_id, goal_id, type,
                    occurred_at, actor, correlation_json, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.schema_version,
                    event.event_id,
                    next_seq,
                    event.run_id,
                    event.goal_id,
                    event.type,
                    event.occurred_at,
                    event.actor,
                    _json(event.correlation),
                    _json(event.data),
                ),
            )
        stored = Event(**{**asdict(event), "seq": next_seq})
        if self.projector is not None and event_type not in {"model.response.delta", "model.response.reset"}:
            self.projector.project(run_id)
        return stored

    def list(self, run_id: str, after_seq: int = 0) -> list[Event]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [_row_to_event(row) for row in rows]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_to_event(row: Any) -> Event:
    return Event(
        schema_version=row["schema_version"],
        event_id=row["event_id"],
        seq=row["seq"],
        run_id=row["run_id"],
        goal_id=row["goal_id"],
        type=row["type"],
        occurred_at=row["occurred_at"],
        actor=row["actor"],
        correlation=json.loads(row["correlation_json"]),
        data=json.loads(row["data_json"]),
    )


def export_jsonl(
    events: Iterable[Event], mode: str = "redacted", workspace: str | Path | None = None
) -> str:
    if mode not in {"redacted", "full"}:
        raise ValueError("export mode must be redacted or full")
    root = Path(workspace).resolve() if workspace else None
    lines: list[str] = []
    try:
        for event in events:
            payload = asdict(event)
            if mode == "redacted":
                payload["data"] = _redact_value(payload["data"], root, "data")
                payload["correlation"] = _redact_value(payload["correlation"], root, "correlation")
            lines.append(_json(payload))
    except Exception as exc:
        raise RuntimeError("redacted export failed") from exc
    return "\n".join(lines) + ("\n" if lines else "")


def _redact_value(value: Any, workspace: Path | None, key: str) -> Any:
    normalized_key = key.lower()
    if normalized_key in SECRET_KEYS:
        return None
    if normalized_key in TEXT_KEYS and isinstance(value, str):
        return {"length": len(value), "redacted": True}
    if normalized_key == "path" and isinstance(value, str):
        candidate = Path(value)
        if workspace is None:
            return "<path>"
        try:
            candidate.resolve().relative_to(workspace)
        except ValueError:
            return "<outside-workspace>"
        return str(candidate.resolve().relative_to(workspace))
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for child_key, child_value in value.items():
            if child_key.lower() in SECRET_KEYS:
                continue
            redacted = _redact_value(child_value, workspace, child_key)
            if isinstance(redacted, str):
                for pattern, replacement in SECRET_PATTERNS:
                    redacted = pattern.sub(replacement, redacted)
            output[child_key] = redacted
        return output
    if isinstance(value, list):
        return [_redact_value(item, workspace, key) for item in value]
    if isinstance(value, str):
        for pattern, replacement in SECRET_PATTERNS:
            value = pattern.sub(replacement, value)
    return value


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
