import pytest

from app.runtime import MockModelGateway, ModelDecision
from app.tools import ToolCall
from test_runtime import make_runtime


@pytest.mark.asyncio
async def test_step_result_and_loop_finish_roll_back_together(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    runtime = make_runtime(tmp_path, MockModelGateway(
        plan_steps=[{"id": "s", "title": "step"}], decisions=[ModelDecision.complete("done")]))
    run = await runtime.create_goal("goal", "goal")
    await runtime.handle_message(run.id, "start")
    append = runtime.events.append

    def interrupted(*args, **kwargs):
        event = append(*args, **kwargs)
        if event.type == "plan.step_finished":
            raise RuntimeError("step commit interrupted")
        return event

    monkeypatch.setattr(runtime.events, "append", interrupted)
    with pytest.raises(RuntimeError, match="step commit interrupted"):
        await runtime.approve_plan(run.id, 1)
    current = runtime.get_run(run.id)
    assert "last_execution_outcome" not in current.budget
    assert runtime.plans.get(current.current_plan_version_id).steps[0].status != "completed"
    assert not any(event.type in {"loop.finished", "plan.step_finished"} for event in runtime.events.list(run.id))
    monkeypatch.setattr(runtime.events, "append", append)
    from app.goal_loop import execute_goal_step
    assert await execute_goal_step(runtime, run.id, runtime.plans.get(current.current_plan_version_id), "s") == "completed"
    finished = [event for event in runtime.events.list(run.id) if event.type == "loop.finished"]
    assert len(finished) == 2
    assert finished[0].data["outcome"]["reason"] == "EXECUTION_INTERRUPTED"
    assert finished[1].data["outcome"]["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,state", [(ModelDecision.complete("done"), "COMPLETED"),
    (ModelDecision.blocked("needs input"), "BLOCKED"), (ModelDecision.await_outcome("waiting"), "AWAITING_OUTCOME")])
async def test_al_t19_goal_terminal_mapping(tmp_path, monkeypatch, decision, state):
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    runtime = make_runtime(tmp_path, MockModelGateway(plan_steps=[{"id": "s", "title": "step"}], decisions=[decision]))
    run = await runtime.create_goal("goal", "goal")
    await runtime.handle_message(run.id, "start")
    assert (await runtime.approve_plan(run.id, 1)).state == state
    if state == "BLOCKED":
        checkpoint = runtime.checkpoints.latest(run.id)
        blocked = next(event for event in reversed(runtime.events.list(run.id)) if event.type == "run.blocked")
        assert checkpoint.last_event_seq >= blocked.seq


@pytest.mark.asyncio
async def test_al_t20_goal_approval_resume_executes_once(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    model = MockModelGateway(plan_steps=[{"id": "s", "title": "step"}], decisions=[
        ModelDecision.tool(ToolCall("write-one", "write_note", {"path": "note.md", "content": "saved"})), ModelDecision.complete("done")])
    runtime = make_runtime(tmp_path, model)
    run = await runtime.create_goal("goal", "goal")
    await runtime.handle_message(run.id, "start")
    await runtime.approve_plan(run.id, 1)
    assert not (tmp_path / "workspace/note.md").exists()
    approval = runtime.pending_approvals(run.id)[0]
    identity_before = runtime.get_run(run.id).budget['agent_loop_context']
    await runtime.grant_approval(approval.id)
    assert runtime.get_run(run.id).budget['agent_loop_context'] == identity_before
    assert (tmp_path / "workspace/note.md").read_text() == "saved"
    import json
    from app.event_envelope import EventMetadata
    finished = [EventMetadata.from_dict(json.loads(event.envelope_json))
                for event in runtime.events.list(run.id) if event.type == "loop.finished"]
    assert [event.attempt for event in finished] == [1, 2]
    assert len({event.operation_id for event in finished}) == 1
    assert len({event.context.span_id for event in finished}) == 2
    assert [event.outcome.status.value for event in finished] == ["awaiting_approval", "completed"]
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_calls WHERE id='write-one'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_approval_and_checkpoint_roll_back_when_attempt_finish_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    model = MockModelGateway(plan_steps=[{"id": "s", "title": "step"}], decisions=[
        ModelDecision.tool(ToolCall("write-one", "write_note", {"path": "note.md", "content": "saved"}))])
    runtime = make_runtime(tmp_path, model)
    try:
        run = await runtime.create_goal("goal", "goal")
        await runtime.handle_message(run.id, "start")
        append = runtime.events.append

        def interrupted(*args, **kwargs):
            event = append(*args, **kwargs)
            if event.type == "loop.finished":
                raise RuntimeError("finish unavailable")
            return event

        monkeypatch.setattr(runtime.events, "append", interrupted)
        with pytest.raises(RuntimeError, match="finish unavailable"):
            await runtime.approve_plan(run.id, 1)
        assert runtime.pending_approvals(run.id) == []
        assert not (tmp_path / "workspace/note.md").exists()
        assert "last_execution_outcome" not in runtime.get_run(run.id).budget
        assert runtime.checkpoints.latest(run.id) is None
        assert not any(e.type in {"approval.requested", "loop.finished"} for e in runtime.events.list(run.id))
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_al_t21_goal_plain_text_exhausts(tmp_path, monkeypatch):
    from test_conversation_loop import Gateway
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    model = MockModelGateway(plan_steps=[{"id": "s", "title": "step"}])
    runtime = make_runtime(tmp_path, model)
    run = await runtime.create_goal("goal", "goal")
    await runtime.handle_message(run.id, "start")
    gateway = Gateway([("plain text", [])] * 5)
    model.gateway = gateway
    result = await runtime.approve_plan(run.id, 1)
    assert result.state == "BLOCKED"
    assert result.budget["blocked_reason_code"] == "REACT_ITERATION_BUDGET_EXHAUSTED"
    assert len(gateway.requests) == 5
    assert any("必须调用终止型能力" in m.get("content", "") for m in gateway.requests[-1].messages)


@pytest.mark.asyncio
async def test_native_goal_gateway_uses_terminal_tool_and_bound_identity(tmp_path, monkeypatch):
    from test_conversation_loop import Gateway, tool
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    model = MockModelGateway(plan_steps=[{"id": "s", "title": "step"}])
    runtime = make_runtime(tmp_path, model)
    run = await runtime.create_goal("goal", "goal")
    await runtime.handle_message(run.id, "start")
    gateway = Gateway([("", [tool("finish_step", {"output": "done"})])])
    model.gateway = gateway
    assert (await runtime.approve_plan(run.id, 1)).state == "COMPLETED"
    assert len(gateway.requests) == 1
    assert "finish_step" in {s["function"]["name"] for s in gateway.requests[0].tools}


@pytest.mark.asyncio
async def test_missing_source_identity_blocks_before_model(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    runtime = make_runtime(tmp_path, MockModelGateway(plan_steps=[{"id": "s", "title": "step"}]))
    run = await runtime.create_goal("goal", "goal")
    await runtime.handle_message(run.id, "start")
    runtime._set_run_fields(run.id, source_turn_id="deleted-turn")
    # The root is frozen at creation. A changed source is refused before a
    # transition can append a falsely attributed execution event.
    with pytest.raises(ValueError, match="run source identity changed"):
        await runtime.approve_plan(run.id, 1)


@pytest.mark.asyncio
async def test_native_goal_preserves_gateway_failure_reason(tmp_path, monkeypatch):
    from app.model_gateway import GatewayError
    from test_conversation_loop import Gateway
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    model = MockModelGateway(plan_steps=[{"id": "s", "title": "step"}])
    runtime = make_runtime(tmp_path, model)
    run = await runtime.create_goal("goal", "goal")
    await runtime.handle_message(run.id, "start")
    class DeniedGateway(Gateway):
        async def complete(self, *args, **kwargs):
            raise GatewayError("revoked", "context_invalidated")
    model.gateway = DeniedGateway([])
    result = await runtime.approve_plan(run.id, 1)
    assert result.state == "BLOCKED"
    assert result.budget["blocked_reason_code"] == "MODEL_CONTEXT_INVALIDATED"


@pytest.mark.asyncio
async def test_reconciliation_keeps_pending_call_after_block(tmp_path, monkeypatch):
    from app.tools import ToolReconciliationRequired
    from app.tool_executor import ToolExecutor
    monkeypatch.setenv('BETTER_AGENT_LOOP_MODE', 'loop')
    model = MockModelGateway(plan_steps=[{'id': 's', 'title': 'read'}], decisions=[
        ModelDecision.tool(ToolCall('uncertain', 'read_note', {'path': 'note.md'}))])
    runtime = make_runtime(tmp_path, model)
    run = await runtime.create_goal('goal', 'read')
    await runtime.handle_message(run.id, 'start')
    async def uncertain(*args, **kwargs):
        raise ToolReconciliationRequired('uncertain')
    monkeypatch.setattr(ToolExecutor, 'execute_async', uncertain)
    result = await runtime.approve_plan(run.id, 1)
    assert result.budget['blocked_reason_code'] == 'TOOL_RECONCILIATION_REQUIRED'
    assert runtime.checkpoints.latest(run.id).pending_actions[0]['id'] == 'uncertain'
