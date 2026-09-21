"""D2 acceptance: plain chat executes the eight goal business tools.

The tests drive the real ``ManagedTurnWorker`` with a scripted model gateway,
so the tool loop, the pending-approval pause, the continuation resume and the
transcript replay all run through the production code paths. Deterministic
compiler fixtures mean no network call is made.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from test_runtime import make_runtime


def _tool_call(name: str, arguments: dict | None = None) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {}, ensure_ascii=False)},
    }


class ScriptedGateway:
    """Pops one scripted response per model call and records every request.

    A scripted entry may be a dict or a callable receiving the request, which
    lets a later call use identifiers returned by an earlier tool result.
    """

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.requests: list[list[dict]] = []

    async def complete(self, request, **kwargs):
        self.requests.append(list(request.messages))
        if not self.responses:
            raise AssertionError("gateway ran out of scripted responses")
        scripted = self.responses.pop(0)
        if callable(scripted):
            scripted = scripted(request)
        message = scripted.get("message", "")
        tool_calls = scripted.get("tool_calls", [])
        if message and kwargs.get("on_text_delta") is not None:
            kwargs["on_text_delta"](message)
        return SimpleNamespace(message=message, tool_calls=tool_calls, finish_reason="stop")


def _last_tool_data(request) -> dict:
    for message in reversed(request.messages):
        if message.get("role") == "tool":
            return json.loads(message["content"]).get("data") or {}
    raise AssertionError("no tool result in request history")


def _answer(body: str) -> dict:
    return {
        "message": (
            '{"v":1,"policy":"answer","content_shape":"general","reason_code":"content_only"}\n'
            + body
        )
    }


def _make_runtime(tmp_path, gateway):
    from app.goal_program_compiler import FixedGoalProgramCompiler
    from app.goal_tools import register_goal_tools
    from app.live_model import LiveConversationModel

    runtime = make_runtime(tmp_path, LiveConversationModel(gateway))
    runtime.goal_programs.compiler = FixedGoalProgramCompiler()
    register_goal_tools(
        runtime.tools,
        goal_programs=runtime.goal_programs,
        plan_documents=runtime.plan_documents,
    )
    return runtime


def _count(runtime, table: str) -> int:
    with runtime.db.connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _tool_rows(runtime, turn_id: str):
    with runtime.db.connection() as connection:
        return connection.execute(
            "SELECT * FROM turn_tool_calls WHERE turn_id=? ORDER BY created_at,id", (turn_id,)
        ).fetchall()


async def _assert_artifact_creation_race(runtime, monkeypatch, *, deleted):
    """Insert another committed writer after the worker's empty-document read."""
    thread = runtime.conversation.create_thread("artifact create race")
    accepted = runtime.conversation.accept_turn(thread.id, "artifact-race", "保存一份旅游攻略文档", [])
    original = runtime.plan_documents.save_model_revision
    winner = []

    def interleave(**kwargs):
        if kwargs.get("source_turn_id") == accepted.turn_id and not winner:
            version = original(thread_id=thread.id, title="并发入口的攻略",
                               markdown_content="# 并发入口\n\n保留这一份内容。",
                               source_turn_id=None, source_message_id=None, actor="user", create_only=True)
            winner.append(version)
            if deleted:
                runtime.plan_documents.delete_document(version.plan_document_id,
                    expected_version=version.version, expected_file_hash=version.content_hash)
        return original(**kwargs)

    monkeypatch.setattr(runtime.plan_documents, "save_model_revision", interleave)
    assert await runtime.turn_worker.run_once()
    assert len(winner) == 1
    version = winner[0]
    assert _count(runtime, "plan_document_versions") == 1
    assert _count(runtime, "approvals") == 0
    assert _count(runtime, "goal_programs") == 0
    with runtime.db.connection() as connection:
        doc = connection.execute("SELECT * FROM plan_documents WHERE id=?", (version.plan_document_id,)).fetchone()
        assert (doc["deleted_at"] is not None) == deleted
        assert doc["current_version_id"] == version.id
        assert connection.execute("SELECT COUNT(*) FROM plan_document_versions WHERE source_turn_id=?",
                                  (accepted.turn_id,)).fetchone()[0] == 0
    # This is an explicit conflict, never a silent overwrite or resurrection.
        event = connection.execute("SELECT * FROM thread_events WHERE turn_id=? AND type='plan.document_conflict'",
                                   (accepted.turn_id,)).fetchone()
        assert event is not None


