import pytest

from app.runtime import MockModelGateway
from app.task_runtime import LeaseLost
from test_runtime import make_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["approve", "revise", "cancel_step"])
async def test_plan_mutation_rejects_expired_execution_before_writing(tmp_path, monkeypatch, operation):
    runtime = make_runtime(tmp_path, MockModelGateway())
    try:
        run = await runtime.create_goal("goal", "work")
        await runtime.handle_message(run.id, "start")
        plan = runtime.plans.current(run.id)
        original_get = runtime.get_run
        expired = False

        def get_run(*args, **kwargs):
            nonlocal expired
            result = original_get(*args, **kwargs)
            if not expired and runtime._execution_token.get() is not None:
                expired = True
                with runtime.db.transaction() as connection:
                    connection.execute("UPDATE runs SET execution_until='2000-01-01T00:00:00+00:00' WHERE id=?", (run.id,))
            return result

        monkeypatch.setattr(runtime, "get_run", get_run)
        with pytest.raises(LeaseLost):
            if operation == "approve":
                await runtime.approve_plan(run.id, plan.version)
            elif operation == "revise":
                await runtime.revise_plan(run.id, plan.version, [{"title": "late revision"}])
            else:
                await runtime.cancel_step(run.id, plan.steps[0].id)
        assert runtime.plans.current(run.id) == plan
    finally:
        runtime.db.close()
