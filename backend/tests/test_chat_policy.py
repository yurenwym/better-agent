from dataclasses import replace

import pytest

from app.chat_tools import ChatToolCallStore, ChatToolRunner
from app.conversation import ConversationService
from app.db import Database
from app.tools import ToolRegistry, ToolResult, ToolRisk, ToolSpec


@pytest.mark.asyncio
@pytest.mark.parametrize("risk", [ToolRisk.READ, ToolRisk.WRITE])
@pytest.mark.parametrize("fault", [None, "owner", "deleted", "missing_context", "foreign_trace"])
async def test_policy_uses_current_persisted_identity_before_read_or_approval(tmp_path, fault, risk):
    db = Database(tmp_path / "policy.db")
    try:
        conversation = ConversationService(db)
        thread = conversation.create_thread("policy")
        accepted = conversation.accept_turn(thread.id, "one", "read", [])
        root = conversation.harness_context.load_turn_context(accepted.turn_id)
        calls = []
        from app.domain import ApprovalService
        approvals = ApprovalService(db)
        registry = ToolRegistry(tmp_path, db=db, approval_service=approvals)
        registry.register(ToolSpec("read", "read", {"type": "object"}, risk,
                                  lambda _: (calls.append("read"), ToolResult(True, "read"))[1]))
        runner = ChatToolRunner(registry, approvals, ChatToolCallStore(db), turn_id=accepted.turn_id,
            thread_id=thread.id, owner_id="foreign" if fault == "owner" else "local-user",
            project_id=None, skill_names=(), runtime_bundle_id=root.runtime_bundle_id,
            root_budget_id=root.root_budget_id, harness=root, capability_names=frozenset({"read"}))
        if fault in {"deleted", "missing_context"}:
            with db.transaction() as connection:
                if fault == "deleted":
                    connection.execute("UPDATE threads SET deleted_at='2026-10-10' WHERE id=?", (thread.id,))
                else:
                    connection.execute("UPDATE turns SET execution_context_json=NULL,execution_context_digest=NULL WHERE id=?", (accepted.turn_id,))
        parent = replace(root, trace_id="a" * 32) if fault == "foreign_trace" else None
        outcome = await runner.execute(tool_name="read", params={}, parent_harness=parent)
        waiting = fault is None and risk == ToolRisk.WRITE
        assert outcome.pending == waiting
        if not waiting:
            assert outcome.result.ok == (fault is None)
        assert calls == (["read"] if fault is None and not waiting else [])
        if fault:
            assert outcome.result.error == "CONTEXT_IDENTITY_CONFLICT"
            assert outcome.result.effect == "not_started"
        with db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == int(waiting)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "schema", "version", "source_context", "cancelled"])
async def test_approval_rechecks_definition_and_source_before_writing(tmp_path, change):
    from app.domain import ApprovalService
    db = Database(tmp_path / "definition.db")
    try:
        conversation = ConversationService(db)
        thread = conversation.create_thread("source")
        accepted = conversation.accept_turn(thread.id, "one", "write", [])
        root = conversation.harness_context.load_turn_context(accepted.turn_id)
        approvals = ApprovalService(db)
        store = ChatToolCallStore(db)
        registry = ToolRegistry(tmp_path, db=db, approval_service=approvals)
        writes = []
        spec = ToolSpec("write", "write", {"type": "object"}, ToolRisk.WRITE,
                        lambda _: (writes.append(1), ToolResult(True, "written"))[1])
        registry.register(spec)
        runner = ChatToolRunner(registry, approvals, store, turn_id=accepted.turn_id,
            thread_id=thread.id, owner_id=root.owner_id, project_id=None, skill_names=(),
            runtime_bundle_id=root.runtime_bundle_id, root_budget_id=root.root_budget_id,
            harness=root, capability_names=frozenset({"write"}))
        pending = await runner.execute(tool_name="write", params={})
        call = store.get(pending.call_id)
        approvals.grant(call.approval_id, runner.run_id, call.id, call.params, call.binding)
        with db.transaction() as connection:
            connection.execute("UPDATE turn_tool_calls SET status='APPROVED' WHERE id=?", (call.id,))
        call = store.get(call.id)
        if change in {"schema", "version"}:
            registry.unregister("write")
            registry.register(replace(spec, **({"version": "2"} if change == "version" else
                                              {"schema": {"type": "object", "additionalProperties": False}})))
        if change == "source_context":
            with db.transaction() as connection:
                connection.execute("UPDATE turns SET execution_context_json=NULL,execution_context_digest=NULL WHERE id=?", (accepted.turn_id,))
        if change == "cancelled":
            with db.transaction() as connection:
                connection.execute("UPDATE turn_tool_calls SET status='CANCELLED' WHERE id=?", (call.id,))
        result = await runner.resume(call)
        assert result.ok == (change is None)
        assert writes == ([1] if change is None else [])
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "owner", "thread", "deleted", "stale_result"])
async def test_resume_scopes_cached_result_and_reloads_durable_call(tmp_path, fault):
    db = Database(tmp_path / "resume.db")
    try:
        conversation = ConversationService(db)
        thread = conversation.create_thread("source")
        accepted = conversation.accept_turn(thread.id, "one", "read", [])
        store = ChatToolCallStore(db)
        call = store.create(turn_id=accepted.turn_id, thread_id=thread.id,
                            tool_name="read", params={}, risk="READ", status="EXECUTED",
                            result=ToolResult(True, "private result").as_dict())
        if fault == "deleted":
            with db.transaction() as connection:
                connection.execute("UPDATE threads SET deleted_at='2026-10-10' WHERE id=?", (thread.id,))
        if fault == "stale_result":
            call = replace(call, result=ToolResult(True, "stale result").as_dict())
        runner = ChatToolRunner(ToolRegistry(tmp_path), None, store,
            turn_id=accepted.turn_id,
            thread_id=conversation.create_thread("other").id if fault == "thread" else thread.id,
            owner_id="other" if fault == "owner" else "local-user",
            project_id=None, skill_names=())
        result = await runner.resume(call)
        if fault in {"owner", "thread", "deleted"}:
            assert not result.ok
            assert result.error == "CONTEXT_IDENTITY_CONFLICT"
            assert "private result" not in result.summary
        else:
            assert result.ok
            assert result.summary == "private result"
        assert store.get(call.id).result == ToolResult(True, "private result").as_dict()
    finally:
        db.close()
