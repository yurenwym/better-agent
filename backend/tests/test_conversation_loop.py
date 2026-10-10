import json
from types import SimpleNamespace

import pytest

from app.live_model import LiveConversationModel
from app.model_gateway import ModelResponse, Timing, UsageBuckets


class Gateway:
    def __init__(self, responses):
        from app.model_gateway import ModelProfile
        self.profile = ModelProfile("https://unused.invalid", "test", "UNUSED", context_window=131072)
        self.responses = iter(responses)
        self.requests = []

    def resolved_profile(self, context):
        return self.profile

    async def complete(self, request, **kwargs):
        self.requests.append(request)
        message, calls = next(self.responses)
        if message and kwargs.get("on_text_delta"):
            kwargs["on_text_delta"](message)
        return ModelResponse(message, calls, "stop", UsageBuckets(), Timing(0, 0, 0), 1)


def tool(name, params):
    return {"id": name + "-one", "type": "function", "function": {"name": name, "arguments": json.dumps(params)}}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["research", "expert"])
async def test_reused_handoff_closes_current_attempt(tmp_path, monkeypatch, kind):
    from app.startup import build_runtime
    from app.event_envelope import EventMetadata
    from app.execution_context import create_child_context
    from dataclasses import replace

    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    call = tool("start_research", {"topic": "topic", "scope": "web"}) if kind == "research" else tool(
        "delegate_experts", {"objective": "topic", "roles": ["critic"]})
    gateway = Gateway([("", [call])])
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    try:
        thread = runtime.conversation.create_thread("reuse")
        accepted = runtime.conversation.accept_turn(thread.id, "one", "request", [])
        await runtime.turn_worker.run_once()
        turn = runtime.conversation.turn(accepted.turn_id)
        assert turn.status == "COMPLETED"
        worker = runtime.turn_worker.primary
        with runtime.db.transaction() as connection:
            connection.execute("UPDATE turn_jobs SET status='QUEUED',finished_at=NULL WHERE turn_id=?", (turn.id,))
        assert worker.claim_next() == turn.id
        root = runtime.conversation.harness_context.load_turn_context(turn.id)
        metadata = EventMetadata(create_child_context(root), "conversation_loop", turn.id, 2)
        start = runtime.conversation.events.append(thread.id, turn.id, "loop.started", "worker", {}, metadata=metadata)
        worker._loop_attempt = replace(metadata, causation_event_id=start.event_id)
        if kind == "research":
            target = worker.handoff_start_research(turn, "topic", "web").id
            table = "research_jobs"
        else:
            target = worker.handoff_start_expert(turn, "topic", ("critic",), content="request",
                history=[], plan_context=None, source_message_id=worker._user_message(turn.id).id)["id"]
            table = "agent_runs"
        with runtime.db.connection() as connection:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        finishes = [e for e in runtime.conversation.events.list(thread.id) if e.type == "loop.finished"]
        assert len(finishes) == 2
        assert finishes[-1].event_id == start.event_id + ":finished"
        assert finishes[-1].data["outcome"]["target_ref"] == target
        assert len(gateway.requests) == 1
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_unbound_historical_turn_cannot_send_or_mint_context(tmp_path, monkeypatch):
    from app.startup import build_runtime
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    gateway = Gateway([("should not send", [])])
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    try:
        monkeypatch.setattr(runtime.conversation, "_persist_turn_root", lambda *args: None)
        thread = runtime.conversation.create_thread("historical")
        accepted = runtime.conversation.accept_turn(thread.id, "one", "request", [])
        await runtime.turn_worker.run_once()
        assert gateway.requests == []
        assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
        with runtime.db.connection() as connection:
            row = connection.execute("SELECT execution_context_json,execution_context_digest FROM turns WHERE id=?", (accepted.turn_id,)).fetchone()
        assert row["execution_context_json"] is None and row["execution_context_digest"] is None
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_reclaimed_turn_closes_interrupted_attempt_before_new_send(tmp_path, monkeypatch):
    from app.startup import build_runtime
    from app.event_envelope import EventMetadata
    from app.execution_context import create_child_context
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    gateway = Gateway([("answer", [])])
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    try:
        thread = runtime.conversation.create_thread("restart")
        accepted = runtime.conversation.accept_turn(thread.id, "one", "request", [])
        assert runtime.turn_worker.primary.claim_next() == accepted.turn_id
        root = runtime.conversation.harness_context.load_turn_context(accepted.turn_id)
        started = runtime.conversation.events.append(thread.id, accepted.turn_id, "loop.started", "worker", {},
            metadata=EventMetadata(create_child_context(root), "conversation_loop", accepted.turn_id, 1))
        with runtime.db.transaction() as connection:
            connection.execute("UPDATE turn_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE turn_id=?", (accepted.turn_id,))
        await runtime.turn_worker.run_once()
        assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
        assert len(gateway.requests) == 1
        finished = [event for event in runtime.conversation.events.list(thread.id) if event.type == "loop.finished"]
        assert len(finished) == 2
        assert finished[0].event_id == f"{started.event_id}:finished"
        assert finished[0].data["outcome"]["reason"] == "WORKER_LEASE_LOST"
        metadata = [EventMetadata.from_dict(json.loads(event.envelope_json)) for event in finished]
        assert [item.attempt for item in metadata] == [1, 2]
        assert metadata[0].context.span_id != metadata[1].context.span_id
    finally:
        runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("calls,policy", [
    ([], "answer"),
    ([tool("start_research", {"topic": "合成主题", "scope": "web"})], "start_research"),
    ([tool("delegate_experts", {"objective": "合成主题", "roles": ["critic"]})], "start_expert"),
])
async def test_native_conversation_main_chain(tmp_path, monkeypatch, calls, policy):
    from app.startup import build_runtime
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    gateway = Gateway([("" if calls else "直接回答", calls)])
    model = LiveConversationModel(gateway)
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy classifier called")
    for name in ("_classify_explicit_remember", "_classify_explicit_research_request"):
        monkeypatch.setattr(model, name, forbidden)
    import app.live_model as live
    for name in ("_is_explicit_research_command", "_explicit_expert_request"):
        monkeypatch.setattr(live, name, forbidden)
    runtime = build_runtime(tmp_path, conversation_model=model)
    try:
        thread = runtime.conversation.create_thread("loop test")
        accepted = runtime.conversation.accept_turn(thread.id, "one", "合成请求", [])
        await runtime.turn_worker.run_once()
        turn = runtime.conversation.turn(accepted.turn_id)
        assert turn.status == "COMPLETED", [vars(e) for e in runtime.conversation.events.list(thread.id) if "failed" in e.type]
        assert turn.policy == policy
        assert len(gateway.requests) == 1
        events = runtime.conversation.events.list(thread.id)
        started = [event for event in events if event.type == "loop.started"]
        finished = [event for event in events if event.type == "loop.finished"]
        assert len(started) == len(finished) == 1
        from app.event_envelope import EventMetadata
        start_meta, finish_meta = (EventMetadata.from_dict(json.loads(event.envelope_json)) for event in (started[0], finished[0]))
        assert finish_meta.context.span_id == start_meta.context.span_id
        assert finish_meta.causation_event_id == started[0].event_id
        assert finish_meta.outcome.status == ("completed" if policy == "answer" else "handoff")
        assert 'create_plan_draft' not in {schema['function']['name'] for schema in gateway.requests[0].tools}
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_loop_success_rolls_back_with_turn_commit(tmp_path, monkeypatch):
    from app.startup import build_runtime
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(Gateway([("回答", [])])))
    try:
        thread = runtime.conversation.create_thread("rollback")
        accepted = runtime.conversation.accept_turn(thread.id, "one", "请求", [])
        append = runtime.conversation.events.append

        def interrupted(*args, **kwargs):
            event = append(*args, **kwargs)
            if event.type == "loop.finished" and event.data["outcome"]["status"] == "completed":
                raise RuntimeError("commit interrupted")
            return event

        monkeypatch.setattr(runtime.conversation.events, "append", interrupted)
        await runtime.turn_worker.run_once()
        assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
        events = runtime.conversation.events.list(thread.id)
        assert not any(event.type == "turn.completed" for event in events)
        finished = [event for event in events if event.type == "loop.finished"]
        assert len(finished) == 1
        assert finished[0].data["outcome"]["status"] == "failed"
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_cancellation_before_final_commit_does_not_publish_loop_success(tmp_path, monkeypatch):
    from app.startup import build_runtime
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(Gateway([("回答", [])])))
    try:
        thread = runtime.conversation.create_thread("cancel")
        accepted = runtime.conversation.accept_turn(thread.id, "one", "请求", [])
        finish = runtime.turn_worker.primary._finish_success

        def cancel_then_finish(turn, *args, **kwargs):
            runtime.conversation.cancel_turn(turn.id)
            return finish(turn, *args, **kwargs)

        monkeypatch.setattr(runtime.turn_worker.primary, "_finish_success", cancel_then_finish)
        await runtime.turn_worker.run_once()
        assert runtime.conversation.turn(accepted.turn_id).status == "CANCELLED"
        events = runtime.conversation.events.list(thread.id)
        finished = [event for event in events if event.type == "loop.finished"]
        assert len(finished) == 1
        assert finished[0].data["outcome"]["status"] == "cancelled"
        assert not any(event.type == "turn.completed" for event in events)
    finally:
        runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['approve', 'reject'])
