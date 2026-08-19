from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .ask import (
    AskQuestion,
    AskRequest,
    format_answer_message,
    normalize_answers,
    questions_from_json,
    tool_result_payload,
)
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
        history: list[dict[str, Any]],
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
    skill_names: tuple[str, ...]
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
class AskSnapshot:
    id: str
    turn_id: str
    call_id: str
    questions: tuple[AskQuestion, ...]
    status: str
    continuation_turn_id: str | None
    created_at: str
    answered_at: str | None


@dataclass(frozen=True)
class AskAnswerResult:
    ask_id: str
    turn: TurnSnapshot


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


@dataclass(frozen=True)
class MaterializationResult:
    goal_id: str
    session_id: str
    run_id: str
    created: bool


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
        self.materializer = ExecutionMaterializer(db, agent_runtime, self.events)

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

    def turns(self, thread_id: str) -> list[TurnSnapshot]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM turns WHERE thread_id = ? ORDER BY created_at, id",
                (thread_id,),
            ).fetchall()
        return [_turn_from_row(row) for row in rows]

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

    def cancel_turn(self, turn_id: str) -> TurnSnapshot:
        now = _now()
        active = False
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT turns.*, turn_jobs.status AS job_status FROM turns "
                "JOIN turn_jobs ON turn_jobs.turn_id = turns.id WHERE turns.id = ?",
                (turn_id,),
            ).fetchone()
            if row is None:
                raise KeyError(turn_id)
            if row["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                return _turn_from_row(row)
            connection.execute(
                "UPDATE turn_jobs SET cancel_requested_at = ? WHERE turn_id = ?",
                (now, turn_id),
            )
            self.events.append(
                row["thread_id"], turn_id, "turn.cancel_requested", "user", {},
                connection=connection, occurred_at=now,
            )
            if row["job_status"] == "QUEUED" or row["status"] in {"AWAITING_DIRECTION", "AWAITING_INPUT"}:
                connection.execute(
                    "UPDATE turn_jobs SET status = 'CANCELLED', finished_at = ? WHERE turn_id = ?",
                    (now, turn_id),
                )
                if row["status"] == "AWAITING_INPUT":
                    ask = connection.execute(
                        "SELECT id FROM turn_asks WHERE turn_id = ? AND status = 'PENDING' ORDER BY created_at DESC LIMIT 1",
                        (turn_id,),
                    ).fetchone()
                    if ask is not None:
                        connection.execute(
                            "UPDATE turn_asks SET status = 'CANCELLED', cancelled_at = ? WHERE id = ?",
                            (now, ask["id"]),
                        )
                        self.events.append(
                            row["thread_id"], turn_id, "ask.cancelled", "user",
                            {"ask_id": ask["id"]}, connection=connection, occurred_at=now,
                        )
                connection.execute(
                    "UPDATE turns SET status = 'CANCELLED', version = version + 1, updated_at = ? WHERE id = ?",
                    (now, turn_id),
                )
                self.events.append(
                    row["thread_id"], turn_id, "turn.cancelled", "user", {},
                    connection=connection, occurred_at=now,
                )
            else:
                active = True
        if active:
            event = self._cancel_events.get(turn_id)
            if event is not None:
                event.set()
        return self.turn(turn_id)

    def pending_ask(self, turn_id: str) -> AskSnapshot | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM turn_asks WHERE turn_id = ? AND status = 'PENDING' "
                "ORDER BY created_at DESC LIMIT 1",
                (turn_id,),
            ).fetchone()
        return _ask_from_row(row) if row is not None else None

    def answer_ask(
        self,
        turn_id: str,
        expected_version: int,
        idempotency_key: str,
        answers: Any,
    ) -> AskAnswerResult:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        now = _now()
        continuation_id: str | None = None
        ask_id: str | None = None
        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT id, turn_id, continuation_turn_id FROM turn_asks WHERE answer_idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["turn_id"] != turn_id or not existing["continuation_turn_id"]:
                    raise ValueError("answer idempotency key already used")
                continuation_id = existing["continuation_turn_id"]
                ask_id = existing["id"]
            else:
                turn_row = connection.execute(
                    "SELECT * FROM turns WHERE id = ?", (turn_id,)
                ).fetchone()
                ask_row = connection.execute(
                    "SELECT * FROM turn_asks WHERE turn_id = ? AND status = 'PENDING' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (turn_id,),
                ).fetchone()
                if turn_row is None or ask_row is None:
                    raise ValueError("no pending ask for this turn")
                if turn_row["status"] != "AWAITING_INPUT":
                    raise ValueError("turn is not waiting for an answer")
                if int(turn_row["version"]) != int(expected_version):
                    raise ValueError("turn version conflict")
                questions = questions_from_json(ask_row["questions_json"])
                normalized = normalize_answers(questions, answers)
                ask_id = ask_row["id"]
                continuation_id = f"turn_{uuid.uuid4().hex}"
                client_turn_id = f"ask:{ask_id}:{uuid.uuid4().hex}"
                answer_message_id = f"message_{uuid.uuid4().hex}"
                answer_content = format_answer_message(questions, normalized)
                connection.execute(
                    "INSERT INTO turns(id, thread_id, client_turn_id, parent_turn_id, status, version, skill_names_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'ACCEPTED', 0, ?, ?, ?)",
                    (
                        continuation_id,
                        turn_row["thread_id"],
                        client_turn_id,
                        turn_id,
                        turn_row["skill_names_json"],
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO thread_messages(id, thread_id, turn_id, role, content, status, generation, content_length, created_at) "
                    "VALUES (?, ?, ?, 'user', ?, 'ready', 1, ?, ?)",
                    (answer_message_id, turn_row["thread_id"], continuation_id, answer_content, len(answer_content), now),
                )
                connection.execute(
                    "INSERT INTO turn_jobs(turn_id, status, attempts) VALUES (?, 'QUEUED', 0)",
                    (continuation_id,),
                )
                connection.execute(
                    "UPDATE turn_asks SET status = 'ANSWERED', answer_json = ?, answer_idempotency_key = ?, "
                    "continuation_turn_id = ?, answered_at = ? WHERE id = ?",
                    (json.dumps(normalized, ensure_ascii=False), idempotency_key, continuation_id, now, ask_id),
                )
                connection.execute(
                    "UPDATE turns SET status = 'COMPLETED', version = version + 1, updated_at = ? WHERE id = ?",
                    (now, turn_id),
                )
                connection.execute(
                    "UPDATE threads SET version = version + 1, active_turn_id = ?, updated_at = ? WHERE id = ?",
                    (continuation_id, now, turn_row["thread_id"]),
                )
                self.events.append(
                    turn_row["thread_id"], turn_id, "ask.answered", "user",
                    {"ask_id": ask_id, "continuation_turn_id": continuation_id, "answer_count": len(normalized)},
                    connection=connection, occurred_at=now,
                )
                self.events.append(
                    turn_row["thread_id"], turn_id, "turn.completed", "user", {},
                    connection=connection, occurred_at=now,
                )
                self.events.append(
                    turn_row["thread_id"], continuation_id, "turn.accepted", "user",
                    {"parent_turn_id": turn_id, "ask_id": ask_id, "message_id": answer_message_id},
                    connection=connection, occurred_at=now,
                )
        if continuation_id is None or ask_id is None:
            raise RuntimeError("ask continuation was not created")
        return AskAnswerResult(ask_id, self.turn(continuation_id))

    async def select_direction(
        self,
        turn_id: str,
        action: str,
        expected_version: int,
        idempotency_key: str,
    ) -> TurnSnapshot:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if action == "modify_plan":
            return self._modify_direction(turn_id, expected_version, idempotency_key)
        if action != "continue_execution":
            raise ValueError("unsupported direction action")
        result = self.materializer.materialize(
            turn_id, expected_version, idempotency_key, action
        )
        if result.created and self.agent_runtime is not None:
            content = self._user_content(turn_id)
            await self.agent_runtime.handle_message(result.run_id, content, [])
        return self.turn(turn_id)

    def _modify_direction(
        self,
        turn_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> TurnSnapshot:
        now = _now()
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if row is None:
                raise KeyError(turn_id)
            if row["direction_idempotency_key"] == idempotency_key:
                return _turn_from_row(row)
            _validate_direction(row, expected_version)
            connection.execute(
                "UPDATE turns SET status = 'COMPLETED', direction_action = 'modify_plan', "
                "direction_idempotency_key = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (idempotency_key, now, turn_id),
            )
            self.events.append(
                row["thread_id"], turn_id, "turn.direction_selected", "user",
                {"action": "modify_plan", "idempotency_key": idempotency_key},
                connection=connection, occurred_at=now,
            )
            self.events.append(
                row["thread_id"], turn_id, "turn.completed", "worker", {},
                connection=connection, occurred_at=now,
            )
        return self.turn(turn_id)

    def _user_content(self, turn_id: str) -> str:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT content FROM thread_messages WHERE turn_id = ? AND role = 'user' "
                "ORDER BY created_at, id LIMIT 1",
                (turn_id,),
            ).fetchone()
        if row is None:
            raise KeyError(turn_id)
        return str(row["content"])

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
            if thread["active_turn_id"]:
                active_turn = connection.execute(
                    "SELECT status FROM turns WHERE id = ?", (thread["active_turn_id"],)
                ).fetchone()
                if active_turn is not None and active_turn["status"] == "AWAITING_INPUT":
                    raise ValueError("answer the pending ask before sending another message")
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
                    id, thread_id, client_turn_id, parent_turn_id, status, version, skill_names_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'ACCEPTED', 0, ?, ?, ?)
                """,
                (turn_id, thread_id, client_turn_id, parent_turn_id, json.dumps(selected_skills, ensure_ascii=False), now, now),
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


class ExecutionMaterializer:
    def __init__(self, db: Database, agent_runtime, thread_events: ThreadEventStore) -> None:
        self.db = db
        self.agent_runtime = agent_runtime
        self.thread_events = thread_events

    def materialize(
        self,
        turn_id: str,
        expected_version: int,
        idempotency_key: str,
        action: str,
    ) -> MaterializationResult:
        now = _now()
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if row is None:
                raise KeyError(turn_id)
            if row["direction_idempotency_key"] == idempotency_key and row["materialized_run_id"]:
                return MaterializationResult(
                    row["materialized_goal_id"],
                    self._session_for_run(connection, row["materialized_run_id"]),
                    row["materialized_run_id"],
                    False,
                )
            _validate_direction(row, expected_version)
            if row["policy"] != "propose_execution":
                raise ValueError("execution direction is not available")
            duplicate = connection.execute(
                "SELECT id FROM turns WHERE direction_idempotency_key = ? AND id != ?",
                (idempotency_key, turn_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("direction idempotency key already used")
            user = connection.execute(
                "SELECT content FROM thread_messages WHERE turn_id = ? AND role = 'user' "
                "ORDER BY created_at, id LIMIT 1",
                (turn_id,),
            ).fetchone()
            if user is None:
                raise ValueError("turn user message is missing")
            content = str(user["content"])
            goal_id = f"goal_{uuid.uuid4().hex}"
            session_id = f"session_{uuid.uuid4().hex}"
            run_id = f"run_{uuid.uuid4().hex}"
            title = content.splitlines()[0].strip()[:80] or "已确认的执行任务"
            budget = self.agent_runtime.initial_budget() if self.agent_runtime else {
                "react_iterations_remaining": 5,
                "react_iteration": 0,
                "consecutive_tool_errors": 0,
                "identical_actions": {},
                "applied_memory_versions": [],
            }
            connection.execute(
                "INSERT INTO goals(id, title, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (goal_id, title, content, now, now),
            )
            connection.execute(
                "INSERT INTO sessions(id, goal_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, goal_id, now, now),
            )
            connection.execute(
                "INSERT INTO runs(id, goal_id, session_id, state, budget_json, skill_names_json, source_turn_id, created_at, updated_at) "
                "VALUES (?, ?, ?, 'RECEIVED', ?, ?, ?, ?, ?)",
                (run_id, goal_id, session_id, json.dumps(budget, ensure_ascii=False), row["skill_names_json"], turn_id, now, now),
            )
            connection.execute(
                "UPDATE turns SET status = 'COMPLETED', direction_action = ?, "
                "direction_idempotency_key = ?, materialized_goal_id = ?, materialized_run_id = ?, "
                "version = version + 1, updated_at = ? WHERE id = ?",
                (action, idempotency_key, goal_id, run_id, now, turn_id),
            )
            self.thread_events.append(
                row["thread_id"], turn_id, "turn.direction_selected", "user",
                {"action": action, "idempotency_key": idempotency_key},
                connection=connection, occurred_at=now,
            )
            self.thread_events.append(
                row["thread_id"], turn_id, "execution.materialized", "runtime",
                {"goal_id": goal_id, "run_id": run_id, "source_turn_id": turn_id},
                connection=connection, occurred_at=now,
            )
            self.thread_events.append(
                row["thread_id"], turn_id, "turn.completed", "runtime", {},
                connection=connection, occurred_at=now,
            )
            if self.agent_runtime is not None:
                self.agent_runtime.events.append(
                    run_id,
                    goal_id,
                    "run.created",
                    "runtime",
                    {"source_turn_id": turn_id},
                    connection=connection,
                    occurred_at=now,
                )
        return MaterializationResult(goal_id, session_id, run_id, True)

    @staticmethod
    def _session_for_run(connection: Any, run_id: str) -> str:
        row = connection.execute("SELECT session_id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return str(row["session_id"])


def _validate_direction(row: Any, expected_version: int) -> None:
    if row["status"] != "AWAITING_DIRECTION":
        raise ValueError("execution direction is not available")
    if int(row["version"]) != int(expected_version):
        raise ValueError("turn version conflict")


SAFE_FAILURE_MESSAGE = "当前暂时无法生成可用回答，请重试。"
SAFE_CANCEL_MESSAGE = "已停止生成。"
ASK_PROMPT_MESSAGE = "为了更准确地完成这个目标，请先补充以下信息。"


class ManagedTurnWorker:
    """Durable, single-concurrency owner of conversation Turn Jobs."""

    def __init__(
        self,
        conversation: ConversationService,
        *,
        owner: str | None = None,
        lease_seconds: float = 30.0,
        poll_interval: float = 0.05,
    ) -> None:
        self.conversation = conversation
        self.db = conversation.db
        self.owner = owner or f"turn-worker-{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        self._task: asyncio.Task[Any] | None = None
        self._stop_event: asyncio.Event | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="better-agent-turn-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        task, self._task = self._task, None
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def claim_next(self) -> str | None:
        now = _now()
        lease_until = _after_seconds(self.lease_seconds)
        with self.db.transaction() as connection:
            row = connection.execute(
                """
                SELECT turn_jobs.turn_id, turns.thread_id, turn_jobs.attempts
                FROM turn_jobs JOIN turns ON turns.id = turn_jobs.turn_id
                WHERE turn_jobs.status = 'QUEUED'
                   OR (turn_jobs.status = 'RUNNING' AND turn_jobs.lease_until IS NOT NULL
                       AND turn_jobs.lease_until <= ?)
                ORDER BY turn_jobs.started_at IS NOT NULL, turn_jobs.started_at, turn_jobs.turn_id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            turn_id = row["turn_id"]
            connection.execute(
                """
                UPDATE turn_jobs SET status = 'RUNNING', lease_owner = ?, lease_until = ?,
                    attempts = attempts + 1, started_at = COALESCE(started_at, ?)
                WHERE turn_id = ?
                """,
                (self.owner, lease_until, now, turn_id),
            )
            turn = connection.execute("SELECT status FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if turn["status"] == "ACCEPTED":
                connection.execute(
                    "UPDATE turns SET status = 'ROUTING', version = version + 1, updated_at = ? WHERE id = ?",
                    (now, turn_id),
                )
            self.conversation.events.append(
                row["thread_id"], turn_id, "turn.started", "worker", {"attempt": int(row["attempts"]) + 1},
                connection=connection, occurred_at=now,
            )
        return turn_id

    async def run_once(self) -> bool:
        turn_id = self.claim_next()
        if turn_id is None:
            return False
        await self._process(turn_id)
        return True

    async def _run_loop(self) -> None:
        while self._stop_event is None or not self._stop_event.is_set():
            if not await self.run_once():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval) if self._stop_event else await asyncio.sleep(self.poll_interval)
                except asyncio.TimeoutError:
                    pass

    async def _process(self, turn_id: str) -> None:
        turn = self.conversation.turn(turn_id)
        user_message = self._user_message(turn_id)
        generation = self._prepare_generation(turn)
        cancel_event = asyncio.Event()
        self.conversation._cancel_events[turn_id] = cancel_event
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        def on_delta(delta: str) -> None:
            queue.put_nowait(("delta", delta))

        def on_reset() -> None:
            queue.put_nowait(("reset", None))

        model_task = asyncio.create_task(
            self.conversation.route_model.route_and_respond(
                content=user_message.content,
                history=self._history(turn.thread_id, turn_id),
                skill_names=list(turn.skill_names),
                on_text_delta=on_delta,
                on_text_reset=on_reset,
                cancel_event=cancel_event,
            ),
            name=f"conversation-turn-{turn_id}",
        )
        decoder = ControlHeadDecoder()
        message_id: str | None = None
        pending = ""
        last_flush = asyncio.get_running_loop().time()
        try:
            while not model_task.done() or not queue.empty():
                if self._cancel_requested(turn_id):
                    cancel_event.set()
                    if not model_task.done():
                        model_task.cancel()
                try:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                if kind == "reset":
                    decoder = ControlHeadDecoder()
                    if message_id is not None:
                        self._interrupt_message(turn, message_id, generation, "retry")
                        message_id = None
                        generation += 1
                    pending = ""
                    last_flush = asyncio.get_running_loop().time()
                    continue
                try:
                    body = decoder.feed(value or "")
                except RouteProtocolError:
                    raise
                if decoder.header is not None and message_id is None:
                    message_id = self._start_message(turn, decoder.header, generation)
                if body:
                    pending += body
                now = asyncio.get_running_loop().time()
                if message_id is not None and pending and (len(pending) >= 128 or now - last_flush >= 0.025):
                    self._flush_delta(turn, message_id, generation, pending)
                    pending = ""
                    last_flush = now
            if self._cancel_requested(turn_id) or cancel_event.is_set():
                if not model_task.done():
                    model_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await model_task
                self._finish_cancelled(turn, message_id, generation, pending)
                return
            result = await model_task
            if isinstance(result, AskRequest):
                self._finish_ask(turn, result, generation)
                return
            decoder.finish()
            if message_id is None:
                message_id = self._start_message(turn, decoder.header, generation)
            if pending:
                self._flush_delta(turn, message_id, generation, pending)
            self._finish_success(turn, message_id, generation, decoder.header.policy)
        except asyncio.CancelledError:
            cancel_event.set()
            self._finish_cancelled(turn, message_id, generation, pending)
        except Exception as exc:
            self._finish_failure(turn, message_id, generation, str(exc))
        finally:
            if not model_task.done():
                model_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await model_task
            self.conversation._cancel_events.pop(turn_id, None)

    def _prepare_generation(self, turn: TurnSnapshot) -> int:
        now = _now()
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM thread_messages WHERE turn_id = ? AND role = 'assistant' ORDER BY generation DESC",
                (turn.id,),
            ).fetchall()
            if rows and rows[0]["status"] == "streaming":
                row = rows[0]
                connection.execute(
                    "UPDATE thread_messages SET status = 'interrupted', completed_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                self.conversation.events.append(
                    turn.thread_id, turn.id, "message.completed", "worker",
                    {"message_id": row["id"], "generation": row["generation"], "finish_reason": "interrupted"},
                    connection=connection, occurred_at=now,
                )
            return (int(rows[0]["generation"]) + 1) if rows else 1

    def _start_message(self, turn: TurnSnapshot, decision: RouteDecision, generation: int) -> str:
        message_id = f"message_{uuid.uuid4().hex}"
        now = _now()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE turns SET status = 'STREAMING', policy = ?, content_shape = ?, reason_code = ?, "
                "version = version + 1, updated_at = ? WHERE id = ?",
                (decision.policy, decision.content_shape, decision.reason_code, now, turn.id),
            )
            connection.execute(
                """
                INSERT INTO thread_messages(
                    id, thread_id, turn_id, role, content, status, generation, content_length, created_at
                ) VALUES (?, ?, ?, 'assistant', '', 'streaming', ?, 0, ?)
                """,
                (message_id, turn.thread_id, turn.id, generation, now),
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, "turn.policy_decided", "model",
                {"policy": decision.policy, "content_shape": decision.content_shape, "reason_code": decision.reason_code},
                connection=connection, occurred_at=now,
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, "message.started", "model",
                {"message_id": message_id, "generation": generation},
                connection=connection, occurred_at=now,
            )
        return message_id

    def _flush_delta(self, turn: TurnSnapshot, message_id: str, generation: int, delta: str) -> None:
        if not delta:
            return
        now = _now()
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT content_length FROM thread_messages WHERE id = ? AND thread_id = ?",
                (message_id, turn.thread_id),
            ).fetchone()
            if row is None:
                return
            offset = int(row["content_length"])
            length = offset + len(delta)
            connection.execute(
                "UPDATE thread_messages SET content = content || ?, content_length = ? WHERE id = ?",
                (delta, length, message_id),
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, "message.delta", "model",
                {"message_id": message_id, "generation": generation, "offset": offset, "delta": delta},
                connection=connection, occurred_at=now,
            )

    def _finish_ask(self, turn: TurnSnapshot, request: AskRequest, generation: int) -> None:
        now = _now()
        ask_id = f"ask_{uuid.uuid4().hex}"
        message_id = f"message_{uuid.uuid4().hex}"
        questions = [question.as_dict() for question in request.questions]
        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM turn_asks WHERE call_id = ?", (request.call_id,)
            ).fetchone()
            if existing is not None:
                connection.execute(
                    "UPDATE turn_jobs SET status = 'COMPLETED', lease_owner = NULL, lease_until = NULL, finished_at = ? "
                    "WHERE turn_id = ?",
                    (now, turn.id),
                )
                return
            connection.execute(
                "INSERT INTO turn_asks(id, turn_id, call_id, questions_json, status, created_at) "
                "VALUES (?, ?, ?, ?, 'PENDING', ?)",
                (ask_id, turn.id, request.call_id, json.dumps(questions, ensure_ascii=False), now),
            )
            connection.execute(
                "INSERT INTO thread_messages(id, thread_id, turn_id, role, content, status, generation, content_length, created_at, completed_at) "
                "VALUES (?, ?, ?, 'assistant', ?, 'ready', ?, ?, ?, ?)",
                (message_id, turn.thread_id, turn.id, ASK_PROMPT_MESSAGE, generation, len(ASK_PROMPT_MESSAGE), now, now),
            )
            connection.execute(
                "UPDATE turns SET status = 'AWAITING_INPUT', policy = 'ask', content_shape = 'ask', "
                "reason_code = 'model_requested_input', version = version + 1, updated_at = ? WHERE id = ?",
                (now, turn.id),
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, "turn.policy_decided", "model",
                {"policy": "ask", "content_shape": "ask", "reason_code": "model_requested_input"},
                connection=connection, occurred_at=now,
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, "message.started", "model",
                {"message_id": message_id, "generation": generation},
                connection=connection, occurred_at=now,
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, "message.completed", "model",
                {
                    "message_id": message_id,
                    "generation": generation,
                    "finish_reason": "ask",
                    "content_length": len(ASK_PROMPT_MESSAGE),
                },
                connection=connection, occurred_at=now,
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, "ask.requested", "model",
                {"ask_id": ask_id, "questions": questions},
                connection=connection, occurred_at=now,
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, "turn.awaiting_input", "worker",
                {"ask_id": ask_id}, connection=connection, occurred_at=now,
            )
            connection.execute(
                "UPDATE turn_jobs SET status = 'COMPLETED', lease_owner = NULL, lease_until = NULL, finished_at = ? WHERE turn_id = ?",
                (now, turn.id),
            )

    def _finish_success(self, turn: TurnSnapshot, message_id: str, generation: int, policy: str) -> None:
        now = _now()
        terminal = "AWAITING_DIRECTION" if policy == "propose_execution" else "COMPLETED"
        event_type = "turn.awaiting_direction" if terminal == "AWAITING_DIRECTION" else "turn.completed"
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT content, content_length FROM thread_messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            connection.execute(
                "UPDATE thread_messages SET status = 'ready', completed_at = ? WHERE id = ?",
                (now, message_id),
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, "message.completed", "model",
                {"message_id": message_id, "generation": generation, "finish_reason": "stop", "content_length": row["content_length"]},
                connection=connection, occurred_at=now,
            )
            connection.execute(
                "UPDATE turns SET status = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (terminal, now, turn.id),
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, event_type, "worker", {},
                connection=connection, occurred_at=now,
            )
            connection.execute(
                "UPDATE turn_jobs SET status = 'COMPLETED', lease_owner = NULL, lease_until = NULL, finished_at = ? WHERE turn_id = ?",
                (now, turn.id),
            )

    def _finish_cancelled(self, turn: TurnSnapshot, message_id: str | None, generation: int, pending: str) -> None:
        if message_id is not None and pending:
            self._flush_delta(turn, message_id, generation, pending)
        now = _now()
        with self.db.transaction() as connection:
            if message_id is None:
                message_id = f"message_{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO thread_messages(id, thread_id, turn_id, role, content, status, generation, content_length, created_at) "
                    "VALUES (?, ?, ?, 'assistant', ?, 'cancelled', ?, ?, ?)",
                    (message_id, turn.thread_id, turn.id, SAFE_CANCEL_MESSAGE, generation, len(SAFE_CANCEL_MESSAGE), now),
                )
                self.conversation.events.append(
                    turn.thread_id, turn.id, "message.started", "worker",
                    {"message_id": message_id, "generation": generation}, connection=connection, occurred_at=now,
                )
            connection.execute(
                "UPDATE thread_messages SET status = 'cancelled', completed_at = ? WHERE id = ?",
                (now, message_id),
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, "message.completed", "worker",
                {"message_id": message_id, "generation": generation, "finish_reason": "cancelled"},
                connection=connection, occurred_at=now,
            )
            connection.execute(
                "UPDATE turns SET status = 'CANCELLED', version = version + 1, updated_at = ? WHERE id = ?",
                (now, turn.id),
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, "turn.cancelled", "worker", {},
                connection=connection, occurred_at=now,
            )
            connection.execute(
                "UPDATE turn_jobs SET status = 'CANCELLED', lease_owner = NULL, lease_until = NULL, finished_at = ? WHERE turn_id = ?",
                (now, turn.id),
            )

    def _finish_failure(self, turn: TurnSnapshot, message_id: str | None, generation: int, error: str) -> None:
        now = _now()
        with self.db.transaction() as connection:
            readable = None
            if message_id is not None:
                readable = connection.execute(
                    "SELECT id, content, content_length, generation FROM thread_messages "
                    "WHERE id = ? AND thread_id = ? AND role = 'assistant' AND content_length > 0",
                    (message_id, turn.thread_id),
                ).fetchone()
            if readable is None:
                readable = connection.execute(
                    "SELECT id, content, content_length, generation FROM thread_messages "
                    "WHERE turn_id = ? AND role = 'assistant' AND status = 'interrupted' AND content_length > 0 "
                    "ORDER BY generation DESC, created_at DESC LIMIT 1",
                    (turn.id,),
                ).fetchone()

            if readable is not None:
                message_id = readable["id"]
                readable_generation = int(readable["generation"])
                connection.execute(
                    "UPDATE thread_messages SET status = 'ready', completed_at = ? WHERE id = ?",
                    (now, message_id),
                )
                self.conversation.events.append(
                    turn.thread_id, turn.id, "message.snapshot", "worker",
                    {
                        "message_id": message_id,
                        "generation": readable_generation,
                        "content": readable["content"],
                        "status": "ready",
                    },
                    connection=connection, occurred_at=now,
                )
                self.conversation.events.append(
                    turn.thread_id, turn.id, "message.completed", "worker",
                    {
                        "message_id": message_id,
                        "generation": readable_generation,
                        "finish_reason": "error",
                        "content_length": readable["content_length"],
                    },
                    connection=connection, occurred_at=now,
                )
            else:
                if message_id is None:
                    message_id = f"message_{uuid.uuid4().hex}"
                    connection.execute(
                        "INSERT INTO thread_messages(id, thread_id, turn_id, role, content, status, generation, content_length, created_at) "
                        "VALUES (?, ?, ?, 'assistant', ?, 'ready', ?, ?, ?)",
                        (message_id, turn.thread_id, turn.id, SAFE_FAILURE_MESSAGE, generation, len(SAFE_FAILURE_MESSAGE), now),
                    )
                    self.conversation.events.append(
                        turn.thread_id, turn.id, "message.started", "worker",
                        {"message_id": message_id, "generation": generation}, connection=connection, occurred_at=now,
                    )
                connection.execute(
                    "UPDATE thread_messages SET content = ?, content_length = ?, status = 'ready', completed_at = ? WHERE id = ?",
                    (SAFE_FAILURE_MESSAGE, len(SAFE_FAILURE_MESSAGE), now, message_id),
                )
                self.conversation.events.append(
                    turn.thread_id, turn.id, "message.snapshot", "worker",
                    {
                        "message_id": message_id,
                        "generation": generation,
                        "content": SAFE_FAILURE_MESSAGE,
                        "status": "ready",
                    },
                    connection=connection, occurred_at=now,
                )
                self.conversation.events.append(
                    turn.thread_id, turn.id, "message.completed", "worker",
                    {"message_id": message_id, "generation": generation, "finish_reason": "error"},
                    connection=connection, occurred_at=now,
                )
            connection.execute(
                "UPDATE turns SET status = 'FAILED', version = version + 1, updated_at = ? WHERE id = ?",
                (now, turn.id),
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, "turn.failed", "worker", {"reason": "conversation generation failed"},
                connection=connection, occurred_at=now,
            )
            connection.execute(
                "UPDATE turn_jobs SET status = 'FAILED', last_error_json = ?, lease_owner = NULL, lease_until = NULL, finished_at = ? WHERE turn_id = ?",
                (json.dumps({"error": error[:240]}, ensure_ascii=False), now, turn.id),
            )

    def _interrupt_message(self, turn: TurnSnapshot, message_id: str, generation: int, reason: str) -> None:
        now = _now()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE thread_messages SET status = 'interrupted', completed_at = ? WHERE id = ?",
                (now, message_id),
            )
            self.conversation.events.append(
                turn.thread_id, turn.id, "message.completed", "worker",
                {"message_id": message_id, "generation": generation, "finish_reason": reason},
                connection=connection, occurred_at=now,
            )

    def _user_message(self, turn_id: str) -> ThreadMessageSnapshot:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM thread_messages WHERE turn_id = ? AND role = 'user' ORDER BY created_at, id LIMIT 1",
                (turn_id,),
            ).fetchone()
        if row is None:
            raise KeyError(turn_id)
        return _message_from_row(row)

    def _history(self, thread_id: str, turn_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT id, turn_id, role, content FROM thread_messages WHERE thread_id = ? AND turn_id != ? ORDER BY created_at, id",
                (thread_id, turn_id),
            ).fetchall()
            ask_rows = connection.execute(
                "SELECT * FROM turn_asks WHERE status = 'ANSWERED' AND turn_id != ? ORDER BY created_at, id",
                (turn_id,),
            ).fetchall()
        asks_by_turn: dict[str, list[Any]] = {}
        for ask in ask_rows:
            asks_by_turn.setdefault(ask["turn_id"], []).append(ask)
        history: list[dict[str, Any]] = []
        for row in rows:
            asks = asks_by_turn.get(row["turn_id"], []) if row["role"] == "assistant" else []
            if not asks:
                history.append({"role": row["role"], "content": row["content"]})
                continue
            questions = questions_from_json(asks[0]["questions_json"])
            answers = json.loads(asks[0]["answer_json"] or "[]")
            history.append({
                "role": "assistant",
                "content": row["content"],
                "tool_calls": [{
                    "id": asks[0]["call_id"],
                    "type": "function",
                    "function": {
                        "name": "ask_user",
                        "arguments": json.dumps({"questions": [question.as_dict() for question in questions]}, ensure_ascii=False),
                    },
                }],
            })
            history.append({
                "role": "tool",
                "tool_call_id": asks[0]["call_id"],
                "content": json.dumps(tool_result_payload(questions, answers), ensure_ascii=False),
            })
        return history

    def _cancel_requested(self, turn_id: str) -> bool:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT cancel_requested_at FROM turn_jobs WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        return bool(row and row["cancel_requested_at"])


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
        skill_names=tuple(json.loads(row["skill_names_json"] or "[]")),
        materialized_goal_id=row["materialized_goal_id"],
        materialized_run_id=row["materialized_run_id"],
        direction_action=row["direction_action"],
        direction_idempotency_key=row["direction_idempotency_key"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _ask_from_row(row: Any) -> AskSnapshot:
    return AskSnapshot(
        id=row["id"],
        turn_id=row["turn_id"],
        call_id=row["call_id"],
        questions=questions_from_json(row["questions_json"]),
        status=row["status"],
        continuation_turn_id=row["continuation_turn_id"],
        created_at=row["created_at"],
        answered_at=row["answered_at"],
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


def _after_seconds(seconds: float) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