def _artifact_gateway():
    return ScriptedGateway([
        {"message": '{"v":2,"policy":"answer","content_shape":"plan","reason_code":"explicit_save",'
                    '"artifact":{"kind":"plan_document","operation":"upsert","title":"新攻略"}}\n# 新攻略\n\n新内容。'},
        {"message": '{"plan_document_request":true}'},
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True])
async def test_first_artifact_race_preserves_other_writer_and_tombstone(tmp_path, monkeypatch, deleted):
    await _assert_artifact_creation_race(_make_runtime(tmp_path, _artifact_gateway()), monkeypatch, deleted=deleted)


@pytest.mark.asyncio
async def test_read_tool_executes_inline_without_execution_rows(tmp_path) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("get_today_tasks", {})]},
        _answer("今天没有安排行动。"),
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("chat tools")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-read-1", "今天要做什么？", [])

    assert await runtime.turn_worker.run_once() is True

    turn = runtime.conversation.turn(accepted.turn_id)
    assert turn.status == "COMPLETED"
    messages = runtime.conversation.messages(thread.id)
    assert messages[-1].content == "今天没有安排行动。"
    rows = _tool_rows(runtime, accepted.turn_id)
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "get_today_tasks"
    assert rows[0]["status"] == "EXECUTED"
    assert rows[0]["risk"] == "READ"
    assert _count(runtime, "goals") == 0
    assert _count(runtime, "runs") == 0
    assert _count(runtime, "approvals") == 0


@pytest.mark.asyncio
async def test_write_tool_pauses_until_approved_then_commits_document(tmp_path) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("create_plan_draft", {"title": "桂林旅游攻略", "markdown_content": "# 桂林\n第一天…"})]},
        _answer("攻略已保存，可在计划页面查看。"),
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("chat tools")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-write-1", "保存这份攻略", [])

    assert await runtime.turn_worker.run_once() is True

    paused = runtime.conversation.turn(accepted.turn_id)
    assert paused.status == "AWAITING_TOOL_APPROVAL"
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)
    assert pending is not None
    assert pending.tool_name == "create_plan_draft"
    assert pending.status == "PENDING_APPROVAL"
    assert _count(runtime, "plan_documents") == 0
    with runtime.db.connection() as connection:
        approval = connection.execute(
            "SELECT status FROM approvals WHERE id=?", (pending.approval_id,)
        ).fetchone()
    assert approval["status"] == "pending"

    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "decision-1"
    )

    assert continuation.parent_turn_id == accepted.turn_id
    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    assert await runtime.turn_worker.run_once() is True
    finished = runtime.conversation.turn(continuation.id)
    assert finished.status == "COMPLETED"
    assert _count(runtime, "plan_documents") == 1
    assert _count(runtime, "plan_document_versions") == 1
    assert _count(runtime, "goals") == 0
    assert _count(runtime, "runs") == 0
    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "EXECUTED"
    assert stored.result["ok"] is True
    assert stored.result["data"]["document_id"]
    assert stored.result["data"]["activated"] is False


@pytest.mark.asyncio
async def test_rejected_write_never_writes_and_reports_rejection(tmp_path) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("create_plan_draft", {"title": "草稿", "markdown_content": "# 草稿"})]},
        _answer("好的，我没有保存这份文档。"),
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("chat tools")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-write-2", "保存这份攻略", [])

    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(accepted.turn_id)
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)
    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "reject", paused.version, "decision-reject"
    )

    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(continuation.id).status == "COMPLETED"
    assert _count(runtime, "plan_documents") == 0
    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "REJECTED"
    with runtime.db.connection() as connection:
        approval = connection.execute(
            "SELECT status FROM approvals WHERE id=?", (pending.approval_id,)
        ).fetchone()
    assert approval["status"] == "rejected"
    resumed = [event for event in runtime.conversation.events.list(thread.id)
               if event.type == "chat_tool.resumed"]
    assert len(resumed) == 1
    assert resumed[0].data["ok"] is False
    assert resumed[0].data["error"] == "USER_REJECTED"
    assert runtime.conversation.messages(thread.id)[-1].content == "好的，我没有保存这份文档。"


