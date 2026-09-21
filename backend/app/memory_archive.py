from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .token_budget import DEFAULT_TOKEN_COUNTER, StaticArchivePolicy, static_archive_policy
from .transcript import CanonicalTranscript, CanonicalTurnTranscriptBuilder


PROMPT_VERSION = "episode-v4"
# The merged Episode budget is measured in UTF-8 bytes by
# `Utf8UpperBoundTokenCounter`, and the field names plus every
# `source_message_ids` entry count towards it. It is deliberately small; see
# DECISION_OUTCOME_RESERVE_RATIO for how decisions/outcomes are protected.
MAX_SUMMARY_OUTPUT_TOKENS = 1_600
# Fraction of the output budget held back from `synopsis` so a long multi-chunk
# archive cannot drain the whole Episode on synopses and lose every decision
# and outcome. Without this, `_merge_chunk_summaries` starved them completely.
DECISION_OUTCOME_RESERVE_RATIO = 0.3
ALLOWED_FIELDS = {"synopsis", "topics", "decisions", "outcomes", "open_loops", "sensitivity"}
STRUCTURED_FIELDS = ("synopsis", "topics", "decisions", "outcomes", "open_loops")
SENSITIVITY = {"normal": 0, "sensitive": 1, "restricted": 2}
SECRET_RE = re.compile(r"(?i)(api[_ -]?key|password|passwd|token|secret|private[_ -]?key)\s*[:=]\s*([^\s,;]+)")


class ArchiveError(RuntimeError):
    code = "archive_error"


class ArchiveLeaseLost(ArchiveError):
    code = "lease_lost"


class InvalidEpisodeSummary(ArchiveError):
    code = "invalid_summary"


class ArchiveInputTooLarge(ArchiveError):
    """The immutable transcript batch cannot fit the summarizer input budget."""

    code = "oversized_input"


class ArchiveSourceChanged(ArchiveError):
    """The immutable transcript no longer matches the queued archive job."""

    code = "source_changed"


class ArchiveUnavailable(ArchiveError):
    code = "archive_unavailable"
    public_message = "历史归档尚未恢复；当前请求需要完整上下文，请先重试归档。"


class ArchiveBindingMissing(ArchiveError):
    code = "runtime_binding_missing"


class ArchiveWaitTimeout(ArchiveUnavailable):
    """The bounded foreground wait for an archival pass ran out.

    Deliberately a distinct code from ``archive_unavailable``: "we waited and
    gave up" and "archival is broken" need different operator responses, and the
    user must be told which one happened rather than being answered with
    silently missing history.
    """

    code = "archive_wait_timeout"
    public_message = "历史整理超时；你的输入已保存，稍后可以直接继续，不需要重新输入。"


# R2-04: the foreground wait is a *UX* budget, not a model-capacity one. It does
# not scale with ``H``, so unlike every knob in ``token_budget`` it is not
# profile policy -- it is an operational limit read from the environment, the
# same family as ``AGENT_ARCHIVE_MAX_JOBS_PER_MINUTE``.
DEFAULT_ARCHIVE_WAIT_MS = 2_000
DEFAULT_ARCHIVE_WAIT_POLL_MS = 50


@dataclass(frozen=True)
class ArchiveWaitPolicy:
    deadline_ms: int
    poll_ms: int

    def public_view(self) -> dict[str, Any]:
        return {"deadline_ms": self.deadline_ms, "poll_ms": self.poll_ms}


class WaitDeadline:
    """A wall-clock bound for the foreground archival wait.

    The loop must never sleep past the deadline, and it must notice a cancel
    promptly rather than after one long sleep. Both are properties of the sleep
    slice, so they are computed here instead of being spread through the loop.
    """

    def __init__(self, policy: ArchiveWaitPolicy, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.policy = policy
        self._clock = clock
        self._started = clock()

    @property
    def elapsed_ms(self) -> int:
        return max(int((self._clock() - self._started) * 1000), 0)

    @property
    def remaining_ms(self) -> int:
        return max(self.policy.deadline_ms - self.elapsed_ms, 0)

    @property
    def expired(self) -> bool:
        return self.remaining_ms <= 0

    def sleep_seconds(self) -> float:
        """One slice: never longer than the poll interval or what is left."""
        return max(min(self.policy.poll_ms, self.remaining_ms), 0) / 1000.0


@dataclass(frozen=True)
class ArchiveClaim:
    job_id: str
    owner_id: str
    thread_id: str
    start_sequence: int
    end_sequence: int
    source_hash: str
    lease_owner: str
    lease_epoch: int
    attempts: int
    prompt_version: str = PROMPT_VERSION
    tokenizer_version: str = DEFAULT_TOKEN_COUNTER.version
    runtime_bundle_id: str | None = None
    root_budget_id: str | None = None


Summarizer = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | str]]


