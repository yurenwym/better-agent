"""I01-I06: one trusted execution context across the minimal chain.

The tests drive the *production* chain - ``ManagedTurnWorker`` ->
``LiveConversationModel`` -> ``ChatToolRunner`` -> ``ToolRegistry`` ->
``GoalProgramService`` - with a **fixed-response** routed gateway, then read the
context back from the database.  The gateway is deterministic, so this proves
propagation, persistence and the absence of hand-assembled identities; it is not
a quality evaluation of any model.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest


# --------------------------------------------------------------------------- #
# fixed-response gateway
# --------------------------------------------------------------------------- #

def tool_call(name: str, arguments: dict | None = None) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {}, ensure_ascii=False)},
    }


def answer(body: str) -> dict:
    return {
        "message": (
            '{"v":1,"policy":"answer","content_shape":"general","reason_code":"content_only"}\n' + body
        )
    }


class FixedGateway:
    """Scripted ``execute_attempt``: one deterministic response per role.

    ``conversation`` and ``planner`` are separate queues so a tool-internal
    compile cannot consume a conversation turn.
    """

    def __init__(self, conversation: list | None = None, planner: list | None = None) -> None:
        self.conversation = list(conversation or [])
        self.planner = list(planner or [])
        self.requests: list = []

    async def __call__(self, profile, request, **kwargs):
        from app.model_gateway import ModelResponse, Timing, UsageBuckets

        self.requests.append(request)
        queue = self.planner if request.role == "planner" else self.conversation
        if not queue:
            raise AssertionError(
                f"no scripted response for role={request.role!r} purpose={request.purpose!r}"
            )
        scripted = queue.pop(0)
        if callable(scripted):
            scripted = scripted(request)
        message = scripted.get("message", "")
        if message and kwargs.get("on_text_delta") is not None:
            kwargs["on_text_delta"](message)
        return ModelResponse(
            message, scripted.get("tool_calls", []), scripted.get("finish_reason", "stop"),
            UsageBuckets(1, 0, 0, 1, 0), Timing(0, 0, 1), 1,
        )


# --------------------------------------------------------------------------- #
# runtime under test
# --------------------------------------------------------------------------- #

def _control_plane(db, monkeypatch):
    """A pinned runtime bundle plus the profile versions it routes to."""
    from types import SimpleNamespace

    from app.behavior import BehaviorBundleService
    from app.model_admin import ModelAdminService

    admin = ModelAdminService(db)
    versions = {}
    for name, capabilities in {
        "chat": {"text": True, "streaming": True},
        "planner": {"text": True, "json_object": True},
    }.items():
        env = f"{name.upper()}_KEY"
        monkeypatch.setenv(env, "secret")
        versions[name] = admin.create_profile({
            "name": name, "provider_protocol": "openai_compatible", "provider_name": name,
            "base_url": f"https://{name}.test/v1", "model_name": name,
            "credential_env_ref": env, "capabilities": capabilities,
            # The conversation system prompt plus the tool catalogue must fit, so
            # the test window is generous; it is not the subject under test.
            "context_window": 131072, "max_output_tokens": 4096, "timeout_seconds": 5,
            "max_attempts": 1,
        })["versions"][0]["id"]
    policy = admin.create_policy("runtime", {
        "conversation": {"primary": versions["chat"], "fallback": []},
        "planner": {"primary": versions["planner"], "fallback": []},
    })
    bundles = BehaviorBundleService(db)
    bundle = bundles.ensure({
        "model_routing": {"policy_id": policy["id"], "digest": policy["policy_digest"]},
        "model_role_bindings": policy["roles"],
    })
    bundles.activate("stable", bundle.id, "stable")
    return SimpleNamespace(
        admin=admin, bundles=bundles, versions=versions, policy=policy, bundle_id=bundle.id,
    )


def build_runtime(tmp_path, monkeypatch, script, *, evolution=None):
    """The real runtime, wired to a routed gateway driven by ``script``."""
    from app.db import Database
    from app.domain import ApprovalService, CheckpointStore, PlanVersionService
    from app.events import EventStore
    from app.goal_tools import register_goal_tools
    from app.live_model import LiveConversationModel
    from app.memory import MemoryService
    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.runtime import AgentRuntime
    from app.tools import create_default_registry

    db = Database(tmp_path / "agent.db", workspace=tmp_path / "workspace")
    control = _control_plane(db, monkeypatch)
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=script)
    events = EventStore(db)
    approvals = ApprovalService(db)
    runtime = AgentRuntime(
        db=db,
        events=events,
        plans=PlanVersionService(db),
        approvals=approvals,
        checkpoints=CheckpointStore(db),
        memory=MemoryService(db, events, tmp_path / "memory"),
        tools=create_default_registry(tmp_path / "workspace", db=db, approval_service=approvals),
        model=LiveConversationModel(gateway),
    )
    register_goal_tools(
        runtime.tools, goal_programs=runtime.goal_programs, plan_documents=runtime.plan_documents,
    )
    runtime.evolution = evolution
    runtime.gateway = gateway
    runtime.control = control
    runtime.bundle_id = control.bundle_id
    return runtime


# --------------------------------------------------------------------------- #
# reading the persisted identity back
# --------------------------------------------------------------------------- #

def stored_context(row) -> dict | None:
    payload = row["execution_context_json"]
    return json.loads(payload)["context"] if payload else None


def turn_context(runtime, turn_id: str) -> dict:
    with runtime.db.connection() as connection:
        row = connection.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
    context = stored_context(row)
    assert context is not None, f"turn {turn_id} has no persisted execution context"
    from app.execution_context import execution_context_digest

    assert execution_context_digest(row["execution_context_json"]) == row["execution_context_digest"]
    return context


def tool_contexts(runtime, turn_id: str) -> list[dict]:
    with runtime.db.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM turn_tool_calls WHERE turn_id=? ORDER BY created_at,id", (turn_id,),
        ).fetchall()
    return [stored_context(row) for row in rows]


def invocation_contexts(runtime, turn_id: str) -> list[dict]:
    with runtime.db.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM model_invocations WHERE turn_id=? ORDER BY created_at,id", (turn_id,),
        ).fetchall()
    return [stored_context(row) for row in rows]


def _count(runtime, table: str) -> int:
    with runtime.db.connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class _StableEvolution:
    """Assigns one fixed bundle to every run.

    Without it a turn has no versioned binding at all (``runtime_bundle_id`` is
    NULL), and "the same bundle across every layer" would be vacuously true.
    """

    def __init__(self, bundle_id: str) -> None:
        self.bundle_id = bundle_id

    def assign_run(self, run_id, assignment_key, *, connection=None):
        return self.bundle_id, "deployment"

    def finish_run_exposure(self, run_id, *, success, safety_pass=None, connection=None):
        return None


def _versioned(runtime):
    runtime.evolution = _StableEvolution(runtime.bundle_id)
    return runtime


def reopen_runtime(tmp_path, script, *, evolution=None):
    """A second runtime over the *same* database.

    Nothing is carried over in memory: a resumed approval must be able to rebuild
    its identity from the persisted record alone.  The control plane already
    exists, so it is read back instead of re-created.
    """
    from app.db import Database
    from app.domain import ApprovalService, CheckpointStore, PlanVersionService
    from app.events import EventStore
    from app.goal_tools import register_goal_tools
    from app.live_model import LiveConversationModel
    from app.memory import MemoryService
    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.runtime import AgentRuntime
    from app.tools import create_default_registry

    db = Database(tmp_path / "agent.db", workspace=tmp_path / "workspace")
    with db.connection() as connection:
        row = connection.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()
    events = EventStore(db)
    approvals = ApprovalService(db)
    runtime = AgentRuntime(
        db=db,
        events=events,
        plans=PlanVersionService(db),
        approvals=approvals,
        checkpoints=CheckpointStore(db),
        memory=MemoryService(db, events, tmp_path / "memory"),
        tools=create_default_registry(tmp_path / "workspace", db=db, approval_service=approvals),
        model=LiveConversationModel(RoutedModelGateway(db, ModelControlStore(db), execute_attempt=script)),
    )
    register_goal_tools(
        runtime.tools, goal_programs=runtime.goal_programs, plan_documents=runtime.plan_documents,
    )
    runtime.evolution = evolution
    runtime.bundle_id = row["bundle_id"] if row is not None else None
    return runtime


def _no_duplicate_spans(contexts: list[dict], *, root_span: str) -> None:
    """No span repeats, nothing points at itself, every parent is the root."""
    spans = [context["span_id"] for context in contexts]
    assert len(spans) == len(set(spans))
    for context in contexts:
        assert context["span_id"] != context["parent_span_id"]
        assert context["parent_span_id"] == root_span


# --------------------------------------------------------------------------- #
# I01: new conversation -> LLM -> READ tool
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_i01_read_tool_chain_shares_one_trace(tmp_path, monkeypatch) -> None:
    script = FixedGateway([
        {"tool_calls": [tool_call("get_today_tasks", {})]},
        answer("今天没有安排行动。"),
    ])
    runtime = _versioned(build_runtime(tmp_path, monkeypatch, script))
    thread = runtime.conversation.create_thread("上下文链路")
    accepted = runtime.conversation.accept_turn(thread.id, "i01", "今天要做什么？", [])

    assert await runtime.turn_worker.run_once() is True

    turn = runtime.conversation.turn(accepted.turn_id)
    assert turn.status == "COMPLETED"
    assert runtime.conversation.messages(thread.id)[-1].content == "今天没有安排行动。"

    root = turn_context(runtime, accepted.turn_id)
    assert root["turn_id"] == accepted.turn_id
    assert root["thread_id"] == thread.id
    assert root["owner_id"] == "local-user"
    assert root["run_id"] == f"chat-turn:{accepted.turn_id}"
    assert root["parent_span_id"] is None
    assert root["task_id"] is None and root["root_task_id"] is None

    invocations = invocation_contexts(runtime, accepted.turn_id)
    assert len(invocations) >= 2
    for context in invocations:
        assert context["trace_id"] == root["trace_id"]
        assert context["owner_id"] == root["owner_id"]
        assert context["root_budget_id"] == root["root_budget_id"]
        assert context["runtime_bundle_id"] == root["runtime_bundle_id"]
    # Every LLM call in the turn is a direct child of the turn root, one span each.
    _no_duplicate_spans(invocations, root_span=root["span_id"])

    tools = tool_contexts(runtime, accepted.turn_id)
    assert len(tools) == 1
    tool = tools[0]
    assert tool["trace_id"] == root["trace_id"]
    assert tool["owner_id"] == root["owner_id"]
    assert tool["turn_id"] == accepted.turn_id
    assert tool["thread_id"] == thread.id
    assert tool["run_id"] == f"chat-turn:{accepted.turn_id}"
    # The tool is a child of the LLM call that produced it, not of the turn.
    assert tool["parent_span_id"] in {context["span_id"] for context in invocations}
    assert tool["span_id"] not in {context["span_id"] for context in invocations}


# --------------------------------------------------------------------------- #
# I02: two tools in one response, then another LLM call
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_i02_parallel_tools_share_one_parent_and_next_call_is_new(tmp_path, monkeypatch) -> None:
    script = FixedGateway([
        {"tool_calls": [tool_call("get_today_tasks", {}), tool_call("query_goals", {})]},
        answer("目标列表和今日任务都为空。"),
    ])
    runtime = _versioned(build_runtime(tmp_path, monkeypatch, script))
    thread = runtime.conversation.create_thread("并行工具")
    accepted = runtime.conversation.accept_turn(thread.id, "i02", "看看目标和任务", [])

    assert await runtime.turn_worker.run_once() is True
    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"

    root = turn_context(runtime, accepted.turn_id)
    tools = tool_contexts(runtime, accepted.turn_id)
    assert len(tools) == 2
    # Two tool calls from one response: two spans, one shared parent (that LLM call).
    assert tools[0]["span_id"] != tools[1]["span_id"]
    assert tools[0]["parent_span_id"] == tools[1]["parent_span_id"]
    for tool in tools:
        assert tool["trace_id"] == root["trace_id"]
        assert tool["root_budget_id"] == root["root_budget_id"]
        assert tool["runtime_bundle_id"] == root["runtime_bundle_id"]

    invocations = invocation_contexts(runtime, accepted.turn_id)
    _no_duplicate_spans(invocations, root_span=root["span_id"])
    # The follow-up LLM call is a *new* child of the turn, not a reuse of the first.
    assert tools[0]["parent_span_id"] in {context["span_id"] for context in invocations}
    assert len(invocations) >= 2
    assert len({context["span_id"] for context in invocations}) == len(invocations)
    assert len({context["trace_id"] for context in invocations + tools}) == 1


# --------------------------------------------------------------------------- #
# I03: tool-internal LLM call (goal plan compile)
# --------------------------------------------------------------------------- #

def _program_fixture(days: int = 5, *, daily_minutes: int = 60) -> dict:
    from datetime import date, timedelta

    start = date(2026, 9, 1)
    return {
        "objective_title": "五周训练计划",
        "objective_summary": "按计划完成训练",
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=days - 1)).isoformat(),
        "assumptions": [],
        "milestones": [{
            "logical_key": "m1", "title": "完成周期",
            "target_date": (start + timedelta(days=days - 1)).isoformat(),
        }],
        "actions": [
            {"logical_key": f"d{i + 1}", "scheduled_date": (start + timedelta(days=i)).isoformat(),
             "position": 1, "title": f"第 {i + 1} 天", "description": "练习",
             "estimated_minutes": daily_minutes, "completion_criteria": "完成", "required": True}
            for i in range(days)
        ],
    }


@pytest.mark.asyncio
async def test_i03_tool_internal_llm_call_is_a_child_of_the_tool(tmp_path, monkeypatch) -> None:
    script = FixedGateway(
        conversation=[
            {"tool_calls": [tool_call("activate_goal_plan", {"mode": "preview"})]},
            answer("预览已经生成，请确认后激活。"),
        ],
        planner=[{"message": json.dumps(_program_fixture(), ensure_ascii=False)}],
    )
    runtime = _versioned(build_runtime(tmp_path, monkeypatch, script))
    # The real ``GoalProgramCompiler`` must drive the internal call.
    from app.goal_program_compiler import GoalProgramCompiler

    assert isinstance(runtime.goal_programs.compiler, GoalProgramCompiler)

    thread = runtime.conversation.create_thread("计划编译")
    document = runtime.plan_documents.save_model_revision(
        thread_id=thread.id, title="五周训练计划",
        markdown_content="# 五周训练计划\n\n- 每周三次力量训练",
        source_turn_id=None, source_message_id=None, actor="model",
    )
    script.conversation[0]["tool_calls"][0]["function"]["arguments"] = json.dumps({
        "mode": "preview",
        "document_id": document.plan_document_id,
        "expected_document_version_id": document.id,
        "start_date": "2026-09-01",
        "end_date": "2026-09-05",
        "timezone": "Asia/Shanghai",
        "daily_minutes": 60,
    }, ensure_ascii=False)

    accepted = runtime.conversation.accept_turn(thread.id, "i03", "生成执行预览", [])
    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(accepted.turn_id)
    assert paused.status == "AWAITING_TOOL_APPROVAL"
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)

    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "i03-approve",
    )
    assert await runtime.turn_worker.run_once() is True
    assert runtime.conversation.turn(continuation.id).status == "COMPLETED"
    assert _count(runtime, "goal_programs") == 1

    root = turn_context(runtime, accepted.turn_id)
    tool = [context for context in tool_contexts(runtime, accepted.turn_id) if context][0]
    # The compile ran inside the tool: its span hangs off the tool span, and the
    # identity is the original tool's, not the continuation turn's.
    with runtime.db.connection() as connection:
        planner_rows = connection.execute(
            "SELECT * FROM model_invocations WHERE role='planner' ORDER BY created_at,id"
        ).fetchall()
    assert len(planner_rows) == 1
    planner = stored_context(planner_rows[0])
    from app.event_envelope import EventMetadata
    resume = next(event for event in runtime.conversation.events.list(thread.id)
                  if event.type == "chat_tool.context_resumed")
    attempt = EventMetadata.from_dict(json.loads(resume.envelope_json)).context
    assert attempt.parent_span_id == tool["span_id"]
    assert planner["parent_span_id"] == attempt.span_id
    assert planner["trace_id"] == root["trace_id"]
    assert planner["owner_id"] == root["owner_id"]
    assert planner["turn_id"] == accepted.turn_id
    assert planner["thread_id"] == thread.id
    # The tool's own budget root and bundle are reused; no substitute root is minted.
    assert planner["root_budget_id"] == tool["root_budget_id"] == root["root_budget_id"]
    assert planner["runtime_bundle_id"] == tool["runtime_bundle_id"] == root["runtime_bundle_id"]
    assert planner_rows[0]["runtime_bundle_id"] == root["runtime_bundle_id"]

    # The tool span itself was persisted before the approval, and never replaced.
    assert pending.turn_id == accepted.turn_id
    assert tool["run_id"] == f"chat-turn:{accepted.turn_id}"


# --------------------------------------------------------------------------- #
# I04: two owners interleaved
# --------------------------------------------------------------------------- #

def _owner_bundle(runtime, monkeypatch, owner_id: str, *, env_prefix: str, profile_suffix: str = "") -> str:
    """Another owner's own profiles, policy and bundle.

    Model profiles and routing policies are owner-scoped, so a bundle that
    routes another owner's turns must reference that owner's policy - which is
    exactly what makes a cross-owner mix-up detectable.
    """
    from app.model_admin import ModelAdminService

    admin = ModelAdminService(runtime.db, owner_id=owner_id)
    versions = {}
    for name, capabilities in {
        "chat": {"text": True, "streaming": True},
        "planner": {"text": True, "json_object": True},
    }.items():
        env = f"{env_prefix}_{name.upper()}_KEY"
        monkeypatch.setenv(env, "secret")
        versions[name] = admin.create_profile({
            "name": f"{name}{profile_suffix}", "provider_protocol": "openai_compatible",
            "provider_name": f"{name}{profile_suffix}",
            "base_url": f"https://{owner_id}-{name}{profile_suffix}.test/v1",
            "model_name": f"{name}{profile_suffix}",
            "credential_env_ref": env, "capabilities": capabilities,
            "context_window": 131072, "max_output_tokens": 4096, "timeout_seconds": 5,
            "max_attempts": 1,
        })["versions"][0]["id"]
    policy = admin.create_policy(f"runtime{profile_suffix}", {
        "conversation": {"primary": versions["chat"], "fallback": []},
        "planner": {"primary": versions["planner"], "fallback": []},
    })
    return runtime.control.bundles.ensure({
        "model_routing": {"policy_id": policy["id"], "digest": policy["policy_digest"]},
        "model_role_bindings": policy["roles"],
        "owner": owner_id,
    }).id


class _PerOwnerEvolution:
    """Assigns a distinct bundle per thread so bundle mixing is detectable."""

    def __init__(self, bundles: dict[str, str]) -> None:
        self.bundles = bundles

    def assign_run(self, run_id, assignment_key, *, connection=None):
        return self.bundles.get(assignment_key), "deployment"

    def finish_run_exposure(self, run_id, *, success, safety_pass=None, connection=None):
        return None


@pytest.mark.asyncio
async def test_i04_two_owners_never_share_identity_or_version(tmp_path, monkeypatch) -> None:

    from app.conversation import ManagedTurnWorkerPool

    def scripted(request):
        """Answer whoever asked - the two turns interleave in one event loop."""
        blob = json.dumps(request.messages, ensure_ascii=False)
        owner = "A" if "A 的问题" in blob else "B"
        if not any(message.get("role") == "tool" for message in request.messages):
            return {"tool_calls": [tool_call("get_today_tasks", {})]}
        return answer(f"{owner} 的答案。")

    script = FixedGateway([scripted] * 4)
    runtime = build_runtime(tmp_path, monkeypatch, script)
    # Each owner gets its own profiles, policy and bundle.  Routing policies are
    # owner-scoped, so a turn routed through another owner's bundle cannot even
    # resolve - which is what makes a cross-owner mix-up fail loudly.
    bundle_a = _owner_bundle(runtime, monkeypatch, "owner-a", env_prefix="OWNERA")
    bundle_b = _owner_bundle(runtime, monkeypatch, "owner-b", env_prefix="OWNERB")
    assert bundle_a != bundle_b
    runtime.evolution = _PerOwnerEvolution({})

    thread_a = runtime.conversation.create_thread("owner-a 会话", owner_id="owner-a")
    thread_b = runtime.conversation.create_thread("owner-b 会话", owner_id="owner-b")
    runtime.evolution.bundles = {thread_a.id: bundle_a, thread_b.id: bundle_b}

    first = runtime.conversation.accept_turn(thread_a.id, "i04-a", "A 的问题", [], owner_id="owner-a")
    second = runtime.conversation.accept_turn(thread_b.id, "i04-b", "B 的问题", [], owner_id="owner-b")
    assert runtime.conversation.turn(first.turn_id, "owner-a").runtime_bundle_id == bundle_a
    assert runtime.conversation.turn(second.turn_id, "owner-b").runtime_bundle_id == bundle_b

    pool = ManagedTurnWorkerPool(runtime.conversation, concurrency=2)
    results = await asyncio.gather(pool.workers[0].run_once(), pool.workers[1].run_once())
    assert all(results)

    root_a = turn_context(runtime, first.turn_id)
    root_b = turn_context(runtime, second.turn_id)
    assert root_a["owner_id"] == "owner-a" and root_b["owner_id"] == "owner-b"
    assert root_a["trace_id"] != root_b["trace_id"]
    assert root_a["runtime_bundle_id"] == bundle_a
    assert root_b["runtime_bundle_id"] == bundle_b

    for turn_id, root, owner, bundle in (
        (first.turn_id, root_a, "owner-a", bundle_a),
        (second.turn_id, root_b, "owner-b", bundle_b),
    ):
        contexts = invocation_contexts(runtime, turn_id) + tool_contexts(runtime, turn_id)
        assert contexts, f"turn {turn_id} persisted no execution context"
        for context in contexts:
            assert context["owner_id"] == owner
            assert context["trace_id"] == root["trace_id"]
            assert context["runtime_bundle_id"] == bundle
            assert context["turn_id"] == turn_id
        with runtime.db.connection() as connection:
            rows = connection.execute(
                "SELECT runtime_bundle_id FROM model_invocations WHERE turn_id=?", (turn_id,),
            ).fetchall()
        assert rows and {row["runtime_bundle_id"] for row in rows} == {bundle}
    assert runtime.conversation.messages(thread_a.id, "owner-a")[-1].content == "A 的答案。"
    assert runtime.conversation.messages(thread_b.id, "owner-b")[-1].content == "B 的答案。"


# --------------------------------------------------------------------------- #
# I05: a failed turn must not leak its context into the next one
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_i05_failed_turn_does_not_leak_its_context(tmp_path, monkeypatch) -> None:
    from app.model_gateway import GatewayError

    def explode(_request):
        raise GatewayError("provider down", "server")

    script = FixedGateway([explode, {"tool_calls": [tool_call("get_today_tasks", {})]}, answer("第二次正常。")])
    runtime = build_runtime(tmp_path, monkeypatch, script)
    thread = runtime.conversation.create_thread("失败恢复")
    failed = runtime.conversation.accept_turn(thread.id, "i05-fail", "第一次", [])

    await runtime.turn_worker.run_once()
    assert runtime.conversation.turn(failed.turn_id).status in {"FAILED", "ERROR"}
    # The ambient ContextVar was reset on the failure path.
    assert runtime.gateway._call_context.get() is None

    second = runtime.conversation.accept_turn(thread.id, "i05-ok", "第二次", [])
    assert await runtime.turn_worker.run_once() is True
    assert runtime.conversation.turn(second.turn_id).status == "COMPLETED"

    first_root = turn_context(runtime, failed.turn_id)
    second_root = turn_context(runtime, second.turn_id)
    assert first_root["trace_id"] != second_root["trace_id"]
    assert first_root["span_id"] != second_root["span_id"]
    for context in invocation_contexts(runtime, second.turn_id) + tool_contexts(runtime, second.turn_id):
        assert context["trace_id"] == second_root["trace_id"]
        assert context["turn_id"] == second.turn_id
        assert context["span_id"] != first_root["span_id"]


# --------------------------------------------------------------------------- #
# I06: the internal call keeps the bundle it started with
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("learned_policy", [False, True])
async def test_i06_internal_call_keeps_the_original_bundle_after_a_stable_switch(
    tmp_path, monkeypatch, learned_policy,
) -> None:
    from app.execution_context import create_child_context, create_root_context

    script = FixedGateway(planner=[{"message": json.dumps(_program_fixture(), ensure_ascii=False)}])
    runtime = build_runtime(tmp_path, monkeypatch, script)
    control = runtime.control

    # A second planner profile and a second bundle: if the compile resolved the
    # *current* stable bundle it would route to planner-b and the assertion below
    # would catch it, instead of only recording a stale id in the audit row.
    env = "PLANNER_B_KEY"
    monkeypatch.setenv(env, "secret")
    planner_b = control.admin.create_profile({
        "name": "planner-b", "provider_protocol": "openai_compatible", "provider_name": "planner-b",
        "base_url": "https://planner-b.test/v1", "model_name": "planner-b",
        "credential_env_ref": env, "capabilities": {"text": True, "json_object": True},
        "context_window": 131072, "max_output_tokens": 4096, "timeout_seconds": 5,
        "max_attempts": 1,
    })["versions"][0]["id"]
    policy_b = control.admin.create_policy("runtime", {
        "conversation": {"primary": control.versions["chat"], "fallback": []},
        "planner": {"primary": planner_b, "fallback": []},
    })
    switched = control.bundles.ensure({
        "model_routing": {"policy_id": policy_b["id"], "digest": policy_b["policy_digest"]},
        "model_role_bindings": policy_b["roles"],
    })

    tool_span = create_child_context(create_root_context(
        owner_id="local-user", run_id="chat-turn:t", runtime_bundle_id=runtime.bundle_id,
    ))
    # The stable channel moves on *after* the tool call was bound to bundle A.
    control.bundles.activate("stable", switched.id, "switch")

    service = runtime.goal_programs
    compile_thread = runtime.conversation.create_thread("切换")
    document = runtime.plan_documents.save_model_revision(
        thread_id=compile_thread.id,
        title="五周训练计划", markdown_content="# 五周训练计划\n\n- 力量训练",
        source_turn_id=None, source_message_id=None, actor="model",
    )
    if learned_policy:
        from types import SimpleNamespace
        from dataclasses import replace
        # Include a real thread binding so _compile enters its learning branch.
        tool_span = replace(tool_span, thread_id=compile_thread.id)
        def resolve(*args):
            pytest.fail("a pinned tool compile must not resolve a new learned bundle")
        service.learning = SimpleNamespace(planning_constraints=lambda *a, **kw: None,
                                          resolve_task_policy=resolve)
    program = await service.preview(
        document.plan_document_id, start_date="2026-09-01", timezone_name="Asia/Shanghai",
        daily_minutes=60, requested_end_date="2026-09-05",
        idempotency_key="i06-preview", owner_id="local-user", harness=tool_span,
    )
    assert program["status"] == "DRAFT"

    with runtime.db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM model_invocations WHERE role='planner' ORDER BY created_at,id"
        ).fetchone()
    assert row is not None
    context = stored_context(row)
    assert context["runtime_bundle_id"] == runtime.bundle_id
    assert context["parent_span_id"] == tool_span.span_id
    assert context["trace_id"] == tool_span.trace_id
    assert row["runtime_bundle_id"] == runtime.bundle_id
    # The *actual* request was routed through bundle A's policy, not the new one.
    route = json.loads(row["route_snapshot_json"])
    assert route["profile_version_id"] == control.versions["planner"]
    assert route["profile_version_id"] != planner_b
    assert row["routing_policy_digest"] == control.policy["policy_digest"]
    assert row["routing_policy_digest"] != policy_b["policy_digest"]