@pytest.mark.asyncio
async def test_tool_decision_replay_never_double_writes(tmp_path) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("create_plan_draft", {"title": "草稿", "markdown_content": "# 草稿"})]},
        _answer("已保存。"),
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("chat tools")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-replay", "保存这份攻略", [])
    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(accepted.turn_id)

    first = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "decision-replay"
    )
    second = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "decision-replay"
    )

    assert first.id == second.id
    assert await runtime.turn_worker.run_once() is True
    assert _count(runtime, "plan_document_versions") == 1
    assert len(_tool_rows(runtime, accepted.turn_id)) == 1
    with pytest.raises(ValueError, match="no pending tool call"):
        runtime.conversation.decide_tool_call(
            accepted.turn_id, "reject", paused.version, "decision-other"
        )


@pytest.mark.asyncio
async def test_cancelled_turn_rejects_pending_approval(tmp_path) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("create_plan_draft", {"title": "草稿", "markdown_content": "# 草稿"})]},
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("chat tools")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-cancel", "保存这份攻略", [])
    await runtime.turn_worker.run_once()
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)

    cancelled = runtime.conversation.cancel_turn(accepted.turn_id)

    assert cancelled.status == "CANCELLED"
    assert runtime.conversation.chat_tool_calls.get(pending.id).status == "CANCELLED"
    with runtime.db.connection() as connection:
        approval = connection.execute(
            "SELECT status FROM approvals WHERE id=?", (pending.approval_id,)
        ).fetchone()
    assert approval["status"] == "rejected"
    assert _count(runtime, "plan_documents") == 0


@pytest.mark.asyncio
async def test_later_turn_replays_the_tool_exchange_to_the_model(tmp_path) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("get_today_tasks", {})]},
        _answer("今天没有安排行动。"),
        _answer("仍然没有安排。"),
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("chat tools")
    first = runtime.conversation.accept_turn(thread.id, "turn-history-1", "今天要做什么？", [])
    await runtime.turn_worker.run_once()
    assert runtime.conversation.turn(first.turn_id).status == "COMPLETED"

    second = runtime.conversation.accept_turn(thread.id, "turn-history-2", "再确认一次", [])
    await runtime.turn_worker.run_once()
    assert runtime.conversation.turn(second.turn_id).status == "COMPLETED"

    final_request = gateway.requests[-1]
    tool_messages = [message for message in final_request if message.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "get_today_tasks" in json.dumps(final_request, ensure_ascii=False)
    assert "items" in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_crash_after_commit_before_result_persistence_recovers_same_version(tmp_path, monkeypatch) -> None:
    """The committed document is never written twice when the result is lost."""
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("create_plan_draft", {"title": "攻略", "markdown_content": "# 攻略"})]},
        _answer("已保存。"),
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("chat tools")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-recovery", "保存这份攻略", [])
    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(accepted.turn_id)
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)
    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "decision-recovery"
    )

    from app.chat_tools import ChatToolCallStore, ChatToolRunner

    store = runtime.conversation.chat_tool_calls
    call = store.get(pending.id)
    runner = ChatToolRunner(
        runtime.tools, runtime.approvals, store,
        turn_id=continuation.id, thread_id=thread.id, owner_id="local-user",
        project_id=None, skill_names=(),
        goal_programs=runtime.goal_programs, plan_documents=runtime.plan_documents,
    )
    original = ChatToolCallStore.record_result

    def flaky(self, call_id, *, result, error_code, status):
        if call_id == pending.id:
            raise RuntimeError("simulated crash before the tool result was persisted")
        return original(self, call_id, result=result, error_code=error_code, status=status)

    monkeypatch.setattr(ChatToolCallStore, "record_result", flaky)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await runner.resume(call)

    # The business write committed, but the chat row still has no result.
    assert _count(runtime, "plan_document_versions") == 1
    assert runtime.conversation.chat_tool_calls.get(pending.id).result is None

    monkeypatch.setattr(ChatToolCallStore, "record_result", original)
    recovered = await runner.resume(call)

    assert recovered.ok is True
    assert _count(runtime, "plan_document_versions") == 1
    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "EXECUTED"
    assert stored.result["ok"] is True


