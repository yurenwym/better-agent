"""MCP client manager: configuration, discovery cache, calls and teardown.

Every test here spawns the real test server in ``mcp_test_server.py`` and
speaks the real protocol over stdio. Nothing is mocked at the transport level,
so a failure in the handshake, the pagination loop or the cache accounting is a
real failure.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from app.mcp_client import (
    McpClientManager,
    McpConfigError,
    McpDisconnected,
    McpProtocolError,
    cache_scope_from_meta,
    parse_server_configs,
    server_config_from_dict,
    ttl_seconds_from_meta,
)

SERVER = str(Path(__file__).with_name("mcp_test_server.py"))


def server_config(server_id: str = "t", **overrides):
    payload = {
        "server_id": server_id,
        "transport": "stdio",
        "command": sys.executable,
        "args": [SERVER],
        "timeout_seconds": 20,
    }
    payload.update(overrides)
    return server_config_from_dict(payload)


@pytest_asyncio.fixture
async def manager():
    created: list[McpClientManager] = []

    def build(configs):
        instance = McpClientManager(configs)
        created.append(instance)
        return instance

    yield build
    for instance in created:
        await instance.close()


# --------------------------------------------------------------------- config


def test_environment_is_optional_and_malformed_config_never_raises(monkeypatch) -> None:
    assert parse_server_configs(None) == []
    assert parse_server_configs("   ") == []
    from app.mcp_client import load_manager_from_env

    monkeypatch.setenv("BETTER_AGENT_MCP_SERVERS", "{not json")
    assert load_manager_from_env().server_ids == ()


def test_credentials_are_resolved_by_reference_and_never_exposed(monkeypatch) -> None:
    monkeypatch.setenv("MY_MCP_TOKEN", "super-secret-value")
    config = server_config_from_dict({
        "server_id": "remote",
        "transport": "http",
        "url": "https://mcp.example.test/mcp",
        "headers": {"Authorization": {"from_env": "MY_MCP_TOKEN"}},
    })
    assert config.headers == {"Authorization": "super-secret-value"}
    public = json.dumps(config.public_view(), ensure_ascii=False)
    assert "super-secret-value" not in public
    assert "Authorization" in public


def test_credential_rotation_changes_the_config_version(monkeypatch) -> None:
    monkeypatch.setenv("MY_MCP_TOKEN", "first")
    first = server_config_from_dict({
        "server_id": "remote", "transport": "http", "url": "https://x.test/mcp",
        "headers": {"Authorization": {"from_env": "MY_MCP_TOKEN"}},
    })
    monkeypatch.setenv("MY_MCP_TOKEN", "second")
    second = server_config_from_dict({
        "server_id": "remote", "transport": "http", "url": "https://x.test/mcp",
        "headers": {"Authorization": {"from_env": "MY_MCP_TOKEN"}},
    })
    assert first.config_version != second.config_version
    # The version is a digest, so the value itself still never appears.
    assert "second" not in second.config_version


def test_a_credential_outside_env_indirection_is_scrubbed_from_the_public_view() -> None:
    """The public view goes into events, so "safe for logs" must hold anyway.

    Credentials are documented to arrive through ``from_env`` references, but
    a token in a URL query or in a ``--flag=value`` argument is a normal
    mistake. Neither may reach an event body or a log line.
    """
    config = server_config_from_dict({
        "server_id": "remote",
        "transport": "http",
        "url": "https://user:s3cr3t@mcp.example.test/mcp?api_key=also-secret#frag",
    })
    public = json.dumps(config.public_view(), ensure_ascii=False)
    assert "s3cr3t" not in public
    assert "also-secret" not in public
    assert "user:" not in public
    assert "mcp.example.test/mcp" in public

    stdio = server_config_from_dict({
        "server_id": "local",
        "transport": "stdio",
        "command": "python",
        "args": ["-m", "server", "--api-key=hidden-value", "--token", "hidden-too", "--port", "8080"],
    })
    rendered = json.dumps(stdio.public_view(), ensure_ascii=False)
    assert "hidden-value" not in rendered
    assert "hidden-too" not in rendered
    # The command stays diagnosable: only the values are masked.
    assert "--api-key=<redacted>" in rendered
    assert "--token" in rendered
    assert "8080" in rendered


def test_a_failed_server_names_the_command_it_tried_to_start() -> None:
    """A spawn that exits before the handshake must not report only "closed"."""
    import asyncio

    from app.mcp_client import McpUnavailable

    config = server_config_from_dict({
        "server_id": "ghost", "transport": "stdio", "command": sys.executable,
        "args": ["-c", "import sys; sys.exit(3)"], "timeout_seconds": 10,
    })

    async def attempt() -> str:
        manager = McpClientManager([config])
        try:
            failures = await manager.start()
            return failures["ghost"]
        finally:
            await manager.close()

    message = asyncio.run(attempt())
    assert "ghost" in message
    assert "sys.exit(3)" in message


def test_missing_required_credential_is_a_config_error(monkeypatch) -> None:
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    with pytest.raises(McpConfigError, match="MISSING_TOKEN"):
        server_config_from_dict({
            "server_id": "remote", "transport": "http", "url": "https://x.test/mcp",
            "headers": {"Authorization": {"from_env": "MISSING_TOKEN"}},
        })


def test_duplicate_server_ids_are_rejected() -> None:
    with pytest.raises(McpConfigError, match="duplicate"):
        parse_server_configs(json.dumps([
            {"server_id": "same", "transport": "stdio", "command": "x"},
            {"server_id": "same", "transport": "stdio", "command": "y"},
        ]))


def test_transport_requirements_are_enforced() -> None:
    with pytest.raises(McpConfigError, match="stdio transport needs a command"):
        server_config_from_dict({"server_id": "a", "transport": "stdio"})
    with pytest.raises(McpConfigError, match="http transport needs a url"):
        server_config_from_dict({"server_id": "b", "transport": "http"})
    with pytest.raises(McpConfigError, match="unsupported transport"):
        server_config_from_dict({"server_id": "c", "transport": "carrier-pigeon"})


def test_one_broken_entry_does_not_disable_the_other_servers(monkeypatch) -> None:
    """A typo in server B must not silently remove server A's tools.

    Dropping the whole document is fail-open in the wrong direction: the
    operator believes both servers are live. The good entry must survive, and
    the bad one must be reported with enough detail to fix it.
    """
    from app.mcp_client import load_manager_from_env, parse_server_configs_lenient

    raw = json.dumps([
        {"server_id": "good", "transport": "stdio", "command": "python"},
        {"server_id": "bad", "transport": "stdio"},  # no command
        {"server_id": "good", "transport": "stdio", "command": "python"},  # duplicate
    ])
    configs, rejections = parse_server_configs_lenient(raw)
    assert [config.server_id for config in configs] == ["good"]
    assert [rejection.index for rejection in rejections] == [1, 2]
    assert rejections[0].server_id == "bad"
    assert "command" in rejections[0].reason
    assert "duplicate" in rejections[1].reason

    monkeypatch.setenv("BETTER_AGENT_MCP_SERVERS", raw)
    manager = load_manager_from_env()
    assert manager.server_ids == ("good",)
    assert len(manager.config_rejections) == 2


def test_a_broken_document_reports_one_rejection_and_no_servers(monkeypatch) -> None:
    from app.mcp_client import load_manager_from_env

    monkeypatch.setenv("BETTER_AGENT_MCP_SERVERS", "[{oops")
    manager = load_manager_from_env()
    assert manager.server_ids == ()
    assert len(manager.config_rejections) == 1
    assert "not valid JSON" in manager.config_rejections[0].reason


def test_ttl_hint_parsing_matches_the_specification() -> None:
    assert ttl_seconds_from_meta({"ttlMs": 1500}) == 1.5
    assert ttl_seconds_from_meta({"ttlMs": 0}) is None
    assert ttl_seconds_from_meta({"ttlMs": -5}) is None
    assert ttl_seconds_from_meta({}) is None
    assert ttl_seconds_from_meta(None) is None
    assert ttl_seconds_from_meta({"ttlMs": "abc"}) is None
    assert cache_scope_from_meta({"cacheScope": "public"}) == "public"
    assert cache_scope_from_meta({"cacheScope": "private"}) == "private"
    assert cache_scope_from_meta(None) == "private"


# ----------------------------------------------------------------- discovery


@pytest.mark.asyncio
async def test_real_handshake_and_discovery(manager) -> None:
    client = manager([server_config()])
    assert await client.start() == {}
    snapshot = await client.ensure_catalog("t", "local-user")
    assert client.protocol_version("t")
    assert {tool.name for tool in snapshot.tools()} >= {"echo", "sum_numbers"}
    assert all(tool.definition_digest for tool in snapshot.tools())
    assert client.metrics.discovery_requests == 1


@pytest.mark.asyncio
async def test_positive_ttl_is_reused_within_its_validity(manager, monkeypatch) -> None:
    monkeypatch.setenv("MCP_TEST_TTL_MS", "60000")
    client = manager([server_config()])
    await client.start()
    first = await client.ensure_catalog("t", "u1")
    second = await client.ensure_catalog("t", "u1")
    assert first is second
    assert client.metrics.discovery_requests == 1
    assert client.metrics.cache_hits == 1


@pytest.mark.asyncio
async def test_missing_ttl_hint_is_treated_as_immediately_stale(manager) -> None:
    client = manager([server_config()])
    await client.start()
    first = await client.ensure_catalog("t", "u1")
    second = await client.ensure_catalog("t", "u1")
    assert first is not second
    assert client.metrics.discovery_requests == 2
    assert client.metrics.cache_hits == 0


@pytest.mark.asyncio
async def test_expired_ttl_triggers_rediscovery(manager, monkeypatch) -> None:
    monkeypatch.setenv("MCP_TEST_TTL_MS", "120")
    client = manager([server_config()])
    await client.start()
    await client.ensure_catalog("t", "u1")
    await asyncio.sleep(0.2)
    await client.ensure_catalog("t", "u1")
    assert client.metrics.discovery_requests == 2
    assert client.metrics.catalog_refreshes == 2


@pytest.mark.asyncio
async def test_operator_ttl_override_applies_only_without_a_server_hint(manager) -> None:
    client = manager([server_config(cache_ttl_seconds=60)])
    await client.start()
    first = await client.ensure_catalog("t", "u1")
    second = await client.ensure_catalog("t", "u1")
    assert first is second


@pytest.mark.asyncio
async def test_catalogue_cache_is_isolated_per_authorization_scope(manager, monkeypatch) -> None:
    monkeypatch.setenv("MCP_TEST_TTL_MS", "60000")
    client = manager([server_config()])
    await client.start()
    first = await client.ensure_catalog("t", "owner-a")
    second = await client.ensure_catalog("t", "owner-b")
    assert first is not second
    assert client.metrics.discovery_requests == 2


@pytest.mark.asyncio
async def test_pagination_walks_every_page_and_caches_per_page(manager, monkeypatch) -> None:
    monkeypatch.setenv("MCP_TEST_TOOLSET", "paged")
    monkeypatch.setenv("MCP_TEST_PAGE_SIZE", "2")
    monkeypatch.setenv("MCP_TEST_TTL_MS", "60000")
    client = manager([server_config()])
    await client.start()
    snapshot = await client.ensure_catalog("t", "u1")
    names = [tool.name for tool in snapshot.tools()]
    assert len(names) == 5
    assert len(snapshot.pages) == 3
    assert [page.cursor for page in snapshot.pages] == [None, "2", "4"]
    assert client.metrics.discovery_pages == 3
    # Every page carries its own TTL, so the whole catalogue stays fresh.
    await client.ensure_catalog("t", "u1")
    assert client.metrics.discovery_requests == 3
    assert client.metrics.cache_hits == 1


@pytest.mark.asyncio
async def test_tool_change_notification_invalidates_the_catalogue(manager, monkeypatch) -> None:
    monkeypatch.setenv("MCP_TEST_TTL_MS", "60000")
    client = manager([server_config()])
    await client.start()
    before = await client.ensure_catalog("t", "u1")
    assert "changed" not in json.dumps(
        [tool.definition_view() for tool in before.tools()], ensure_ascii=False,
    )
    await client.call_tool("t", "flip_description", {})
    await asyncio.sleep(0.2)
    after = await client.ensure_catalog("t", "u1")
    assert after is not before
    assert client.metrics.catalog_invalidations >= 1
    assert "changed" in json.dumps(
        [tool.definition_view() for tool in after.tools()], ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_capability_discovery_is_recorded(manager) -> None:
    client = manager([server_config()])
    await client.start()
    handle = client.handle("t")
    assert "tools" in handle.capabilities
    assert handle.server_info["name"] == "better-mcp-test-server"


@pytest.mark.asyncio
async def test_server_without_tools_registers_an_empty_catalogue(manager, monkeypatch) -> None:
    monkeypatch.setenv("MCP_TEST_TOOLSET", "empty")
    client = manager([server_config()])
    await client.start()
    snapshot = await client.ensure_catalog("t", "u1")
    assert snapshot.tools() == ()
    assert snapshot.fresh() is False  # no TTL hint means stale now


# --------------------------------------------------------------------- calls


@pytest.mark.asyncio
async def test_text_and_structured_results_are_returned_verbatim(manager) -> None:
    client = manager([server_config()])
    await client.start()
    echo = await client.call_tool("t", "echo", {"text": "hello"})
    assert echo.content[0].text == "echo:hello"
    summed = await client.call_tool("t", "sum_numbers", {"values": [2, 3, 4]})
    assert summed.structuredContent == {"total": 9, "count": 3}


@pytest.mark.asyncio
async def test_read_timeout_is_reported_as_a_disconnect(manager) -> None:
    client = manager([server_config(timeout_seconds=0.6)])
    await client.start()
    with pytest.raises(McpDisconnected, match="timed out"):
        await client.call_tool("t", "slow", {"seconds": 5}, timeout_seconds=0.6)
    assert client.metrics.call_failures == 1


@pytest.mark.asyncio
async def test_lost_connection_is_detected_and_the_next_call_reconnects(manager) -> None:
    client = manager([server_config()])
    await client.start()
    with pytest.raises(McpDisconnected):
        await client.call_tool("t", "write_then_disconnect", {"value": "landed"})
    assert client.connected("t") is False
    # The next need re-establishes the session instead of failing forever.
    snapshot = await client.ensure_catalog("t", "u1")
    assert snapshot.tools()
    assert client.metrics.reconnects >= 1


@pytest.mark.asyncio
async def test_unreachable_server_reports_an_error_instead_of_raising(manager) -> None:
    config = server_config_from_dict({
        "server_id": "ghost",
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-c", "import sys; sys.exit(3)"],
    })
    client = manager([config])
    failures = await client.start()
    assert "ghost" in failures
    assert client.connected("ghost") is False


@pytest.mark.asyncio
async def test_close_releases_the_child_process(manager) -> None:
    client = manager([server_config()])
    await client.start()
    handle = client.handle("t")
    await client.ensure_catalog("t", "u1")
    await client.close()
    assert client.connected("t") is False
    assert handle.session is None


@pytest.mark.asyncio
async def test_discovery_on_a_server_without_tool_support_is_rejected(manager) -> None:
    client = manager([server_config()])
    await client.start()
    handle = client.handle("t")
    handle.capabilities = {}
    with pytest.raises(McpProtocolError, match="does not advertise tool support"):
        await client.ensure_catalog("t", "u1")


@pytest.mark.asyncio
async def test_pagination_guard_rejects_a_repeating_cursor(manager) -> None:
    from mcp import types

    client = manager([server_config()])
    await client.start()
    handle = client.handle("t")

    class RepeatingSession:
        async def list_tools(self, cursor=None):
            return types.ListToolsResult(tools=[], nextCursor="same")

    handle.session = RepeatingSession()
    with pytest.raises(McpProtocolError, match="repeating cursor"):
        await client.ensure_catalog("t", "u1")
