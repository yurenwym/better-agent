import asyncio

import pytest

from app.db import Database
from app.runtime import MockModelGateway
from app.task_runtime import LeaseLost
from pg02_helpers import database, make_runtime


@pytest.fixture
def second_database(database):
    allocated = []
    class TrackedDatabase(Database):
        def __init__(self, *args, **kwargs):
            allocated.append(self)
            super().__init__(*args, **kwargs)
    def create():
        return TrackedDatabase(database.database_url, workspace=database.workspace)
    yield create
    for opened in reversed(allocated):
        if getattr(opened, "_pool", None) is not None:
            opened.close()


@pytest.mark.asyncio
async def test_two_runtime_instances_share_goal_execution_authority(database, tmp_path, second_database):
    entered = asyncio.Event()
    release = asyncio.Event()

    class Model(MockModelGateway):
        async def needs_clarification(self, goal, interactions):
            entered.set()
            await release.wait()
            return False

    first = make_runtime(database, tmp_path, Model())
    second_db = second_database()
    second = make_runtime(second_db, tmp_path, MockModelGateway())
    run = await first.create_goal("goal", "work")
    pending = asyncio.create_task(first.handle_message(run.id, "start"))
    expected_lease_lost = False
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
        if not pending.done():
            pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        except LeaseLost:
            if not expected_lease_lost:
                raise


@pytest.mark.asyncio
async def test_expired_goal_worker_cannot_commit_model_response(database, tmp_path, second_database):
    entered = asyncio.Event()
    release = asyncio.Event()

    class Model(MockModelGateway):
        async def needs_clarification(self, goal, interactions):
            entered.set()
            await release.wait()
            return False

    first = make_runtime(database, tmp_path, Model())
    second_db = second_database()
    second = make_runtime(second_db, tmp_path, MockModelGateway())
    run = await first.create_goal("goal", "work")
    pending = asyncio.create_task(first.handle_message(run.id, "start"))
    expected_lease_lost = False
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with first.db.transaction() as connection:
            connection.execute("UPDATE runs SET execution_until='2000-01-01T00:00:00+00:00' WHERE id=?", (run.id,))
        recovered = await second.handle_message(run.id, "recover")
        assert recovered.state == "AWAITING_APPROVAL"
        release.set()
        with pytest.raises(LeaseLost):
            await pending
        expected_lease_lost = True
        assert first.get_run(run.id).state == "AWAITING_APPROVAL"
        with first.db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM plan_versions WHERE run_id=?", (run.id,)).fetchone()[0] == 1
            assert connection.execute("SELECT execution_epoch FROM runs WHERE id=?", (run.id,)).fetchone()[0] == 2
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        except LeaseLost:
            if not expected_lease_lost:
                raise
