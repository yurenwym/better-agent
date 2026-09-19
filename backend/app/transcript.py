from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

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

    def project_context(
        self,
        transcript: CanonicalTranscript,
        *,
        limit_bytes: int,
    ) -> CanonicalTranscript:
        """Replace oversized tool results with a bounded projection.

        This is the *context* view only. The canonical transcript keeps the
        original result, so the archiver still summarises what really happened;
        only what the model reads is projected. The projection is mechanical -
        see :mod:`app.tool_projection` - so a tool result is never replaced by an
        unverified paraphrase.
        """
        from .tool_projection import project_result_content

        turns: list[TranscriptTurn] = []
        for turn in transcript.turns:
            events: list[TranscriptEvent] = []
            for event in turn.events:
                if (
                    event.event_type == "tool_result"
                    and len(event.content.encode("utf-8")) > limit_bytes
                ):
                    reference = {
                        "tool": "ask_user",
                        "call_id": event.call_id,
                        "turn_id": event.turn_id,
                    }
                    events.append(replace(event, content=project_result_content(
                        event.content, limit_bytes=limit_bytes, reference=reference,
                    )))
                else:
                    events.append(event)
            turns.append(replace(turn, events=tuple(events)))
        manifest = transcript.source_manifest
        return CanonicalTranscript(
            transcript.scope, tuple(turns),
            _source_hash(transcript.scope, turns, manifest), manifest,
        )

    def measure(
        self,
        transcript: CanonicalTranscript,
        *,
        counter: TokenCounter = DEFAULT_TOKEN_COUNTER,
    ) -> int:
        """Conservative size of a transcript in the units ``pack_recent`` uses."""
        return sum(
            counter.count_text(json.dumps(turn.canonical(), ensure_ascii=False, sort_keys=True))
            for turn in transcript.turns
        )

    def pack_recent(
        self,
        transcript: CanonicalTranscript,
        token_budget: int,
        *,
        min_turns: int = 0,
        recent_budget: int | None = None,
        counter: TokenCounter = DEFAULT_TOKEN_COUNTER,
        on_degraded: Callable[[str], None] | None = None,
    ) -> CanonicalTranscript:
        """Keep a contiguous newest-Turn suffix as the hot window.

        Two constraints apply to the *soft-protected* recent window, and they do
        different jobs:

        * ``min_turns`` (``N``) is a **floor on the count** - the newest few
          turns are a property of the window, not a best effort. It is why how
          many turns survive stops depending on how large one turn happens to be.
        * ``recent_budget`` (``R``) is a **ceiling on the bytes** the recent window
        may occupy. Without it a long recent conversation fills the whole budget
        and crowds out the summary, which is the channel that carries the older
        facts. It bounds both the protected suffix and the expansion into older
        turns - bounding only the floor would leave the expansion free to refill
        the window and defeat the reservation.

        When the protected suffix exceeds ``R``, protection is released from the
        **oldest** recent turn first, so the newest turns keep their guarantee.
        Protection is never released below one turn, and the release never skips
        a newer turn to backfill an older smaller one: the suffix stays
        contiguous.

        Two things are deliberately *not* done:

        * If the whole transcript fits ``token_budget``, nothing is trimmed for
          ``R`` - the window cannot crowd out a summary that also fits, so
          trimming would be a pure loss of original text.
        * If a single Turn alone exceeds the *hard cap* (``token_budget``), that
          is reported through ``on_degraded`` rather than silently absorbed. The
          hard cap, not ``R``, decides this: a huge newest turn is not dropped
          merely for exceeding the window share.
        """
        ordered = list(transcript.turns)
        total = len(ordered)
        if total == 0:
            return transcript
        costs = [
            counter.count_text(json.dumps(turn.canonical(), ensure_ascii=False, sort_keys=True))
            for turn in ordered
        ]

        # Everything fits the hard cap: keep the original text and do not let R
        # trim it.
        if sum(costs) <= token_budget:
            return transcript

        required = min(max(int(min_turns), 0), total)
        window_budget = token_budget if recent_budget is None else max(int(recent_budget), 0)

        # R2-02: release soft protection from the oldest recent turn until the
        # protected suffix fits R, never below one turn.
        if recent_budget is not None and required > 0:
            protected = sum(costs[total - required:])
            released = 0
            while required > 1 and protected > window_budget:
                required -= 1
                released += 1
                protected = sum(costs[total - required:])
            if released and on_degraded is not None:
                on_degraded(
                    f"recent window exceeded recent_budget={window_budget}; released soft "
                    f"protection from {released} oldest of the newest turns "
                    f"(min_turns floor now {required})"
                )

        # The newest `required` complete Turns are carried unconditionally, even
        # when together they exceed the budget: the floor is a property of the
        # window, not a best effort. The hard cap is enforced later, by the
        # final request validation, which sees the whole request.
        kept = required
        used = sum(costs[total - required:]) if required else 0
        guaranteed = costs[total - required:] if required else []

        if guaranteed and max(guaranteed) > token_budget:
            # Degradation: a single Turn alone already exceeds the whole budget,
            # so no contiguous window can hold it. Keep the newest suffix that
            # does fit, never reach back past the boundary, and report it.
            kept = 0
            used = 0
            for index in range(total - 1, -1, -1):
                if used + costs[index] <= token_budget:
                    kept += 1
                    used += costs[index]
                else:
                    break
            if on_degraded is not None:
                on_degraded(
                    f"a single complete turn exceeds budget={token_budget}; "
                    f"kept {kept} of {total} complete turns (min_turns={required})"
                )
        else:
            for index in range(total - required - 1, -1, -1):
                cost = costs[index]
                if used + cost <= window_budget:
                    kept += 1
                    used += cost
                else:
                    break

        selected = ordered[total - kept:] if kept else []
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
