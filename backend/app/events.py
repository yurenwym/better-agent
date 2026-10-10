from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, replace
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


def _metadata_json(metadata) -> str | None:
    from .event_envelope import EventMetadata
    if metadata is None:
        return None
    if not isinstance(metadata, EventMetadata):
        raise TypeError("metadata must be EventMetadata")
    return _json(metadata.to_dict())


def _lock_stream(connection, backend: str, kind: str, stream_id: str) -> None:
    if backend == "postgresql":
        # Transaction-scoped lock also works before a domain run row exists.
        # SQLite writers already hold BEGIN IMMEDIATE.
        if kind == "thread":
            connection.execute("SELECT id FROM threads WHERE id=? FOR UPDATE", (stream_id,)).fetchone()
        else:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(?,0))", (kind + ":" + stream_id,))


def _duplicate(connection, table, event, decode):
    row = connection.execute(f"SELECT * FROM {table} WHERE event_id=?", (event.event_id,)).fetchone()
    if row is None:
        return None
    previous = decode(row)
    fields = ("type", "actor", "data", "thread_id", "turn_id") if table == "thread_events" else (
        "type", "actor", "data", "run_id", "goal_id", "correlation")
    if any(getattr(previous, key) != getattr(event, key) for key in fields):
        raise ValueError("event id reused with different content")
    if event.envelope_json is not None and previous.envelope_json != event.envelope_json:
        raise ValueError("event id reused with different metadata")
    return previous


def _bind_metadata(connection, event):
    """Resolve existing durable identities; never invent a root for old data."""
    from .event_metadata import bind_event_metadata
    return bind_event_metadata(connection, event)


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
    envelope_json: str | None = None


@dataclass(frozen=True)
class ThreadEvent:
    schema_version: int
    event_id: str
    seq: int
    thread_id: str
    turn_id: str
    type: str
    occurred_at: str
    actor: str
    data: dict[str, Any]
    envelope_json: str | None = None


class ThreadEventStore:
    """Append-only semantic events with a durable per-thread cursor."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def append(
        self,
        thread_id: str,
        turn_id: str,
        event_type: str,
        actor: str,
        data: dict[str, Any],
        *,
        connection: Any | None = None,
        occurred_at: str | None = None,
        event_id: str | None = None,
        metadata=None,
    ) -> ThreadEvent:
        timestamp = occurred_at or utc_now()
        event = ThreadEvent(
            schema_version=1,
            event_id=event_id or f"tevt_{uuid.uuid4().hex}",
            seq=0,
            thread_id=thread_id,
            turn_id=turn_id,
            type=event_type,
            occurred_at=timestamp,
            actor=actor,
            data=data,
            envelope_json=_metadata_json(metadata),
        )
        if connection is None:
            with self.db.transaction() as transaction:
                return self._append(transaction, event)
        return self._append(connection, event)

    def _append(self, connection: Any, event: ThreadEvent) -> ThreadEvent:
        _lock_stream(connection, self.db.backend, "thread", event.thread_id)
        previous = _duplicate(connection, "thread_events", event, _row_to_thread_event)
        if previous is not None:
            return previous
        event = _bind_metadata(connection, event)
        row = connection.execute(
            "SELECT next_event_seq FROM threads WHERE id = ?",
            (event.thread_id,),
        ).fetchone()
        if row is None:
            raise KeyError(event.thread_id)
        seq = int(row["next_event_seq"])
        connection.execute(
            "UPDATE threads SET next_event_seq = ?, updated_at = ? WHERE id = ?",
            (seq + 1, event.occurred_at, event.thread_id),
        )
        connection.execute(
            """
            INSERT INTO thread_events(
                schema_version, event_id, seq, thread_id, turn_id, type,
                occurred_at, actor, data_json, envelope_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.schema_version,
                event.event_id,
                seq,
                event.thread_id,
                event.turn_id,
                event.type,
                event.occurred_at,
                event.actor,
                _json(event.data),
                event.envelope_json,
            ),
        )
        return replace(event, seq=seq)

    def list(self, thread_id: str, after_seq: int = 0) -> list[ThreadEvent]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM thread_events WHERE thread_id = ? AND seq > ? ORDER BY seq",
                (thread_id, after_seq),
            ).fetchall()
        return [_row_to_thread_event(row) for row in rows]


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
        connection: Any | None = None,
        *, event_id: str | None = None, metadata=None,
    ) -> Event:
        event = Event(
            schema_version=1,
            event_id=event_id or f"evt_{uuid.uuid4().hex}",
            seq=0,
            run_id=run_id,
            goal_id=goal_id,
            type=event_type,
            occurred_at=occurred_at or utc_now(),
            actor=actor,
            correlation=correlation or {},
            data=data,
            envelope_json=_metadata_json(metadata),
        )
        if connection is None:
            with self.db.transaction() as transaction:
                stored = self._append(transaction, event)
        else:
            stored = self._append(connection, event)
        if connection is None and self.projector is not None and event_type not in {"model.response.delta", "model.response.reset"}:
            self.projector.project(run_id)
        return stored

    def _append(self, connection: Any, event: Event) -> Event:
        _lock_stream(connection, self.db.backend, "run", event.run_id)
        previous = _duplicate(connection, "events", event, _row_to_event)
        if previous is not None:
            return previous
        event = _bind_metadata(connection, event)
        next_seq = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?",
            (event.run_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO events(
                schema_version, event_id, seq, run_id, goal_id, type,
                occurred_at, actor, correlation_json, data_json, envelope_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                event.envelope_json,
            ),
        )
        return replace(event, seq=next_seq)

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
        envelope_json=row["envelope_json"] if "envelope_json" in row.keys() else None,
    )


def _row_to_thread_event(row: Any) -> ThreadEvent:
    return ThreadEvent(
        schema_version=row["schema_version"],
        event_id=row["event_id"],
        seq=row["seq"],
        thread_id=row["thread_id"],
        turn_id=row["turn_id"],
        type=row["type"],
        occurred_at=row["occurred_at"],
        actor=row["actor"],
        data=json.loads(row["data_json"]),
        envelope_json=row["envelope_json"] if "envelope_json" in row.keys() else None,
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
            # The legacy export shape never includes private execution metadata.
            payload.pop("envelope_json", None)
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
