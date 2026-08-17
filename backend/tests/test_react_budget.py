import pytest

from test_runtime import make_runtime


@pytest.mark.asyncio
async def test_react_budget_exhaustion_saves_checkpoint_and_recovers_after_budget_addition(tmp_path) -> None:
    from app.runtime import MockModelGateway, ModelDecision, RuntimeConfig

    runtime = make_runtime(
        tmp_path,
        MockModelGateway(
            plan_steps=[{"id": "step-1", "title": "Keep trying"}],
            decisions=[ModelDecision.continue_() for _ in range(5)],
        ),
    )
    runtime.config = RuntimeConfig(max_react_iterations_per_step=5)
    run = await runtime.create_goal("Budget", "Use budget")
    await runtime.handle_message(run.id, "Use budget")

    blocked = await runtime.approve_plan(run.id, 1)

    assert blocked.state == "BLOCKED"
    checkpoint = runtime.checkpoints.latest(run.id)
    assert checkpoint is not None
    assert checkpoint.react_iteration == 5
    assert any(event.type == "budget.exhausted" for event in runtime.events.list(run.id))

    await runtime.add_budget(run.id, 1)
    resumed = await runtime.resume(run.id)
    assert resumed.state == "COMPLETED"
    assert any(event.type == "run.resumed" for event in runtime.events.list(run.id))

