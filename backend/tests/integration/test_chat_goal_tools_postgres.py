"""D2 on PostgreSQL: the same pending-call chain against the real schema.

Reuses the scripted gateway and assertions from the SQLite unit suite, but the
write, approval and replay now run on the migrated PostgreSQL schema, including
the new ``turn_tool_calls`` table.
"""

from __future__ import annotations

import pytest

from test_chat_goal_tools import (
    _artifact_gateway,
    _assert_artifact_creation_race,
    ScriptedGateway,
    _answer,
    _count,
    _tool_call,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True])
async def test_first_artifact_race_on_postgres(chat_runtime, monkeypatch, deleted):
    await _assert_artifact_creation_race(chat_runtime(_artifact_gateway()), monkeypatch, deleted=deleted)


def _make_pg_runtime(url, tmp_path, gateway):
    from app.db import Database
    from app.domain import ApprovalService, CheckpointStore, PlanVersionService
    from app.events import EventStore
    from app.goal_program_compiler import FixedGoalProgramCompiler
    from app.goal_tools import register_goal_tools
    from app.live_model import LiveConversationModel
    from app.memory import MemoryService
    from app.runtime import AgentRuntime
    from app.tools import create_default_registry

    db = Database(url, workspace=tmp_path / "workspace")
    events = EventStore(db)
    approvals = ApprovalService(db)
    model = LiveConversationModel(gateway)
    runtime = AgentRuntime(
        db=db,
        events=events,
        plans=PlanVersionService(db),
        approvals=approvals,
        checkpoints=CheckpointStore(db),
        memory=MemoryService(db, events, tmp_path / "memory"),
        tools=create_default_registry(tmp_path / "workspace", db=db, approval_service=approvals),
        model=model,
    )
    runtime.goal_programs.compiler = FixedGoalProgramCompiler()
    register_goal_tools(
        runtime.tools,
        goal_programs=runtime.goal_programs,
        plan_documents=runtime.plan_documents,
    )
    return runtime


@pytest.fixture
def chat_runtime(migrated_postgres_url, tmp_path):
    created = []

    def build(gateway):
        runtime = _make_pg_runtime(migrated_postgres_url, tmp_path, gateway)
        created.append(runtime)
        return runtime

    yield build
    for runtime in created:
        runtime.db.close()


@pytest.mark.asyncio
async def test_approved_chat_write_commits_once_on_postgres(chat_runtime) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("create_plan_draft", {"title": "攻略", "markdown_content": "# 攻略"})]},
        _answer("已保存。"),
    ])
    runtime = chat_runtime(gateway)
    thread = runtime.conversation.create_thread("pg tools")
    accepted = runtime.conversation.accept_turn(thread.id, "pg-approve", "保存这份攻略", [])

    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(accepted.turn_id)
    assert paused.status == "AWAITING_TOOL_APPROVAL"
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)

    first = runtime.conversation.decide_tool_call(accepted.turn_id, "approve", paused.version, "pg-decision")
    replay = runtime.conversation.decide_tool_call(accepted.turn_id, "approve", paused.version, "pg-decision")
    assert first.id == replay.id
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(first.id).status == "COMPLETED"
    assert _count(runtime, "plan_documents") == 1
    assert _count(runtime, "plan_document_versions") == 1
    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "EXECUTED"
    assert stored.result["ok"] is True


@pytest.mark.asyncio
async def test_modify_tool_commits_one_version_on_postgres(chat_runtime) -> None:
    import json
    import re

    def modify_call(request):
        text = json.dumps(request.messages, ensure_ascii=False)
        match = re.search(r"document_id=(plan_[0-9a-f]+) version_id=(planv_[0-9a-f]+)", text)
        assert match, "plan context ids missing"
        return {"tool_calls": [_tool_call("modify_plan_document", {
            "document_id": match.group(1),
            "expected_version_id": match.group(2),
            "title": "攻略（减量）",
            "markdown_content": "# 攻略\n\n- 减少一个景点",
        })]}

    gateway = ScriptedGateway([modify_call, _answer("计划文档已更新。")])
    runtime = chat_runtime(gateway)
    thread = runtime.conversation.create_thread("pg modify")
    runtime.plan_documents.save_model_revision(
        thread_id=thread.id, title="攻略", markdown_content="# 攻略\n\n- 两个景点",
        source_turn_id=None, source_message_id=None, actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "pg-modify", "把攻略改简单一点", [])

    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(accepted.turn_id)
    assert paused.status == "AWAITING_TOOL_APPROVAL"
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)
    assert pending.tool_name == "modify_plan_document"

    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "pg-modify-decision"
    )
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(continuation.id).status == "COMPLETED"
    with runtime.db.connection() as connection:
        committed = connection.execute(
            "SELECT COUNT(*) AS total FROM plan_document_versions WHERE status='committed'"
        ).fetchone()["total"]
    assert committed == 2
    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "EXECUTED"
    assert stored.result["data"]["execution_updated"] is False


@pytest.mark.asyncio
async def test_rejected_chat_write_never_writes_on_postgres(chat_runtime) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("create_plan_draft", {"title": "攻略", "markdown_content": "# 攻略"})]},
        _answer("没有保存。"),
    ])
    runtime = chat_runtime(gateway)
    thread = runtime.conversation.create_thread("pg tools")
    accepted = runtime.conversation.accept_turn(thread.id, "pg-reject", "保存这份攻略", [])

    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(accepted.turn_id)
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)
    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "reject", paused.version, "pg-reject-decision"
    )
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(continuation.id).status == "COMPLETED"
    assert _count(runtime, "plan_documents") == 0
    assert runtime.conversation.chat_tool_calls.get(pending.id).status == "REJECTED"
    with runtime.db.connection() as connection:
        approval = connection.execute(
            "SELECT status FROM approvals WHERE id=%s", (pending.approval_id,)
        ).fetchone()
    assert approval["status"] == "rejected"
