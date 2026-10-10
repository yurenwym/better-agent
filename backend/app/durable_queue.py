from __future__ import annotations

from dataclasses import dataclass
import json
import os

from .db import Database
from .task_runtime import TaskKind, TaskRef, TaskRuntime, LeaseToken as TaskLeaseToken, LeaseLost


@dataclass(frozen=True)
class LeaseToken:
    job_id: str
    thread_id: str
    owner: str
    epoch: int

    def task_token(self) -> TaskLeaseToken:
        """Keep queue routing metadata outside the shared execution token."""
        return TaskLeaseToken(TaskRef(TaskKind.TURN, self.job_id), self.owner, self.epoch)


class DurableQueue:
    """PostgreSQL-owned turn queue with per-thread exclusion and lease fencing."""

    def __init__(self, db: Database, *, max_running: int | None = None) -> None:
        if db.backend != "postgresql":
            raise ValueError("DurableQueue requires PostgreSQL")
        self.db = db
        self.tasks = TaskRuntime(db)
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
                    AND NOT EXISTS (
                      SELECT 1 FROM turn_jobs waiting
                      JOIN turns earlier ON earlier.id=waiting.turn_id
                      JOIN turns current ON current.id=eligible.turn_id
                      WHERE waiting.thread_id=h.id AND waiting.status='QUEUED'
                        AND waiting.archive_job_id IS NOT NULL
                        AND (earlier.created_at < current.created_at
                          OR (earlier.created_at=current.created_at AND earlier.id < current.id))
                    )
                    AND (eligible.archive_job_id IS NULL OR eligible.cancel_requested_at IS NOT NULL
                      OR eligible.archive_wait_until <= clock_timestamp() OR EXISTS (
                        SELECT 1 FROM memory_archive_jobs a WHERE a.id=eligible.archive_job_id
                          AND a.status IN ('COMPLETED','DEAD_LETTER','LEASE_LOST')
                      ))
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
                SELECT j.turn_id,j.thread_id
                FROM turn_jobs j
                JOIN turns t ON t.id=j.turn_id AND t.thread_id=j.thread_id
                WHERE j.thread_id=? AND NOT EXISTS (
                  SELECT 1 FROM turn_jobs waiting JOIN turns earlier ON earlier.id=waiting.turn_id
                  WHERE waiting.thread_id=j.thread_id AND waiting.status='QUEUED'
                    AND waiting.archive_job_id IS NOT NULL
                    AND (earlier.created_at < t.created_at
                      OR (earlier.created_at=t.created_at AND earlier.id < t.id))
                ) AND (j.archive_job_id IS NULL OR j.cancel_requested_at IS NOT NULL
                  OR j.archive_wait_until <= clock_timestamp() OR EXISTS (
                    SELECT 1 FROM memory_archive_jobs a WHERE a.id=j.archive_job_id
                      AND a.status IN ('COMPLETED','DEAD_LETTER','LEASE_LOST')
                  )) AND (
                  (j.status='RUNNING' AND j.lease_until <= clock_timestamp())
                  OR (j.status='QUEUED' AND NOT EXISTS (
                    SELECT 1 FROM turn_jobs active
                    WHERE active.thread_id=j.thread_id AND active.status='RUNNING'
                  ))
                )
                ORDER BY (j.status='QUEUED'),j.started_at NULLS LAST,t.created_at,t.id
                FOR UPDATE OF j SKIP LOCKED LIMIT 1
                """, (claimed_thread["thread_id"],),
            ).fetchone()
            if row is None:
                return None
            attempt = self.tasks.claim(connection, TaskRef(TaskKind.TURN, row["turn_id"]), owner, lease_seconds)
            if attempt is None:
                return None
            connection.execute(
                "UPDATE turn_jobs SET started_at=COALESCE(started_at,clock_timestamp()) WHERE turn_id=?",
                (row["turn_id"],),
            )
            return LeaseToken(row["turn_id"], row["thread_id"], owner, attempt.token.epoch)

    def heartbeat(self, token: LeaseToken, lease_seconds: float) -> None:
        with self.db.transaction() as connection:
            self.tasks.heartbeat(connection, token.task_token(), lease_seconds)

    def request_cancel(self, job_id: str) -> bool:
        with self.db.transaction() as connection:
            if not self.tasks.request_cancel(connection, TaskRef(TaskKind.TURN, job_id)):
                return False
            connection.execute(
                "UPDATE turn_jobs SET status='CANCELLED',finished_at=clock_timestamp() "
                "WHERE turn_id=? AND status='QUEUED'", (job_id,),
            )
            return True

    def fail(self, token: LeaseToken, error_code: str) -> None:
        with self.db.transaction() as connection:
            self.tasks.finish(connection, token.task_token(), "FAILED")
            connection.execute(
                "UPDATE turn_jobs SET finished_at=clock_timestamp(),last_error_json=? WHERE turn_id=?",
                (json.dumps({"error_code": error_code[:100]}, ensure_ascii=False), token.job_id),
            )

    def recover_expired(self) -> int:
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT turn_id FROM turn_jobs WHERE status='RUNNING' AND lease_until<=clock_timestamp() "
                "FOR UPDATE SKIP LOCKED"
            ).fetchall()
            return sum(self.tasks.recover_expired(connection, TaskRef(TaskKind.TURN, row["turn_id"]), "QUEUED")
                       for row in rows)

    def finish(self, token: LeaseToken, status: str = "COMPLETED") -> None:
        with self.db.transaction() as connection:
            self.tasks.finish(connection, token.task_token(), status)
            connection.execute("UPDATE turn_jobs SET finished_at=clock_timestamp() WHERE turn_id=?", (token.job_id,))
