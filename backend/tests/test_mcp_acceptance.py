"""MCP phase-1 acceptance matrix.

The model is deliberately scripted for deterministic protocol tests, but
every MCP server here is real: the official reference servers are spawned as
subprocesses and spoken to over the real protocol, and the controlled server in
``mcp_test_server.py`` is a real server too. Nothing replaces a remote call with
a recorded answer.

Running this module with ``MCP_ACCEPTANCE_EVIDENCE=<path>`` writes the observed
matrix - SDK version, protocol version, tool identities, definition digests and
the countable cache behaviour - as JSON evidence for the acceptance report.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from app.mcp_client import McpClientManager, McpProtocolError, server_config_from_dict
from app.mcp_tools import McpToolAdapter, McpToolRegistrySync
from app.tools import ToolRegistry, create_default_registry
from test_runtime import make_runtime

CONTROLLED_SERVER = str(Path(__file__).with_name("mcp_test_server.py"))
EVIDENCE_PATH = os.getenv("MCP_ACCEPTANCE_EVIDENCE")


def _available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def time_config(**overrides):
    payload = {
        "server_id": "time",
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-m", "mcp_server_time", "--local-timezone", "Asia/Shanghai"],
        "timeout_seconds": 60,
        "enabled_tools": ["get_current_time", "convert_time"],
        "risk": {"get_current_time": "READ", "convert_time": "READ"},
    }
    payload.update(overrides)
    return server_config_from_dict(payload)


def fetch_config(**overrides):
    payload = {
        "server_id": "fetch",
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-m", "mcp_server_fetch", "--ignore-robots-txt"],
        "timeout_seconds": 60,
        "enabled_tools": ["fetch"],
        "max_result_bytes": 8192,
    }
    payload.update(overrides)
    return server_config_from_dict(payload)


def controlled_config(**overrides):
    payload = {
        "server_id": "ctl",
        "transport": "stdio",
        "command": sys.executable,
        "args": [CONTROLLED_SERVER],
        "timeout_seconds": 20,
    }
    payload.update(overrides)
    return server_config_from_dict(payload)


_EVIDENCE: dict[str, object] = {"environment": {}, "cases": {}}


@pytest.fixture(scope="session", autouse=True)
def _write_evidence():
    yield
    if not EVIDENCE_PATH:
        return
    import mcp

    _EVIDENCE["environment"] = {
        "sdk_module": "mcp",
        "sdk_version": getattr(mcp, "__version__", None) or _installed_version("mcp"),
        "protocol_version_latest": _latest_protocol_version(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "model": "scripted gateway (deterministic protocol tests; provider credentials not checked)",
    }
    target = Path(EVIDENCE_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_EVIDENCE, ensure_ascii=False, indent=2), encoding="utf-8")


def _installed_version(distribution: str) -> str | None:
    from importlib.metadata import version

    try:
        return version(distribution)
    except Exception:  # noqa: BLE001
        return None


def _latest_protocol_version() -> str | None:
    from mcp import types

    return getattr(types, "LATEST_PROTOCOL_VERSION", None)


def _record(case: str, **payload) -> None:
    _EVIDENCE["cases"][case] = payload


@pytest_asyncio.fixture
async def managers():
    created: list[McpClientManager] = []

    def build(configs):
        instance = McpClientManager(configs)
        created.append(instance)
        return instance

    yield build
    for instance in created:
        await instance.close()


# ------------------------------------------------------- real SDK / protocol


@pytest.mark.asyncio
async def test_reference_time_server_records_sdk_and_protocol_version(managers) -> None:
    if not _available("mcp_server_time"):
        pytest.skip("mcp-server-time is not installed")
    client = managers([time_config()])
    assert await client.start() == {}
    snapshot = await client.ensure_catalog("time", "local-user")
    names = sorted(tool.name for tool in snapshot.tools())
    assert names == ["convert_time", "get_current_time"]
    protocol = client.protocol_version("time")
    assert protocol
    _record(
        "reference_time_server",
        protocol_version=protocol,
        sdk_version=_installed_version("mcp"),
        server_info=client.handle("time").server_info,
        tools=[
            {"name": tool.name, "digest": tool.definition_digest, "read_only_hint": tool.read_only_hint}
            for tool in snapshot.tools()
        ],
        cache_scope=snapshot.cache_scope,
        ttl_seconds=[page.ttl_seconds for page in snapshot.pages],
    )


@pytest.mark.asyncio
async def test_reference_fetch_server_reads_a_real_page(managers) -> None:
    if not _available("mcp_server_fetch"):
        pytest.skip("mcp-server-fetch is not installed")
    client = managers([fetch_config()])
    assert await client.start() == {}
    snapshot = await client.ensure_catalog("fetch", "local-user")
    assert [tool.name for tool in snapshot.tools()] == ["fetch"]
    raw = await client.call_tool("fetch", "fetch", {"url": "https://example.com", "max_length": 600})
    text = raw.content[0].text if raw.content else ""
    assert raw.isError is False
    assert "Example Domain" in text
    _record(
        "reference_fetch_server",
        tool="fetch",
        url="https://example.com",
        is_error=raw.isError,
        content_length=len(text),
        excerpt=text[:200],
    )


# -------------------------------------------------------- the required matrix


@pytest.mark.asyncio
async def test_matrix_normal_multi_turn_call_against_a_real_server(tmp_path) -> None:
    from app.live_model import LiveConversationModel
    from test_mcp_chat_loop import ScriptedGateway, _answer, _tool_call

    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__time__get_current_time", {"timezone": "Asia/Tokyo"})]},
        _answer("东京当前时间已取得。"),
        {"tool_calls": [_tool_call("mcp__time__convert_time", {
            "source_timezone": "Asia/Tokyo", "time": "09:00", "target_timezone": "UTC",
        })]},
        _answer("换算完成。"),
    ])
    runtime = make_runtime(tmp_path, LiveConversationModel(gateway))
    manager = McpClientManager([time_config()])
    runtime.mcp_manager = manager
    runtime.mcp_sync = McpToolRegistrySync(manager, runtime.tools, manager.configs())
    await runtime.mcp_sync.sync_all("local-user")
    try:
        thread = runtime.conversation.create_thread("acceptance")
        first = runtime.conversation.accept_turn(thread.id, "acc-turn-1", "东京现在几点？", [])
        assert await runtime.turn_worker.run_once() is True
        second = runtime.conversation.accept_turn(thread.id, "acc-turn-2", "换成 UTC 呢？", [])
        assert await runtime.turn_worker.run_once() is True

        assert runtime.conversation.turn(first.turn_id).status == "COMPLETED"
        assert runtime.conversation.turn(second.turn_id).status == "COMPLETED"
        with runtime.db.connection() as connection:
            rows = connection.execute(
                "SELECT tool_name,status,result_json FROM turn_tool_calls ORDER BY created_at,id"
            ).fetchall()
        assert [row["tool_name"] for row in rows] == [
            "mcp__time__get_current_time", "mcp__time__convert_time",
        ]
        assert all(row["status"] == "EXECUTED" for row in rows)
        first_payload = json.loads(rows[0]["result_json"])
        assert "Tokyo" in json.dumps(first_payload, ensure_ascii=False)
        _record(
            "normal_multi_turn",
            tool_call_ids=[row["tool_name"] for row in rows],
            results=[json.loads(row["result_json"])["data"] for row in rows],
        )
    finally:
        await runtime.mcp_sync.close()


@pytest.mark.asyncio
async def test_matrix_ttl_reuse_and_expiry(managers) -> None:
    client = managers([controlled_config(env={"MCP_TEST_TTL_MS": "150"})])
    await client.start()
    await client.ensure_catalog("ctl", "u1")
    await client.ensure_catalog("ctl", "u1")
    reused = client.metrics.discovery_requests
    assert reused == 1
    await asyncio.sleep(0.25)
    await client.ensure_catalog("ctl", "u1")
    assert client.metrics.discovery_requests == 2
    _record("ttl_reuse_and_expiry", requests_within_ttl=reused,
            requests_after_expiry=client.metrics.discovery_requests)


@pytest.mark.asyncio
async def test_matrix_missing_ttl_hint_is_immediately_stale(managers) -> None:
    client = managers([controlled_config()])
    await client.start()
    await client.ensure_catalog("ctl", "u1")
    await client.ensure_catalog("ctl", "u1")
    assert client.metrics.discovery_requests == 2
    assert client.metrics.cache_hits == 0
    _record("missing_ttl_hint", discovery_requests=client.metrics.discovery_requests,
            cache_hits=client.metrics.cache_hits)


@pytest.mark.asyncio
async def test_matrix_tool_change_invalidates_the_catalogue(managers) -> None:
    client = managers([controlled_config(env={"MCP_TEST_TTL_MS": "60000"})])
    await client.start()
    before = await client.ensure_catalog("ctl", "u1")
    await client.call_tool("ctl", "flip_description", {})
    await asyncio.sleep(0.2)
    after = await client.ensure_catalog("ctl", "u1")
    assert after is not before
    assert client.metrics.catalog_invalidations >= 1
    _record("tool_change_invalidation", invalidations=client.metrics.catalog_invalidations)


@pytest.mark.asyncio
async def test_matrix_private_scope_isolation(managers) -> None:
    client = managers([controlled_config(env={"MCP_TEST_TTL_MS": "60000"})])
    await client.start()
    first = await client.ensure_catalog("ctl", "owner-a")
    second = await client.ensure_catalog("ctl", "owner-b")
    assert first is not second
    assert first.auth_scope == "owner-a" and second.auth_scope == "owner-b"
    _record("private_scope_isolation", discovery_requests=client.metrics.discovery_requests,
            scopes=[first.auth_scope, second.auth_scope])


@pytest.mark.asyncio
async def test_matrix_pagination(managers) -> None:
    client = managers([
        controlled_config(env={"MCP_TEST_TOOLSET": "paged", "MCP_TEST_PAGE_SIZE": "2", "MCP_TEST_TTL_MS": "60000"}),
    ])
    await client.start()
    snapshot = await client.ensure_catalog("ctl", "u1")
    assert len(snapshot.pages) == 3
    assert len(snapshot.tools()) == 5
    _record("pagination", pages=len(snapshot.pages), tools=len(snapshot.tools()),
            cursors=[page.cursor for page in snapshot.pages])


@pytest.mark.asyncio
async def test_matrix_unknown_tool_invalid_arguments_and_out_of_scope_resource(tmp_path) -> None:
    registry = create_default_registry(tmp_path / "workspace")
    config = controlled_config(resource_allow={"read_repo_file": {"repo": ["repo-A"]}})
    client = McpClientManager([config])
    await client.start()
    adapter = McpToolAdapter(client, config, registry)
    await adapter.sync("local-user")
    try:
        from app.tools import ToolCall, ToolExecutionContext, ToolRejected

        context = ToolExecutionContext(
            owner_id="local-user", run_id="chat-turn:t", tool_call_id="c", thread_id="th",
        )
        with pytest.raises(ToolRejected, match="unknown tool"):
            await registry.execute_async(
                ToolCall("c", "mcp__ctl__not_a_tool", {}),
                context=context, skill_tools=adapter.model_names(),
            )
        with pytest.raises(ToolRejected):
            await registry.execute_async(
                ToolCall("c", "mcp__ctl__read_repo_file", {"repo": "repo-A"}),
                context=context, skill_tools=adapter.model_names(),
            )
        with pytest.raises(ToolRejected, match="资源范围"):
            await registry.execute_async(
                ToolCall("c", "mcp__ctl__read_repo_file", {"repo": "repo-B", "path": "a.md"}),
                context=context, skill_tools=adapter.model_names(),
            )
        allowed = await registry.execute_async(
            ToolCall("c", "mcp__ctl__read_repo_file", {"repo": "repo-A", "path": "a.md"}),
            context=context, skill_tools=adapter.model_names(),
        )
        assert allowed.ok is True
        _record(
            "unknown_invalid_and_out_of_scope",
            unknown_tool="rejected",
            missing_argument="rejected",
            out_of_scope_resource="rejected",
            authorised_resource=allowed.data["text"],
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_matrix_disconnect_and_timeout(managers) -> None:
    from app.mcp_client import McpDisconnected

    client = managers([controlled_config(timeout_seconds=1.0)])
    await client.start()
    with pytest.raises(McpDisconnected):
        await client.call_tool("ctl", "slow", {"seconds": 5}, timeout_seconds=1.0)
    with pytest.raises(McpDisconnected):
        await client.call_tool("ctl", "write_then_disconnect", {"value": "x"})
    assert client.connected("ctl") is False
    snapshot = await client.ensure_catalog("ctl", "u1")
    assert snapshot.tools()
    _record("disconnect_and_timeout", reconnects=client.metrics.reconnects,
            call_failures=client.metrics.call_failures)


@pytest.mark.asyncio
async def test_matrix_large_result_is_bounded(tmp_path) -> None:
    registry = create_default_registry(tmp_path / "workspace")
    config = controlled_config(max_result_bytes=2048, enabled_tools=["big_result"])
    client = McpClientManager([config])
    await client.start()
    adapter = McpToolAdapter(client, config, registry)
    await adapter.sync("local-user")
    try:
        from app.tools import ToolCall, ToolExecutionContext

        context = ToolExecutionContext(
            owner_id="local-user", run_id="chat-turn:t", tool_call_id="c", thread_id="th",
        )
        result = await registry.execute_async(
            ToolCall("c", "mcp__ctl__big_result", {"kilobytes": 40}),
            context=context, skill_tools=adapter.model_names(),
        )
        assert result.ok is True
        assert result.meta["truncated"] is True
        assert result.artifact_ref
        assert (registry.workspace / result.artifact_ref).is_file()
        _record(
            "large_result",
            original_bytes=result.meta["original_bytes"],
            max_result_bytes=config.max_result_bytes,
            artifact_ref=result.artifact_ref,
            excerpt_chars=len(result.data["excerpt"]["text"]),
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_matrix_definition_change_is_refused(tmp_path) -> None:
    registry = create_default_registry(tmp_path / "workspace")
    config = controlled_config()
    client = McpClientManager([config])
    await client.start()
    adapter = McpToolAdapter(client, config, registry)
    await adapter.sync("local-user")
    try:
        from app.tools import ToolCall, ToolExecutionContext

        context = ToolExecutionContext(
            owner_id="local-user", run_id="chat-turn:t", tool_call_id="c", thread_id="th",
        )
        before = adapter.digest_for("mcp__ctl__echo")
        await client.call_tool("ctl", "change_definition", {"name": "echo"})
        result = await registry.execute_async(
            ToolCall("c", "mcp__ctl__echo", {"text": "hi"}),
            context=context, skill_tools=adapter.model_names(),
        )
        assert result.ok is False
        assert result.error == "MCP_DEFINITION_CHANGED"
        assert adapter.digest_for("mcp__ctl__echo") != before
        _record("definition_change", before=before,
                after=adapter.digest_for("mcp__ctl__echo"), error=result.error)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_matrix_existing_business_tools_still_work(tmp_path) -> None:
    from app.goal_program_compiler import FixedGoalProgramCompiler
    from app.goal_tools import register_goal_tools
    from app.live_model import LiveConversationModel
    from test_mcp_chat_loop import ScriptedGateway, _answer, _tool_call

    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("get_today_tasks", {})]},
        _answer("今天没有安排行动。"),
    ])
    runtime = make_runtime(tmp_path, LiveConversationModel(gateway))
    runtime.goal_programs.compiler = FixedGoalProgramCompiler()
    register_goal_tools(runtime.tools, goal_programs=runtime.goal_programs, plan_documents=runtime.plan_documents)
    manager = McpClientManager([controlled_config()])
    runtime.mcp_manager = manager
    runtime.mcp_sync = McpToolRegistrySync(manager, runtime.tools, manager.configs())
    await runtime.mcp_sync.sync_all("local-user")
    try:
        thread = runtime.conversation.create_thread("regression")
        accepted = runtime.conversation.accept_turn(thread.id, "acc-regression", "今天要做什么？", [])
        assert await runtime.turn_worker.run_once() is True
        assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
        assert runtime.conversation.messages(thread.id)[-1].content == "今天没有安排行动。"
        offered = {item["function"]["name"] for item in gateway.tools_seen[0]}
        assert "get_today_tasks" in offered
        assert "mcp__ctl__echo" in offered
        _record("business_tool_regression", offered_tools=sorted(offered))
    finally:
        await runtime.mcp_sync.close()


@pytest.mark.asyncio
async def test_matrix_unavailable_server_does_not_block_chat(tmp_path) -> None:
    from app.live_model import LiveConversationModel
    from test_mcp_chat_loop import ScriptedGateway, _answer

    gateway = ScriptedGateway([_answer("没有工具也能回答。")])
    runtime = make_runtime(tmp_path, LiveConversationModel(gateway))
    broken = server_config_from_dict({
        "server_id": "ghost", "transport": "stdio", "command": sys.executable,
        "args": ["-c", "import sys; sys.exit(3)"],
    })
    manager = McpClientManager([broken])
    runtime.mcp_manager = manager
    runtime.mcp_sync = McpToolRegistrySync(manager, runtime.tools, manager.configs())
    report = await runtime.mcp_sync.sync_all("local-user")
    try:
        thread = runtime.conversation.create_thread("optional mcp")
        accepted = runtime.conversation.accept_turn(thread.id, "acc-ghost", "随便聊", [])
        assert await runtime.turn_worker.run_once() is True
        assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
        assert report["ghost"]["registered"] == 0
        _record("unavailable_server", report=report)
    finally:
        await runtime.mcp_sync.close()


@pytest.mark.asyncio
async def test_matrix_unsupported_content_and_input_required(tmp_path) -> None:
    registry = create_default_registry(tmp_path / "workspace")
    config = controlled_config(enabled_tools=["picture"])
    client = McpClientManager([config])
    await client.start()
    adapter = McpToolAdapter(client, config, registry)
    await adapter.sync("local-user")
    try:
        from app.tools import ToolCall, ToolExecutionContext

        context = ToolExecutionContext(
            owner_id="local-user", run_id="chat-turn:t", tool_call_id="c", thread_id="th",
        )
        result = await registry.execute_async(
            ToolCall("c", "mcp__ctl__picture", {}),
            context=context, skill_tools=adapter.model_names(),
        )
        assert result.ok is False
        assert result.error == "MCP_UNSUPPORTED_CONTENT"

        from app.mcp_tools import map_call_result

        class _Result:
            content = []
            structuredContent = None
            isError = False
            meta = {"resultType": "input_required", "requestState": "opaque"}

        mapped = map_call_result(_Result(), server_id="ctl", remote_name="x")
        assert mapped.ok is False
        assert mapped.error == "MCP_INPUT_REQUIRED_UNSUPPORTED"
        _record(
            "unsupported_content",
            content_error=result.error,
            content_types=result.meta["unsupported_content_types"],
            input_required_error=mapped.error,
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_matrix_cancellation_stops_a_pending_remote_write(tmp_path) -> None:
    from app.live_model import LiveConversationModel
    from test_mcp_chat_loop import ScriptedGateway, _tool_call

    state_file = tmp_path / "state.json"
    config = controlled_config(
        allow_write_tools=True, default_risk="WRITE",
        enabled_tools=["write_marker"],
        env={"MCP_TEST_TOOLSET": "write", "MCP_TEST_STATE_FILE": str(state_file)},
    )
    gateway = ScriptedGateway([
        {"tool_calls": [_tool_call("mcp__ctl__write_marker", {"value": "cancel-me"})]},
    ])
    runtime = make_runtime(tmp_path, LiveConversationModel(gateway))
    manager = McpClientManager([config])
    runtime.mcp_manager = manager
    runtime.mcp_sync = McpToolRegistrySync(manager, runtime.tools, manager.configs())
    await runtime.mcp_sync.sync_all("local-user")
    try:
        thread = runtime.conversation.create_thread("cancel")
        accepted = runtime.conversation.accept_turn(thread.id, "acc-cancel", "写到远端", [])
        await runtime.turn_worker.run_once()
        pending = runtime.conversation.pending_tool_call(accepted.turn_id)
        runtime.conversation.cancel_turn(accepted.turn_id)
        assert runtime.conversation.chat_tool_calls.get(pending.id).status == "CANCELLED"
        assert not state_file.exists()
        _record("cancellation", pending_status="CANCELLED", remote_effect="none")
    finally:
        await runtime.mcp_sync.close()


@pytest.mark.asyncio
async def test_matrix_http_transport_is_configurable() -> None:
    """The HTTP transport is wired; only the stdio path has a live server here."""
    config = server_config_from_dict({
        "server_id": "remote",
        "transport": "http",
        "url": "https://mcp.invalid.test/mcp",
        "timeout_seconds": 1.0,
        "headers": {"Authorization": {"from_env": "MCP_ACCEPTANCE_HTTP_TOKEN", "required": False}},
    })
    assert config.transport == "http"
    client = McpClientManager([config])
    failures = await client.start()
    assert "remote" in failures
    await client.close()
    _record("http_transport", configured=True, reachable=False, failure=failures["remote"])


@pytest.mark.asyncio
async def test_matrix_discovery_on_a_server_without_tools_is_not_an_error(managers) -> None:
    client = managers([controlled_config(env={"MCP_TEST_TOOLSET": "empty"})])
    await client.start()
    snapshot = await client.ensure_catalog("ctl", "u1")
    assert snapshot.tools() == ()
    _record("empty_catalogue", tools=0)


@pytest.mark.asyncio
async def test_matrix_protocol_error_is_reported(managers) -> None:
    client = managers([controlled_config()])
    await client.start()
    handle = client.handle("ctl")
    handle.capabilities = {}
    with pytest.raises(McpProtocolError, match="does not advertise tool support"):
        await client.ensure_catalog("ctl", "u1")
    _record("protocol_error", reported="does not advertise tool support")
