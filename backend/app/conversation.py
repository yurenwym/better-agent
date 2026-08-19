from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .db import Database
from .events import ThreadEvent, ThreadEventStore


class RouteProtocolError(ValueError):
    """The model did not produce the bounded conversation response protocol."""


@dataclass(frozen=True)
class RouteDecision:
    policy: str
    content_shape: str = ""
    reason_code: str = ""


class ControlHeadDecoder:
    """Decode one bounded JSON control line without exposing it as Markdown."""

    _policies = {"answer", "propose_execution", "clarify"}

    def __init__(self, max_header_bytes: int = 1024) -> None:
        self.max_header_bytes = max_header_bytes
        self._buffer = ""
        self.header: RouteDecision | None = None

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        if self.header is not None:
            return chunk
        self._buffer += chunk
        if len(self._buffer.encode("utf-8")) > self.max_header_bytes:
            raise RouteProtocolError("conversation control header is too long")
        newline = self._buffer.find("\n")
        if newline < 0:
            return ""
        line = self._buffer[:newline].rstrip("\r")
        remainder = self._buffer[newline + 1 :]
        try:
            payload = json.loads(line)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RouteProtocolError("conversation control header is invalid") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise RouteProtocolError("conversation control header version is invalid")
        if set(payload) - {"v", "policy", "content_shape", "reason_code"}:
            raise RouteProtocolError("conversation control header has unknown fields")
        policy = payload.get("policy")
        if policy not in self._policies:
            raise RouteProtocolError("conversation policy is invalid")
        content_shape = payload.get("content_shape", "")
        reason_code = payload.get("reason_code", "")
        if not isinstance(content_shape, str) or not isinstance(reason_code, str):
            raise RouteProtocolError("conversation control header fields are invalid")
        self.header = RouteDecision(policy, content_shape, reason_code)
        self._buffer = ""
        return remainder

    def finish(self) -> RouteDecision:
        if self.header is None:
            raise RouteProtocolError("conversation control header is missing")
        return self.header


class RouteAndRespondModel(Protocol):
    async def route_and_respond(
        self,
        *,
        content: str,
        history: list[dict[str, str]],
        skill_names: list[str],
        on_text_delta,
        on_text_reset,
        cancel_event,
    ) -> Any: ...


@dataclass(frozen=True)
class ThreadSnapshot:
    id: str
    title: str
    version: int
    active_turn_id: str | None
    next_event_seq: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TurnSnapshot:
    id: str
    thread_id: str
    client_turn_id: str
    parent_turn_id: str | None
    status: str
    policy: str | None
    content_shape: str | None
    reason_code: str | None
    version: int
    materialized_goal_id: str | None
    materialized_run_id: str | None
    direction_action: str | None
    direction_idempotency_key: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ThreadMessageSnapshot:
    id: str
    thread_id: str
    turn_id: str
    role: str
    content: str
    status: str
    generation: int
    content_length: int
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class TurnSubmission:
    thread_id: str
    turn_id: str
    status: str
    version: int
    event_cursor: int


class _FallbackConversationModel:
    async def route_and_respond(
        self,
        *,
        content: str,
        history: list[dict[str, str]],
        skill_names: list[str],
        on_text_delta,
        on_text_reset,
        cancel_event,
    ) -> Any:
        if on_text_delta is not None:
            on_text_delta(
                '{"v":1,"policy":"answer","content_shape":"general",'
                '"reason_code":"content_only"}\n'
                + content
            )
        return None


