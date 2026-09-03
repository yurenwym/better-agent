from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .token_budget import DEFAULT_TOKEN_COUNTER
from .transcript import CanonicalTranscript, CanonicalTurnTranscriptBuilder


PROMPT_VERSION = "episode-v2"
MAX_SUMMARY_OUTPUT_TOKENS = 1_600
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

    def enqueue(self, thread_id: str, owner_id: str | None = None) -> str | None:
        scope = self.transcripts.resolve_scope(thread_id, owner_id)
        with self.db.connection() as connection:
            state = connection.execute(
                "SELECT archived_through_seq FROM conversation_archive_state WHERE owner_id=? AND thread_id=?",
                (scope.owner_id, thread_id),
            ).fetchone()
        covered = int(state["archived_through_seq"]) if state else 0
        transcript = self.transcripts.build(thread_id, expected_owner_id=scope.owner_id, after_sequence=covered)
        selected = self._select_archive_prefix(transcript)
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
                "status,available_at,max_attempts,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?, 'QUEUED',?,?,?,?)",
                (
                    job_id, scope.owner_id, thread_id, start, end, selected.source_hash,
                    PROMPT_VERSION, DEFAULT_TOKEN_COUNTER.version, now, self.max_attempts, now, now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM memory_archive_jobs WHERE owner_id=? AND thread_id=? AND start_message_seq=? "
                "AND end_message_seq=? AND source_hash=? AND prompt_version=?",
                (scope.owner_id, thread_id, start, end, selected.source_hash, PROMPT_VERSION),
            ).fetchone()
        return row["id"]

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
        )

    async def process(self, claim: ArchiveClaim):
        try:
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
            if input_tokens > self.max_summary_tokens:
                raise ArchiveInputTooLarge(
                    f"summary input requires {input_tokens} tokens but limit is {self.max_summary_tokens}"
                )
            result = await self.summarizer(payload)
            summary = self._validate_summary(result, transcript)
            return self._commit(claim, transcript, summary)
        except ArchiveLeaseLost:
            raise
        except Exception as exc:
            self._fail(claim, exc)
            return None

    def _commit(self, claim: ArchiveClaim, transcript: CanonicalTranscript, summary: dict[str, Any]):
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
                # A lease was valid while summarization ran, but the source
                # changed before the fenced transaction could commit.  Keep
                # this as a lease-loss signal to the caller; no retry of the
                # stale summary is allowed.
                raise ArchiveLeaseLost("archive source changed before commit")
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
                "UPDATE conversation_archive_state SET archived_through_seq=MAX(archived_through_seq,?),state='IDLE',"
                "reserved_start_seq=NULL,reserved_end_seq=NULL,source_hash=NULL,lease_owner=NULL,lease_until=NULL,error=NULL,attempts=0 "
                "WHERE owner_id=? AND thread_id=?",
                (claim.end_sequence, claim.owner_id, claim.thread_id),
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
        terminal = isinstance(exc, (ArchiveInputTooLarge, ArchiveSourceChanged)) or claim.attempts >= self.max_attempts
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
                self.enqueue(claim.thread_id, claim.owner_id)
            except Exception:
                # The old job remains dead-lettered and observable; a future
                # terminal-turn signal or operator retry can enqueue again.
                pass

    def _select_archive_prefix(self, transcript: CanonicalTranscript) -> CanonicalTranscript:
        turns = list(transcript.turns)
        if not turns:
            return CanonicalTranscript(transcript.scope, (), transcript.source_hash, ())
        if self.keep_tokens is not None:
            keep = self.transcripts.pack_recent(transcript, self.keep_tokens)
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
    def _validate_summary(value: dict[str, Any] | str, transcript: CanonicalTranscript) -> dict[str, Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise InvalidEpisodeSummary("summarizer returned invalid JSON") from exc
        if not isinstance(value, dict) or set(value) != ALLOWED_FIELDS:
            raise InvalidEpisodeSummary("summary has missing or unknown fields")
        allowed_ids = set(transcript.message_ids)
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
                )},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
            ],
            tools=[], temperature=0, max_tokens=1600, role="reflector", purpose="summarize_episode",
        ))
        return response.message


class ManagedArchiveWorker:
    def __init__(self, archiver: ConversationArchiver, poll_interval: float = 0.25) -> None:
        self.archiver = archiver
        self.poll_interval = poll_interval
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

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

    async def run_once(self) -> bool:
        signal = self._next_signal()
        if signal is not None:
            try:
                self.archiver.enqueue(signal["thread_id"])
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
        claim = self.archiver.claim()
        if claim is None:
            return False
        try:
            await self.archiver.process(claim)
        except ArchiveLeaseLost:
            # A different worker owns the fenced job; continue polling.
            pass
        return True

    def _next_signal(self):
        with self.archiver.db.connection() as connection:
            return connection.execute(
                "SELECT * FROM memory_archive_signals ORDER BY created_at,turn_id LIMIT 1"
            ).fetchone()


def _owns(row: Any, claim: ArchiveClaim) -> bool:
    return bool(
        row is not None and row["status"] == "RUNNING" and row["lease_owner"] == claim.lease_owner
        and int(row["lease_epoch"]) == claim.lease_epoch and row["source_hash"] == claim.source_hash
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