@pytest.mark.asyncio
async def test_modify_tool_pauses_for_approval_and_commits_one_version(tmp_path) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("modify_plan_document", {
            "document_id": "placeholder",
            "expected_version_id": "placeholder",
            "title": "攻略（减量）",
            "markdown_content": "# 攻略\n\n- 减少一个景点",
        })]},
        _answer("计划文档已更新。"),
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("chat modify")
    document = runtime.plan_documents.save_model_revision(
        thread_id=thread.id, title="攻略", markdown_content="# 攻略\n\n- 两个景点",
        source_turn_id=None, source_message_id=None, actor="model",
    )
    # Feed the real identifiers into the scripted call.
    gateway.responses[0]["tool_calls"][0]["function"]["arguments"] = json.dumps({
        "document_id": document.plan_document_id,
        "expected_version_id": document.id,
        "title": "攻略（减量）",
        "markdown_content": "# 攻略\n\n- 减少一个景点",
    }, ensure_ascii=False)

    accepted = runtime.conversation.accept_turn(thread.id, "turn-modify", "把攻略改简单一点", [])
    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(accepted.turn_id)
    assert paused.status == "AWAITING_TOOL_APPROVAL"
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)
    assert pending.tool_name == "modify_plan_document"

    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "decision-modify"
    )
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(continuation.id).status == "COMPLETED"
    assert _count(runtime, "plan_document_versions") == 2
    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "EXECUTED"
    assert stored.result["ok"] is True
    assert stored.result["data"]["execution_updated"] is False
    current = runtime.plan_documents.current_version(document.plan_document_id)
    assert current.version == 2
    assert "减少一个景点" in current.markdown_content


@pytest.mark.asyncio
async def test_artifact_modification_is_suspended_and_rejection_keeps_original(tmp_path) -> None:
    artifact = (
        '{"v":2,"policy":"answer","content_shape":"plan","reason_code":"content_only",'
        '"artifact":{"kind":"plan_document","operation":"upsert","title":"攻略（减量）"}}\n'
        "# 攻略\n\n- 减少一个景点"
    )
    gateway = ScriptedGateway([
        {"message": artifact},
        {"message": '{"plan_document_request": true}'},
        {"message": '{"save_existing_plan": false}'},
        _answer("没有保存修改，原计划保持不变。"),
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("artifact modify")
    document = runtime.plan_documents.save_model_revision(
        thread_id=thread.id, title="攻略", markdown_content="# 攻略\n\n- 两个景点",
        source_turn_id=None, source_message_id=None, actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "turn-artifact", "把攻略改简单一点", [])

    await runtime.turn_worker.run_once()

    waiting = runtime.conversation.turn(accepted.turn_id)
    assert waiting.status == "AWAITING_TOOL_APPROVAL"
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)
    assert pending.tool_name == "modify_plan_document"
    assert pending.params["expected_version_id"] == document.id
    assert runtime.plan_documents.current_version(document.plan_document_id).version == 1

    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "reject", waiting.version, "decision-artifact-reject"
    )
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(continuation.id).status == "COMPLETED"
    assert runtime.plan_documents.current_version(document.plan_document_id).version == 1
    assert runtime.conversation.chat_tool_calls.get(pending.id).status == "REJECTED"


