from dataclasses import replace

import pytest

from app.runtime import MockModelGateway, ModelDecision
from app.tools import ToolCall
from test_runtime import make_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "loop"])
async def test_approved_goal_write_rejects_changed_definition(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", mode)
    call = ToolCall("write", "write_note", {"path": "note.md", "content": "one"})
    runtime = make_runtime(tmp_path, MockModelGateway(
        plan_steps=[{"id": "s", "title": "write"}],
        decisions=[ModelDecision.tool(call), ModelDecision.complete("done")],
    ))
    run = await runtime.create_goal("write", "write")
    await runtime.handle_message(run.id, "start")
    await runtime.approve_plan(run.id, 1)
    approval = runtime.pending_approvals(run.id)[0]
    old = runtime.tools.spec("write_note")
    runtime.tools.unregister("write_note")
    runtime.tools.register(replace(old, version="2"))
    result = await runtime.grant_approval(approval.id)
    assert result.state.value == "BLOCKED"
    assert not (runtime.tools.workspace / "note.md").exists()
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_execution_claims WHERE tool_call_id=?", (call.id,)).fetchone()[0] == 0
