from datetime import datetime, timedelta, timezone

import pytest

from app.conversation import ConversationService
from app.db import Database
from app.research.service import ResearchService
from app.task_runtime import TaskRuntime, TaskRef, TaskKind, LeaseLost, TaskCancelled


def test_reused_worker_name_cannot_revive_old_epoch(tmp_path):
    db = Database(tmp_path / "lease.db")
    try:
        conversation = ConversationService(db)
        service = ResearchService(db, conversation.events)
        job = service.create_manual(conversation.create_thread().id, "topic", "one", ("web",))
        runtime = TaskRuntime(db)
        ref = TaskRef(TaskKind.RESEARCH, job.id)
        with db.transaction() as connection:
            first = runtime.claim(connection, ref, "same-worker", 30)
            assert runtime.claim(connection, ref, "second", 30) is None
            connection.execute("UPDATE research_jobs SET lease_until=? WHERE id=?",
                               ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job.id))
        with db.transaction() as connection:
            with pytest.raises(LeaseLost):
                runtime.heartbeat(connection, first.token, 30)
            second = runtime.claim(connection, ref, "same-worker", 30)
            assert second.token.epoch == first.token.epoch + 1
            assert second.number == first.number + 1
        with db.transaction() as connection:
            with pytest.raises(LeaseLost):
                runtime.finish(connection, first.token, "COMPLETED")
            assert runtime.request_cancel(connection, ref)
            assert not runtime.request_cancel(connection, ref)
            with pytest.raises(TaskCancelled):
                runtime.finish(connection, second.token, "COMPLETED")
            runtime.finish(connection, second.token, "CANCELLED")
        assert service.get(job.id).status == "CANCELLED"
    finally:
        db.close()
