import json

import pytest

from app.live_model import LiveConversationModel
from app.startup import build_runtime
from test_conversation_loop import Gateway, tool


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["research", "expert"])
async def test_conversation_starts_queries_and_cancels_task(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    call = tool("start_research", {"topic": "topic", "scope": "web"}) if kind == "research" else tool(
        "delegate_experts", {"objective": "topic", "roles": ["critic"]})
    gateway = Gateway([("", [call])])
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    try:
        thread = runtime.conversation.create_thread("tasks")
        first = runtime.conversation.accept_turn(thread.id, "start", "start task", [])
        await runtime.turn_worker.run_once()
        assert runtime.conversation.turn(first.turn_id).status == "COMPLETED"
        started = runtime.conversation.chat_tool_calls.for_turn(first.turn_id)[0]
        ref = started.result["data"]["task_ref"]
        assert ref["kind"] == kind
        for index, name in enumerate(("get_task", "cancel_task", "cancel_task")):
            gateway.responses = iter([("", [tool(name, ref)]), ("已处理。", [])])
            content = "查询刚才那个任务的当前进度" if name == "get_task" else "取消刚才那个任务"
            accepted = runtime.conversation.accept_turn(thread.id, f"follow-{index}", content, [])
            await runtime.turn_worker.run_once()
            assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
            result = runtime.conversation.chat_tool_calls.for_turn(accepted.turn_id)[0].result
            assert result["ok"]
            assert result["data"]["status"] == ("QUEUED" if name == "get_task" else "CANCELLED")
            events = [event for event in runtime.conversation.events.list(thread.id)
                      if event.turn_id == accepted.turn_id and event.type.startswith("task.")]
            assert len(events) == 1
            assert events[0].data["task_ref"] == ref
            root = runtime.conversation.harness_context.load_turn_context(accepted.turn_id)
            assert json.loads(events[0].envelope_json)["context"]["context"]["trace_id"] == root.trace_id
            assert root.trace_id != runtime.conversation.harness_context.load_turn_context(first.turn_id).trace_id
            if index == 0:
                history = json.dumps(gateway.requests[-2].messages, ensure_ascii=False)
                assert ref["id"] in history
        with runtime.db.connection() as connection:
            table = "research_jobs" if kind == "research" else "agent_runs"
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    finally:
        runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["RUNNING", "FAILED", "PARTIAL"])
