from dataclasses import replace

import pytest

from app.conversation_capabilities import ConversationCapabilityBinding, register_research_capability
from app.conversation_capabilities import register_expert_capability
from app.conversation_capabilities import register_remember_capability
from app.conversation_capabilities import register_publish_capability
from app.tools import ToolCall, ToolExecutionContext, ToolRejected


def setup_research(tmp_path, *, incomplete=False):
    from app.startup import build_runtime
    runtime = build_runtime(tmp_path)
    thread = runtime.conversation.create_thread("synthetic capability test")
    accepted = runtime.conversation.accept_turn(thread.id, "one", "研究测试", [])
    worker = runtime.turn_worker.primary
    assert worker.claim_next() == accepted.turn_id
    turn = runtime.conversation.turn(accepted.turn_id)
    binding = ConversationCapabilityBinding(worker, turn, "local-user", incomplete)
    register_research_capability(runtime.tools, lambda _: binding)
    context = ToolExecutionContext(owner_id="local-user", run_id="chat:" + turn.id,
        tool_call_id="research-one", thread_id=thread.id, turn_id=turn.id,
        runtime_bundle_id=turn.runtime_bundle_id, root_budget_id=turn.root_budget_id)
    return runtime, turn, context


@pytest.mark.asyncio
async def test_al_t09_t14_research_handoff_idempotent(tmp_path):
    runtime, turn, context = setup_research(tmp_path)
    try:
        for identity in ("research-one", "research-two"):
            result = await runtime.tools.execute_async(ToolCall(identity, "start_research", {"topic": "合成课题", "scope": "web"}),
                context=replace(context, tool_call_id=identity), skill_tools=None)
            assert result.ok and result.meta["capability_kind"] == "handoff"
        assert runtime.conversation.turn(turn.id).policy == "start_research"
        with runtime.db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM research_jobs WHERE source_turn_id=?", (turn.id,)).fetchone()[0] == 1
        events = runtime.conversation.events.list(turn.thread_id)
        assert sum(e.type == "research.queued" for e in events) == 1
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_al_t10_incomplete_research_is_observation(tmp_path):
    runtime, turn, context = setup_research(tmp_path, incomplete=True)
    try:
        result = await runtime.tools.execute_async(ToolCall("research-one", "start_research", {"topic": "合成课题"}), context=context, skill_tools=None)
        assert not result.ok
        assert runtime.conversation.turn(turn.id).policy != "start_research"
        with runtime.db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM research_jobs").fetchone()[0] == 0
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_al_t13_identity_rejected_before_research(tmp_path):
    runtime, turn, context = setup_research(tmp_path)
    try:
        with pytest.raises(ToolRejected):
            await runtime.tools.execute_async(ToolCall("research-one", "start_research", {"topic": "合成课题", "owner_id": "foreign"}), context=context, skill_tools=None)
        with runtime.db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM research_jobs").fetchone()[0] == 0
    finally:
        runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_service", "lost_lease", "foreign_owner"])
