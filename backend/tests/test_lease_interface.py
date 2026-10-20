"""W01: observable lease behavior, plus the archive worker's own fence.

TaskRuntime is the shared contract for task rows. Archive, embedding,
learning and notification are not forced through it; their own tests cover
the verbs they actually have.
"""
import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import pg_fixtures

from app.conversation import ConversationService
from app.db import Database
from app.memory_archive import ArchiveLeaseLost, ConversationArchiver, ManagedArchiveWorker
from app.research.service import ResearchService
from app.task_runtime import LeaseLost, LeaseRuntime, TaskCancelled, TaskKind, TaskRef, TaskRuntime
from pg02_helpers import database, thread_with_turns


async def _summary(payload):
    ids = [event["message_id"] for turn in payload["turns"] for event in turn["events"] if event["message_id"]]
    return {
        "synopsis": [{"text": "s", "source_message_ids": [ids[0]]}],
        "topics": [], "decisions": [], "outcomes": [], "open_loops": [], "sensitivity": "normal",
    }


def test_task_runtime_satisfies_the_shared_lease_protocol(database, tmp_path):
    runtime = TaskRuntime(database)
    assert isinstance(runtime, LeaseRuntime)


def _research_task(database):
    conversation = ConversationService(database)
    research = ResearchService(database, conversation.events)
    job = research.create_manual(conversation.create_thread().id, "topic", "one", ("web",))
    return database, research, job, TaskRuntime(database), TaskRef(TaskKind.RESEARCH, job.id)


def _row(database, job_id):
    with database.connection() as connection:
        return connection.execute("SELECT * FROM research_jobs WHERE id=?", (job_id,)).fetchone()


def _expire(database, job_id):
    with database.transaction() as connection:
        connection.execute(
            "UPDATE research_jobs SET lease_until=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job_id),
        )


def test_only_one_worker_holds_a_live_claim(database, tmp_path):
    database, _, job, runtime, ref = _research_task(database)
    try:
        with database.transaction() as connection:
            first = runtime.claim(connection, ref, "worker-a", 30)
            assert first is not None
            assert runtime.claim(connection, ref, "worker-b", 30) is None
        row = _row(database, job.id)
        assert row["lease_owner"] == "worker-a"
        assert row["status"] == "RUNNING"
        with database.connection() as connection:
            owner = connection.execute(
                "SELECT owner_id FROM threads WHERE id=?", (job.thread_id,),
            ).fetchone()["owner_id"]
        assert owner == "local-user"
        assert owner != row["lease_owner"]
    finally:
        database.close()


def test_expired_lease_recovers_once_and_rejects_the_old_epoch(database, tmp_path):
    database, research, job, runtime, ref = _research_task(database)
    try:
        with database.transaction() as connection:
            first = runtime.claim(connection, ref, "worker-a", 30)
        _expire(database, job.id)
        with database.transaction() as connection:
            assert runtime.recover_expired(connection, ref, "FAILED") is True
            assert runtime.recover_expired(connection, ref, "FAILED") is False
            with pytest.raises(LeaseLost):
                runtime.heartbeat(connection, first.token, 30)
            with pytest.raises(LeaseLost):
                runtime.finish(connection, first.token, "COMPLETED")
        assert research.get(job.id).status == "FAILED"
        assert _row(database, job.id)["lease_owner"] is None
    finally:
        database.close()


def test_reclaim_after_expiry_fences_the_previous_epoch(database, tmp_path):
    database, research, job, runtime, ref = _research_task(database)
    try:
        with database.transaction() as connection:
            first = runtime.claim(connection, ref, "worker-a", 30)
        _expire(database, job.id)
        with database.transaction() as connection:
            second = runtime.claim(connection, ref, "worker-a", 30)
            assert second.token.epoch == first.token.epoch + 1
            with pytest.raises(LeaseLost):
                runtime.finish(connection, first.token, "COMPLETED")
            runtime.finish(connection, second.token, "COMPLETED")
            with pytest.raises(LeaseLost):
                runtime.finish(connection, second.token, "COMPLETED")
        assert research.get(job.id).status == "COMPLETED"
        assert _row(database, job.id)["lease_owner"] is None
    finally:
        database.close()


