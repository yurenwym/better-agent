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
async def test_react_observation_includes_tool_result_data(tmp_path) -> None:
    from app.runtime import MockModelGateway, ModelDecision
    from app.tools import ToolCall

    class RecordingModel(MockModelGateway):
        def __init__(self):
            super().__init__(
                plan_steps=[{"id": "step-1", "title": "计算结果"}],
                decisions=[
                    ModelDecision.tool(ToolCall("calc-1", "calculator", {"expression": "2 + 2"})),
                    ModelDecision.complete("已得到结果"),
                ],
            )
            self.observations = []

        async def decide(self, step, observation, iteration):
            self.observations.append(observation)
            return await super().decide(step, observation, iteration)

    model = RecordingModel()
    runtime = make_runtime(tmp_path, model)
    run = await runtime.create_goal("Calculate", "Calculate 2 + 2")
    await runtime.handle_message(run.id, "Calculate 2 + 2")

    completed = await runtime.approve_plan(run.id, 1)

    assert completed.state == "COMPLETED"
    assert '"value": 4' in model.observations[1]


@pytest.mark.asyncio
async def test_react_receives_full_plan_step_deliverable(tmp_path) -> None:
    from app.runtime import MockModelGateway, ModelDecision

    class RecordingModel(MockModelGateway):
        def __init__(self):
            super().__init__(
                plan_steps=[{
                    "id": "step-1",
                    "title": "生成菜单",
                    "description": "输出 7 天的每日三餐和热量分配",
                }],
                decisions=[ModelDecision.complete("已生成菜单")],
            )
            self.steps = []

        async def decide(self, step, observation, iteration):
            self.steps.append(step)
            return await super().decide(step, observation, iteration)

    model = RecordingModel()
    runtime = make_runtime(tmp_path, model)
    run = await runtime.create_goal("Diet", "Prepare a diet plan")
    await runtime.handle_message(run.id, "Prepare a diet plan")

    completed = await runtime.approve_plan(run.id, 1)

    assert completed.state == "COMPLETED"
    assert model.steps[0]["description"] == "输出 7 天的每日三餐和热量分配"


@pytest.mark.asyncio
async def test_react_budget_resets_for_each_plan_step(tmp_path) -> None:
    from app.runtime import MockModelGateway, ModelDecision, RuntimeConfig

    runtime = make_runtime(
        tmp_path,
        MockModelGateway(
            plan_steps=[
                {"id": "step-1", "title": "第一步"},
                {"id": "step-2", "title": "第二步"},
            ],
            decisions=[ModelDecision.complete("完成第一步"), ModelDecision.complete("完成第二步")],
        ),
    )
    runtime.config = RuntimeConfig(max_react_iterations_per_step=1)
    run = await runtime.create_goal("Two steps", "Complete two steps")
    await runtime.handle_message(run.id, "Complete two steps")

    completed = await runtime.approve_plan(run.id, 1)

    assert completed.state == "COMPLETED"


@pytest.mark.asyncio
async def test_invalid_reflection_candidate_does_not_block_completion(tmp_path) -> None:
    from app.runtime import MockModelGateway, ModelDecision

    runtime = make_runtime(
        tmp_path,
        MockModelGateway(
            plan_steps=[{"id": "step-1", "title": "完成交付"}],
            decisions=[ModelDecision.complete("已完成")],
            reflection_candidates=[{
                "kind": "preference",
                "content": "Nutrition topic",
                "scope": "nutrition",
                "confidence": 0.8,
                "evidence_event_ids": [],
            }],
        ),
    )
    run = await runtime.create_goal("Goal", "Complete it")
    await runtime.handle_message(run.id, "Complete it")

    completed = await runtime.approve_plan(run.id, 1)

    assert completed.state == "COMPLETED"
    assert runtime.memory.all_records() == []


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


@pytest.mark.asyncio
async def test_model_gateway_failure_blocks_run_and_saves_checkpoint(tmp_path) -> None:
    from app.model_gateway import GatewayError

    class FailingModel:
        async def needs_clarification(self, goal, interactions):
            return False

        async def plan(self, goal, interactions):
            raise GatewayError("retry exhausted", "server", 4)

        async def decide(self, step, observation, iteration):
            raise AssertionError("react should not run")

        async def reflect(self, goal, plan, run_id):
            raise AssertionError("reflection should not run")

    runtime = make_runtime(tmp_path, FailingModel())
    run = await runtime.create_goal("Failure", "Model unavailable")

    blocked = await runtime.handle_message(run.id, "Try")

    assert blocked.state.value == "BLOCKED"
    assert runtime.checkpoints.latest(run.id) is not None
    assert any(event.type == "run.blocked" for event in runtime.events.list(run.id))


@pytest.mark.asyncio
async def test_unknown_tool_decision_blocks_run_without_leaking_exception(tmp_path) -> None:
    from app.runtime import MockModelGateway, ModelDecision
    from app.tools import ToolCall

    runtime = make_runtime(
        tmp_path,
        MockModelGateway(
            plan_steps=[{"id": "step-1", "title": "Use tool"}],
            decisions=[ModelDecision.tool(ToolCall("unknown-call", "shell", {}))],
        ),
    )
    run = await runtime.create_goal("Unknown tool", "Reject it")
    await runtime.handle_message(run.id, "Use it")

    blocked = await runtime.approve_plan(run.id, 1)

    assert blocked.state.value == "BLOCKED"
    assert any(event.type == "run.blocked" for event in runtime.events.list(run.id))


@pytest.mark.asyncio
async def test_runtime_builds_context_snapshot_before_live_model_call(tmp_path) -> None:
    from app.runtime import MockModelGateway

    class ContextModel(MockModelGateway):
        def __init__(self):
            super().__init__()
            self.snapshots = []

        def set_context_snapshot(self, snapshot_hash, text):
            self.snapshots.append((snapshot_hash, text))

    model = ContextModel()
    runtime = make_runtime(tmp_path, model)
    run = await runtime.create_goal("Context", "Use confirmed context")
    await runtime.handle_message(run.id, "Plan with context")

    assert model.snapshots
    assert any(event.type == "context.snapshot_created" for event in runtime.events.list(run.id))


def test_runtime_react_skill_allowlist_excludes_unsupported_tools(tmp_path) -> None:
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())

    assert "write_note" in runtime._skill_tools_for("react")
    assert "shell" not in runtime._skill_tools_for("react")
