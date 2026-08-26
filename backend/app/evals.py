from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from .db import Database
from .domain import ApprovalService, CheckpointStore, PlanVersionService
from .events import EventStore
from .memory import MemoryService
from .model_gateway import GatewayError, ModelGateway, ModelProfile, ModelRequest
from .runtime import AgentRuntime, ModelDecision, MockModelGateway
from .tools import ToolCall, ToolRejected, ToolResult, create_default_registry


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    passed: bool
    detail: str = ""
    run_id: str | None = None


@dataclass(frozen=True)
class SuiteReport:
    results: tuple[ScenarioResult, ...]

    @property
    def passed(self) -> int:
        return sum(result.passed for result in self.results)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed


Scenario = Callable[[Path], Awaitable[str]]


def run_deterministic_suite() -> SuiteReport:
    results: list[ScenarioResult] = []
    for name, scenario in _SCENARIOS:
        with tempfile.TemporaryDirectory(prefix=f"better-agent-eval-{name}-") as temporary:
            try:
                detail = asyncio.run(scenario(Path(temporary)))
                results.append(ScenarioResult(name, True, detail))
            except Exception as exc:  # The report is a test artifact; keep the failure local to its case.
                results.append(ScenarioResult(name, False, f"{type(exc).__name__}: {exc}"))
    return SuiteReport(tuple(results))


def check_invariants(runtime: AgentRuntime, run_id: str) -> list[str]:
    errors: list[str] = []
    events = runtime.events.list(run_id)
    if [event.seq for event in events] != list(range(1, len(events) + 1)):
        errors.append("event seq is not contiguous")
    for event in events:
        if event.run_id != run_id:
            errors.append("event run binding changed")
        if event.schema_version != 1:
            errors.append("unknown event schema")
    exported = runtime.events.list(run_id)
    if any(event.type == "tool.execution.started" and event.actor != "tool" for event in exported):
        errors.append("tool execution actor is invalid")
    return errors


async def _normal_loop(root: Path) -> str:
    runtime = _runtime(root, MockModelGateway(plan_steps=[{"id": "step-1", "title": "完成任务"}], decisions=[ModelDecision.complete("已完成")]))
    run = await runtime.create_goal("常规任务", "完成一个小任务")
    await runtime.handle_message(run.id, "请完成它")
    finished = await runtime.approve_plan(run.id, 1)
    assert finished.state.value == "COMPLETED"
    assert check_invariants(runtime, run.id) == []
    return run.id


async def _clarification(root: Path) -> str:
    runtime = _runtime(root, MockModelGateway(clarification_required=True))
    run = await runtime.create_goal("澄清任务", "")
    assert (await runtime.handle_message(run.id, "请帮助我")).state.value == "CLARIFYING"
    assert (await runtime.handle_message(run.id, "使用本地项目")).state.value == "AWAITING_APPROVAL"
    return run.id


async def _plan_revision(root: Path) -> str:
    runtime = _runtime(root, MockModelGateway(plan_steps=[{"id": "old", "title": "旧步骤"}]))
    run = await runtime.create_goal("修改计划", "调整这份计划")
    await runtime.handle_message(run.id, "请制定计划")
    revised = await runtime.revise_plan(run.id, 1, [{"id": "new", "title": "新步骤"}])
    assert revised.version == 2
    return run.id


async def _step_cancel(root: Path) -> str:
    runtime = _runtime(root, MockModelGateway(plan_steps=[{"id": "step-1", "title": "取消步骤"}]))
    run = await runtime.create_goal("取消步骤", "停止一个步骤")
    await runtime.handle_message(run.id, "停止它")
    current = await runtime.cancel_step(run.id, "step-1")
    assert runtime.plans.current(run.id).steps[0].status == "cancelled"
    assert current.state.value == "AWAITING_APPROVAL"
    return run.id


async def _write_rejection(root: Path) -> str:
    call = ToolCall("write-rejected", "write_note", {"path": "rejected.md", "content": "no"})
    runtime = _runtime(root, MockModelGateway(plan_steps=[{"id": "step-1", "title": "写入内容"}], decisions=[ModelDecision.tool(call)]))
    run = await runtime.create_goal("拒绝写入", "不要写入")
    await runtime.handle_message(run.id, "仅在批准后写入")
    await runtime.approve_plan(run.id, 1)
    approval = runtime.pending_approvals(run.id)[0]
    await runtime.reject_approval(approval.id)
    assert not (root / "workspace" / "rejected.md").exists()
    assert any(event.type == "approval.rejected" for event in runtime.events.list(run.id))
    return run.id


async def _unknown_tool(root: Path) -> str:
    runtime = _runtime(root, MockModelGateway())
    try:
        runtime.tools.authorize(ToolCall("unknown", "missing_tool", {}), run_id="run", skill_tools=None)
    except ToolRejected:
        return "registry rejected unknown tool"
    raise AssertionError("unknown tool was accepted")