class ConversationService:
    def __init__(self, db: Database, *, agent_runtime=None, route_model=None) -> None:
        self.db = db
        self.agent_runtime = agent_runtime
        self.route_model: RouteAndRespondModel = route_model or _FallbackConversationModel()
        if not hasattr(self.route_model, "route_and_respond"):
            self.route_model = _FallbackConversationModel()
        self.events = ThreadEventStore(db)
        self._worker = None
        self._cancel_events: dict[str, asyncio.Event] = {}

    def create_thread(self, title: str = "新的对话") -> ThreadSnapshot:
        thread_id = f"thread_{uuid.uuid4().hex}"
        now = _now()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO threads(id, title, version, next_event_seq, created_at, updated_at) "
                "VALUES (?, ?, 0, 1, ?, ?)",
                (thread_id, (title or "新的对话").strip()[:120] or "新的对话", now, now),
            )
        return self.thread(thread_id)

    def thread(self, thread_id: str) -> ThreadSnapshot:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if row is None:
            raise KeyError(thread_id)
        return ThreadSnapshot(
            id=row["id"],
            title=row["title"],
            version=row["version"],
            active_turn_id=row["active_turn_id"],
            next_event_seq=row["next_event_seq"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def turn(self, turn_id: str) -> TurnSnapshot:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        if row is None:
            raise KeyError(turn_id)
        return _turn_from_row(row)

    def messages(self, thread_id: str) -> list[ThreadMessageSnapshot]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM thread_messages WHERE thread_id = ? ORDER BY created_at, id",
                (thread_id,),
            ).fetchall()
        return [_message_from_row(row) for row in rows]

    def pending_turn_jobs(self) -> list[str]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT turn_id FROM turn_jobs WHERE status IN ('QUEUED', 'RUNNING') ORDER BY turn_id"
            ).fetchall()
        return [row["turn_id"] for row in rows]

    def accept_turn(
        self,
        thread_id: str,
        client_turn_id: str,
        content: str,
        skill_names: list[str] | tuple[str, ...] | None = None,
    ) -> TurnSubmission:
        if not isinstance(client_turn_id, str) or not client_turn_id.strip():
            raise ValueError("client_turn_id is required")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content is required")
        selected_skills = list(skill_names or [])
        if not all(isinstance(name, str) for name in selected_skills):
            raise ValueError("skill_names must be an array of strings")
        if self.agent_runtime is not None:
            selected_skills = list(self.agent_runtime.skills.validate(selected_skills))
        now = _now()
        with self.db.transaction() as connection:
            thread = connection.execute(
                "SELECT * FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if thread is None:
                raise KeyError(thread_id)
            existing = connection.execute(
                "SELECT * FROM turns WHERE thread_id = ? AND client_turn_id = ?",
                (thread_id, client_turn_id),
            ).fetchone()
            if existing is not None:
                return TurnSubmission(
                    thread_id=thread_id,
                    turn_id=existing["id"],
                    status=existing["status"],
                    version=existing["version"],
                    event_cursor=max(int(thread["next_event_seq"]) - 1, 0),
                )
            turn_id = f"turn_{uuid.uuid4().hex}"
            message_id = f"message_{uuid.uuid4().hex}"
            parent_turn_id = thread["active_turn_id"]
            connection.execute(
                """
                INSERT INTO turns(
                    id, thread_id, client_turn_id, parent_turn_id, status, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'ACCEPTED', 0, ?, ?)
                """,
                (turn_id, thread_id, client_turn_id, parent_turn_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO thread_messages(
                    id, thread_id, turn_id, role, content, status, generation,
                    content_length, created_at
                ) VALUES (?, ?, ?, 'user', ?, 'ready', 1, ?, ?)
                """,
                (message_id, thread_id, turn_id, content, len(content), now),
            )
            connection.execute(
                "INSERT INTO turn_jobs(turn_id, status, attempts) VALUES (?, 'QUEUED', 0)",
                (turn_id,),
            )
            connection.execute(
                "UPDATE threads SET version = version + 1, active_turn_id = ?, updated_at = ? WHERE id = ?",
                (turn_id, now, thread_id),
            )
            event = self.events.append(
                thread_id,
                turn_id,
                "turn.accepted",
                "user",
                {"message_id": message_id, "client_turn_id": client_turn_id, "skill_names": selected_skills},
                connection=connection,
                occurred_at=now,
            )
        return TurnSubmission(thread_id, turn_id, "ACCEPTED", 0, event.seq)


def _turn_from_row(row: Any) -> TurnSnapshot:
    return TurnSnapshot(
        id=row["id"],
        thread_id=row["thread_id"],
        client_turn_id=row["client_turn_id"],
        parent_turn_id=row["parent_turn_id"],
        status=row["status"],
        policy=row["policy"],
        content_shape=row["content_shape"],
        reason_code=row["reason_code"],
        version=row["version"],
        materialized_goal_id=row["materialized_goal_id"],
        materialized_run_id=row["materialized_run_id"],
        direction_action=row["direction_action"],
        direction_idempotency_key=row["direction_idempotency_key"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _message_from_row(row: Any) -> ThreadMessageSnapshot:
    return ThreadMessageSnapshot(
        id=row["id"],
        thread_id=row["thread_id"],
        turn_id=row["turn_id"],
        role=row["role"],
        content=row["content"],
        status=row["status"],
        generation=row["generation"],
        content_length=row["content_length"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