async def test_native_modify_plan_approval_and_read_continuation(tmp_path, monkeypatch, decision):
    from app.startup import build_runtime
    monkeypatch.setenv('BETTER_AGENT_LOOP_MODE', 'loop')
    gateway = Gateway([])
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    try:
        thread = runtime.conversation.create_thread('modify')
        original = runtime.plan_documents.save_model_revision(thread_id=thread.id, title='原计划', markdown_content='# 原计划\n\n每天阅读。', source_turn_id=None, source_message_id=None, actor='model')
        gateway.responses = iter([('', [tool('query_goals', {}), tool('modify_plan_document', {
            'document_id': original.plan_document_id, 'expected_version_id': original.id,
            'title': '更新计划', 'markdown_content': '# 更新计划\n\n每天练习。'})]), ('处理完毕', [])])
        accepted = runtime.conversation.accept_turn(thread.id, 'modify', '修改计划', [])
        await runtime.turn_worker.run_once()
        paused = runtime.conversation.turn(accepted.turn_id)
        assert paused.status == 'AWAITING_TOOL_APPROVAL'
        assert runtime.plan_documents.current_version(original.plan_document_id).id == original.id
        with runtime.db.connection() as connection:
            calls = connection.execute('SELECT tool_name,status FROM turn_tool_calls WHERE turn_id=?', (paused.id,)).fetchall()
        assert any(c['tool_name'] == 'query_goals' and c['status'] == 'EXECUTED' for c in calls)
        resumed = runtime.conversation.decide_tool_call(paused.id, decision, paused.version, 'decision')
        await runtime.turn_worker.run_once()
        assert runtime.conversation.turn(resumed.id).status == 'COMPLETED'
        current = runtime.plan_documents.current_version(original.plan_document_id)
        assert (current.id != original.id) == (decision == 'approve')
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_al_t16_native_ask_then_answer(tmp_path, monkeypatch):
    from app.startup import build_runtime
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    question = {"id": "q", "header": "对象", "question": "需要处理什么？", "options": [], "multi_select": False, "allow_free_text": True}
    gateway = Gateway([("", [tool("ask_user", {"questions": [question]})]), ("收到补充", [])])
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    try:
        thread = runtime.conversation.create_thread("ask")
        accepted = runtime.conversation.accept_turn(thread.id, "one", "处理一下", [])
        await runtime.turn_worker.run_once()
        assert runtime.conversation.turn(accepted.turn_id).status == "AWAITING_INPUT"
        pending = runtime.conversation.turn(accepted.turn_id)
        continued = runtime.conversation.answer_ask(pending.id, pending.version, "answer",
            [{"question_id": "q", "selected_options": [], "free_text": "整理书桌"}])
        await runtime.turn_worker.run_once()
        assert runtime.conversation.turn(continued.turn.id).status == "COMPLETED", json.dumps([vars(e) for e in runtime.conversation.events.list(thread.id) if "failed" in e.type], ensure_ascii=False)
        assert any(m.get("role") == "tool" and "整理书桌" in m.get("content", "") for m in gateway.requests[-1].messages)
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_al_t23_native_plan_publish(tmp_path, monkeypatch):
    from app.startup import build_runtime
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    text = "# 计划\n\n每天阅读。"
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(Gateway([(text, [tool("publish_plan_document", {"title": "计划"})])])))
    try:
        thread = runtime.conversation.create_thread("plan")
        accepted = runtime.conversation.accept_turn(thread.id, "one", "保存计划", [])
        await runtime.turn_worker.run_once()
        turn = runtime.conversation.turn(accepted.turn_id)
        assert turn.status == "COMPLETED" and turn.content_shape == "plan_document"
        with runtime.db.connection() as connection:
            row = connection.execute("SELECT m.content,v.markdown_content,v.content_hash FROM thread_messages m JOIN plan_document_versions v ON v.id=m.plan_document_version_id WHERE m.turn_id=?", (turn.id,)).fetchone()
        import hashlib
        assert row["content"] == row["markdown_content"] == text
        assert row["content_hash"] == "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        ready = [e for e in runtime.conversation.events.list(thread.id) if e.type == 'plan.document_ready'][-1]
        assert ready.data['source_message_id'] and ready.data['content_hash'] == row['content_hash']
    finally:
        runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["empty", "mixed", "history", "missing_history", "existing"])
