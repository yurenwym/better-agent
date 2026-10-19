from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import json
import math

from pgvector import Vector
from pgvector.psycopg import register_vector

from .db import Database
from .embedding import EmbeddingProtocolError, EmbeddingProvider, EmbeddingRequestError


class EmbeddingLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddingLease:
    revision_id: str
    profile_id: str
    content_hash: str
    owner: str
    epoch: int


class EmbeddingWorker:
    def __init__(self, db: Database, provider: EmbeddingProvider, *, owner: str) -> None:
        if db.backend != "postgresql":
            raise ValueError("EmbeddingWorker requires PostgreSQL")
        self.db = db
        self.provider = provider
        self.owner = owner

    def claim(self, lease_seconds: float = 30) -> EmbeddingLease | None:
        if lease_seconds <= 0:
            raise ValueError("embedding lease duration must be positive")
        with self.db.transaction() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                  SELECT revision_id,profile_id,content_hash,status FROM embedding_jobs
                  WHERE ((status IN ('QUEUED','RETRY_WAIT') AND available_at<=clock_timestamp())
                    OR (status='RUNNING' AND lease_until<=clock_timestamp()))
                  ORDER BY available_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE embedding_jobs j SET status='RUNNING',lease_owner=%s,
                  lease_epoch=j.lease_epoch+1,lease_until=clock_timestamp()+(%s*interval '1 second'),
                  attempts=j.attempts+1,updated_at=clock_timestamp()
                FROM candidate c WHERE j.revision_id=c.revision_id AND j.profile_id=c.profile_id
                  AND j.content_hash=c.content_hash
                RETURNING j.revision_id,j.profile_id,j.content_hash,j.lease_epoch,j.attempts,
                  c.status AS previous_status
                """,
                (self.owner, lease_seconds),
            ).fetchone()
            if row is None:
                return None
            attempt = int(row["attempts"])
            if attempt < 1:
                raise RuntimeError("embedding attempt must be at least 1")
            event_type = "embedding.recovered" if row["previous_status"] == "RUNNING" else "embedding.claimed"
            self._trace(connection, event_type, {
                "job_id": _job_id(row["revision_id"], row["profile_id"], row["content_hash"]),
                "revision_id": row["revision_id"], "profile_id": row["profile_id"],
                "content_hash": row["content_hash"], "status": "RUNNING",
                "attempt": attempt, "lease_epoch": int(row["lease_epoch"]),
                "lease_owner": self.owner,
            })
        return EmbeddingLease(
            row["revision_id"], row["profile_id"], row["content_hash"],
            self.owner, int(row["lease_epoch"]),
        )

    def heartbeat(self, lease: EmbeddingLease, lease_seconds: float = 30) -> None:
        if lease_seconds <= 0:
            raise ValueError("embedding lease duration must be positive")
        with self.db.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE embedding_jobs
                SET lease_until=clock_timestamp()+(%s*interval '1 second'),updated_at=clock_timestamp()
                WHERE revision_id=%s AND profile_id=%s AND content_hash=%s
                  AND status='RUNNING' AND lease_owner=%s AND lease_epoch=%s
                  AND lease_until>clock_timestamp()
                """,
                (
                    lease_seconds, lease.revision_id, lease.profile_id,
                    lease.content_hash, lease.owner, lease.epoch,
                ),
            ).rowcount
        if changed != 1:
            raise EmbeddingLeaseLost("embedding lease was lost")

    def run_once(self, lease_seconds: float = 30) -> bool:
        lease = self.claim(lease_seconds)
        if lease is None:
            return False
        try:
            self.process(lease)
        except EmbeddingLeaseLost:
            pass
        return True

    def process(self, lease: EmbeddingLease) -> None:
        """Embed the current revision, then write it only while this lease holds.

        The provider call is the cost boundary. It runs only after the database
        clock still shows this worker holding the job, the revision is current,
        and the profile is active. Those locks are not held across that call.
        The commit transaction locks the memory entry and then the profile,
        checks them again, and only then writes the vector. A lease lost after
        the call, or a source that changed before those locks, discards the
        result. The remote request may already have been accepted, so a later
        worker can call the provider again. This does not promise exactly-once
        at the provider.
        """
        with self.db.connection() as connection:
            if not self._lease_current(connection, lease):
                raise EmbeddingLeaseLost("embedding lease was lost")
            problem = self._source_problem(connection, lease)
            if problem is None:
                revision = connection.execute(
                    "SELECT r.content,p.model,p.dimensions FROM memory_revisions r "
                    "JOIN memory_entries e ON e.id=r.entry_id AND e.current_revision_id=r.id "
                    "JOIN embedding_profiles p ON p.id=%s AND p.active=true "
                    "WHERE r.id=%s AND r.content_hash=%s",
                    (lease.profile_id, lease.revision_id, lease.content_hash),
                ).fetchone()
            else:
                revision = None
        if revision is None:
            self._fail(lease, problem or "stale_revision", retryable=False)
            return
        try:
            batch = self.provider.embed([revision["content"]])
            if batch.model != revision["model"] or batch.dimensions != revision["dimensions"]:
                raise EmbeddingProtocolError("embedding batch does not match active profile")
            if len(batch.vectors) != 1:
                raise EmbeddingProtocolError("embedding batch count does not match job")
            values = batch.vectors[0]
            if len(values) != revision["dimensions"]:
                raise EmbeddingProtocolError("embedding vector dimensions do not match profile")
            if not all(math.isfinite(value) for value in values) or not any(values):
                raise EmbeddingProtocolError("embedding vector is invalid for cosine search")
            vector = Vector(list(values))
            with self.db.transaction() as connection:
                register_vector(connection)
                locked = connection.execute(
                    "SELECT 1 FROM embedding_jobs WHERE revision_id=%s AND profile_id=%s "
                    "AND content_hash=%s AND status='RUNNING' AND lease_owner=%s "
                    "AND lease_epoch=%s AND lease_until>clock_timestamp() FOR UPDATE",
                    (
                        lease.revision_id, lease.profile_id, lease.content_hash,
                        lease.owner, lease.epoch,
                    ),
                ).fetchone()
                if locked is None:
                    raise EmbeddingLeaseLost("embedding lease was lost")
                self._lock_commit_source(connection, lease)
                changed = connection.execute(
                    """
                    INSERT INTO memory_embeddings(
                      revision_id,profile_id,owner_id,scope_type,scope_id,content_hash,embedding
                    )
                    SELECT r.id,%s,e.owner_id,e.scope_type,e.scope_id,%s,%s
                    FROM memory_revisions r JOIN memory_entries e ON e.id=r.entry_id
                    WHERE r.id=%s AND r.content_hash=%s AND e.current_revision_id=r.id
                      AND EXISTS (SELECT 1 FROM embedding_profiles p WHERE p.id=%s AND p.active=true)
                    ON CONFLICT(revision_id,profile_id) DO UPDATE SET
                      content_hash=excluded.content_hash,embedding=excluded.embedding,
                      embedded_at=clock_timestamp()
                    """,
                    (
                        lease.profile_id, lease.content_hash, vector, lease.revision_id,
                        lease.content_hash, lease.profile_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise EmbeddingProtocolError("embedding job revision is no longer current")
                finished = connection.execute(
                    """
                    UPDATE embedding_jobs SET status='COMPLETED',lease_owner=NULL,lease_until=NULL,
                      updated_at=clock_timestamp()
                    WHERE revision_id=%s AND profile_id=%s AND content_hash=%s
                      AND status='RUNNING' AND lease_owner=%s AND lease_epoch=%s
                      AND lease_until>clock_timestamp()
                    """,
                    (
                        lease.revision_id, lease.profile_id, lease.content_hash,
                        lease.owner, lease.epoch,
                    ),
                ).rowcount
                if finished != 1:
                    raise EmbeddingLeaseLost("embedding lease was lost")
                self._trace(connection, "embedding.committed", {
                    "job_id": _job_id(lease.revision_id, lease.profile_id, lease.content_hash),
                    "revision_id": lease.revision_id, "profile_id": lease.profile_id,
                    "content_hash": lease.content_hash, "status": "COMPLETED",
                    "attempt": self._recorded_attempt(connection, lease), "lease_epoch": lease.epoch,
                    "lease_owner": lease.owner,
                })
        except EmbeddingLeaseLost:
            raise
        except Exception as exc:
            retryable = isinstance(exc, EmbeddingRequestError) and exc.retryable
            code = exc.kind if isinstance(exc, EmbeddingRequestError) else (
                "protocol" if isinstance(exc, EmbeddingProtocolError) else type(exc).__name__
            )
            self._fail(lease, code, retryable=retryable)

    def _fail(self, lease: EmbeddingLease, code: str, *, retryable: bool) -> None:
        with self.db.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE embedding_jobs SET status=CASE WHEN %s=false OR attempts>=max_attempts THEN 'DEAD_LETTER' ELSE 'RETRY_WAIT' END,
                  lease_owner=NULL,lease_until=NULL,available_at=CASE WHEN %s=false OR attempts>=max_attempts THEN available_at ELSE clock_timestamp()+interval '5 seconds' END,
                  last_error_code=%s,last_error_json=%s,updated_at=clock_timestamp()
                WHERE revision_id=%s AND profile_id=%s AND content_hash=%s
                  AND status='RUNNING' AND lease_owner=%s AND lease_epoch=%s
                  AND lease_until>clock_timestamp()
                """,
                (
                    retryable, retryable, code, json.dumps({"error_kind": code}),
                    lease.revision_id, lease.profile_id, lease.content_hash,
                    lease.owner, lease.epoch,
                ),
            ).rowcount
        if changed != 1:
            raise EmbeddingLeaseLost("embedding lease was lost")

    def _lease_current(self, connection, lease: EmbeddingLease) -> bool:
        row = connection.execute(
            """
            SELECT 1 FROM embedding_jobs
            WHERE revision_id=%s AND profile_id=%s AND content_hash=%s
              AND status='RUNNING' AND lease_owner=%s AND lease_epoch=%s
              AND lease_until>clock_timestamp()
            """,
            (
                lease.revision_id, lease.profile_id, lease.content_hash,
                lease.owner, lease.epoch,
            ),
        ).fetchone()
        return row is not None

    def _source_problem(self, connection, lease: EmbeddingLease) -> str | None:
        current = connection.execute(
            """
            SELECT 1 FROM memory_revisions r
            JOIN memory_entries e ON e.id=r.entry_id AND e.current_revision_id=r.id
            WHERE r.id=%s AND r.content_hash=%s
            """,
            (lease.revision_id, lease.content_hash),
        ).fetchone()
        if current is None:
            return "stale_revision"
        active = connection.execute(
            "SELECT 1 FROM embedding_profiles WHERE id=%s AND active=true",
            (lease.profile_id,),
        ).fetchone()
        if active is None:
            return "profile_inactive"
        return None

    def _lock_commit_source(self, connection, lease: EmbeddingLease) -> None:
        """Lock the entry, then the profile, and re-check both before the vector write.

        The order is memory entry, then embedding profile. Both locks stay until
        the commit transaction ends, so a wait on the existing vector row cannot
        reuse a check made before that wait. The provider call is already over.
        """
        identity = connection.execute(
            """
            SELECT e.id AS entry_id
            FROM memory_revisions r
            JOIN memory_entries e ON e.id=r.entry_id
            WHERE r.id=%s AND r.content_hash=%s
            """,
            (lease.revision_id, lease.content_hash),
        ).fetchone()
        if identity is None:
            raise EmbeddingProtocolError("embedding job revision is no longer current")
        entry = connection.execute(
            "SELECT current_revision_id FROM memory_entries WHERE id=%s FOR UPDATE",
            (identity["entry_id"],),
        ).fetchone()
        profile = connection.execute(
            "SELECT active FROM embedding_profiles WHERE id=%s FOR UPDATE",
            (lease.profile_id,),
        ).fetchone()
        current = entry is not None and entry["current_revision_id"] == lease.revision_id
        if not current or profile is None or profile["active"] is not True:
            raise EmbeddingProtocolError("embedding source changed before the vector write")

    def _recorded_attempt(self, connection, lease: EmbeddingLease) -> int:
        row = connection.execute(
            """
            SELECT attempts FROM embedding_jobs
            WHERE revision_id=%s AND profile_id=%s AND content_hash=%s
            """,
            (lease.revision_id, lease.profile_id, lease.content_hash),
        ).fetchone()
        attempt = int(row["attempts"]) if row is not None else 1
        return attempt if attempt >= 1 else 1

    def _trace(self, connection, event_type: str, data: dict) -> None:
        """Record one lifecycle fact on a turn that already cites this revision.

        A memory with no thread evidence does not get a synthetic turn or
        execution context. The existing binder uses the turn's stored context
        or records a legacy row. Callers invoke this only after the matching
        state change has succeeded in the same transaction.
        """
        linked = _linked_turn(connection, data["revision_id"])
        if linked is None:
            return
        thread_id, turn_id = linked
        from .events import ThreadEventStore
        ThreadEventStore(self.db).append(
            thread_id, turn_id, event_type, "worker", data, connection=connection,
        )


def _job_id(revision_id: str, profile_id: str, content_hash: str) -> str:
    return f"{revision_id}:{profile_id}:{content_hash}"


def _linked_turn(connection, revision_id: str) -> tuple[str, str] | None:
    for source_type, table, event_key in (
        ("thread_message", "thread_messages", "id"),
        ("thread_event", "thread_events", "event_id"),
    ):
        row = connection.execute(
            f"""
            SELECT src.thread_id, src.turn_id
            FROM memory_evidence_links l
            JOIN memory_revisions r ON r.id=l.aggregate_id
            JOIN memory_entries e ON e.id=r.entry_id
            JOIN {table} src ON l.source_type=%s AND src.{event_key}=l.source_id
            JOIN threads t ON t.id=src.thread_id AND t.owner_id=e.owner_id AND t.deleted_at IS NULL
            JOIN turns tu ON tu.id=src.turn_id AND tu.thread_id=src.thread_id
            WHERE l.aggregate_type='revision' AND l.aggregate_id=%s
            ORDER BY l.created_at, l.id
            LIMIT 1
            """,
            (source_type, revision_id),
        ).fetchone()
        if row is not None:
            return row["thread_id"], row["turn_id"]
    return None


class ManagedEmbeddingWorker:
    def __init__(self, worker: EmbeddingWorker, *, poll_interval: float = 0.25) -> None:
        self.worker = worker
        self.poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="better-agent-embedding-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        if self._stop is not None:
            self._stop.set()
        task, self._task = self._task, None
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while self._stop is not None and not self._stop.is_set():
            processed = await asyncio.to_thread(self.worker.run_once)
            if not processed:
                try:
                    await asyncio.wait_for(self._stop.wait(), self.poll_interval)
                except asyncio.TimeoutError:
                    pass