class ConversationArchiver:
    def __init__(
        self, db, store, summarizer: Summarizer | None = None, *, keep_messages: int | None = None,
        keep_tokens: int | None = 12_000, max_attempts: int = 3, lease_seconds: int = 30,
        worker_id: str | None = None, max_summary_tokens: int = 12_000,
    ) -> None:
        self.db = db
        self.store = store
        self.summarizer = summarizer
        self.keep_messages = keep_messages or 0
        # An explicit legacy message count opts into compatibility behavior;
        # production defaults always use a token budget.
        self.keep_tokens = None if keep_messages is not None else keep_tokens
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        if not isinstance(max_summary_tokens, int) or isinstance(max_summary_tokens, bool) or max_summary_tokens <= 0:
            raise ValueError("max_summary_tokens must be a positive integer")
        self.max_summary_tokens = max_summary_tokens
        self.worker_id = worker_id or f"archiver-{uuid.uuid4().hex}"
        self.transcripts = CanonicalTurnTranscriptBuilder(db)

    async def archive_thread(self, thread_id: str, owner_id: str | None = None):
        """Compatibility entry point: enqueue, claim and process one batch."""
        scope = self.transcripts.resolve_scope(thread_id, owner_id)
        job_id = self.enqueue(thread_id, scope.owner_id)
        if job_id is None:
            return None
        claim = self.claim(job_id)
        if claim is None:
            return None
        return await self.process(claim)

    # --- R2-03: the static early-archival line -----------------------------
    #
    # A background pass must not be started per terminal turn. Without a line,
    # every completed turn grows the transcript, produces a new
    # (start,end,source_hash) triple, and therefore a *new* archive job and a new
    # model call -- a queue storm that scales with conversation activity. The
    # line makes a pass conditional on the unarchived prefix actually having
    # grown past the profile's own budget, and the target bounds how far one
    # pass goes so the next pass is far away.

    def static_policy_and_version_for_turn(
        self, thread_id: str, turn_id: str,
    ) -> tuple[StaticArchivePolicy | None, str | None]:
        """The static line for the profile this turn will actually answer with.

        The profile is the one the turn *routed to*, resolved through the same
        store the send path uses, so the line is derived from the effective
        frozen profile rather than from whatever the archiver happens to be
        configured with. The second element is the frozen profile version id,
        recorded on the job so a pass can be audited against the budget it was
        created under.

        ``(None, None)`` means no profile is reachable (a test double or an
        unrouted gateway), in which case there is no defensible line to evaluate
        and the caller must not invent one.
        """
        gateway = getattr(self.summarizer, "gateway", None)
        store = getattr(gateway, "control_store", None)
        if store is None:
            return None, None
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT th.owner_id,t.runtime_bundle_id,t.root_budget_id FROM turns t "
                "JOIN threads th ON th.id=t.thread_id WHERE t.id=? AND t.thread_id=?",
                (turn_id, thread_id),
            ).fetchone()
        if row is None:
            return None, None
        from .model_control import ModelCallContext

        try:
            profile = store.resolved_profile(ModelCallContext(
                role="conversation", purpose="route_and_respond",
                owner_id=row["owner_id"], thread_id=thread_id, turn_id=turn_id,
                runtime_bundle_id=row["runtime_bundle_id"], root_budget_id=row["root_budget_id"],
                invocation_id=f"static-archive:{turn_id}",
            ))
        except Exception:
            # Routing is not available for this turn; the foreground path still
            # enforces the hard cap, so declining to archive is safe.
            return None, None
        return static_archive_policy(profile), getattr(profile, "registered_profile_version_id", None)

    def static_policy_for_turn(self, thread_id: str, turn_id: str) -> StaticArchivePolicy | None:
        return self.static_policy_and_version_for_turn(thread_id, turn_id)[0]

    def uncovered_cost(
        self, thread_id: str, *, owner_id: str | None = None, exclude_turn_id: str | None = None,
        counter: Any | None = None,
    ) -> int:
        """Size of the compressible prefix: everything past the coverage cursor.

        This is the quantity the static line is drawn on. It is measured from
        the *committed* coverage cursor, so a job that has not committed yet is
        still counted -- an in-flight pass never makes the prefix look smaller
        than it is.

        ``counter`` must be the counter of the profile whose static policy is
        being compared against; otherwise the prefix is measured with a
        different ruler than the trigger/target it is checked against.
        """
        scope = self.transcripts.resolve_scope(thread_id, owner_id)
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT archived_through_seq FROM conversation_archive_state WHERE owner_id=? AND thread_id=?",
                (scope.owner_id, thread_id),
            ).fetchone()
        transcript = self.transcripts.build(
            thread_id, expected_owner_id=scope.owner_id,
            exclude_turn_id=exclude_turn_id,
            after_sequence=int(row["archived_through_seq"]) if row else 0,
        )
        return self.transcripts.measure(transcript, counter=counter or DEFAULT_TOKEN_COUNTER)

    def static_trigger_state(
        self, thread_id: str, policy: StaticArchivePolicy, *,
        owner_id: str | None = None, exclude_turn_id: str | None = None,
        counter: Any | None = None,
    ) -> dict[str, Any]:
        """Evaluate the static line against a stable snapshot.

        ``reached`` is the trigger alone. ``actionable`` additionally requires
        that a pass can actually reduce the prefix, which is what stops a
        no-op enqueue per turn inside the band between the trigger line and the
        target.
        """
        pending = self.uncovered_cost(
            thread_id, owner_id=owner_id, exclude_turn_id=exclude_turn_id, counter=counter,
        )
        return {
            "pending": pending,
            "trigger": policy.trigger,
            "target": policy.target,
            "headroom": policy.headroom,
            "reserve": policy.reserve,
            "policy_version": policy.version,
            "reached": pending >= policy.trigger,
            "actionable": pending >= policy.trigger and pending > policy.target,
        }

    def enqueue(
        self, thread_id: str, owner_id: str | None = None, *, proactive: bool = False,
        source_turn_id: str | None = None, runtime_bundle_id: str | None = None,
        root_budget_id: str | None = None, static_policy: StaticArchivePolicy | None = None,
        budget_profile_version_id: str | None = None, force_prefix: bool = False,
        counter: Any | None = None,
    ) -> str | None:
        scope = self.transcripts.resolve_scope(thread_id, owner_id)
        with self.db.connection() as connection:
            state = connection.execute(
                "SELECT archived_through_seq FROM conversation_archive_state WHERE owner_id=? AND thread_id=?",
                (scope.owner_id, thread_id),
            ).fetchone()
            if source_turn_id is not None:
                binding = connection.execute(
                    "SELECT t.runtime_bundle_id,t.root_budget_id FROM turns t JOIN threads th ON th.id=t.thread_id "
                    "WHERE t.id=? AND t.thread_id=? AND th.owner_id=?",
                    (source_turn_id, thread_id, scope.owner_id),
                ).fetchone()
                if binding is None:
                    raise ArchiveBindingMissing("archive source turn is missing or outside the owner scope")
            elif runtime_bundle_id is None or root_budget_id is None:
                binding = connection.execute(
                    "SELECT runtime_bundle_id,root_budget_id FROM turns WHERE thread_id=? "
                    "ORDER BY created_at DESC,id DESC LIMIT 1",
                    (thread_id,),
                ).fetchone()
            if binding is not None:
                if runtime_bundle_id is not None and runtime_bundle_id != binding["runtime_bundle_id"]:
                    raise ArchiveBindingMissing("archive runtime bundle does not match its source turn")
                if root_budget_id is not None and root_budget_id != binding["root_budget_id"]:
                    raise ArchiveBindingMissing("archive root budget does not match its source turn")
                runtime_bundle_id = binding["runtime_bundle_id"]
                root_budget_id = binding["root_budget_id"]
        covered = int(state["archived_through_seq"]) if state else 0
        transcript = self.transcripts.build(thread_id, expected_owner_id=scope.owner_id, after_sequence=covered)
        if static_policy is not None:
            # The static line is only allowed to *suppress* a proactive pass. It
            # never suppresses an explicit foreground enqueue, because the
            # foreground is already reacting to a request that does not fit.
            if proactive:
                state_now = self.static_trigger_state(
                    thread_id, static_policy, owner_id=scope.owner_id,
                    exclude_turn_id=source_turn_id, counter=counter,
                )
                if not state_now["actionable"]:
                    return None
            selected = self._select_archive_prefix(
                transcript, proactive=proactive,
                target=static_policy.target, min_turns=static_policy.min_turns,
                force_prefix=force_prefix,
            )
        else:
            selected = self._select_archive_prefix(transcript, proactive=proactive)
        if not selected.turns:
            return None
        start = int(selected.start_sequence or 0)
        end = int(selected.end_sequence or 0)
        now = _now()
        job_id = f"archive_{uuid.uuid4().hex}"
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO conversation_archive_state(owner_id,thread_id) VALUES (?,?)",
                (scope.owner_id, thread_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO memory_archive_jobs("
                "id,owner_id,thread_id,start_message_seq,end_message_seq,source_hash,prompt_version,tokenizer_version,"
                "status,available_at,max_attempts,created_at,updated_at,runtime_bundle_id,root_budget_id,"
                "budget_policy_version,budget_profile_version_id) "
                "VALUES (?,?,?,?,?,?,?,?, 'QUEUED',?,?,?,?,?,?,?,?)",
                (
                    job_id, scope.owner_id, thread_id, start, end, selected.source_hash,
                    PROMPT_VERSION, DEFAULT_TOKEN_COUNTER.version, now, self.max_attempts, now, now,
                    runtime_bundle_id, root_budget_id,
                    static_policy.version if static_policy is not None else None,
                    budget_profile_version_id,
                ),
            )
            row = connection.execute(
                "SELECT id FROM memory_archive_jobs WHERE owner_id=? AND thread_id=? AND start_message_seq=? "
                "AND end_message_seq=? AND source_hash=? AND prompt_version=?",
                (scope.owner_id, thread_id, start, end, selected.source_hash, PROMPT_VERSION),
            ).fetchone()
        return row["id"]

    def status(self, thread_id: str, owner_id: str) -> dict[str, Any]:
        self.transcripts.resolve_scope(thread_id, owner_id)
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT id,status,attempts,max_attempts,last_error_code,start_message_seq,end_message_seq,updated_at "
                "FROM memory_archive_jobs WHERE owner_id=? AND thread_id=? ORDER BY created_at DESC,id DESC LIMIT 20",
                (owner_id, thread_id),
            ).fetchall()
            state = connection.execute("SELECT archived_through_seq FROM conversation_archive_state WHERE owner_id=? AND thread_id=?", (owner_id, thread_id)).fetchone()
        return {"archived_through_seq": int(state[0]) if state else 0, "jobs": [dict(row) for row in rows]}

    def retry(self, thread_id: str, owner_id: str, job_id: str, expected_updated_at: str) -> dict[str, Any]:
        self.transcripts.resolve_scope(thread_id, owner_id)
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM memory_archive_jobs WHERE id=? AND thread_id=? AND owner_id=?", (job_id, thread_id, owner_id)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] in {"QUEUED", "RUNNING", "RETRY_WAIT", "COMPLETED"}:
                return {"id": row["id"], "status": row["status"]}
            if row["status"] != "DEAD_LETTER":
                raise ArchiveError("archive job cannot be retried")
            if row["updated_at"] != expected_updated_at:
                raise ArchiveError("archive job changed; refresh before retrying")
        transcript = self.transcripts.build(thread_id, expected_owner_id=owner_id, after_sequence=row["start_message_seq"] - 1, through_sequence=row["end_message_seq"])
        if transcript.source_hash != row["source_hash"] or row["prompt_version"] != PROMPT_VERSION or row["tokenizer_version"] != DEFAULT_TOKEN_COUNTER.version:
            raise ArchiveSourceChanged("archive source or summarizer version changed")
        if self.db.backend == "postgresql" and row["root_budget_id"]:
            from .costs import CostService
            if CostService(self.db).root_seconds_remaining(owner_id, row["root_budget_id"]) <= 0:
                raise ArchiveError("归档原任务期限已过，重试不会重置预算。当前独立问题仍可通过受限回答处理。")
        with self.db.transaction() as connection:
            changed = connection.execute("UPDATE memory_archive_jobs SET status='QUEUED',attempts=0,available_at=?,finished_at=NULL,updated_at=? WHERE id=? AND status='DEAD_LETTER' AND updated_at=?", (_now(), _now(), job_id, expected_updated_at)).rowcount
            if changed != 1:
                raise ArchiveError("archive job changed; refresh before retrying")
        return {"id": job_id, "status": "QUEUED"}

    def claim(self, job_id: str | None = None) -> ArchiveClaim | None:
        now = datetime.now(timezone.utc)
        until = (now + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.db.transaction() as connection:
            if job_id is None:
                row = connection.execute(
                    "SELECT * FROM memory_archive_jobs WHERE "
                    "((status IN ('QUEUED','RETRY_WAIT') AND available_at<=?) OR (status='RUNNING' AND lease_until<?)) "
                    "ORDER BY available_at,created_at,id LIMIT 1",
                    (now.isoformat(), now.isoformat()),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM memory_archive_jobs WHERE id=? AND "
                    "((status IN ('QUEUED','RETRY_WAIT') AND available_at<=?) OR (status='RUNNING' AND lease_until<?))",
                    (job_id, now.isoformat(), now.isoformat()),
                ).fetchone()
            if row is None:
                return None
            epoch = int(row["lease_epoch"]) + 1
            attempts = int(row["attempts"]) + 1
            changed = connection.execute(
                "UPDATE memory_archive_jobs SET status='RUNNING',lease_owner=?,lease_epoch=?,lease_until=?,"
                "attempts=?,updated_at=?,last_error_code=NULL,last_error_json=NULL WHERE id=? AND lease_epoch=?",
                (self.worker_id, epoch, until, attempts, now.isoformat(), row["id"], row["lease_epoch"]),
            ).rowcount
            if changed != 1:
                return None
        return ArchiveClaim(
            row["id"], row["owner_id"], row["thread_id"], int(row["start_message_seq"]),
            int(row["end_message_seq"]), row["source_hash"], self.worker_id, epoch, attempts,
            row["prompt_version"], row["tokenizer_version"],
            row["runtime_bundle_id"], row["root_budget_id"],
        )

    async def process(self, claim: ArchiveClaim):
        try:
            if claim.prompt_version != PROMPT_VERSION or claim.tokenizer_version != DEFAULT_TOKEN_COUNTER.version:
                raise ArchiveSourceChanged("archive summarizer or tokenizer version changed")
            transcript = self.transcripts.build(
                claim.thread_id, expected_owner_id=claim.owner_id,
                after_sequence=claim.start_sequence - 1, through_sequence=claim.end_sequence,
            )
            if transcript.source_hash != claim.source_hash:
                raise ArchiveSourceChanged("source transcript changed")
            if self.summarizer is None:
                raise InvalidEpisodeSummary("episode summarizer is not configured")
            payload = self._input_payload(transcript)
            input_tokens = DEFAULT_TOKEN_COUNTER.count_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            if input_tokens <= self.max_summary_tokens:
                result = await self._summarize_with_lease(claim, payload)
                summary = self._validate_summary(result, transcript)
            else:
                summaries = []
                for chunk in self._chunk_payloads(transcript):
                    result = await self._summarize_with_lease(claim, chunk)
                    summaries.append(self._validate_summary(
                        result,
                        transcript,
                        allowed_source_ids=set(chunk["source"]["source_message_ids"]),
                    ))
                summary = self._merge_chunk_summaries(summaries)
            return self._commit(claim, transcript, summary)
        except ArchiveLeaseLost:
            raise
        except Exception as exc:
            self._fail(claim, exc)
            return None

    async def _summarize_with_lease(self, claim: ArchiveClaim, payload: dict[str, Any]):
        """Keep the fenced job leased for the entire external model call."""
        self._renew_lease(claim)
        stop = asyncio.Event()
        gateway = getattr(self.summarizer, "gateway", None)
        context_token = None
        if getattr(gateway, "control_store", None) is not None:
            from .model_control import ModelCallContext

            if self.db.backend == "postgresql" and (
                claim.runtime_bundle_id is None or claim.root_budget_id is None
            ):
                raise ArchiveBindingMissing(
                    "archive model call is missing its immutable bundle or root task budget"
                )

            context_token = gateway.set_call_context(ModelCallContext(
                role="reflector", purpose="summarize_episode", thread_id=claim.thread_id,
                owner_id=claim.owner_id, runtime_bundle_id=claim.runtime_bundle_id,
                root_budget_id=claim.root_budget_id,
            ))
        summary_task = asyncio.create_task(self.summarizer(payload))
        heartbeat_task = asyncio.create_task(self._lease_heartbeat(claim, stop))
        try:
            done, _ = await asyncio.wait(
                {summary_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                exc = heartbeat_task.exception()
                if exc is not None:
                    summary_task.cancel()
                    await asyncio.gather(summary_task, return_exceptions=True)
                    raise exc
            result = await summary_task
        finally:
            stop.set()
            if not heartbeat_task.done():
                await heartbeat_task
            if context_token is not None:
                gateway.reset_call_context(context_token)
        self._renew_lease(claim)
        return result

    async def _lease_heartbeat(self, claim: ArchiveClaim, stop: asyncio.Event) -> None:
        interval = max(min(self.lease_seconds / 3, 10.0), 0.05)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                self._renew_lease(claim)

    def _renew_lease(self, claim: ArchiveClaim) -> None:
        until = (datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.db.transaction() as connection:
            changed = connection.execute(
                "UPDATE memory_archive_jobs SET lease_until=?,updated_at=? WHERE id=? AND status='RUNNING' "
                "AND lease_owner=? AND lease_epoch=? AND source_hash=?",
                (until, _now(), claim.job_id, claim.lease_owner, claim.lease_epoch, claim.source_hash),
            ).rowcount
        if changed != 1:
            raise ArchiveLeaseLost("archive lease was lost during summarization")

    def _chunk_payloads(self, transcript: CanonicalTranscript) -> list[dict[str, Any]]:
        """Split an oversized transcript by event content without splitting source identity."""
        chunks: list[dict[str, Any]] = []
        chunk_number = 0
        for turn in transcript.turns:
            turn_source_ids = list(dict.fromkeys(
                event.message_id for event in turn.events if event.message_id is not None
            ))
            for event in turn.events:
                content = event.content
                offset = 0
                fragment_index = 0
                while offset < len(content) or (not content and fragment_index == 0):
                    chunk_number += 1
                    fragment_index += 1
                    base_event = event.canonical()
                    base_event["content"] = ""
                    reference_ids = [event.message_id] if event.message_id else turn_source_ids
                    payload = self._chunk_payload(
                        transcript, turn.canonical(), base_event, reference_ids,
                        chunk_number, fragment_index,
                    )
                    overhead = self._payload_tokens(payload)
                    if overhead > self.max_summary_tokens:
                        raise ArchiveInputTooLarge(
                            f"summary chunk metadata requires {overhead} tokens but limit is {self.max_summary_tokens}"
                        )
                    remaining = content[offset:]
                    if not remaining:
                        chunks.append(payload)
                        break
                    low, high, best = 1, len(remaining), 0
                    while low <= high:
                        middle = (low + high) // 2
                        payload["turns"][0]["events"][0]["content"] = remaining[:middle]
                        payload["source"]["fragment_start"] = offset
                        payload["source"]["fragment_end"] = offset + middle
                        if self._payload_tokens(payload) <= self.max_summary_tokens:
                            best = middle
                            low = middle + 1
                        else:
                            high = middle - 1
                    if best <= 0:
                        raise ArchiveInputTooLarge("summary chunk cannot fit any source content")
                    payload["turns"][0]["events"][0]["content"] = remaining[:best]
                    payload["source"]["fragment_start"] = offset
                    payload["source"]["fragment_end"] = offset + best
                    chunks.append(payload)
                    offset += best
        if not chunks:
            raise InvalidEpisodeSummary("oversized transcript contains no summarizable events")
        total = len(chunks)
        for index, payload in enumerate(chunks, 1):
            payload["source"]["chunk_index"] = index
            payload["source"]["chunk_count"] = total
            if self._payload_tokens(payload) > self.max_summary_tokens:
                # Decimal growth in chunk_count is normally tiny, but keep the
                # configured hard bound exact for every generated request.
                raise ArchiveInputTooLarge("summary chunk index metadata exceeds input budget")
        return chunks

    @staticmethod
    def _chunk_payload(
        transcript: CanonicalTranscript, turn: dict[str, Any], event: dict[str, Any],
        reference_ids: list[str], chunk_number: int, fragment_index: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": "episode-input-chunk-v2",
            "source": {
                "thread_id": transcript.scope.thread_id,
                "start_message_seq": transcript.start_sequence,
                "end_message_seq": transcript.end_sequence,
                "source_hash": transcript.source_hash,
                "chunk_index": chunk_number,
                # Reserve more digits than any practical batch so filling the
                # real total later can only make the payload smaller.
                "chunk_count": 999999999,
                "fragment_index": fragment_index,
                "fragment_start": 0,
                "fragment_end": 0,
                "source_message_ids": reference_ids,
            },
            "turns": [{
                "turn_id": turn["turn_id"], "outcome": turn["outcome"],
                "start_sequence": turn["start_sequence"], "end_sequence": turn["end_sequence"],
                "events": [event],
            }],
        }

    @staticmethod
    def _payload_tokens(payload: dict[str, Any]) -> int:
        return DEFAULT_TOKEN_COUNTER.count_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    @staticmethod
    def _merge_chunk_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
        if not summaries:
            raise InvalidEpisodeSummary("no chunk summaries were produced")
        merged: dict[str, Any] = {field: [] for field in STRUCTURED_FIELDS}
        merged["sensitivity"] = max(
            (summary["sensitivity"] for summary in summaries), key=SENSITIVITY.get,
        )
        seen: set[tuple[str, str, tuple[str, ...]]] = set()

        def merged_bytes() -> int:
            return DEFAULT_TOKEN_COUNTER.count_text(
                json.dumps(merged, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )

        def take(field: str, item: dict[str, Any], limit: int) -> bool:
            key = (field, item["text"], tuple(item["source_message_ids"]))
            if key in seen or len(merged[field]) >= 32:
                return False
            candidate = {
                name: list(value) if isinstance(value, list) else value
                for name, value in merged.items()
            }
            candidate[field].append(item)
            if DEFAULT_TOKEN_COUNTER.count_text(
                json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            ) > limit:
                return False
            merged[field].append(item)
            seen.add(key)
            return True

        # Cover every chunk's synopsis first, but only within a capped share of
        # the budget: the reserved remainder belongs to decisions and outcomes.
        synopsis_limit = max(
            int(MAX_SUMMARY_OUTPUT_TOKENS * (1 - DECISION_OUTCOME_RESERVE_RATIO)),
            MAX_SUMMARY_OUTPUT_TOKENS // 4,
        )
        for summary in summaries:
            for item in summary["synopsis"]:
                take("synopsis", item, synopsis_limit)

        # Then interleave across chunks and between the two decision fields, so
        # neither a long chunk nor a long list of decisions can take the whole
        # remaining budget away from outcomes.
        for summary in summaries:
            decisions, outcomes = summary["decisions"], summary["outcomes"]
            for offset in range(max(len(decisions), len(outcomes))):
                if offset < len(decisions):
                    take("decisions", decisions[offset], MAX_SUMMARY_OUTPUT_TOKENS)
                if offset < len(outcomes):
                    take("outcomes", outcomes[offset], MAX_SUMMARY_OUTPUT_TOKENS)

        for summary in summaries:
            for field in ("open_loops", "topics"):
                for item in summary[field]:
                    take(field, item, MAX_SUMMARY_OUTPUT_TOKENS)

        if not merged["synopsis"]:
            raise InvalidEpisodeSummary("merged summary has no synopsis")
        return merged

    def _commit(self, claim: ArchiveClaim, transcript: CanonicalTranscript, summary: dict[str, Any]):
        if claim.prompt_version != PROMPT_VERSION or claim.tokenizer_version != DEFAULT_TOKEN_COUNTER.version:
            raise ArchiveSourceChanged("archive summarizer or tokenizer version changed before commit")
        synopsis = summary["synopsis"]
        text = " ".join(item["text"] for item in synopsis).strip()
        sensitivity = summary["sensitivity"]
        with self.db.transaction() as connection:
            job = connection.execute(
                "SELECT * FROM memory_archive_jobs WHERE id=?", (claim.job_id,)
            ).fetchone()
            if not _owns(job, claim):
                raise ArchiveLeaseLost("archive lease was lost before commit")
            fresh = self.transcripts.build(
                claim.thread_id, expected_owner_id=claim.owner_id,
                after_sequence=claim.start_sequence - 1, through_sequence=claim.end_sequence,
            )
            if fresh.source_hash != claim.source_hash:
                raise ArchiveSourceChanged("archive source changed before commit")
            episode = self.store.save_episode(
                owner_id=claim.owner_id, thread_id=claim.thread_id, project_id=transcript.scope.project_id,
                start_message_seq=claim.start_sequence, end_message_seq=claim.end_sequence,
                source_hash=claim.source_hash, summary=text, sensitivity=sensitivity,
                synopsis=synopsis, topics=summary["topics"], decisions=summary["decisions"],
                outcomes=summary["outcomes"], open_loops=summary["open_loops"],
                source_message_ids=list(transcript.message_ids), prompt_version=PROMPT_VERSION,
                tokenizer_version=DEFAULT_TOKEN_COUNTER.version,
                source_token_count=DEFAULT_TOKEN_COUNTER.count_text(json.dumps(self._input_payload(transcript), ensure_ascii=False)),
                summary_token_count=DEFAULT_TOKEN_COUNTER.count_text(json.dumps(summary, ensure_ascii=False)),
                connection=connection,
            )
            connection.execute(
                "UPDATE conversation_archive_state SET archived_through_seq=CASE WHEN archived_through_seq<? "
                "THEN ? ELSE archived_through_seq END,state='IDLE',"
                "reserved_start_seq=NULL,reserved_end_seq=NULL,source_hash=NULL,lease_owner=NULL,lease_until=NULL,error=NULL,attempts=0 "
                "WHERE owner_id=? AND thread_id=?",
                (claim.end_sequence, claim.end_sequence, claim.owner_id, claim.thread_id),
            )
            changed = connection.execute(
                "UPDATE memory_archive_jobs SET status='COMPLETED',lease_owner=NULL,lease_until=NULL,finished_at=?,updated_at=? "
                "WHERE id=? AND status='RUNNING' AND lease_owner=? AND lease_epoch=? AND source_hash=?",
                (_now(), _now(), claim.job_id, claim.lease_owner, claim.lease_epoch, claim.source_hash),
            ).rowcount
            if changed != 1:
                raise ArchiveLeaseLost("archive lease was lost during commit")
        return episode

    def _fail(self, claim: ArchiveClaim, exc: Exception) -> None:
        now = datetime.now(timezone.utc)
        # Invalid immutable input is not made better by retrying.  Keep the job
        # inspectable in the dead-letter state and leave coverage unchanged.
        terminal = isinstance(
            exc, (ArchiveInputTooLarge, ArchiveSourceChanged, ArchiveBindingMissing)
        ) or claim.attempts >= self.max_attempts
        status = "DEAD_LETTER" if terminal else "RETRY_WAIT"
        available = now if terminal else now + timedelta(seconds=min(2 ** claim.attempts, 60))
        with self.db.transaction() as connection:
            changed = connection.execute(
                "UPDATE memory_archive_jobs SET status=?,available_at=?,lease_owner=NULL,lease_until=NULL,"
                "last_error_code=?,last_error_json=?,updated_at=?,finished_at=? "
                "WHERE id=? AND status='RUNNING' AND lease_owner=? AND lease_epoch=?",
                (
                    status, available.isoformat(), getattr(exc, "code", type(exc).__name__),
                    json.dumps({"message": str(exc)[:240]}, ensure_ascii=False), now.isoformat(),
                    now.isoformat() if terminal else None, claim.job_id, claim.lease_owner, claim.lease_epoch,
                ),
            ).rowcount
            if changed != 1:
                return
            connection.execute(
                "UPDATE conversation_archive_state SET state='FAILED',error=?,lease_owner=NULL,lease_until=NULL "
                "WHERE owner_id=? AND thread_id=?",
                (str(exc)[:240], claim.owner_id, claim.thread_id),
            )
        if isinstance(exc, ArchiveSourceChanged):
            # The signal may already have been acknowledged.  Rebuild a job
            # against the current canonical hash so a changed transcript is
            # never retried under stale input.
            try:
                self.enqueue(
                    claim.thread_id, claim.owner_id,
                    runtime_bundle_id=claim.runtime_bundle_id,
                    root_budget_id=claim.root_budget_id,
                )
            except Exception:
                # The old job remains dead-lettered and observable; a future
                # terminal-turn signal or operator retry can enqueue again.
                pass

    def _select_archive_prefix(
        self, transcript: CanonicalTranscript, *, proactive: bool = False,
        target: int | None = None, min_turns: int = 0, force_prefix: bool = False,
    ) -> CanonicalTranscript:
        turns = list(transcript.turns)
        if not turns:
            return CanonicalTranscript(transcript.scope, (), transcript.source_hash, ())
        if target is not None:
            # R2-03: the pass stops at the profile's static target, and it may
            # never archive into the newest `min_turns` complete turns. The
            # protected suffix is split off *before* selection instead of
            # relying on `pack_recent` soft-protection release: a single huge
            # recent Turn can otherwise make the release drop below N and the
            # prefix selector would archive a protected Turn.
            protected = min(max(int(min_turns), 0), len(turns))
            old_prefix = turns[: len(turns) - protected] if protected else list(turns)
            costs = [
                DEFAULT_TOKEN_COUNTER.count_text(
                    json.dumps(turn.canonical(), ensure_ascii=False, sort_keys=True)
                )
                for turn in turns
            ]
            remaining = sum(costs)
            archive_count = 0
            for index, turn in enumerate(old_prefix):
                if remaining <= max(int(target), 0):
                    break
                remaining -= costs[index]
                archive_count += 1
            if force_prefix and archive_count <= 0 and old_prefix:
                # The complete request did not fit even though the raw prefix is
                # already under the static target. A foreground pass may still
                # take the oldest archivable batch; the static trigger line is a
                # background policy, not a veto here.
                archive_count = 1
        elif self.keep_tokens is not None:
            budget = int(self.keep_tokens * .8) if proactive else self.keep_tokens
            keep = self.transcripts.pack_recent(transcript, budget)
            archive_count = len(turns) - len(keep.turns)
        else:
            remaining_messages = sum(
                sum(event.message_id is not None for event in turn.events) for turn in turns
            )
            archive_count = 0
            for turn in turns:
                count = sum(event.message_id is not None for event in turn.events)
                if remaining_messages - count < self.keep_messages:
                    break
                remaining_messages -= count
                archive_count += 1
        if archive_count <= 0:
            return CanonicalTranscript(transcript.scope, (), transcript.source_hash, ())
        # A batch is bounded by the summarizer input budget.  Never split a
        # Turn; if the first Turn itself is too large it is deliberately kept
        # as a one-Turn batch so process() records an explicit dead letter.
        selected_list: list[Any] = []
        for turn in turns[:archive_count]:
            candidate = tuple([*selected_list, turn])
            candidate_ids = {item.turn_id for item in candidate}
            candidate_manifest = tuple(
                row for row in transcript.source_manifest if row["turn_id"] in candidate_ids
            )
            # Hash bytes are part of the serialized prompt.  Use a same-size
            # placeholder while packing so the estimate cannot undercount the
            # real source hash that process() will send.
            candidate_transcript = CanonicalTranscript(
                transcript.scope, candidate, "sha256:" + ("0" * 64), candidate_manifest
            )
            candidate_payload = self._input_payload(candidate_transcript)
            candidate_tokens = DEFAULT_TOKEN_COUNTER.count_text(
                json.dumps(candidate_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            if selected_list and candidate_tokens > self.max_summary_tokens:
                break
            selected_list.append(turn)
            if candidate_tokens > self.max_summary_tokens:
                break
        selected_turns = tuple(selected_list)
        if not selected_turns:
            return CanonicalTranscript(transcript.scope, (), transcript.source_hash, ())
        end = selected_turns[-1].end_sequence
        return self.transcripts.build(
            transcript.scope.thread_id, expected_owner_id=transcript.scope.owner_id,
            after_sequence=selected_turns[0].start_sequence - 1, through_sequence=end,
        )

    @staticmethod
    def _input_payload(transcript: CanonicalTranscript) -> dict[str, Any]:
        return {
            "schema_version": "episode-input-v2",
            "source": {
                "thread_id": transcript.scope.thread_id,
                "start_message_seq": transcript.start_sequence,
                "end_message_seq": transcript.end_sequence,
                "source_hash": transcript.source_hash,
            },
            "turns": [turn.canonical() for turn in transcript.turns],
        }

    @staticmethod
    def _validate_summary(
        value: dict[str, Any] | str,
        transcript: CanonicalTranscript,
        *,
        allowed_source_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise InvalidEpisodeSummary("summarizer returned invalid JSON") from exc
        if not isinstance(value, dict) or set(value) != ALLOWED_FIELDS:
            raise InvalidEpisodeSummary("summary has missing or unknown fields")
        allowed_ids = set(transcript.message_ids) if allowed_source_ids is None else allowed_source_ids
        source_roles = {
            event.message_id: event.role
            for turn in transcript.turns
            for event in turn.events
            if event.message_id is not None
        }
        normalized: dict[str, Any] = {}
        for field in STRUCTURED_FIELDS:
            items = value[field]
            if not isinstance(items, list) or len(items) > 32:
                raise InvalidEpisodeSummary(f"{field} must be a bounded list")
            normalized_items = []
            for item in items:
                if not isinstance(item, dict) or set(item) != {"text", "source_message_ids"}:
                    raise InvalidEpisodeSummary(f"{field} item is invalid")
                if not isinstance(item["text"], str):
                    raise InvalidEpisodeSummary(f"{field} item text must be a string")
                text = item["text"].strip()
                refs = item["source_message_ids"]
                if not text or len(text) > 800 or not isinstance(refs, list) or not refs or not all(isinstance(ref, str) for ref in refs):
                    raise InvalidEpisodeSummary(f"{field} item is empty or too long")
                if any(ref not in allowed_ids for ref in refs) or len(set(refs)) != len(refs):
                    raise InvalidEpisodeSummary(f"{field} references messages outside the source batch")
                if field in {"decisions", "outcomes"} and not any(source_roles.get(ref) == "user" for ref in refs):
                    raise InvalidEpisodeSummary(f"{field} must cite a user message")
                if SECRET_RE.search(text):
                    raise InvalidEpisodeSummary("summary contains secret-like content")
                normalized_items.append({"text": text, "source_message_ids": refs})
            normalized[field] = normalized_items
        if not normalized["synopsis"]:
            raise InvalidEpisodeSummary("summary synopsis is required")
        sensitivity = value["sensitivity"]
        if sensitivity not in SENSITIVITY:
            raise InvalidEpisodeSummary("invalid sensitivity")
        source_text = "\n".join(event.content for turn in transcript.turns for event in turn.events)
        deterministic = "restricted" if SECRET_RE.search(source_text) else "normal"
        normalized["sensitivity"] = max((sensitivity, deterministic), key=SENSITIVITY.get)
        normalized_tokens = DEFAULT_TOKEN_COUNTER.count_text(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        if normalized_tokens > MAX_SUMMARY_OUTPUT_TOKENS:
            raise InvalidEpisodeSummary("summary exceeds output token budget")
        return normalized


class LiveEpisodeSummarizer:
    def __init__(self, gateway) -> None:
        self.gateway = gateway

    async def __call__(self, payload: dict[str, Any]) -> dict[str, Any] | str:
        from .model_gateway import ModelRequest

        response = await self.gateway.complete(ModelRequest(
            messages=[
                {"role": "system", "content": (
                    "Return strict JSON only with exactly these fields: synopsis, topics, decisions, outcomes, "
                    "open_loops, sensitivity. Each list item must contain exactly text and source_message_ids. "
                    "Every source id must come from the supplied transcript. Describe past conversation as lossy, "
                    "attributed history; do not turn statements into permanent user facts. sensitivity is normal, "
                    "sensitive, or restricted."
                    " All five fields synopsis, topics, decisions, outcomes and open_loops are arrays, never strings. "
                    "Use this exact shape: {\"synopsis\":[{\"text\":\"Brief attributed summary\","
                    "\"source_message_ids\":[\"ID_FROM_TRANSCRIPT\"]}],\"topics\":[],\"decisions\":[],"
                    "\"outcomes\":[],\"open_loops\":[],\"sensitivity\":\"normal\"}. "
                    "Keep each text concise and the entire JSON below 1600 tokens. Do not wrap JSON in Markdown."
                    " Use at most 6 items TOTAL across all arrays, each text at most 120 characters, "
                    "and at most 2 source_message_ids per item. Omit low-value details, never list every message."
                    " Decisions and outcomes MUST cite at least one message whose role is user. "
                    "Assistant suggestions are not user decisions or verified outcomes: put them in synopsis or "
                    "open_loops with attribution, or omit them. If no user message supports a decision/outcome, "
                    "return an empty array for that field. Preserve source IDs exactly, including underscores."
                )},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
            ],
            tools=[], temperature=0, max_tokens=4096, role="reflector", purpose="summarize_episode", thinking=False,
        ))
        return response.message


class ManagedArchiveWorker:
    """One background archival pass at a time, behind the static line.

    Three limits keep the background from becoming the problem it exists to
    solve:

    * **One job at a time.** ``run_once`` claims a single fenced job, so the
      archive never runs concurrently with itself and cannot multiply model
      calls.
    * **Rate cap.** ``max_jobs_per_minute`` bounds how many passes are started
      per minute regardless of how much activity arrives. Cost itself is not
      re-checked here: every archive call already goes through the model
      gateway, which fails closed on the root task budget, and a rejected call
      is recorded against the job. A second budget check in this loop would be
      a duplicate enforcement path.
    * **Foreground first.** ``foreground_probe`` is consulted before any work;
      while a user turn is queued the worker does nothing. It never holds a lock
      the answer path needs, so a background pass cannot block generation -- the
      worst case is that it finishes after the answer was already sent.
    """

    def __init__(
        self, archiver: ConversationArchiver, poll_interval: float = 0.25, *,
        max_jobs_per_minute: int | None = None,
        foreground_probe: Callable[[], bool] | None = None,
    ) -> None:
        self.archiver = archiver
        self.poll_interval = poll_interval
        if max_jobs_per_minute is not None and int(max_jobs_per_minute) <= 0:
            raise ValueError("max_jobs_per_minute must be a positive integer")
        self.max_jobs_per_minute = None if max_jobs_per_minute is None else int(max_jobs_per_minute)
        self.foreground_probe = foreground_probe
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._recent_jobs: deque[float] = deque()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="memory-archive-worker")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            await self._task
        self._task = None

    async def _run(self) -> None:
        while self._stop is not None and not self._stop.is_set():
            progressed = await self.run_once()
            if progressed:
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    def _rate_allows(self) -> bool:
        if self.max_jobs_per_minute is None:
            return True
        now = time.monotonic()
        while self._recent_jobs and now - self._recent_jobs[0] >= 60.0:
            self._recent_jobs.popleft()
        return len(self._recent_jobs) < self.max_jobs_per_minute

    def _foreground_busy(self) -> bool:
        if self.foreground_probe is None:
            return False
        try:
            return bool(self.foreground_probe())
        except Exception:
            # A broken probe must not stall archival; treat it as idle.
            return False

    async def run_once(self) -> bool:
        if self._foreground_busy():
            return False
        signal = self._next_signal()
        if signal is not None:
            return self._consume_signal(signal)
        if not self._rate_allows():
            return False
        claim = self.archiver.claim()
        if claim is None:
            return False
        try:
            await self.archiver.process(claim)
        except ArchiveLeaseLost:
            # A different worker owns the fenced job; continue polling.
            pass
        self._recent_jobs.append(time.monotonic())
        return True

    def _consume_signal(self, signal: Any) -> bool:
        """Enqueue against the static line, then acknowledge the signal.

        The signal is acknowledged whether or not a job was created. A signal
        that did not cross the line is *spent*, not pending: keeping it would
        make every poll re-evaluate the same snapshot, and the next completed
        turn raises a fresh signal anyway. Job idempotency for a snapshot is the
        unique index on (owner, thread, start, end, source_hash, prompt_version),
        so a re-raised signal for an unchanged snapshot cannot create a second
        job either.
        """
        policy = None
        profile_version_id = None
        try:
            policy, profile_version_id = self.archiver.static_policy_and_version_for_turn(
                signal["thread_id"], signal["turn_id"],
            )
        except Exception:
            policy, profile_version_id = None, None
        counter = None
        if profile_version_id:
            with self.archiver.db.connection() as connection:
                row = connection.execute(
                    "SELECT counter_id,counter_evidence_version FROM model_profile_versions WHERE id=?",
                    (profile_version_id,),
                ).fetchone()
            if row is not None:
                from .token_budget import counter_for_fields

                counter = counter_for_fields(
                    row["counter_id"], row["counter_evidence_version"],
                ).counter
        try:
            self.archiver.enqueue(
                signal["thread_id"], proactive=True, source_turn_id=signal["turn_id"],
                static_policy=policy, budget_profile_version_id=profile_version_id,
                counter=counter,
            )
        except Exception:
            # Keep the durable signal for a later poll/retry. A transient
            # database or scope error must not terminate the worker loop.
            return False
        try:
            with self.archiver.db.transaction() as connection:
                deleted = connection.execute(
                    "DELETE FROM memory_archive_signals WHERE turn_id=?", (signal["turn_id"],)
                ).rowcount
                if deleted != 1:
                    # Another worker consumed it between selection and
                    # deletion; no durable work is lost, but do not claim
                    # progress for a row we did not delete.
                    return False
        except Exception:
            # Keep the signal durable.  The next poll can retry the
            # acknowledgement after a transient database failure.
            return False
        return True

    def _next_signal(self):
        with self.archiver.db.connection() as connection:
            return connection.execute(
                "SELECT * FROM memory_archive_signals ORDER BY created_at,turn_id LIMIT 1"
            ).fetchone()


def foreground_turn_pending(db) -> bool:
    """True while a user turn is queued and waiting for a worker slot.

    This is the foreground-priority probe the archive worker consults. It is
    deliberately narrow: it asks whether someone is *waiting*, not whether any
    turn is running. Treating "any turn in flight" as foreground activity would
    stop archival entirely on a busy system, which is the opposite of the point.
    """
    with db.connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM turn_jobs WHERE status='QUEUED' LIMIT 1"
        ).fetchone()
    return row is not None


def _positive_int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def archive_jobs_per_minute_from_env() -> int | None:
    raw = (os.getenv("AGENT_ARCHIVE_MAX_JOBS_PER_MINUTE") or "").strip()
    if not raw:
        return None
    return _positive_int_env("AGENT_ARCHIVE_MAX_JOBS_PER_MINUTE", 1)


def archive_wait_policy() -> ArchiveWaitPolicy:
    """The bounded foreground wait.

    A zero poll interval would spin, so the poll is floored at 1ms. The deadline
    is not floored: an operator who sets it below the poll interval gets a wait
    that gives up on the first check, which is a coherent way to disable the
    wait entirely.
    """
    return ArchiveWaitPolicy(
        deadline_ms=_positive_int_env("AGENT_ARCHIVE_WAIT_MS", DEFAULT_ARCHIVE_WAIT_MS),
        poll_ms=max(_positive_int_env("AGENT_ARCHIVE_WAIT_POLL_MS", DEFAULT_ARCHIVE_WAIT_POLL_MS), 1),
    )


def _owns(row: Any, claim: ArchiveClaim) -> bool:
    return bool(
        row is not None and row["status"] == "RUNNING" and row["lease_owner"] == claim.lease_owner
        and int(row["lease_epoch"]) == claim.lease_epoch and row["source_hash"] == claim.source_hash
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