async def test_al_t24_t26_plan_binding_boundaries(tmp_path, monkeypatch, mode):
    from app.startup import build_runtime
    import app.live_model as live
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy plan routing called")
    monkeypatch.setattr(live, "_is_plain_existing_plan_save", forbidden)
    monkeypatch.setattr(LiveConversationModel, "_classify_explicit_plan_document_request", forbidden)
    text = "# 阅读计划\n\n- 每天阅读二十分钟。\n- 周末回顾笔记。"
    params = {"title": "阅读计划"}
    if mode in {"history", "missing_history"}:
        params["source"] = "latest_assistant_plan"
    calls = [tool("publish_plan_document", params)]
    if mode == "mixed":
        calls.append(tool("query_goals", {}))
    responses = []
    if mode == "history":
        responses.append((text, []))
    responses.extend([("" if mode in {"empty", "history", "missing_history"} else text, calls), ("未保存", [])])
    gateway = Gateway(responses)
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    try:
        thread = runtime.conversation.create_thread("binding")
        if mode == "existing":
            runtime.plan_documents.save_model_revision(thread_id=thread.id, title="原计划", markdown_content="# 原计划\n\n保持原样。", source_turn_id=None, source_message_id=None, actor="model")
        if mode == "history":
            runtime.conversation.accept_turn(thread.id, "prior", "先写计划", [])
            await runtime.turn_worker.run_once()
        accepted = runtime.conversation.accept_turn(thread.id, "publish", "保存计划", [])
        await runtime.turn_worker.run_once()
        assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED", json.dumps([vars(e) for e in runtime.conversation.events.list(thread.id) if "failed" in e.type], ensure_ascii=False)
        with runtime.db.connection() as connection:
            versions = connection.execute("SELECT markdown_content FROM plan_document_versions").fetchall()
        assert len(versions) == (1 if mode in {"history", "existing"} else 0)
        if mode == "history":
            assert versions[0]["markdown_content"] == text
        elif mode == "existing":
            assert "保持原样" in versions[0]["markdown_content"]
    finally:
        runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["budget", "unknown_write"])
