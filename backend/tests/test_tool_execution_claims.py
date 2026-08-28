import threading
import time

import pytest


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
