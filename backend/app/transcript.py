from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from .ask import questions_from_json, tool_result_payload
from .token_budget import DEFAULT_TOKEN_COUNTER, TokenCounter


@dataclass(frozen=True)
class ResolvedMemoryScope:
    owner_id: str
    thread_id: str
    project_id: str | None


@dataclass(frozen=True)
class TranscriptEvent:
    event_type: str
    turn_id: str
    message_id: str | None
    call_id: str | None
    role: str
    content: str
    sequence: int
    generation: int | None = None

    def canonical(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "turn_id": self.turn_id,
            "message_id": self.message_id,
            "call_id": self.call_id,
            "role": self.role,
            "content": self.content,
            "sequence": self.sequence,
            "generation": self.generation,
        }


@dataclass(frozen=True)
class TranscriptTurn:
    turn_id: str
    outcome: str
    start_sequence: int
    end_sequence: int
    events: tuple[TranscriptEvent, ...]

    def canonical(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "outcome": self.outcome.lower(),
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "events": [event.canonical() for event in self.events],
        }


@dataclass(frozen=True)
class CanonicalTranscript:
    scope: ResolvedMemoryScope
    turns: tuple[TranscriptTurn, ...]
    source_hash: str
    source_manifest: tuple[dict[str, Any], ...] = ()

    @property
    def start_sequence(self) -> int | None:
        return self.turns[0].start_sequence if self.turns else None

    @property
    def end_sequence(self) -> int | None:
        return self.turns[-1].end_sequence if self.turns else None

    @property
    def message_ids(self) -> tuple[str, ...]:
        # Tool-call events intentionally point back to the assistant message
        # that emitted them.  Episode source references are a set-like ordered
        # list, so expose each underlying message exactly once.
        seen: set[str] = set()
        result: list[str] = []
        for turn in self.turns:
            for event in turn.events:
                if event.message_id is not None and event.message_id not in seen:
                    seen.add(event.message_id)
                    result.append(event.message_id)
        return tuple(result)


class ScopeMismatch(PermissionError):
    pass