async def test_query_reports_committed_research_state(tmp_path, monkeypatch, status):
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    gateway = Gateway([])
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    try:
        thread = runtime.conversation.create_thread("progress")
        job = runtime.research.create_manual(thread.id, "topic", "progress", ("web",))
        claimed = runtime.research.claim(job.id, "worker", 60)
        if status == "FAILED":
            runtime.research.fail(job.id, "worker", "SOURCE_UNAVAILABLE", epoch=claimed.lease_epoch)
        elif status == "PARTIAL":
            runtime.research.complete_partial(job.id, "worker", "partial", "partial answer", 0, 0,
                                              (), ("missing source",), epoch=claimed.lease_epoch)
        gateway.responses = iter([("", [tool("get_task", {"kind": "research", "id": job.id})]), ("已查询。", [])])
        turn = runtime.conversation.accept_turn(thread.id, "query", "query progress", [])
        await runtime.turn_worker.run_once()
        result = runtime.conversation.chat_tool_calls.for_turn(turn.turn_id)[0].result
        assert result["ok"] and result["data"]["status"] == status
        assert runtime.research.get(job.id).status == status
        assert len(gateway.requests) == 2
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_completed_task_in_deleted_thread_cannot_be_read(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(Gateway([])))
    try:
        original = runtime.conversation.create_thread("old")
        job = runtime.research.create_manual(original.id, "topic", "one", ("web",))
        claimed = runtime.research.claim(job.id, "worker", 60)
        runtime.research.complete(job.id, "worker", "report", "answer", 0, 0, epoch=claimed.lease_epoch)
        runtime.conversation.delete_thread(original.id)
        gateway = Gateway([("", [tool("get_task", {"kind": "research", "id": job.id})]), ("不可访问。", [])])
        runtime.conversation.route_model = LiveConversationModel(gateway)
        thread = runtime.conversation.create_thread("new")
        accepted = runtime.conversation.accept_turn(thread.id, "query", "query old task", [])
        await runtime.turn_worker.run_once()
        result = runtime.conversation.chat_tool_calls.for_turn(accepted.turn_id)[0].result
        assert result["error"] == "TASK_NOT_ACCESSIBLE"
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_completed_research_is_delivered_once_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    gateway = Gateway([("", [tool("start_research", {"topic": "topic", "scope": "web"})])])
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    thread = runtime.conversation.create_thread("report")
    accepted = runtime.conversation.accept_turn(thread.id, "start", "research", [])
    await runtime.turn_worker.run_once()
    ref = runtime.conversation.chat_tool_calls.for_turn(accepted.turn_id)[0].result["data"]["task_ref"]
    claimed = runtime.research.claim(ref["id"], "worker", 60)
    for _ in range(2):
        runtime.research.complete(ref["id"], "worker", "Report", "# Durable report", 0, 0, epoch=claimed.lease_epoch)
    runtime.db.close()

    gateway = Gateway([("", [tool("get_task", ref)]), ("报告已完成。", [])])
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    try:
        follow = runtime.conversation.accept_turn(thread.id, "follow", "what did the report find", [])
        await runtime.turn_worker.run_once()
        result = runtime.conversation.chat_tool_calls.for_turn(follow.turn_id)[0].result
        assert result["data"]["status"] == "COMPLETED"
        assert result["data"]["report_url"].endswith(ref["id"] + "/report")
        messages = runtime.conversation.messages(thread.id)
        assert sum(message.content == "# Durable report" for message in messages) == 1
        assert "# Durable report" in json.dumps(gateway.requests[0].messages, ensure_ascii=False)
        assert len([event for event in runtime.conversation.events.list(thread.id) if event.type == "research.completed"]) == 1
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_completed_expert_is_delivered_once_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    gateway = Gateway([("", [tool("delegate_experts", {"objective": "topic", "roles": ["critic"]})])])
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    thread = runtime.conversation.create_thread("expert report")
    accepted = runtime.conversation.accept_turn(thread.id, "start", "expert task", [])
    await runtime.turn_worker.run_once()
    ref = runtime.conversation.chat_tool_calls.for_turn(accepted.turn_id)[0].result["data"]["task_ref"]
    claimed = runtime.agent_tasks.claim_next("worker", 60)
    for _ in range(2):
        runtime.agent_tasks.complete(ref["id"], "worker", claimed["lease_epoch"], "expert_synthesis", {"summary": "Durable expert answer"})
    runtime.db.close()
    gateway = Gateway([("", [tool("get_task", ref)]), ("专家已完成。", [])])
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    try:
        follow = runtime.conversation.accept_turn(thread.id, "follow", "what did the experts find", [])
        await runtime.turn_worker.run_once()
        result = runtime.conversation.chat_tool_calls.for_turn(follow.turn_id)[0].result
        assert result["data"]["status"] == "SUCCEEDED"
        assert "Durable expert answer" in json.dumps(gateway.requests[0].messages, ensure_ascii=False)
        with runtime.db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM thread_messages WHERE thread_id=? AND content LIKE '%Durable expert answer%'", (thread.id,)).fetchone()[0] == 1
        assert len([event for event in runtime.conversation.events.list(thread.id) if event.type == "expert.run.completed"]) == 1
    finally:
        runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get_task", "cancel_task"])
@pytest.mark.parametrize("kind", ["research", "expert"])
@pytest.mark.parametrize("missing", [False, True])
async def test_task_from_another_owner_is_not_accessible(tmp_path, monkeypatch, operation, kind, missing):
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    gateway = Gateway([])
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    try:
        foreign = runtime.conversation.create_thread("private", owner_id="someone-else")
        job = runtime.research.create_manual(foreign.id, "private topic", "private", ("web",))
        if kind == "expert":
            from app.behavior import BehaviorBundleService
            bundle = BehaviorBundleService(runtime.db).ensure({"code": "private"})
            run = runtime.agent_tasks.create_run("someone-else", "private", {}, bundle.id,
                                                thread_id=foreign.id, idempotency_key="private")
            task_id = run["coordinator_task_id"]
        else:
            task_id = job.id
        gateway.responses = iter([("", [tool(operation, {"kind": kind, "id": "missing" if missing else task_id})]), ("不可访问。", [])])
        thread = runtime.conversation.create_thread("local")
        accepted = runtime.conversation.accept_turn(thread.id, "one", operation, [])
        await runtime.turn_worker.run_once()
        result = runtime.conversation.chat_tool_calls.for_turn(accepted.turn_id)[0].result
        assert result["error"] == "TASK_NOT_ACCESSIBLE"
        assert runtime.research.get(job.id).status == "QUEUED"
        assert runtime.research.get(job.id).cancel_requested_at is None
    finally:
        runtime.db.close()
