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
                  SELECT revision_id,profile_id,content_hash FROM embedding_jobs
                  WHERE ((status IN ('QUEUED','RETRY_WAIT') AND available_at<=clock_timestamp())
                    OR (status='RUNNING' AND lease_until<=clock_timestamp()))
                  ORDER BY available_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE embedding_jobs j SET status='RUNNING',lease_owner=%s,
                  lease_epoch=j.lease_epoch+1,lease_until=clock_timestamp()+(%s*interval '1 second'),
                  attempts=j.attempts+1,updated_at=clock_timestamp()
                FROM candidate c WHERE j.revision_id=c.revision_id AND j.profile_id=c.profile_id
                  AND j.content_hash=c.content_hash
                RETURNING j.revision_id,j.profile_id,j.content_hash,j.lease_epoch
                """,
                (self.owner, lease_seconds),
            ).fetchone()
        if row is None:
            return None
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
        with self.db.connection() as connection:
            revision = connection.execute(
                "SELECT r.content,r.content_hash,p.model,p.dimensions FROM memory_revisions r "
                "JOIN memory_entries e ON e.id=r.entry_id AND e.current_revision_id=r.id "
                "JOIN embedding_profiles p ON p.id=%s AND p.active=true "
                "WHERE r.id=%s AND r.content_hash=%s",
                (lease.profile_id, lease.revision_id, lease.content_hash),
            ).fetchone()
        if revision is None:
            self._fail(lease, "stale_revision", retryable=False)
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