def test_cancel_request_blocks_completion_once(database, tmp_path):
    database, research, job, runtime, ref = _research_task(database)
    try:
        with database.transaction() as connection:
            attempt = runtime.claim(connection, ref, "worker-a", 30)
            assert runtime.request_cancel(connection, ref) is True
            assert runtime.request_cancel(connection, ref) is False
            assert connection.execute(
                "SELECT status, lease_epoch FROM research_jobs WHERE id=?", (job.id,),
            ).fetchone()["status"] == "RUNNING"
            with pytest.raises(TaskCancelled):
                runtime.finish(connection, attempt.token, "COMPLETED")
            runtime.finish(connection, attempt.token, "CANCELLED")
            with pytest.raises(LeaseLost):
                runtime.finish(connection, attempt.token, "CANCELLED")
        assert research.get(job.id).status == "CANCELLED"
    finally:
        database.close()


def test_archive_exposes_the_shared_lease_vocabulary(database, tmp_path):
    _, _, archiver = thread_with_turns(database, tmp_path, [400] * 4)
    for verb in ("claim", "heartbeat", "require_lease", "recover_expired"):
        assert callable(getattr(archiver, verb))


@pytest.mark.asyncio
async def test_lease_loss_blocks_heartbeat_and_commit(database, tmp_path):
    db, thread, archiver = thread_with_turns(database, tmp_path, [400] * 4)
    archiver.summarizer = _summary
    archiver.keep_tokens = 1
    events: list[dict] = []
    archiver.on_lifecycle = events.append

    job = archiver.enqueue(thread.id)
    claim = archiver.claim(job)
    assert claim is not None
    archiver.heartbeat(claim)
    archiver.require_lease(claim)

    # The lease is fenced away by another claim/epoch.
    with db.transaction() as connection:
        connection.execute("UPDATE memory_archive_jobs SET lease_epoch=lease_epoch+1 WHERE id=?", (job,))
    with pytest.raises(ArchiveLeaseLost):
        archiver.require_lease(claim)
    with pytest.raises(ArchiveLeaseLost):
        archiver.heartbeat(claim)
    with pytest.raises(ArchiveLeaseLost):
        await archiver.process(claim)
    # The observed lifecycle reports the claim and heartbeat, never a commit.
    reported = [event["event"] for event in events]
    assert "claimed" in reported and "heartbeat" in reported and "committed" not in reported


@pytest.mark.asyncio
async def test_recovery_is_idempotent_and_reports_its_events(database, tmp_path):
    db, thread, archiver = thread_with_turns(database, tmp_path, [400] * 4)
    archiver.summarizer = _summary
    archiver.keep_tokens = 1
    events: list[dict] = []
    archiver.on_lifecycle = events.append

    job = archiver.enqueue(thread.id)
    first = await archiver.process(archiver.claim(job))
    assert first is not None
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 1
        coverage = connection.execute(
            "SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?", (thread.id,),
        ).fetchone()[0]
    assert coverage > 0

    # Replaying the same immutable input after recovery must not duplicate the
    # episode: make the job claimable again, re-claim it and commit again.
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE memory_archive_jobs SET status='QUEUED',available_at=?,attempts=1 WHERE id=?", (past, job),
        )
    replay_claim = archiver.claim(job)
    assert replay_claim is not None
    replay = await archiver.process(replay_claim)
    assert replay is not None and archiver.claim(job) is None
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 1

    # An expired lease is recovered exactly once and reported.
    with db.transaction() as connection:
        connection.execute(
            "UPDATE memory_archive_jobs SET status='RUNNING',lease_owner='other',"
            "lease_until=?,attempts=1 WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), job),
        )
    assert archiver.recover_expired() == 1
    assert archiver.recover_expired() == 0
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM memory_archive_jobs WHERE id=?", (job,)).fetchone()[0] == "RETRY_WAIT"
    assert any(event["event"] == "recovered" for event in events)


def _past():
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()


