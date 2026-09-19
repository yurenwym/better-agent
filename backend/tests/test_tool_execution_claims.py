import threading
import time

import pytest


@pytest.mark.asyncio
async def test_write_timeout_blocks_runtime_and_preserves_claim_after_late_effect(tmp_path):
    from app.runtime import MockModelGateway, ModelDecision
    from app.tools import ToolCall, ToolRegistry, ToolReconciliationRequired, ToolResult, ToolRisk, ToolSpec
    from test_runtime import make_runtime

    call = ToolCall("slow-write", "write_note", {"path": "late.md", "content": "once"})
    runtime = make_runtime(tmp_path, MockModelGateway(
        plan_steps=[{"id": "step-1", "title": "Write"}],
        decisions=[ModelDecision.tool(call), ModelDecision.complete("must not advance")],
    ))
    release = threading.Event()
    finished = threading.Event()
    effects = []

    def effect(params):
        try:
            if not release.wait(5):
                raise RuntimeError("test did not release handler")
            effects.append(params["content"])
            return ToolResult(True, "done")
        finally:
            finished.set()

    spec = ToolSpec("write_note", "write", {
        "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"], "additionalProperties": False,
    }, ToolRisk.WRITE, effect, timeout_seconds=.01)
    runtime.tools = ToolRegistry(tmp_path / "workspace", db=runtime.db, approval_service=runtime.approvals)
    runtime.tools.register(spec)
    run = await runtime.create_goal("Write", "Write once")
    await runtime.handle_message(run.id, "Write with approval")
    await runtime.approve_plan(run.id, 1)
    approval = runtime.pending_approvals(run.id)[0]
    try:
        blocked = await runtime.grant_approval(approval.id)
        assert blocked.state.value == "BLOCKED"
        assert blocked.budget["blocked_reason_code"] == "TOOL_RECONCILIATION_REQUIRED"
        assert effects == []
        assert runtime.checkpoints.latest(run.id).pending_actions[0]["id"] == call.id
        resumed = await runtime.resume(run.id)
        assert resumed.state.value == "BLOCKED"
        assert resumed.budget["blocked_reason_code"] == "TOOL_RECONCILIATION_REQUIRED"
    finally:
        release.set()
        assert finished.wait(5)

    assert effects == ["once"]
    with runtime.db.connection() as connection:
        claim = connection.execute("SELECT status,error_code FROM tool_execution_claims WHERE tool_call_id=?", (call.id,)).fetchone()
        assert claim["status"] == "RECONCILIATION_REQUIRED"
        assert claim["error_code"] == "timeout"
        assert connection.execute("SELECT COUNT(*) FROM tool_calls WHERE id=?", (call.id,)).fetchone()[0] == 0
    # Recreating the registry must not clear the durable uncertainty.
    registry = ToolRegistry(tmp_path / "workspace", db=runtime.db, approval_service=runtime.approvals)
    registry.register(spec)
    with pytest.raises(ToolReconciliationRequired):
        registry.execute(call, run_id=run.id, skill_tools=None)
    events = [event for event in runtime.events.list(run.id) if event.type == "tool.reconciliation_required"]
    assert len(events) == 1
    assert effects == ["once"]


def test_database_claim_allows_only_one_concurrent_tool_effect(tmp_path):
    from app.db import Database
    from app.tools import ToolCall, ToolRegistry, ToolRejected, ToolResult, ToolRisk, ToolSpec

    db = Database(tmp_path / "agent.db")
    count = 0
    lock = threading.Lock()

    def effect(_):
        nonlocal count
        with lock:
            count += 1
        time.sleep(.05)
        return ToolResult(True, "done")

    registry = ToolRegistry(tmp_path / "workspace", db=db)
    registry.register(ToolSpec("effect", "effect", {"type":"object","properties":{},"additionalProperties":False}, ToolRisk.PURE, effect))
    call = ToolCall("same-call", "effect", {})
    results = []

    def execute():
        try: results.append(registry.execute(call, run_id="run", skill_tools=None))
        except ToolRejected: results.append("rejected")

    first = threading.Thread(target=execute); second = threading.Thread(target=execute)
    first.start(); second.start(); first.join(); second.join()
    assert count == 1
    assert len([item for item in results if item != "rejected"]) == 1
    assert registry.execute(call, run_id="run", skill_tools=None).summary == "done"


def test_stale_write_claim_requires_reconciliation_instead_of_replay(tmp_path):
    from app.db import Database
    from app.domain import ApprovalService
    from app.tools import ToolCall, ToolRejected, create_default_registry

    db = Database(tmp_path / "agent.db")
    approvals = ApprovalService(db)
    registry = create_default_registry(tmp_path / "workspace", db=db, approval_service=approvals)
    call = ToolCall("write-once", "write_note", {"path":"x.md","content":"x"})
    approval = approvals.request("run", call.id, call.name, call.params)
    approvals.grant(approval.id, "run", call.id, call.params)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO tool_execution_claims(logical_action_key,run_id,tool_call_id,tool_name,params_hash,status,created_at,updated_at) VALUES (?,?,?,?,?,'RUNNING',datetime('now'),datetime('now'))",
            ("run:write-once", "run", call.id, call.name, approval.params_hash),
        )
    with pytest.raises(ToolRejected, match="reconciliation"):
        registry.execute(call, run_id="run", skill_tools={"write_note"})
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM tool_execution_claims WHERE logical_action_key='run:write-once'").fetchone()[0] == "RECONCILIATION_REQUIRED"
    assert not (tmp_path / "workspace" / "x.md").exists()


@pytest.mark.asyncio
async def test_stale_write_claim_appends_reconciliation_event_for_a_real_run(tmp_path):
    from app.domain import ApprovalService
    from app.runtime import MockModelGateway
    from app.tools import ToolCall, ToolRejected, create_default_registry
    from test_runtime import make_runtime

    runtime = make_runtime(tmp_path, MockModelGateway())
    run = await runtime.create_goal("Write", "Write")
    approvals = ApprovalService(runtime.db)
    registry = create_default_registry(tmp_path / "workspace-2", db=runtime.db, approval_service=approvals)
    call = ToolCall("write-once", "write_note", {"path": "x.md", "content": "x"})
    approval = approvals.request(run.id, call.id, call.name, call.params)
    approvals.grant(approval.id, run.id, call.id, call.params)
    with runtime.db.transaction() as connection:
        connection.execute(
            "INSERT INTO tool_execution_claims(logical_action_key,run_id,tool_call_id,tool_name,params_hash,status,created_at,updated_at) VALUES (?,?,?,?,?,'RUNNING',datetime('now'),datetime('now'))",
            (f"{run.id}:{call.id}", run.id, call.id, call.name, approval.params_hash),
        )
    with pytest.raises(ToolRejected, match="reconciliation"):
        registry.execute(call, run_id=run.id, skill_tools={"write_note"})
    events = [event for event in runtime.events.list(run.id) if event.type == "tool.reconciliation_required"]
    assert len(events) == 1
    assert events[0].data == {"tool_call_id": call.id, "tool_name": call.name}