class CanonicalTurnTranscriptBuilder:
    def __init__(self, db) -> None:
        self.db = db

    def resolve_scope(self, thread_id: str, expected_owner_id: str | None = None) -> ResolvedMemoryScope:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT owner_id,project_id FROM threads WHERE id=? AND deleted_at IS NULL",
                (thread_id,),
            ).fetchone()
        if row is None:
            raise KeyError(thread_id)
        if expected_owner_id is not None and row["owner_id"] != expected_owner_id:
            raise ScopeMismatch("thread does not belong to the requested owner")
        return ResolvedMemoryScope(row["owner_id"], thread_id, row["project_id"])

    def build(
        self,
        thread_id: str,
        *,
        expected_owner_id: str | None = None,
        exclude_turn_id: str | None = None,
        after_sequence: int = 0,
        through_sequence: int | None = None,
    ) -> CanonicalTranscript:
        scope = self.resolve_scope(thread_id, expected_owner_id)
        with self.db.connection() as connection:
            args: list[Any] = [thread_id, after_sequence]
            sequence_clause = ""
            if through_sequence is not None:
                sequence_clause = " AND m.message_seq<=?"
                args.append(through_sequence)
            exclude_clause = ""
            if exclude_turn_id is not None:
                exclude_clause = " AND t.id<>?"
                args.append(exclude_turn_id)
            rows = connection.execute(
                "SELECT m.id,m.turn_id,m.role,m.content,m.status,m.generation,m.message_seq,"
                "t.status AS turn_status FROM thread_messages m JOIN turns t ON t.id=m.turn_id AND t.thread_id=m.thread_id "
                "WHERE m.thread_id=? AND m.message_seq>?" + sequence_clause + exclude_clause +
                "ORDER BY m.message_seq,m.created_at,m.id",
                args,
            ).fetchall()
            ask_rows = connection.execute(
                "SELECT a.* FROM turn_asks a JOIN turns t ON t.id=a.turn_id "
                "WHERE t.thread_id=? AND a.status='ANSWERED' "
                + ("AND a.turn_id<>? " if exclude_turn_id is not None else "")
                + "ORDER BY a.created_at,a.id",
                (thread_id, exclude_turn_id) if exclude_turn_id is not None else (thread_id,),
            ).fetchall()

        terminal = {"COMPLETED", "FAILED", "CANCELLED"}
        eligible_rows = []
        for row in rows:
            if row["turn_status"] not in terminal:
                break
            eligible_rows.append(row)
        rows = eligible_rows

        asks_by_turn: dict[str, list[Any]] = {}
        for ask in ask_rows:
            asks_by_turn.setdefault(ask["turn_id"], []).append(ask)
        grouped: dict[str, list[Any]] = {}
        order: list[str] = []
        for row in rows:
            if row["turn_id"] not in grouped:
                grouped[row["turn_id"]] = []
                order.append(row["turn_id"])
            grouped[row["turn_id"]].append(row)

        turns: list[TranscriptTurn] = []
        for turn_id in order:
            messages = grouped[turn_id]
            outcome = messages[0]["turn_status"]
            ready_users = [row for row in messages if row["role"] == "user" and row["status"] == "ready"]
            ready_assistants = [row for row in messages if row["role"] == "assistant" and row["status"] == "ready"]
            final_assistant = max(ready_assistants, key=lambda row: (int(row["generation"]), int(row["message_seq"]), row["id"])) if ready_assistants else None
            visible = [*ready_users]
            if outcome == "COMPLETED" and final_assistant is not None:
                visible.append(final_assistant)
            visible.sort(key=lambda row: (int(row["message_seq"]), row["id"]))
            if not visible:
                continue
            events: list[TranscriptEvent] = [
                TranscriptEvent(
                    "message", turn_id, row["id"], None, row["role"], row["content"],
                    int(row["message_seq"]), int(row["generation"]),
                )
                for row in visible
            ]
            if final_assistant is not None and outcome == "COMPLETED":
                sequence = int(final_assistant["message_seq"])
                for ask in asks_by_turn.get(turn_id, []):
                    questions = questions_from_json(ask["questions_json"])
                    answers = json.loads(ask["answer_json"] or "[]")
                    events.append(TranscriptEvent(
                        "tool_call", turn_id, final_assistant["id"], ask["call_id"], "assistant",
                        json.dumps({"questions": [question.as_dict() for question in questions]}, ensure_ascii=False, sort_keys=True),
                        sequence, int(final_assistant["generation"]),
                    ))
                    events.append(TranscriptEvent(
                        "tool_result", turn_id, None, ask["call_id"], "tool",
                        json.dumps(tool_result_payload(questions, answers), ensure_ascii=False, sort_keys=True),
                        sequence, None,
                    ))
            events.sort(key=lambda event: (event.sequence, _event_order(event.event_type), event.call_id or ""))
            turns.append(TranscriptTurn(
                turn_id, outcome, min(event.sequence for event in events),
                max(event.sequence for event in events), tuple(events),
            ))
        manifest = tuple({
            "message_id": row["id"], "turn_id": row["turn_id"], "role": row["role"],
            "status": row["status"], "generation": int(row["generation"]),
            "message_seq": int(row["message_seq"]), "turn_status": row["turn_status"],
            "content_hash": "sha256:" + hashlib.sha256(row["content"].encode("utf-8")).hexdigest(),
        } for row in rows)
        # Coverage includes excluded terminal rows even though their content is
        # never rendered or sent to the summarizer.
        raw_ranges = {
            turn_id: (
                min(int(row["message_seq"]) for row in grouped[turn_id]),
                max(int(row["message_seq"]) for row in grouped[turn_id]),
            )
            for turn_id in grouped
        }
        turns = [
            TranscriptTurn(turn.turn_id, turn.outcome, raw_ranges[turn.turn_id][0], raw_ranges[turn.turn_id][1], turn.events)
            for turn in turns
        ]
        digest = _source_hash(scope, turns, manifest)
        return CanonicalTranscript(scope, tuple(turns), digest, manifest)

    def pack_recent(
        self,
        transcript: CanonicalTranscript,
        token_budget: int,
        *,
        counter: TokenCounter = DEFAULT_TOKEN_COUNTER,
    ) -> CanonicalTranscript:
        selected: list[TranscriptTurn] = []
        used = 0
        for turn in reversed(transcript.turns):
            cost = counter.count_text(json.dumps(turn.canonical(), ensure_ascii=False, sort_keys=True))
            if used + cost <= token_budget:
                selected.append(turn)
                used += cost
            else:
                # The hot window and archive coverage are contiguous. Once the
                # next newest complete Turn cannot fit, older Turns must not be
                # pulled in around that boundary.
                break
        selected.reverse()
        selected_ids = {turn.turn_id for turn in selected}
        manifest = tuple(row for row in transcript.source_manifest if row["turn_id"] in selected_ids)
        return CanonicalTranscript(
            transcript.scope, tuple(selected), _source_hash(transcript.scope, selected, manifest), manifest,
        )

    @staticmethod
    def render_history(transcript: CanonicalTranscript) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for turn in transcript.turns:
            for event in turn.events:
                if event.event_type == "message":
                    history.append({"role": event.role, "content": event.content})
                elif event.event_type == "tool_call":
                    tool_call = {
                        "id": event.call_id,
                        "type": "function",
                        "function": {"name": "ask_user", "arguments": event.content},
                    }
                    if history and history[-1].get("role") == "assistant":
                        history[-1].setdefault("tool_calls", []).append(tool_call)
                    else:
                        history.append({"role": "assistant", "content": "", "tool_calls": [tool_call]})
                elif event.event_type == "tool_result":
                    history.append({"role": "tool", "tool_call_id": event.call_id, "content": event.content})
        return history


def _event_order(event_type: str) -> int:
    return {"message": 0, "tool_call": 1, "tool_result": 2, "turn_outcome": 3}.get(event_type, 9)


def _source_hash(
    scope: ResolvedMemoryScope,
    turns: Iterable[TranscriptTurn],
    source_manifest: Iterable[dict[str, Any]] = (),
) -> str:
    payload = {
        "owner_id": scope.owner_id,
        "thread_id": scope.thread_id,
        "project_id": scope.project_id,
        "turns": [turn.canonical() for turn in turns],
        "source_manifest": list(source_manifest),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
