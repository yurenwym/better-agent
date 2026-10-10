from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.conversation import ConversationService
from app.db import Database
from app.research.service import ResearchService
from app.task_runtime import LeaseLost, TaskKind, TaskRef, TaskRuntime


@pytest.fixture
def research(migrated_postgres_url, tmp_path):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    conversation = ConversationService(db)
    service = ResearchService(db, conversation.events)
    job = service.create_manual(conversation.create_thread().id, "topic", "one", ("web",))
    try:
        yield db, service, job
    finally:
        db.close()


def test_claim_race_and_same_owner_takeover(research):
    db, service, job = research
    runtime = TaskRuntime(db)
    ref = TaskRef(TaskKind.RESEARCH, job.id)
    barrier = Barrier(2)

    def claim(_):
        barrier.wait(timeout=10)
        with db.transaction() as connection:
            return runtime.claim(connection, ref, "worker", 60)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, range(2)))
    first, = [claim for claim in claims if claim]
    with db.transaction() as connection:
        connection.execute("UPDATE research_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=?", (job.id,))
    with db.transaction() as connection:
        with pytest.raises(LeaseLost):
            runtime.heartbeat(connection, first.token, 60)
        second = runtime.claim(connection, ref, "worker", 60)
        assert second.token.epoch == first.token.epoch + 1
        with pytest.raises(LeaseLost):
            runtime.finish(connection, first.token, "COMPLETED")
    assert service.get(job.id).attempts == 2


def test_claim_enforces_schedule_and_attempt_limit(research):
    db, _, job = research
    runtime = TaskRuntime(db)
    ref = TaskRef(TaskKind.RESEARCH, job.id)
    with db.transaction() as connection:
        connection.execute("UPDATE research_jobs SET available_at=clock_timestamp()+interval '1 hour' WHERE id=?", (job.id,))
        assert runtime.claim(connection, ref, "worker", 60) is None
        connection.execute("UPDATE research_jobs SET available_at=clock_timestamp(),attempts=max_attempts WHERE id=?", (job.id,))
        assert runtime.claim(connection, ref, "worker", 60) is None


def test_completion_event_failure_rolls_back_report_and_lease(research, monkeypatch):
    db, service, job = research
    claimed = service.claim(job.id, "worker", 60)
    original = service.events.append

    def fail_terminal(*args, **kwargs):
        if args[2] == "research.completed":
            raise RuntimeError("event persistence unavailable")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(service.events, "append", fail_terminal)
        with pytest.raises(RuntimeError, match="event persistence"):
            service.complete(job.id, "worker", "report", "answer", 0, 0, epoch=claimed.lease_epoch)
    current = service.get(job.id)
    assert current.status == "RUNNING"
    assert not current.report_markdown
    with db.connection() as connection:
        row = connection.execute("SELECT lease_owner,lease_epoch FROM research_jobs WHERE id=?", (job.id,)).fetchone()
        assert row["lease_owner"] == "worker"
        assert row["lease_epoch"] == claimed.lease_epoch
    service.complete(job.id, "worker", "report", "answer", 0, 0, epoch=claimed.lease_epoch)
    assert service.get(job.id).report_markdown == "answer"
