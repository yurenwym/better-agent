"""W02: archive recovery stays fenced, and the real transitions are in the trace."""
from datetime import datetime, timedelta, timezone

import pytest

from app.conversation import ConversationService
from app.events import ThreadEventStore
from app.memory_archive import ArchiveLeaseLost, ConversationArchiver
from app.memory_v2 import MemoryStore
from app.trace_query import TraceQuery
from pg02_helpers import database


async def _summary(payload):
    ids = [event["message_id"] for turn in payload["turns"] for event in turn["events"] if event["message_id"]]
    return {
        "synopsis": [{"text": "s", "source_message_ids": [ids[0]]}],
        "topics": [], "decisions": [], "outcomes": [], "open_loops": [], "sensitivity": "normal",
    }


def _past():
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()


def _archiver(database, tmp_path):
    conversation = ConversationService(database)
    thread = conversation.create_thread("archive")
    turn = conversation.accept_turn(thread.id, "one", "u" * 2000, [])
    with database.transaction() as connection:
        connection.execute("UPDATE turns SET status='COMPLETED' WHERE id=?", (turn.turn_id,))
    archiver = ConversationArchiver(
        database, MemoryStore(database, tmp_path / "memory"), _summary, keep_messages=0,
    )
    return database, conversation, thread, turn, archiver


def _coverage(database, thread_id):
    with database.connection() as connection:
        row = connection.execute(
            "SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
    return int(row["archived_through_seq"]) if row and row["archived_through_seq"] is not None else 0


def _types(database, thread_id):
    view = TraceQuery(database).by_thread(thread_id, owner_id="local-user")
    return view, [event["type"] for event in view["events"]]


@pytest.mark.asyncio
async def test_recovery_and_commit_are_visible_without_moving_the_cursor_early(database, tmp_path):
    database, _, thread, turn, archiver = _archiver(database, tmp_path)
    try:
        job = archiver.enqueue(thread.id, source_turn_id=turn.turn_id)
        assert job is not None
        claim = archiver.claim(job)
        assert claim is not None
        with database.transaction() as connection:
            connection.execute("UPDATE memory_archive_jobs SET lease_until=? WHERE id=?", (_past(), job))
        assert archiver.recover_expired() == 1
        assert _coverage(database, thread.id) == 0
        view, kinds = _types(database, thread.id)
        assert "archive" in view["sources"]["connected"]
        assert "archive" not in view["sources"]["deferred"]
        assert ["archive.queued", "archive.claimed", "archive.recovered"] == [
            kind for kind in kinds if kind.startswith("archive.")
        ]
        recovered = next(event for event in view["events"] if event["type"] == "archive.recovered")
        assert recovered["source"] == "archive"
        assert recovered["outcome"]["status"] == "failed"
        assert recovered["outcome"]["error"]["retryable"] is True
        assert recovered["context"]["context"]["turn_id"] == turn.turn_id
        fresh = archiver.claim(job)
        assert fresh is not None and fresh.lease_epoch > claim.lease_epoch
        with pytest.raises(ArchiveLeaseLost):
            archiver.heartbeat(claim)
        with pytest.raises(ArchiveLeaseLost):
            await archiver.process(claim)
        assert _coverage(database, thread.id) == 0
        assert "archive.committed" not in _types(database, thread.id)[1]
        assert await archiver.process(fresh) is not None
        assert _coverage(database, thread.id) > 0
        committed = [event for event in _types(database, thread.id)[0]["events"] if event["type"] == "archive.committed"]
        assert len(committed) == 1
        assert committed[0]["outcome"]["status"] == "completed"
        assert committed[0]["payload"]["coverage_through"] == fresh.end_sequence
        assert archiver.claim(job) is None
    finally:
        database.close()


@pytest.mark.asyncio
async def test_changed_source_version_cannot_commit_or_advance_coverage(database, tmp_path):
    database, _, thread, turn, archiver = _archiver(database, tmp_path)
    try:
        job = archiver.enqueue(thread.id, source_turn_id=turn.turn_id)
        claim = archiver.claim(job)
        with database.transaction() as connection:
            connection.execute("UPDATE memory_archive_jobs SET prompt_version='other' WHERE id=?", (job,))
        with pytest.raises(ArchiveLeaseLost):
            archiver.heartbeat(claim)
        with pytest.raises(ArchiveLeaseLost):
            await archiver.process(claim)
        assert _coverage(database, thread.id) == 0
        assert "archive.committed" not in _types(database, thread.id)[1]
    finally:
        database.close()


def _job_row(database, job_id):
    with database.connection() as connection:
        return connection.execute("SELECT * FROM memory_archive_jobs WHERE id=?", (job_id,)).fetchone()


def _episode_count(database):
    with database.connection() as connection:
        return connection.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0]


@pytest.mark.asyncio
async def test_expired_lease_cannot_renew_commit_or_fail_before_recovery(database, tmp_path):
    database, _, thread, turn, archiver = _archiver(database, tmp_path)
    try:
        job = archiver.enqueue(thread.id, source_turn_id=turn.turn_id)
        claim = archiver.claim(job)
        archiver.heartbeat(claim)
        renewed = _job_row(database, job)
        assert renewed["lease_until"] > _past() and renewed["lease_epoch"] == claim.lease_epoch
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
        row = _job_row(database, job)
        assert row["status"] == "RUNNING"
        assert row["lease_epoch"] == claim.lease_epoch
        assert row["lease_owner"] == claim.lease_owner
        assert row["lease_until"] == expired
        assert row["last_error_code"] is None
        assert _coverage(database, thread.id) == 0
        assert _episode_count(database) == 0
        kinds = _types(database, thread.id)[1]
        assert "archive.committed" not in kinds and "archive.failed" not in kinds
        with database.connection() as connection:
            state = connection.execute(
                "SELECT state,error FROM conversation_archive_state WHERE thread_id=?", (thread.id,),
            ).fetchone()
        assert state["error"] is None
    finally:
        database.close()


def test_recovery_event_failure_keeps_the_expired_lease(database, tmp_path):
    database, _, thread, turn, archiver = _archiver(database, tmp_path)
    try:
        job = archiver.enqueue(thread.id, source_turn_id=turn.turn_id)
        archiver.claim(job)
        with database.transaction() as connection:
            connection.execute("UPDATE memory_archive_jobs SET lease_until=? WHERE id=?", (_past(), job))
        original = ThreadEventStore.append

        def fail_recovered(self, thread_id, turn_id, event_type, *args, **kwargs):
            if event_type == "archive.recovered":
                raise RuntimeError("event store unavailable")
            return original(self, thread_id, turn_id, event_type, *args, **kwargs)

        ThreadEventStore.append = fail_recovered
        try:
            with pytest.raises(RuntimeError, match="event store unavailable"):
                archiver.recover_expired()
        finally:
            ThreadEventStore.append = original
        with database.connection() as connection:
            row = connection.execute("SELECT status,lease_owner FROM memory_archive_jobs WHERE id=?", (job,)).fetchone()
        assert row["status"] == "RUNNING" and row["lease_owner"]
        assert "archive.recovered" not in _types(database, thread.id)[1]
        assert archiver.recover_expired() == 1
    finally:
        database.close()
