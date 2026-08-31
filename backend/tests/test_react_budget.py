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
    assert blocked.budget["blocked_reason_code"] == "REACT_ITERATION_BUDGET_EXHAUSTED"
    assert blocked.budget["blocked_message"] == "本步骤的执行轮次已用完"
    checkpoint = runtime.checkpoints.latest(run.id)
    assert checkpoint is not None
    assert checkpoint.react_iteration == 5
    assert any(event.type == "budget.exhausted" for event in runtime.events.list(run.id))
    exhausted = next(event for event in runtime.events.list(run.id) if event.type == "budget.exhausted")
    assert exhausted.data["reason_code"] == "REACT_ITERATION_BUDGET_EXHAUSTED"
    assert exhausted.data["message"] == "本步骤的执行轮次已用完"

    await runtime.add_budget(run.id, 1)
    resumed = await runtime.resume(run.id)
    assert resumed.state == "COMPLETED"
    assert any(event.type == "run.resumed" for event in runtime.events.list(run.id))


@pytest.mark.asyncio
async def test_budget_addition_requires_react_budget_block(tmp_path) -> None:
    from app.runtime import MockModelGateway, ModelDecision

    runtime = make_runtime(
        tmp_path,
        MockModelGateway(
            plan_steps=[{"id": "step-1", "title": "Wait for approval"}],
            decisions=[ModelDecision.complete("done")],
        ),
    )
    run = await runtime.create_goal("Guard", "Do not extend normal runs")
    await runtime.handle_message(run.id, "Start")

    with pytest.raises(ValueError, match="只有执行轮次用完后"):
        await runtime.add_budget(run.id, 1)


@pytest.mark.asyncio
async def test_budget_addition_rejects_non_react_block_reason(tmp_path) -> None:
    from app.runtime import MockModelGateway, ModelDecision

    runtime = make_runtime(
        tmp_path,
        MockModelGateway(
            plan_steps=[{"id": "step-1", "title": "Block"}],
            decisions=[ModelDecision.blocked("model needs user input")],
        ),
    )
    run = await runtime.create_goal("Guard", "Do not extend model blocks")
    await runtime.handle_message(run.id, "Start")
    blocked = await runtime.approve_plan(run.id, 1)

    assert blocked.state.value == "BLOCKED"
    with pytest.raises(ValueError, match="只有执行轮次用完后"):
        await runtime.add_budget(run.id, 1)