def _insert_running_job(db, thread, job_id, *, attempts, max_attempts, lease_until):
    past = _past()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO memory_archive_jobs(id,owner_id,thread_id,start_message_seq,end_message_seq,source_hash,"
            "prompt_version,tokenizer_version,status,available_at,attempts,max_attempts,lease_owner,lease_epoch,"
            "lease_until,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'RUNNING',?,?,?,?,?,?,?,?)",
            (job_id, "owner-a", thread.id, 1, 2, "sha256:x", "episode-v4", "utf8-upper-bound-v1",
             past, attempts, max_attempts, "worker-x", 1, lease_until, past, past),
        )


def test_recover_expired_retryable_keeps_coverage_and_resets_state(database, tmp_path):
    db, thread, archiver = thread_with_turns(database, tmp_path, [400] * 4)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO conversation_archive_state(owner_id,thread_id,archived_through_seq,state,lease_owner,lease_until) "
            "VALUES (?,?,?,?,?,?)", ("owner-a", thread.id, 0, "RESERVED", "worker-x", _past()),
        )
    _insert_running_job(db, thread, "job-retry", attempts=1, max_attempts=3, lease_until=_past())
    assert archiver.recover_expired() == 1
    with db.connection() as connection:
        job = connection.execute("SELECT * FROM memory_archive_jobs WHERE id='job-retry'").fetchone()
        state = connection.execute(
            "SELECT archived_through_seq,state,lease_owner FROM conversation_archive_state WHERE owner_id='owner-a' AND thread_id=?",
            (thread.id,),
        ).fetchone()
    assert (job["status"], job["last_error_code"], job["finished_at"]) == ("RETRY_WAIT", "lease_expired", None)
    assert job["lease_epoch"] == 2 and job["lease_owner"] is None
    # Recovery must not advance the committed coverage cursor.
    assert state["archived_through_seq"] == 0 and state["state"] == "IDLE" and state["lease_owner"] is None


def test_recover_expired_after_max_attempts_dead_letters(database, tmp_path):
    db, thread, archiver = thread_with_turns(database, tmp_path, [400] * 4)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO conversation_archive_state(owner_id,thread_id,archived_through_seq,state,lease_owner,lease_until) "
            "VALUES (?,?,?,?,?,?)", ("owner-a", thread.id, 0, "RESERVED", "worker-x", _past()),
        )
    _insert_running_job(db, thread, "job-dead", attempts=3, max_attempts=3, lease_until=_past())
    assert archiver.recover_expired() == 1
    with db.connection() as connection:
        job = connection.execute("SELECT * FROM memory_archive_jobs WHERE id='job-dead'").fetchone()
        state = connection.execute(
            "SELECT archived_through_seq,state,error FROM conversation_archive_state WHERE owner_id='owner-a' AND thread_id=?",
            (thread.id,),
        ).fetchone()
    assert job["status"] == "DEAD_LETTER" and job["finished_at"] and job["last_error_code"] == "lease_expired"
    assert state["state"] == "FAILED" and state["error"] == "lease_expired" and state["archived_through_seq"] == 0
    # The status view exposes the error code, finish time and current status.
    view = next(item for item in archiver.status(thread.id, "owner-a")["jobs"] if item["id"] == "job-dead")
    assert view["status"] == "DEAD_LETTER" and view["last_error_code"] == "lease_expired" and view["finished_at"]


@pytest.mark.asyncio
async def test_worker_reclaims_an_expired_lease_and_fences_the_old_worker(database, tmp_path):
    db, thread, archiver = thread_with_turns(database, tmp_path, [400] * 4)
    archiver.summarizer = _summary
    archiver.keep_tokens = 1
    job = archiver.enqueue(thread.id)
    old = archiver.claim(job)
    assert old is not None
    # Worker A stops; its lease expires.
    with db.transaction() as connection:
        connection.execute("UPDATE memory_archive_jobs SET lease_until=? WHERE id=?", (_past(), job))
    worker_b = ConversationArchiver(db, archiver.store, _summary, keep_tokens=1, worker_id="worker-b")
    assert worker_b.recover_expired() == 1
    fresh = worker_b.claim(job)
    assert fresh is not None and fresh.lease_epoch > old.lease_epoch
    with pytest.raises(ArchiveLeaseLost):
        archiver.require_lease(old)
    with pytest.raises(ArchiveLeaseLost):
        archiver.heartbeat(old)
    assert await worker_b.process(fresh) is not None


