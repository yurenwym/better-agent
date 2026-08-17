import pytest


def make_runtime(tmp_path, model):
    from app.db import Database
    from app.domain import ApprovalService, CheckpointStore, PlanVersionService
    from app.events import EventStore
    from app.memory import MemoryService
    from app.runtime import AgentRuntime
    from app.tools import create_default_registry

    db = Database(tmp_path / "agent.db", workspace=tmp_path / "workspace")
    events = EventStore(db)
    approvals = ApprovalService(db)
    return AgentRuntime(
        db=db,
        events=events,
        plans=PlanVersionService(db),
        approvals=approvals,
        checkpoints=CheckpointStore(db),
        memory=MemoryService(db, events, tmp_path / "memory"),
        tools=create_default_registry(tmp_path / "workspace", db=db, approval_service=approvals),
        model=model,
    )


@pytest.mark.asyncio
async def test_runtime_runs_received_planning_approval_execution_reflection_loop(tmp_path) -> None:
    from app.runtime import AgentRuntime, MockModelGateway, ModelDecision

    runtime = make_runtime(
        tmp_path,
        MockModelGateway(
            plan_steps=[{"id": "step-1", "title": "Draft result"}],
            decisions=[ModelDecision.complete("drafted")],
        ),
    )
    run = await runtime.create_goal("Ship proposal", "Prepare a proposal")
    planned = await runtime.handle_message(run.id, "Ship the proposal")

    assert planned.state == "AWAITING_APPROVAL"
    assert runtime.plans.current(run.id).version == 1
    assert not any(event.type == "plan.step_started" for event in runtime.events.list(run.id))

    completed = await runtime.approve_plan(run.id, 1)

    assert completed.state == "COMPLETED"
    event_types = [event.type for event in runtime.events.list(run.id)]
    assert "plan.approved" in event_types
    assert "react.iteration_started" in event_types
    assert "plan.step_finished" in event_types
    assert event_types[-1] == "run.completed"


@pytest.mark.asyncio
async def test_runtime_enters_clarifying_before_planning_when_information_is_missing(tmp_path) -> None:
    from app.runtime import MockModelGateway, ModelDecision

    runtime = make_runtime(
        tmp_path,
        MockModelGateway(
            clarification_required=True,
            plan_steps=[{"id": "step-1", "title": "Do it"}],
            decisions=[ModelDecision.complete("done")],
        ),
    )
    run = await runtime.create_goal("Vague", "")

    clarifying = await runtime.handle_message(run.id, "Help me")
    assert clarifying.state == "CLARIFYING"
    planned = await runtime.handle_message(run.id, "Use the existing project")
    assert planned.state == "AWAITING_APPROVAL"


@pytest.mark.asyncio
async def test_runtime_write_waits_for_approval_and_never_writes_before_grant(tmp_path) -> None:
    from app.runtime import MockModelGateway, ModelDecision
    from app.tools import ToolCall

    call = ToolCall("write-1", "write_note", {"path": "result.md", "content": "safe"})
    runtime = make_runtime(
        tmp_path,
        MockModelGateway(
            plan_steps=[{"id": "step-1", "title": "Write result"}],
            decisions=[ModelDecision.tool(call), ModelDecision.complete("written")],
        ),
    )
    run = await runtime.create_goal("Write", "Write a note")
    await runtime.handle_message(run.id, "Write it")
    waiting = await runtime.approve_plan(run.id, 1)

    assert waiting.state == "EXECUTING"
    assert not (tmp_path / "workspace" / "result.md").exists()
    approval = runtime.pending_approvals(run.id)[0]

    completed = await runtime.grant_approval(approval.id)
    assert completed.state == "COMPLETED"
    assert (tmp_path / "workspace" / "result.md").read_text(encoding="utf-8") == "safe"


@pytest.mark.asyncio
async def test_runtime_revises_plan_as_a_new_version(tmp_path) -> None:
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway(plan_steps=[{"id": "old", "title": "Old"}]))
    run = await runtime.create_goal("Plan", "Plan it")
    await runtime.handle_message(run.id, "Plan it")

    revised = await runtime.revise_plan(
        run.id,
        expected_version=1,
        steps=[{"id": "new", "title": "New"}],
    )

    assert revised.version == 2
    assert [step.id for step in revised.steps] == ["new"]


@pytest.mark.asyncio
async def test_runtime_cancel_is_a_global_terminal_transition(tmp_path) -> None:
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    run = await runtime.create_goal("Cancel", "Stop it")

    cancelled = await runtime.cancel(run.id)

    assert cancelled.state == "CANCELLED"
    assert any(event.type == "run.cancelled" for event in runtime.events.list(run.id))


@pytest.mark.asyncio
async def test_runtime_cancel_step_saves_checkpoint_and_marks_only_that_step_cancelled(tmp_path) -> None:
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway(plan_steps=[{"id": "step-1", "title": "Cancel me"}]))
    run = await runtime.create_goal("Cancel step", "Stop one step")
    await runtime.handle_message(run.id, "Stop one step")

    current = await runtime.cancel_step(run.id, "step-1")

    assert current.state == "AWAITING_APPROVAL"
    assert runtime.plans.current(run.id).steps[0].status == "cancelled"
    assert runtime.checkpoints.latest(run.id) is not None
