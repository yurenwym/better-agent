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

