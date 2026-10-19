"""W02: archive takeover and recovery races on an isolated PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from app.conversation import ConversationService
from app.db import Database
from app.memory_archive import ArchiveLeaseLost, ConversationArchiver
from app.memory_v2 import MemoryStore
from app.trace_query import TraceQuery


async def _summary(payload):
    ids = [event["message_id"] for turn in payload["turns"] for event in turn["events"] if event["message_id"]]
    return {
        "synopsis": [{"text": "s", "source_message_ids": [ids[0]]}],
        "topics": [], "decisions": [], "outcomes": [], "open_loops": [], "sensitivity": "normal",
    }


def _past():
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()


def _setup(url, tmp_path, *, max_attempts=3):
    database = Database(url, workspace=tmp_path / "workspace")
    conversation = ConversationService(database)
    thread = conversation.create_thread("archive")
    turn = conversation.accept_turn(thread.id, "one", "u" * 2000, [])
    with database.transaction() as connection:
        connection.execute("UPDATE turns SET status='COMPLETED' WHERE id=?", (turn.turn_id,))
    archiver = ConversationArchiver(
        database, MemoryStore(database, tmp_path / "memory"), _summary,
        keep_messages=0, max_attempts=max_attempts, worker_id="worker-a",
    )
    job = archiver.enqueue(thread.id, source_turn_id=turn.turn_id)
    return database, thread, archiver, job


def _coverage(database, thread_id):
    with database.connection() as connection:
        row = connection.execute(
            "SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
    return int(row["archived_through_seq"] or 0)


@pytest.mark.asyncio
async def test_postgres_takeover_rejects_the_old_worker(migrated_postgres_url, tmp_path):
    database, thread, worker_a, job = _setup(migrated_postgres_url, tmp_path)
    try:
        old = worker_a.claim(job)
        assert old is not None
        with database.transaction() as connection:
            connection.execute("UPDATE memory_archive_jobs SET lease_until=? WHERE id=?", (_past(), job))
        worker_b = ConversationArchiver(
            database, worker_a.store, _summary, keep_messages=0, worker_id="worker-b",
        )
        assert worker_b.recover_expired() == 1
        assert _coverage(database, thread.id) == 0
        fresh = worker_b.claim(job)
        assert fresh is not None and fresh.lease_epoch > old.lease_epoch
        with pytest.raises(ArchiveLeaseLost):
            worker_a.heartbeat(old)
        with pytest.raises(ArchiveLeaseLost):
            await worker_a.process(old)
        assert _coverage(database, thread.id) == 0
        assert await worker_b.process(fresh) is not None
        assert _coverage(database, thread.id) > 0
        events = TraceQuery(database).by_thread(thread.id, owner_id="local-user")["events"]
        committed = [event for event in events if event["type"] == "archive.committed"]
        assert len(committed) == 1
        assert committed[0]["payload"]["lease_epoch"] == fresh.lease_epoch
    finally:
        database.close()


@pytest.mark.asyncio
async def test_postgres_expired_lease_cannot_renew_commit_or_fail(migrated_postgres_url, tmp_path):
    database, thread, archiver, job = _setup(migrated_postgres_url, tmp_path)
    try:
        claim = archiver.claim(job)
        assert claim is not None
        archiver.heartbeat(claim)
        expired = _past()
        with database.transaction() as connection:
            connection.execute("UPDATE memory_archive_jobs SET lease_until=? WHERE id=?", (expired, job))
        with pytest.raises(ArchiveLeaseLost):
            archiver.require_lease(claim)
        with pytest.raises(ArchiveLeaseLost):
            archiver.heartbeat(claim)
        transcript = archiver.transcripts.build(
            claim.thread_id, expected_owner_id=claim.owner_id,
            after_sequence=claim.start_sequence - 1, through_sequence=claim.end_sequence,
        )
        summary = await _summary(archiver._input_payload(transcript))
        with pytest.raises(ArchiveLeaseLost):
            archiver._commit(claim, transcript, summary)
        archiver._fail(claim, RuntimeError("summarizer failed after the lease expired"))
        with database.connection() as connection:
            row = connection.execute("SELECT * FROM memory_archive_jobs WHERE id=?", (job,)).fetchone()
            episodes = connection.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0]
            state = connection.execute(
                "SELECT state,error FROM conversation_archive_state WHERE thread_id=?", (thread.id,),
            ).fetchone()
        assert row["status"] == "RUNNING"
        assert row["lease_epoch"] == claim.lease_epoch
        assert row["lease_owner"] == claim.lease_owner
        assert row["lease_until"] == expired
        assert row["last_error_code"] is None
        assert episodes == 0 and _coverage(database, thread.id) == 0
        assert state["error"] is None
        kinds = [event["type"] for event in TraceQuery(database).by_thread(thread.id, owner_id="local-user")["events"]]
        assert "archive.committed" not in kinds and "archive.failed" not in kinds
    finally:
        database.close()


def test_postgres_dual_recovery_dead_letters_once(migrated_postgres_url, tmp_path):
    database, thread, archiver, job = _setup(migrated_postgres_url, tmp_path, max_attempts=1)
    try:
        assert archiver.claim(job) is not None
        with database.transaction() as connection:
            connection.execute("UPDATE memory_archive_jobs SET lease_until=? WHERE id=?", (_past(), job))
        barrier = Barrier(2)

        def recover(_):
            barrier.wait(timeout=10)
            return archiver.recover_expired()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(recover, range(2)))
        assert sorted(results) == [0, 1]
        with database.connection() as connection:
            row = connection.execute("SELECT status,finished_at,updated_at FROM memory_archive_jobs WHERE id=?", (job,)).fetchone()
        assert row["status"] == "DEAD_LETTER"
        assert row["finished_at"] and row["finished_at"] == row["updated_at"]
        assert _coverage(database, thread.id) == 0
        recovered = [
            event for event in TraceQuery(database).by_thread(thread.id, owner_id="local-user")["events"]
            if event["type"] == "archive.recovered"
        ]
        assert len(recovered) == 1
        assert recovered[0]["payload"]["status"] == "DEAD_LETTER"
        assert recovered[0]["outcome"]["error"]["retryable"] is False
    finally:
        database.close()
