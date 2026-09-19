from __future__ import annotations

from dataclasses import dataclass
import json
import os

from .db import Database


@dataclass(frozen=True)
class LeaseToken:
    job_id: str
    thread_id: str
    owner: str
    epoch: int


class LeaseLost(RuntimeError):
    pass


class DurableQueue:
    """PostgreSQL-owned turn queue with per-thread exclusion and lease fencing."""

    def __init__(self, db: Database, *, max_running: int | None = None) -> None:
        if db.backend != "postgresql":
            raise ValueError("DurableQueue requires PostgreSQL")
        self.db = db
        configured = max_running
        if configured is None:
            try:
                configured = int(os.getenv("BETTER_AGENT_TURN_WORKER_CONCURRENCY", "4"))
            except ValueError as exc:
                raise ValueError(
                    "BETTER_AGENT_TURN_WORKER_CONCURRENCY must be an integer from 1 to 16"
                ) from exc
        self.max_running = max(1, min(int(configured), 16))

    def claim_next(self, owner: str, lease_seconds: float) -> LeaseToken | None:
        with self.db.transaction() as connection:
            # Serialize only the short capacity check/claim transaction across
            # processes. Model execution remains fully concurrent.
            connection.execute("SELECT pg_advisory_xact_lock(802240917)")
            running = connection.execute(
                "SELECT COUNT(*) FROM turn_jobs WHERE status='RUNNING' "
                "AND lease_until>clock_timestamp()"
            ).fetchone()[0]
            if int(running) >= self.max_running:
                return None
            claimed_thread = connection.execute(
                """
                SELECT h.id AS thread_id
                FROM threads h
                WHERE EXISTS (
                  SELECT 1
                  FROM turn_jobs eligible
                  WHERE eligible.thread_id=h.id
                    AND (
                      (eligible.status='RUNNING'
                       AND eligible.lease_until <= clock_timestamp())
                      OR (
                        eligible.status='QUEUED'
                        AND NOT EXISTS (
                          SELECT 1 FROM turn_jobs active
                          WHERE active.thread_id=h.id AND active.status='RUNNING'
                        )
                      )
                    )
                )
                ORDER BY EXISTS (
                  SELECT 1 FROM turn_jobs expired
                  WHERE expired.thread_id=h.id AND expired.status='RUNNING'
                    AND expired.lease_until <= clock_timestamp()
                ) DESC,h.id
                FOR UPDATE OF h SKIP LOCKED
                LIMIT 1
                """
            ).fetchone()
            if claimed_thread is None:
                return None
            row = connection.execute(
                """
                WITH candidate AS (
                  SELECT j.turn_id,j.thread_id
                  FROM turn_jobs j
                  JOIN turns t ON t.id=j.turn_id AND t.thread_id=j.thread_id
                  WHERE j.thread_id=%s AND (
                    (j.status='RUNNING' AND j.lease_until <= clock_timestamp())
                    OR (
                      j.status='QUEUED'
                      AND NOT EXISTS (
                        SELECT 1 FROM turn_jobs active
                        WHERE active.thread_id=j.thread_id AND active.status='RUNNING'
                      )
                    )
                  )
                  ORDER BY (j.status='QUEUED'),j.started_at NULLS LAST,t.created_at,t.id
                  FOR UPDATE OF j SKIP LOCKED
                  LIMIT 1
                )
                UPDATE turn_jobs j
                SET status='RUNNING',lease_owner=%s,
                    lease_until=clock_timestamp()+(%s * interval '1 second'),
                    lease_epoch=j.lease_epoch+1,attempts=j.attempts+1,
                    started_at=COALESCE(j.started_at,clock_timestamp())
                FROM candidate c
                WHERE j.turn_id=c.turn_id
                RETURNING j.turn_id,j.thread_id,j.lease_epoch
                """,
                (claimed_thread["thread_id"], owner, lease_seconds),
            ).fetchone()
            if row is None:
                return None
            return LeaseToken(row["turn_id"], row["thread_id"], owner, int(row["lease_epoch"]))

    def heartbeat(self, token: LeaseToken, lease_seconds: float) -> None:
        with self.db.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE turn_jobs
                SET lease_until=clock_timestamp()+(%s * interval '1 second')
                WHERE turn_id=%s AND status='RUNNING' AND lease_owner=%s
                  AND lease_epoch=%s AND lease_until > clock_timestamp()
                """,
                (lease_seconds, token.job_id, token.owner, token.epoch),
            ).rowcount
        if changed != 1:
            raise LeaseLost(token.job_id)

    def request_cancel(self, job_id: str) -> bool:
        with self.db.transaction() as connection:
            return connection.execute(
                """
                UPDATE turn_jobs
                SET cancel_requested_at=clock_timestamp(),
                    status=CASE WHEN status='QUEUED' THEN 'CANCELLED' ELSE status END,
                    finished_at=CASE WHEN status='QUEUED' THEN clock_timestamp() ELSE finished_at END
                WHERE turn_id=%s AND status IN ('QUEUED','RUNNING')
                  AND cancel_requested_at IS NULL
                """,
                (job_id,),
            ).rowcount == 1

    def fail(self, token: LeaseToken, error_code: str) -> None:
        with self.db.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE turn_jobs SET status='FAILED',lease_owner=NULL,lease_until=NULL,
                  finished_at=clock_timestamp(),last_error_json=%s
                WHERE turn_id=%s AND status='RUNNING' AND lease_owner=%s
                  AND lease_epoch=%s AND lease_until>clock_timestamp()
                """,
                (json.dumps({"error_code": error_code[:100]}, ensure_ascii=False),
                 token.job_id, token.owner, token.epoch),
            ).rowcount
        if changed != 1:
            raise LeaseLost(token.job_id)

    def recover_expired(self) -> int:
        with self.db.transaction() as connection:
            return connection.execute(
                """
                UPDATE turn_jobs SET status='QUEUED',lease_owner=NULL,lease_until=NULL,
                  lease_epoch=lease_epoch+1
                WHERE status='RUNNING' AND lease_until<=clock_timestamp()
                """
            ).rowcount

    def finish(self, token: LeaseToken, status: str = "COMPLETED") -> None:
        if status not in {"COMPLETED", "FAILED", "CANCELLED"}:
            raise ValueError("invalid terminal status")
        with self.db.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE turn_jobs SET status=%s,lease_owner=NULL,lease_until=NULL,
                    finished_at=clock_timestamp()
                WHERE turn_id=%s AND status='RUNNING' AND lease_owner=%s
                  AND lease_epoch=%s AND lease_until > clock_timestamp()
                """,
                (status, token.job_id, token.owner, token.epoch),
            ).rowcount
        if changed != 1:
            raise LeaseLost(token.job_id)