async def _rate_limit_retry(root: Path) -> str:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429)
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

    import os

    os.environ["EVAL_MODEL_KEY"] = "configured"
    gateway = ModelGateway(ModelProfile("https://provider.test", "demo", "EVAL_MODEL_KEY", max_attempts=2, network_retries=1, retry_base_seconds=0), transport=httpx.MockTransport(handler))
    response = await gateway.complete(ModelRequest(messages=[]))
    assert response.message == "ok" and response.attempts == 2 and attempts == 2
    return "two attempts"


async def _authentication_blocked(root: Path) -> str:
    import os

    os.environ.pop("EVAL_MISSING_KEY", None)
    gateway = ModelGateway(ModelProfile("https://provider.test", "demo", "EVAL_MISSING_KEY"))
    try:
        await gateway.complete(ModelRequest(messages=[]))
    except GatewayError as exc:
        assert exc.kind == "configuration"
        return "missing key is a hard configuration stop"
    raise AssertionError("missing key did not stop the call")


async def _budget_recovery(root: Path) -> str:
    runtime = _runtime(root, MockModelGateway(plan_steps=[{"id": "step-1", "title": "使用预算"}], decisions=[ModelDecision.continue_() for _ in range(5)]))
    run = await runtime.create_goal("预算恢复", "耗尽预算后恢复")
    await runtime.handle_message(run.id, "使用预算")
    assert (await runtime.approve_plan(run.id, 1)).state.value == "BLOCKED"
    await runtime.add_budget(run.id, 1)
    assert (await runtime.resume(run.id)).state.value == "COMPLETED"
    return run.id


async def _tool_failure(root: Path) -> str:
    runtime = _runtime(root, MockModelGateway())
    result = runtime.tools.execute(ToolCall("missing-note", "read_note", {"path": "missing.md"}), run_id="run", skill_tools=None)
    assert isinstance(result, ToolResult) and not result.ok
    return "read tool returns a closed standard error result"


async def _memory_confirmation(root: Path) -> str:
    runtime = _runtime(root, MockModelGateway())
    candidate = runtime.memory.create_candidate("run-memory", "goal-memory", "preference", "使用简洁的计划", "global", 0.9, [])
    assert runtime.memory.context_memories(None, None) == []
    runtime.memory.confirm(candidate.id)
    applied = runtime.memory.apply_confirmed("run-memory-2", "goal-memory", None, None)
    assert [item.id for item in applied] == [candidate.id]
    return "confirmed memory emitted memory.applied"


async def _restart_recovery(root: Path) -> str:
    call = ToolCall("write-once", "write_note", {"path": "once.md", "content": "once"})
    runtime = _runtime(root, MockModelGateway(plan_steps=[{"id": "step-1", "title": "只写入一次"}], decisions=[ModelDecision.tool(call), ModelDecision.await_outcome("等待")]))
    run = await runtime.create_goal("恢复任务", "恢复执行")
    await runtime.handle_message(run.id, "恢复执行")
    await runtime.approve_plan(run.id, 1)
    approval = runtime.pending_approvals(run.id)[0]
    await runtime.grant_approval(approval.id)
    before = len([event for event in runtime.events.list(run.id) if event.type == "tool.execution.started"])
    restarted = _runtime(root, MockModelGateway())
    resumed = await restarted.resume(run.id)
    after = len([event for event in restarted.events.list(run.id) if event.type == "tool.execution.started"])
    assert resumed.state.value == "AWAITING_OUTCOME" and before == after == 1
    return run.id


def _runtime(root: Path, model: MockModelGateway | object) -> AgentRuntime:
    db = Database(root / "agent.db", workspace=root / "workspace")
    events = EventStore(db)
    approvals = ApprovalService(db)
    return AgentRuntime(
        db=db,
        events=events,
        plans=PlanVersionService(db),
        approvals=approvals,
        checkpoints=CheckpointStore(db),
        memory=MemoryService(db, events, root / "memory"),
        tools=create_default_registry(root / "workspace", db=db, approval_service=approvals),
        model=model,
    )


_SCENARIOS: tuple[tuple[str, Scenario], ...] = (
    ("normal-loop", _normal_loop),
    ("clarification", _clarification),
    ("plan-revision", _plan_revision),
    ("step-cancel", _step_cancel),
    ("write-rejection", _write_rejection),
    ("unknown-tool", _unknown_tool),
    ("rate-limit-retry", _rate_limit_retry),
    ("authentication-blocked", _authentication_blocked),
    ("budget-recovery", _budget_recovery),
    ("tool-failure", _tool_failure),
    ("memory-confirmation", _memory_confirmation),
    ("restart-recovery", _restart_recovery),
)