@pytest.mark.asyncio
async def test_repeated_identical_tool_failure_stops_and_answers(tmp_path) -> None:
    bad_call = {
        "tool_calls": [_tool_call("activate_goal_plan", {
            "mode": "activate", "program_id": "program_x", "expected_version": 1, "document_id": "plan_x",
        })],
    }
    gateway = ScriptedGateway([
        bad_call,
        bad_call,
        _answer("激活没有成功：调用参数被拒绝。请重新确认执行预览后再试。"),
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("chat tools")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-repeat", "激活这个计划", [])

    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    assert runtime.conversation.messages(thread.id)[-1].content == "激活没有成功：调用参数被拒绝。请重新确认执行预览后再试。"
    # The invalid call never persisted a tool row, and the loop stopped after
    # the second identical failure instead of exhausting its budget.
    assert _tool_rows(runtime, accepted.turn_id) == []
    assert len(gateway.responses) == 0


@pytest.mark.asyncio
async def test_parallel_read_calls_in_one_response_all_execute(tmp_path) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [
            _tool_call("get_today_tasks", {}),
            _tool_call("query_goals", {}),
        ]},
        _answer("目标列表和今日任务都为空。"),
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("chat tools")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-parallel", "看看目标和任务", [])

    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    rows = _tool_rows(runtime, accepted.turn_id)
    assert [row["tool_name"] for row in rows] == ["get_today_tasks", "query_goals"]
    assert all(row["status"] == "EXECUTED" for row in rows)
    assert runtime.conversation.messages(thread.id)[-1].content == "目标列表和今日任务都为空。"


@pytest.mark.asyncio
async def test_batched_read_and_write_pauses_after_the_read(tmp_path) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [
            _tool_call("get_today_tasks", {}),
            _tool_call("create_plan_draft", {"title": "攻略", "markdown_content": "# 攻略"}),
        ]},
        _answer("已保存。"),
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("chat tools")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-batch", "看看任务并保存攻略", [])

    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(accepted.turn_id)
    assert paused.status == "AWAITING_TOOL_APPROVAL"
    rows = _tool_rows(runtime, accepted.turn_id)
    assert [row["status"] for row in rows] == ["EXECUTED", "PENDING_APPROVAL"]

    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "decision-batch"
    )
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(continuation.id).status == "COMPLETED"
    assert _count(runtime, "plan_documents") == 1
    # The resumed request replays both the executed read and the approved write.
    resumed_messages = gateway.requests[-1]
    tool_messages = [message for message in resumed_messages if message.get("role") == "tool"]
    assert len(tool_messages) == 2


