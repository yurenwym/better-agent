from __future__ import annotations

import asyncio
import hashlib
import json
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


ROUTING_CATEGORY_MINIMUMS = {
    "direct": 6, "research": 6, "implicit_research": 6, "expert": 6,
    "remember": 6, "clarify": 6, "goal_read": 6, "goal_write": 6,
    "negation": 6, "mixed": 6, "plan_publish": 6, "plan_save": 4,
}
ROUTING_OUTCOMES = frozenset({
    "final", "handoff:research", "handoff:expert", "tool:remember", "ask",
    "tool:query_goals", "approval", "final+plan_document",
})


def routing_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def routing_holdout_record(suite: dict) -> dict:
    holdout = sorted(
        (case for case in suite["cases"] if case["partition"] == "HOLDOUT"),
        key=lambda case: case["case_id"],
    )
    return {"suite": suite["suite"], "count": len(holdout), "sha256": routing_digest(holdout)}


def load_routing_suite(path: Path, freeze_path: Path) -> dict:
    suite = json.loads(path.read_text(encoding="utf-8"))
    if suite.get("suite") != "agent-loop-routing-v1" or suite.get("version") != 1:
        raise ValueError("unsupported routing suite")
    seen: set[str] = set()
    counts = dict.fromkeys(ROUTING_CATEGORY_MINIMUMS, 0)
    for case in suite["cases"]:
        case_id = case["case_id"]
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError("duplicate or invalid case_id")
        seen.add(case_id)
        if case["partition"] not in {"DEV", "HOLDOUT"} or case["category"] not in counts:
            raise ValueError(f"invalid partition/category: {case_id}")
        counts[case["category"]] += 1
        if not isinstance(case["input"], str) or not case["input"].strip():
            raise ValueError(f"empty input: {case_id}")
        if not isinstance(case["history"], list) or any(
            not isinstance(message, dict)
            or message.get("role") not in {"user", "assistant"}
            or not isinstance(message.get("content"), str)
            for message in case["history"]
        ):
            raise ValueError(f"invalid history: {case_id}")
        for key in ("expected", "forbidden"):
            if not isinstance(case[key], list) or any(item not in ROUTING_OUTCOMES for item in case[key]):
                raise ValueError(f"invalid {key}: {case_id}")
        if not case["expected"] or set(case["expected"]) & set(case["forbidden"]):
            raise ValueError(f"contradictory expectation: {case_id}")
    if any(counts[key] < minimum for key, minimum in ROUTING_CATEGORY_MINIMUMS.items()):
        raise ValueError("routing category minimum not met")
    frozen = json.loads(freeze_path.read_text(encoding="utf-8"))
    actual = routing_holdout_record(suite)
    if actual != frozen or actual["count"] < suite["planned_holdout_count"]:
        raise ValueError("HOLDOUT freeze mismatch")
    return suite


def run_routing_mock(suite: dict, *, mode: str = "legacy", responses: dict | None = None) -> dict:
    """Exercise report accounting only. No production route or provider is invoked.

    The default fixed response is deliberately independent of expected labels.
    Injected responses allow invalid/cancelled accounting to be checked offline.
    """
    if mode != "legacy":
        raise ValueError("T00 supports legacy mock only")
    responses = responses if responses is not None else {
        case["case_id"]: {"status": "valid", "observed": ["final"]}
        for case in suite["cases"]
    }
    if set(responses) - {case["case_id"] for case in suite["cases"]}:
        raise ValueError("unknown response case_id")
    rows = []
    for case in suite["cases"]:
        response = responses.get(case["case_id"], {})
        status = response.get("status", "invalid") if isinstance(response, dict) else "invalid"
        observed = response.get("observed", []) if isinstance(response, dict) else []
        if status not in {"valid", "invalid", "cancelled"}:
            status = "invalid"
        if not isinstance(observed, list) or not observed or any(item not in ROUTING_OUTCOMES for item in observed):
            observed = []
            if status == "valid":
                status = "invalid"
        forbidden_hit = bool(set(observed) & set(case["forbidden"]))
        rows.append({
            "case_id": case["case_id"], "partition": case["partition"], "category": case["category"],
            "expected": case["expected"], "forbidden": case["forbidden"],
            "observed": observed, "status": status, "forbidden_hit": forbidden_hit,
            "matched": status == "valid" and set(case["expected"]) <= set(observed) and not forbidden_hit,
            "model_calls": 0, "tokens": None, "cost_usd": None,
            "cost_source": "not_applicable_mock", "first_token_ms": None,
        })

    def summarize(items: list[dict]) -> dict:
        return {
            "planned": len(items), "matched": sum(row["matched"] for row in items),
            "match_fraction": sum(row["matched"] for row in items) / len(items) if items else None,
            "invalid": sum(row["status"] == "invalid" for row in items),
            "cancelled": sum(row["status"] == "cancelled" for row in items),
            "forbidden_hits": sum(row["forbidden_hit"] for row in items),
        }

    return {
        "suite": suite["suite"], "suite_sha256": routing_digest(suite),
        "holdout": routing_holdout_record(suite), "mode": mode, "provider": "mock",
        "scope": "report_accounting_only", "production_route_executed": False,
        "engineering": "PASS", "routing_quality": "INSUFFICIENT_EVIDENCE",
        "summary": summarize(rows),
        "by_category": {key: summarize([row for row in rows if row["category"] == key]) for key in ROUTING_CATEGORY_MINIMUMS},
        "by_partition": {key: summarize([row for row in rows if row["partition"] == key]) for key in ("DEV", "HOLDOUT")},
        "invalid_case_ids": [row["case_id"] for row in rows if row["status"] == "invalid"],
        "cancelled_case_ids": [row["case_id"] for row in rows if row["status"] == "cancelled"],
        "cases": rows,
    }


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
