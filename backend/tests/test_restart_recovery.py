import pytest

from test_runtime import make_runtime


@pytest.mark.asyncio
async def test_restart_resumes_from_checkpoint_without_repeating_completed_write(tmp_path) -> None:
    from app.runtime import MockModelGateway, ModelDecision
    from app.tools import ToolCall

    call = ToolCall("write-once", "write_note", {"path": "once.md", "content": "once"})
    runtime = make_runtime(
        tmp_path,
        MockModelGateway(
            plan_steps=[{"id": "step-1", "title": "Write once"}],
            decisions=[ModelDecision.tool(call), ModelDecision.await_outcome("wait")],
        ),
    )
    run = await runtime.create_goal("Recovery", "Recover safely")
    await runtime.handle_message(run.id, "Recover")
    await runtime.approve_plan(run.id, 1)
    approval = runtime.pending_approvals(run.id)[0]
    waiting = await runtime.grant_approval(approval.id)
    assert waiting.state == "AWAITING_OUTCOME"
    before = len([event for event in runtime.events.list(run.id) if event.type == "tool.execution.started"])

    restarted = make_runtime(tmp_path, MockModelGateway())
    resumed = await restarted.resume(run.id)

    assert resumed.state == "AWAITING_OUTCOME"
    after = len([event for event in restarted.events.list(run.id) if event.type == "tool.execution.started"])
    assert after == before == 1
    assert (tmp_path / "workspace" / "once.md").read_text(encoding="utf-8") == "once"


@pytest.mark.asyncio
async def test_checkpoint_dedupes_completed_write_when_outcome_continues_after_restart(tmp_path) -> None:
    from app.runtime import MockModelGateway, ModelDecision
    from app.tools import ToolCall

    call = ToolCall("write-continue", "write_note", {"path": "continue.md", "content": "once"})
    runtime = make_runtime(
        tmp_path,
        MockModelGateway(
            plan_steps=[{"id": "step-1", "title": "Write and continue"}],
            decisions=[ModelDecision.tool(call), ModelDecision.await_outcome("wait")],
        ),
    )
    run = await runtime.create_goal("Recovery", "Continue safely")
    await runtime.handle_message(run.id, "Recover")
    await runtime.approve_plan(run.id, 1)
    approval = runtime.pending_approvals(run.id)[0]
    await runtime.grant_approval(approval.id)

    restarted = make_runtime(tmp_path, MockModelGateway(decisions=[ModelDecision.tool(call), ModelDecision.complete("done")]))
    resumed = await restarted.continue_outcome(run.id, finished=False)

    assert resumed.state.value == "COMPLETED"
    assert len([event for event in restarted.events.list(run.id) if event.type == "tool.execution.started"]) == 1


@pytest.mark.asyncio
async def test_runtime_scans_non_terminal_runs_for_user_selected_recovery(tmp_path) -> None:
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    run = await runtime.create_goal("Recoverable", "Find me")

    restarted = make_runtime(tmp_path, MockModelGateway())

    assert [item.id for item in restarted.recoverable_runs()] == [run.id]