@pytest.mark.asyncio
async def test_execution_management_previews_then_activates_after_confirmation(tmp_path) -> None:
    """The full slice-3 chain: draft -> preview -> confirm -> activate."""

    def draft_call(_request):
        return {"tool_calls": [_tool_call("create_plan_draft", {
            "title": "五周训练计划", "markdown_content": "# 五周训练计划\n\n- 每周三次力量训练",
        })]}

    def preview_call(request):
        data = _last_tool_data(request)
        return {"tool_calls": [_tool_call("activate_goal_plan", {
            "mode": "preview",
            "document_id": data["document_id"],
            "expected_document_version_id": data["version_id"],
            "start_date": "2026-09-01",
            "end_date": "2026-09-05",
            "timezone": "Asia/Shanghai",
            "daily_minutes": 60,
        })]}

    def activate_call(request):
        data = _last_tool_data(request)
        return {"tool_calls": [_tool_call("activate_goal_plan", {
            "mode": "activate",
            "program_id": data["program_id"],
            "expected_version": data["program_version"],
        })]}

    gateway = ScriptedGateway([
        draft_call, preview_call, activate_call,
        _answer("目标已激活，今天开始执行第一项安排。"),
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("执行管理")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-manage", "帮我管理执行进度", [])

    await runtime.turn_worker.run_once()
    first = runtime.conversation.turn(accepted.turn_id)
    assert first.status == "AWAITING_TOOL_APPROVAL"
    draft = runtime.conversation.pending_tool_call(accepted.turn_id)
    assert draft.tool_name == "create_plan_draft"
    second = runtime.conversation.decide_tool_call(accepted.turn_id, "approve", first.version, "d-1")

    await runtime.turn_worker.run_once()
    preview_paused = runtime.conversation.turn(second.id)
    assert preview_paused.status == "AWAITING_TOOL_APPROVAL"
    preview = runtime.conversation.pending_tool_call(second.id)
    assert preview.tool_name == "activate_goal_plan"
    third = runtime.conversation.decide_tool_call(second.id, "approve", preview_paused.version, "d-2")

    await runtime.turn_worker.run_once()
    activate_paused = runtime.conversation.turn(third.id)
    assert activate_paused.status == "AWAITING_TOOL_APPROVAL"
    assert _count(runtime, "goal_programs") == 1
    activation = runtime.conversation.pending_tool_call(third.id)
    assert activation.tool_name == "activate_goal_plan"
    # The activation approval must carry the compiled schedule snapshot; a plain
    # write approval is not user consent for this specific plan.
    assert activation.binding["goal_activation"]["snapshot_hash"]
    fourth = runtime.conversation.decide_tool_call(third.id, "approve", activate_paused.version, "d-3")

    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(fourth.id).status == "COMPLETED"
    program = runtime.goal_programs.get(activation.params["program_id"], owner_id="local-user")
    assert program["status"] == "ACTIVE"
    assert len(program["actions"]) >= 1
    with runtime.db.connection() as connection:
        decisions = connection.execute(
            "SELECT COUNT(*) AS total FROM approvals a JOIN turn_tool_calls c ON c.approval_id = a.id "
            "WHERE c.thread_id = ? AND a.status = 'granted'",
            (thread.id,),
        ).fetchone()
    assert decisions["total"] == 3


@pytest.mark.asyncio
async def test_today_tasks_and_feedback_use_real_ids_and_versions(tmp_path) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("get_today_tasks", {"date": "2026-09-01"})]},
        _answer("今天有一项训练安排。"),
        lambda request: {"tool_calls": [_tool_call("record_action_feedback", {
            "action_id": _last_tool_data(request)["items"][0]["id"],
            "expected_version": _last_tool_data(request)["items"][0]["version"],
            "kind": "partial",
            "actual_minutes": 30,
            "completed_work": "完成了热身与深蹲",
        })]},
        _answer("已记录 30 分钟进展，行动未被标记完成。"),
    ])
    runtime = _make_runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("反馈")
    document = runtime.plan_documents.save_model_revision(
        thread_id=thread.id, title="五周训练计划", markdown_content="# 五周训练计划\n\n- 力量训练",
        source_turn_id=None, source_message_id=None, actor="model",
    )
    program = await runtime.goal_programs.preview(
        document.plan_document_id, start_date="2026-09-01", timezone_name="Asia/Shanghai",
        daily_minutes=60, requested_end_date="2026-09-05",
        idempotency_key="seed-preview", owner_id="local-user",
    )
    snapshot = runtime.goal_programs.activation_snapshot_hash(program)
    runtime.goal_programs.record_preview_snapshot(
        program["id"], snapshot,
        source_version_id=program["source_plan_document_version_id"],
        source_content_hash=program["source_plan_content_hash"],
        owner_id="local-user",
    )
    runtime.goal_programs.activate(
        program["id"], expected_version=program["version"], idempotency_key="seed-activate",
        owner_id="local-user", expected_snapshot_hash=snapshot,
    )

    first = runtime.conversation.accept_turn(thread.id, "turn-today", "今天有什么任务？", [])
    await runtime.turn_worker.run_once()
    assert runtime.conversation.turn(first.turn_id).status == "COMPLETED"

    second = runtime.conversation.accept_turn(thread.id, "turn-feedback", "我练了30分钟", [])
    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(second.turn_id)
    assert paused.status == "AWAITING_TOOL_APPROVAL"
    feedback = runtime.conversation.pending_tool_call(second.turn_id)
    assert feedback.tool_name == "record_action_feedback"
    continuation = runtime.conversation.decide_tool_call(second.turn_id, "approve", paused.version, "d-feedback")
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(continuation.id).status == "COMPLETED"
    with runtime.db.connection() as connection:
        rows = connection.execute(
            "SELECT kind,actual_minutes FROM goal_action_feedback WHERE action_id=?",
            (feedback.params["action_id"],),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "partial"
    assert rows[0]["actual_minutes"] == 30
    stored = runtime.conversation.chat_tool_calls.get(feedback.id)
    assert stored.result["ok"] is True
    assert "未" in stored.result["summary"]


def test_execution_confirmation_does_not_take_the_document_save_branch() -> None:
    import asyncio

    from app.live_model import LiveConversationModel, _is_execution_confirmation

    assert _is_execution_confirmation("确认，按这个版本激活执行管理。")
    assert _is_execution_confirmation("同意激活")
    assert _is_execution_confirmation("继续执行")
    assert not _is_execution_confirmation("把这份计划保存到计划页面")
    assert not _is_execution_confirmation("生成执行预览")

    class NoCallGateway:
        async def complete(self, *_args, **_kwargs):
            raise AssertionError("the classifier must not be called for a confirmation")

    model = LiveConversationModel(NoCallGateway())
    history = [{
        "role": "assistant",
        "content": "# 桂林攻略\n\n| 日期 | 行动 |\n|---|---|\n| 2026-01-01 | 象鼻山 |",
    }]
    result = asyncio.run(model._classify_existing_plan_save(
        "确认，按这个版本激活执行管理。", history, asyncio.Event(),
    ))
    assert result is False