async def test_al_t10_research_preconditions_fail_without_writes(tmp_path, failure):
    runtime, turn, context = setup_research(tmp_path)
    try:
        if failure == "missing_service":
            runtime.research = None
        elif failure == "lost_lease":
            with runtime.db.transaction() as connection:
                connection.execute("UPDATE turn_jobs SET lease_owner='other' WHERE turn_id=?", (turn.id,))
        else:
            context = replace(context, owner_id="foreign")
        result = await runtime.tools.execute_async(ToolCall("research-one", "start_research", {"topic": "合成课题"}), context=context, skill_tools=None)
        assert not result.ok
        with runtime.db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM research_jobs").fetchone()[0] == 0
        assert runtime.conversation.turn(turn.id).policy != "start_research"
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_al_t11_t14_expert_context_and_idempotence(tmp_path):
    runtime, turn, context = setup_research(tmp_path)
    binding = ConversationCapabilityBinding(runtime.turn_worker.primary, turn, "local-user",
        history=({"role": "user", "content": "synthetic prior"},), content="synthetic request")
    register_expert_capability(runtime.tools, lambda _: binding)
    try:
        ids = []
        for identity in ("expert-one", "expert-two"):
            result = await runtime.tools.execute_async(ToolCall(identity, "delegate_experts", {"objective": "合成审查", "roles": ["critic"]}),
                context=replace(context, tool_call_id=identity), skill_tools=None)
            assert result.ok
            ids.append(result.data["run_id"])
        assert ids[0] == ids[1]
        run = runtime.agent_tasks.get_run(ids[0])
        assert run["runtime_bundle_id"] == turn.runtime_bundle_id
        archived = runtime.agent_tasks.context(run["context_snapshot_id"])
        assert archived["history"] == list(binding.history)
        assert archived["source_turn_id"] == turn.id
        assert run["idempotency_key"] == "conversation-expert:" + turn.id
        assert runtime.conversation.turn(turn.id).policy == "start_expert"
        changed = await runtime.tools.execute_async(ToolCall('expert-changed', 'delegate_experts',
            {'objective': '合成审查', 'roles': ['planner']}), context=replace(context, tool_call_id='expert-changed'), skill_tools=None)
        assert not changed.ok
        with runtime.db.connection() as connection:
            assert connection.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0] == 1
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_al_t12_t13_expert_rejections(tmp_path):
    runtime, turn, context = setup_research(tmp_path)
    binding = ConversationCapabilityBinding(runtime.turn_worker.primary, replace(turn, goal_action_id="action"), "local-user")
    register_expert_capability(runtime.tools, lambda _: binding)
    try:
        for params in ({"objective": "topic", "roles": ["admin"]}, {"objective": "topic", "roles": ["critic"], "owner_id": "foreign"}):
            with pytest.raises(ToolRejected):
                runtime.tools.authorize(ToolCall("bad", "delegate_experts", params), run_id=context.run_id, skill_tools=None)
        result = await runtime.tools.execute_async(ToolCall("expert-one", "delegate_experts", {"objective": "topic", "roles": ["critic"]}), context=context, skill_tools=None)
        assert not result.ok
        with runtime.db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_al_t15_remember_evidence_and_idempotence(tmp_path):
    runtime, turn, context = setup_research(tmp_path)
    with runtime.db.connection() as connection:
        source = connection.execute("SELECT id,content FROM thread_messages WHERE turn_id=? AND role='user'", (turn.id,)).fetchone()
    binding = ConversationCapabilityBinding(runtime.turn_worker.primary, turn, "local-user", source_message_id=source["id"], content=source["content"])
    register_remember_capability(runtime.tools, lambda _: binding)
    try:
        params = {"kind": "preference", "scope": "global", "content": "偏好简短回答"}
        ids = []
        for identity in ("remember-one", "remember-two"):
            result = await runtime.tools.execute_async(ToolCall(identity, "remember", params), context=replace(context, tool_call_id=identity), skill_tools=None)
            assert result.ok, result
            ids.append(result.data["id"])
        assert ids[0] == ids[1]
        with runtime.db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0] == 1
        result = await runtime.tools.execute_async(ToolCall("project", "remember", {**params, "scope": "project"}), context=replace(context, tool_call_id="project"), skill_tools=None)
        assert result.error == "PROJECT_REQUIRED"
        with pytest.raises(ToolRejected):
            runtime.tools.authorize(ToolCall("inject", "remember", {**params, "source_message_id": "foreign"}), run_id=context.run_id, skill_tools=None)
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_publish_reuses_committed_version_after_interruption(tmp_path):
    runtime, turn, context = setup_research(tmp_path)
    binding = ConversationCapabilityBinding(runtime.turn_worker.primary, turn, "local-user")
    binding.bound_response["text"] = "# 合成计划\n\n每天阅读。"
    register_publish_capability(runtime.tools, lambda _: binding)
    try:
        ids = []
        for identity in ("publish-one", "publish-recovery"):
            result = await runtime.tools.execute_async(ToolCall(identity, "publish_plan_document", {"title": "合成计划"}),
                context=replace(context, tool_call_id=identity), skill_tools=None)
            assert result.ok, result
            ids.append(result.data["version_id"])
        assert ids[0] == ids[1]
        with runtime.db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM plan_document_versions").fetchone()[0] == 1
    finally:
        runtime.db.close()
