"""MCP tools inside the plain-chat tool loop.

The tests drive the real ``ManagedTurnWorker`` with a scripted model gateway, so
the tool allowance, the schema projection, the inline execution, the pending
approval pause, the continuation resume and the transcript replay all run
through the production code paths. Only the model is scripted; the MCP server is
the real process in ``mcp_test_server.py``.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.mcp_client import McpClientManager, server_config_from_dict
from app.mcp_tools import McpToolRegistrySync
from test_runtime import make_runtime

SERVER = str(Path(__file__).with_name("mcp_test_server.py"))


def mcp_config(server_id: str = "t", **overrides):
    payload = {
        "server_id": server_id,
        "transport": "stdio",
        "command": sys.executable,
        "args": [SERVER],
        "timeout_seconds": 20,
    }
    payload.update(overrides)
    return server_config_from_dict(payload)


def _tool_call(name: str, arguments: dict | None = None) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {}, ensure_ascii=False)},
    }


class ScriptedGateway:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.requests: list[list[dict]] = []
        self.tools_seen: list[list[dict]] = []

    async def complete(self, request, **kwargs):
        self.requests.append(list(request.messages))
        self.tools_seen.append(list(request.tools or []))
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


def _answer(body: str) -> dict:
    return {
        "message": (
            '{"v":1,"policy":"answer","content_shape":"general","reason_code":"content_only"}\n'
            + body
        )
    }


def _last_tool_data(request) -> dict:
    for message in reversed(request.messages):
        if message.get("role") == "tool":
            return json.loads(message["content"]).get("data") or {}
    raise AssertionError("no tool result in request history")


async def _runtime_with_mcp(tmp_path, gateway, configs, *, auth_scope="local-user"):
    from app.live_model import LiveConversationModel

    runtime = make_runtime(tmp_path, LiveConversationModel(gateway))
    manager = McpClientManager(list(configs))
    runtime.mcp_manager = manager
    runtime.mcp_sync = McpToolRegistrySync(manager, runtime.tools, manager.configs())
    await runtime.mcp_sync.sync_all(auth_scope)
    return runtime


@pytest_asyncio.fixture
async def close_runtimes():
    created = []
    yield created
    for runtime in created:
        await runtime.mcp_sync.close()


@pytest.mark.asyncio
async def test_read_only_mcp_tool_runs_inline_and_reaches_the_model(tmp_path, close_runtimes) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__t__echo", {"text": "来自远端的问候"})]},
        _answer("远端返回：来自远端的问候"),
    ])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [mcp_config()])
    close_runtimes.append(runtime)
    thread = runtime.conversation.create_thread("mcp chat")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-mcp-1", "帮我回显一句话", [])

    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    assert runtime.conversation.messages(thread.id)[-1].content == "远端返回：来自远端的问候"
    # The MCP tool schema was actually offered to the model.
    offered = {item["function"]["name"] for item in gateway.tools_seen[0]}
    assert "mcp__t__echo" in offered
    with runtime.db.connection() as connection:
        rows = connection.execute(
            "SELECT tool_name,risk,status,result_json FROM turn_tool_calls WHERE turn_id=?",
            (accepted.turn_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "mcp__t__echo"
    assert rows[0]["risk"] == "READ"
    assert rows[0]["status"] == "EXECUTED"
    assert json.loads(rows[0]["result_json"])["data"]["text"] == "echo:来自远端的问候"


@pytest.mark.asyncio
async def test_native_goal_tools_still_work_alongside_mcp(tmp_path, close_runtimes) -> None:
    from app.goal_program_compiler import FixedGoalProgramCompiler
    from app.goal_tools import register_goal_tools

    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("get_today_tasks", {})]},
        _answer("今天没有安排行动。"),
    ])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [mcp_config()])
    close_runtimes.append(runtime)
    runtime.goal_programs.compiler = FixedGoalProgramCompiler()
    register_goal_tools(runtime.tools, goal_programs=runtime.goal_programs, plan_documents=runtime.plan_documents)

    thread = runtime.conversation.create_thread("regression")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-native-1", "今天要做什么？", [])
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    assert runtime.conversation.messages(thread.id)[-1].content == "今天没有安排行动。"
    offered = {item["function"]["name"] for item in gateway.tools_seen[0]}
    assert {"get_today_tasks", "mcp__t__echo"} <= offered


@pytest.mark.asyncio
async def test_allowance_excludes_mcp_when_a_skill_intersection_removes_it(
    tmp_path, close_runtimes,
) -> None:
    gateway = ScriptedGateway([_answer("普通回答。")])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [mcp_config()])
    close_runtimes.append(runtime)

    allowance = runtime.conversation_tool_allowance("turn-x", [])
    assert "mcp__t__echo" in allowance

    # A bound Skill narrows the set; the MCP tool must not survive on its own.
    class BindingSkillPlatform:
        def binding(self, *_args, **_kwargs):
            return {"version_ids": ["v1"], "grant_snapshots": {}}

        def version(self, _version_id):
            return {"kind": "tool_bound"}

        def effective_tools(self, *_args, **_kwargs):
            return {"get_today_tasks"}

    runtime.skill_platform = BindingSkillPlatform()
    narrowed = runtime.conversation_tool_allowance("turn-x", ["some-skill"])
    assert "mcp__t__echo" not in narrowed


@pytest.mark.asyncio
async def test_unavailable_server_leaves_plain_chat_working(tmp_path, close_runtimes) -> None:
    gateway = ScriptedGateway([_answer("没有任何工具也能回答。")])
    broken = server_config_from_dict({
        "server_id": "ghost", "transport": "stdio", "command": sys.executable,
        "args": ["-c", "import sys; sys.exit(3)"],
    })
    runtime = await _runtime_with_mcp(tmp_path, gateway, [broken])
    close_runtimes.append(runtime)

    thread = runtime.conversation.create_thread("no mcp")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-no-mcp", "随便聊两句", [])
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    assert runtime.conversation.messages(thread.id)[-1].content == "没有任何工具也能回答。"
    assert all(
        not item["function"]["name"].startswith("mcp__") for item in gateway.tools_seen[0]
    )


@pytest.mark.asyncio
async def test_a_name_that_was_never_offered_is_a_structure_error(tmp_path, close_runtimes) -> None:
    """The model may only call tools the harness actually offered it.

    An invented name is a protocol violation, not a tool failure, so nothing is
    executed and no tool row is written.
    """
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__ghost__whatever", {})]},
        _answer("这个工具当前不可用。"),
    ])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [mcp_config()])
    close_runtimes.append(runtime)

    thread = runtime.conversation.create_thread("unknown mcp")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-unknown", "调用一个不存在的工具", [])
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
    with runtime.db.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM turn_tool_calls WHERE turn_id=?", (accepted.turn_id,),
        ).fetchone()[0]
        job = connection.execute(
            "SELECT last_error_json FROM turn_jobs WHERE turn_id=?", (accepted.turn_id,),
        ).fetchone()
    assert count == 0
    assert "unsupported conversation tool" in job["last_error_json"]


@pytest.mark.asyncio
async def test_runner_refuses_an_mcp_tool_outside_the_allowance(tmp_path, close_runtimes) -> None:
    from app.chat_tools import ChatToolRunner

    gateway = ScriptedGateway([_answer("不会被执行。")])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [mcp_config()])
    close_runtimes.append(runtime)
    manager = runtime.mcp_manager
    before = manager.metrics.call_requests

    runner = ChatToolRunner(
        runtime.tools, runtime.approvals, runtime.conversation.chat_tool_calls,
        turn_id="turn-allow", thread_id="thread-allow", owner_id="local-user",
        project_id=None, skill_names=(), tool_allowance={"get_today_tasks"},
        mcp_sync=runtime.mcp_sync,
    )
    outcome = await runner.execute(tool_name="mcp__t__echo", params={"text": "hi"})

    assert outcome.result is not None
    assert outcome.result.ok is False
    assert outcome.result.error == "TOOL_NOT_ALLOWED"
    # The remote server was never contacted.
    assert manager.metrics.call_requests == before


@pytest.mark.asyncio
async def test_invalid_mcp_arguments_are_rejected_without_reaching_the_server(
    tmp_path, close_runtimes,
) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__t__sum_numbers", {"values": ["not-an-integer"]})]},
        _answer("参数无效，没有执行。"),
    ])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [mcp_config()])
    close_runtimes.append(runtime)

    thread = runtime.conversation.create_thread("bad args")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-bad-args", "求和", [])
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.messages(thread.id)[-1].content == "参数无效，没有执行。"
    with runtime.db.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM turn_tool_calls WHERE turn_id=?", (accepted.turn_id,),
        ).fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_failing_mcp_call_is_reported_honestly(tmp_path, close_runtimes) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__t__broken", {})]},
        _answer("远端工具报告了错误，操作没有完成。"),
    ])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [mcp_config()])
    close_runtimes.append(runtime)

    thread = runtime.conversation.create_thread("failing mcp")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-failing", "调用会失败的工具", [])
    assert await runtime.turn_worker.run_once() is True

    with runtime.db.connection() as connection:
        row = connection.execute(
            "SELECT status,result_json,error_code FROM turn_tool_calls WHERE turn_id=?",
            (accepted.turn_id,),
        ).fetchone()
    assert row["status"] == "FAILED"
    assert row["error_code"] == "MCP_TOOL_ERROR"
    assert json.loads(row["result_json"])["ok"] is False


@pytest.mark.asyncio
async def test_unsupported_content_is_not_reported_as_success(tmp_path, close_runtimes) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__t__picture", {})]},
        _answer("这个结果当前无法展示。"),
    ])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [mcp_config()])
    close_runtimes.append(runtime)

    thread = runtime.conversation.create_thread("unsupported")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-picture", "给我一张图", [])
    assert await runtime.turn_worker.run_once() is True

    with runtime.db.connection() as connection:
        row = connection.execute(
            "SELECT result_json,error_code FROM turn_tool_calls WHERE turn_id=?",
            (accepted.turn_id,),
        ).fetchone()
    assert row["error_code"] == "MCP_UNSUPPORTED_CONTENT"
    assert json.loads(row["result_json"])["ok"] is False


@pytest.mark.asyncio
async def test_oversized_mcp_result_is_truncated_and_kept_as_an_artifact(
    tmp_path, close_runtimes,
) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__t__big_result", {"kilobytes": 24})]},
        _answer("结果很大，已截断保存。"),
    ])
    runtime = await _runtime_with_mcp(
        tmp_path, gateway, [mcp_config(max_result_bytes=2048)],
    )
    close_runtimes.append(runtime)

    thread = runtime.conversation.create_thread("big result")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-big", "给我一个很大的结果", [])
    assert await runtime.turn_worker.run_once() is True

    with runtime.db.connection() as connection:
        row = connection.execute(
            "SELECT result_json FROM turn_tool_calls WHERE turn_id=?", (accepted.turn_id,),
        ).fetchone()
    payload = json.loads(row["result_json"])
    assert payload["ok"] is True
    assert payload["artifact_ref"]
    assert payload["meta"]["truncated"] is True
    assert (runtime.tools.workspace / payload["artifact_ref"]).is_file()


@pytest.mark.asyncio
async def test_write_tool_pauses_for_approval_then_commits_on_resume(
    tmp_path, close_runtimes,
) -> None:
    state_file = tmp_path / "state.json"
    config = mcp_config(
        allow_write_tools=True, default_risk="WRITE",
        enabled_tools=["write_marker", "read_marker"],
        env={"MCP_TEST_TOOLSET": "write", "MCP_TEST_STATE_FILE": str(state_file)},
    )
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__t__write_marker", {"value": "已批准"})]},
        _answer("远端已保存。"),
    ])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [config])
    close_runtimes.append(runtime)

    thread = runtime.conversation.create_thread("mcp write")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-mcp-write", "把标记写到远端", [])
    await runtime.turn_worker.run_once()

    paused = runtime.conversation.turn(accepted.turn_id)
    assert paused.status == "AWAITING_TOOL_APPROVAL"
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)
    assert pending.tool_name == "mcp__t__write_marker"
    # Nothing was written before the user decided.
    assert not state_file.exists()
    # The approval is bound to the server configuration and the definition.
    assert pending.binding["mcp"]["server_id"] == "t"
    assert pending.binding["mcp"]["config_version"] == config.config_version
    assert pending.binding["mcp"]["definition_digest"]

    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "decision-mcp",
    )
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(continuation.id).status == "COMPLETED"
    assert json.loads(state_file.read_text(encoding="utf-8"))["marker"] == "已批准"
    assert runtime.conversation.chat_tool_calls.get(pending.id).status == "EXECUTED"


@pytest.mark.asyncio
async def test_rejected_mcp_write_never_reaches_the_server(tmp_path, close_runtimes) -> None:
    state_file = tmp_path / "state.json"
    config = mcp_config(
        allow_write_tools=True, default_risk="WRITE",
        enabled_tools=["write_marker"],
        env={"MCP_TEST_TOOLSET": "write", "MCP_TEST_STATE_FILE": str(state_file)},
    )
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__t__write_marker", {"value": "不该写入"})]},
        _answer("好的，我没有写入远端。"),
    ])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [config])
    close_runtimes.append(runtime)

    thread = runtime.conversation.create_thread("mcp reject")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-mcp-reject", "写到远端", [])
    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(accepted.turn_id)
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)
    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "reject", paused.version, "decision-mcp-reject",
    )
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(continuation.id).status == "COMPLETED"
    assert not state_file.exists()
    assert runtime.conversation.chat_tool_calls.get(pending.id).status == "REJECTED"


@pytest.mark.asyncio
async def test_mcp_exchange_is_replayed_to_the_model_on_a_later_turn(
    tmp_path, close_runtimes,
) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__t__echo", {"text": "第一次"})]},
        _answer("第一次已回显。"),
        _answer("第二次没有调用工具。"),
    ])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [mcp_config()])
    close_runtimes.append(runtime)

    thread = runtime.conversation.create_thread("mcp history")
    first = runtime.conversation.accept_turn(thread.id, "turn-mcp-h1", "回显第一次", [])
    await runtime.turn_worker.run_once()
    assert runtime.conversation.turn(first.turn_id).status == "COMPLETED"

    second = runtime.conversation.accept_turn(thread.id, "turn-mcp-h2", "再说一次", [])
    await runtime.turn_worker.run_once()
    assert runtime.conversation.turn(second.turn_id).status == "COMPLETED"

    final_request = gateway.requests[-1]
    tool_messages = [message for message in final_request if message.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "echo:第一次" in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_a_server_without_cache_hints_is_rediscovered_at_each_use(
    tmp_path, close_runtimes,
) -> None:
    """Spec behaviour, not an oversight.

    ``ttlMs`` is a freshness hint. A server that omits it is treated as
    immediately stale, so the Harness re-reads the catalogue at turn start and
    again before each execution instead of promising a reuse the server never
    offered.
    """
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__t__echo", {"text": "a"}), _tool_call("mcp__t__echo", {"text": "b"})]},
        _answer("两次都回显了。"),
    ])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [mcp_config()])
    close_runtimes.append(runtime)
    manager = runtime.mcp_manager
    before = manager.metrics.discovery_requests

    thread = runtime.conversation.create_thread("mcp cache")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-mcp-cache", "回显两次", [])
    assert await runtime.turn_worker.run_once() is True

    # One refresh at turn start plus one verification per executed call.
    assert manager.metrics.discovery_requests - before == 3
    with runtime.db.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM turn_tool_calls WHERE turn_id=?", (accepted.turn_id,),
        ).fetchone()[0]
    assert count == 2


@pytest.mark.asyncio
async def test_a_server_with_a_positive_ttl_is_discovered_once_per_turn(
    tmp_path, close_runtimes,
) -> None:
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__t__echo", {"text": "a"}), _tool_call("mcp__t__echo", {"text": "b"})]},
        _answer("两次都回显了。"),
    ])
    runtime = await _runtime_with_mcp(
        tmp_path, gateway, [mcp_config(env={"MCP_TEST_TTL_MS": "60000"})],
    )
    close_runtimes.append(runtime)
    manager = runtime.mcp_manager
    before = manager.metrics.discovery_requests

    thread = runtime.conversation.create_thread("mcp ttl")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-mcp-ttl", "回显两次", [])
    assert await runtime.turn_worker.run_once() is True

    # The catalogue was already fresh, so the turn needed no discovery at all.
    assert manager.metrics.discovery_requests - before == 0
    assert manager.metrics.cache_hits >= 3
    with runtime.db.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM turn_tool_calls WHERE turn_id=?", (accepted.turn_id,),
        ).fetchone()[0]
    assert count == 2


@pytest.mark.asyncio
async def test_definition_change_between_proposal_and_resume_refuses_the_write(
    tmp_path, close_runtimes,
) -> None:
    state_file = tmp_path / "state.json"
    config = mcp_config(
        allow_write_tools=True, default_risk="WRITE",
        enabled_tools=["write_marker", "change_definition"],
        env={"MCP_TEST_TOOLSET": "write", "MCP_TEST_STATE_FILE": str(state_file)},
    )
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__t__write_marker", {"value": "变更后"})]},
        _answer("远端工具定义已变化，操作没有执行。"),
    ])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [config])
    close_runtimes.append(runtime)

    thread = runtime.conversation.create_thread("mcp drift")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-mcp-drift", "写到远端", [])
    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(accepted.turn_id)
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)
    assert pending.tool_name == "mcp__t__write_marker"

    # The remote definition changes while the approval is pending.
    await runtime.mcp_manager.call_tool("t", "change_definition", {"name": "write_marker"})
    adapter = runtime.mcp_sync.adapters["t"]
    original = adapter.digest_for("mcp__t__write_marker")

    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "decision-mcp-drift",
    )
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(continuation.id).status == "COMPLETED"
    assert not state_file.exists()
    assert adapter.digest_for("mcp__t__write_marker") != original
    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "FAILED"
    assert stored.error_code == "MCP_APPROVAL_STALE"


def test_this_turns_tool_schemas_are_charged_against_the_message_budget() -> None:
    """The MCP schema the model is shown is part of the request that is metered."""
    from app.token_budget import (
        ContextOverflow,
        Utf8UpperBoundTokenCounter,
        pack_messages_newest,
    )

    counter = Utf8UpperBoundTokenCounter()
    messages = [{"role": "user", "content": "x" * 400} for _ in range(30)]

    def tool(description_size: int) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "mcp__t__big_result",
                "description": "d" * description_size,
                "parameters": {"type": "object", "properties": {"kilobytes": {"type": "integer"}}},
            },
        }

    without = pack_messages_newest(messages, budget=9000, tools=[], counter=counter)
    with_mcp = pack_messages_newest(messages, budget=9000, tools=[tool(2000)], counter=counter)
    # The schema costs budget, so fewer messages survive.
    assert len(with_mcp) < len(without)
    assert counter.count_payload(with_mcp, [tool(2000)]) <= 9000

    # A schema large enough to consume the whole budget overflows rather than
    # being silently dropped from the accounting.
    with pytest.raises(ContextOverflow):
        pack_messages_newest(messages, budget=9000, tools=[tool(20000)], counter=counter)


@pytest.mark.asyncio
async def test_an_unavailable_server_contributes_no_schema_to_the_model(tmp_path, close_runtimes) -> None:
    gateway = ScriptedGateway([_answer("没问题。")])
    broken = server_config_from_dict({
        "server_id": "ghost", "transport": "stdio", "command": sys.executable,
        "args": ["-c", "import sys; sys.exit(3)"],
    })
    runtime = await _runtime_with_mcp(tmp_path, gateway, [broken])
    close_runtimes.append(runtime)

    allowance = runtime.conversation_tool_allowance("turn-x", [])
    assert not any(name.startswith("mcp__") for name in allowance)

    thread = runtime.conversation.create_thread("ghost")
    runtime.conversation.accept_turn(thread.id, "turn-ghost", "你好", [])
    assert await runtime.turn_worker.run_once() is True
    assert not any(
        item["function"]["name"].startswith("mcp__") for item in gateway.tools_seen[0]
    )


@pytest.mark.asyncio
async def test_cancelled_turn_leaves_no_remote_write(tmp_path, close_runtimes) -> None:
    state_file = tmp_path / "state.json"
    config = mcp_config(
        allow_write_tools=True, default_risk="WRITE",
        enabled_tools=["write_marker"],
        env={"MCP_TEST_TOOLSET": "write", "MCP_TEST_STATE_FILE": str(state_file)},
    )
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__t__write_marker", {"value": "取消"})]},
    ])
    runtime = await _runtime_with_mcp(tmp_path, gateway, [config])
    close_runtimes.append(runtime)

    thread = runtime.conversation.create_thread("mcp cancel")
    accepted = runtime.conversation.accept_turn(thread.id, "turn-mcp-cancel", "写到远端", [])
    await runtime.turn_worker.run_once()
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)

    cancelled = runtime.conversation.cancel_turn(accepted.turn_id)

    assert cancelled.status == "CANCELLED"
    assert runtime.conversation.chat_tool_calls.get(pending.id).status == "CANCELLED"
    assert not state_file.exists()
