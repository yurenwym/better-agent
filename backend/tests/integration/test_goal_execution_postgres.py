import asyncio

import pytest

from app.runtime import MockModelGateway
from app.task_runtime import LeaseLost
def make_runtime(tmp_path, model, database_url):
    from app.db import Database
    from app.domain import ApprovalService, CheckpointStore, PlanVersionService
    from app.events import EventStore
    from app.memory import MemoryService
    from app.runtime import AgentRuntime
    from app.tools import create_default_registry
    db = Database(database_url, workspace=tmp_path / "workspace")
    events = EventStore(db)
    approvals = ApprovalService(db)
    return AgentRuntime(db=db, events=events, plans=PlanVersionService(db), approvals=approvals,
        checkpoints=CheckpointStore(db), memory=MemoryService(db, events, tmp_path / "memory"),
        tools=create_default_registry(tmp_path / "workspace", db=db, approval_service=approvals), model=model)


@pytest.mark.asyncio
async def test_two_runtime_instances_share_goal_execution_authority(tmp_path, migrated_postgres_url):
    entered = asyncio.Event()
    release = asyncio.Event()

    class Model(MockModelGateway):
        async def needs_clarification(self, goal, interactions):
            entered.set()
            await release.wait()
            return False

    first = make_runtime(tmp_path, Model(), migrated_postgres_url)
    second = make_runtime(tmp_path, MockModelGateway(), migrated_postgres_url)
    run = await first.create_goal("goal", "work")
    pending = asyncio.create_task(first.handle_message(run.id, "start"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(LeaseLost, match="already active"):
            await second.handle_message(run.id, "duplicate")
        with first.db.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM interactions WHERE run_id=?", (run.id,)).fetchone()[0]
            assert count == 1
        release.set()
        await pending
        assert first.get_run(run.id).state == "AWAITING_APPROVAL"
    finally:
        release.set()
        await pending
        first.db.close()
        second.db.close()


@pytest.mark.asyncio
async def test_expired_goal_worker_cannot_commit_model_response(tmp_path, migrated_postgres_url):
    entered = asyncio.Event()
    release = asyncio.Event()

    class Model(MockModelGateway):
        async def needs_clarification(self, goal, interactions):
            entered.set()
            await release.wait()
            return False

    first = make_runtime(tmp_path, Model(), migrated_postgres_url)
    second = make_runtime(tmp_path, MockModelGateway(), migrated_postgres_url)
    run = await first.create_goal("goal", "work")
    pending = asyncio.create_task(first.handle_message(run.id, "start"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with first.db.transaction() as connection:
            connection.execute("UPDATE runs SET execution_until='2000-01-01T00:00:00+00:00' WHERE id=?", (run.id,))
        recovered = await second.handle_message(run.id, "recover")
        assert recovered.state == "AWAITING_APPROVAL"
        release.set()
        with pytest.raises(LeaseLost):
            await pending
        assert first.get_run(run.id).state == "AWAITING_APPROVAL"
        with first.db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM plan_versions WHERE run_id=?", (run.id,)).fetchone()[0] == 1
            assert connection.execute("SELECT execution_epoch FROM runs WHERE id=?", (run.id,)).fetchone()[0] == 2
    finally:
        release.set()
        if not pending.done():
            await pending
        first.db.close()
        second.db.close()