def test_worker_recovery_failure_is_never_fatal(database, tmp_path):
    _, _, archiver = thread_with_turns(database, tmp_path, [400])
    worker = ManagedArchiveWorker(archiver)
    def boom():
        raise RuntimeError("recovery unavailable")
    archiver.recover_expired = boom
    assert worker._recover() == 0


def test_two_connections_allow_one_live_claim_and_fence_the_old_owner(database, tmp_path):
    """Two distinct Database pools; B waits in a worker thread, then loses the claim."""
    second = Database(database.database_url, workspace=tmp_path / "second")
    tranche = None
    worker = None
    probe = None
    try:
        conversation = ConversationService(database)
        research = ResearchService(database, conversation.events)
        job = research.create_manual(conversation.create_thread().id, "topic", "one", ("web",))
        runtime_one = TaskRuntime(database)
        runtime_two = TaskRuntime(second)
        ref = TaskRef(TaskKind.RESEARCH, job.id)

        tranche = database.transaction()
        connection_a = tranche.__enter__()
        connection_a.execute("SET LOCAL lock_timeout='8s'")
        connection_a.execute("SET LOCAL statement_timeout='15s'")
        first = runtime_one.claim(connection_a, ref, "worker-a", 30)
        assert first is not None
        backend_a = connection_a.execute("SELECT pg_backend_pid()").fetchone()[0]

        started = threading.Event()
        result: dict[str, object] = {}

        def claim_from_b():
            started.set()
            try:
                with second.transaction() as connection_b:
                    connection_b.execute("SET LOCAL lock_timeout='8s'")
                    connection_b.execute("SET LOCAL statement_timeout='15s'")
                    result["claim"] = runtime_two.claim(connection_b, ref, "worker-b", 30)
            except BaseException as exc:
                result["error"] = exc

        worker = threading.Thread(target=claim_from_b, daemon=True)
        worker.start()
        assert started.wait(timeout=5)

        # Prove B is really waiting on A's row lock via pg_stat_activity.
        probe = pg_fixtures._connect(database.database_url, autocommit=True)
        deadline = time.monotonic() + 5
        waiting = False
        while time.monotonic() < deadline:
            waiting = probe.execute(
                "SELECT 1 FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid)) LIMIT 1",
                (backend_a,),
            ).fetchone() is not None
            if waiting:
                break
            time.sleep(0.02)
        assert waiting, "connection B never waited on connection A's row lock"

        # A commits; B acquires the lock, sees the live lease, and returns None.
        tranche.__exit__(None, None, None)
        tranche = None
        worker.join(timeout=10)
        assert not worker.is_alive()
        if "error" in result:
            raise result["error"]
        assert result["claim"] is None

        _expire(database, job.id)
        with second.transaction() as connection_b:
            connection_b.execute("SET LOCAL lock_timeout='8s'")
            connection_b.execute("SET LOCAL statement_timeout='15s'")
            takeover = runtime_two.claim(connection_b, ref, "worker-b", 30)
            assert takeover is not None
            assert takeover.token.epoch == first.token.epoch + 1

        with database.transaction() as connection_a:
            connection_a.execute("SET LOCAL lock_timeout='8s'")
            with pytest.raises(LeaseLost):
                runtime_one.heartbeat(connection_a, first.token, 30)
            with pytest.raises(LeaseLost):
                runtime_one.finish(connection_a, first.token, "COMPLETED")

        with second.transaction() as connection_b:
            connection_b.execute("SET LOCAL lock_timeout='8s'")
            runtime_two.finish(connection_b, takeover.token, "COMPLETED")
        assert research.get(job.id).status == "COMPLETED"
    finally:
        if tranche is not None:
            tranche.__exit__(None, None, None)
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
        if probe is not None:
            probe.close()
        second.close()