async def test_failed_turn_preserves_model_error_or_reconciliation(tmp_path, monkeypatch, failure):
    from app.startup import build_runtime
    from app.model_gateway import GatewayError
    from app.tools import ToolReconciliationRequired
    from app.event_envelope import EventMetadata

    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    gateway = Gateway([])
    async def fail(*args, **kwargs):
        if failure == "budget":
            raise GatewayError("private provider details", "budget")
        raise ToolReconciliationRequired(operation_ref="pending-operation")
    monkeypatch.setattr(gateway, "complete", fail)
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    try:
        thread = runtime.conversation.create_thread("failure")
        accepted = runtime.conversation.accept_turn(thread.id, "one", "request", [])
        await runtime.turn_worker.run_once()
        assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
        events = runtime.conversation.events.list(thread.id)
        failed = next(e for e in events if e.type == "turn.failed")
        finished = next(e for e in events if e.type == "loop.finished")
        task = EventMetadata.from_dict(json.loads(failed.envelope_json)).outcome
        loop = EventMetadata.from_dict(json.loads(finished.envelope_json)).outcome
        assert task.scope == "task" and loop.scope == "loop"
        assert task.error == loop.error
        assert "private provider details" not in json.dumps(task.to_dict())
        if failure == "budget":
            assert task.error.code == "MODEL_BUDGET" and task.error.phase == "model"
        else:
            assert task.status == "reconciliation_required"
            assert task.reconciliation_ref == loop.reconciliation_ref == "pending-operation"
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_loop_exhaustion_is_not_reclassified_as_internal_error(tmp_path, monkeypatch):
    from app.startup import build_runtime
    from app.event_envelope import EventMetadata
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    # Different invalid tools hit the consecutive-error guard after two sends.
    gateway = Gateway([("", [tool("unavailable_one", {})]), ("", [tool("unavailable_two", {})])])
    runtime = build_runtime(tmp_path, conversation_model=LiveConversationModel(gateway))
    try:
        thread = runtime.conversation.create_thread("exhaustion")
        accepted = runtime.conversation.accept_turn(thread.id, "one", "request", [])
        await runtime.turn_worker.run_once()
        assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
        events = runtime.conversation.events.list(thread.id)
        outcomes = [EventMetadata.from_dict(json.loads(e.envelope_json)).outcome
                    for e in events if e.type in {"turn.failed", "loop.finished"}]
        assert len(outcomes) == 2
        assert {outcome.scope for outcome in outcomes} == {"task", "loop"}
        assert all(outcome.status == "exhausted" and outcome.error is None for outcome in outcomes)
        assert all(outcome.reason == "CONSECUTIVE_TOOL_ERRORS_EXHAUSTED" for outcome in outcomes)
        assert len(gateway.requests) == 2
    finally:
        runtime.db.close()
