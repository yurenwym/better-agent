"""MCP tool definitions mapped onto the Tool Registry.

These tests drive the real adapter against the real test server, then assert on
the registry's own vocabulary: which specs exist, how arguments are validated,
what a remote result becomes, and what the user's approval is bound to.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from app.mcp_client import McpClientManager, server_config_from_dict
from app.mcp_tools import (
    MCP_SOURCE,
    McpToolAdapter,
    McpToolRejected,
    McpToolRegistrySync,
    map_call_result,
    model_tool_name,
    validate_arguments,
)
from app.tools import (
    ToolCall,
    ToolExecutionContext,
    ToolRejected,
    ToolRegistry,
    ToolReconciliationRequired,
    ToolResult,
    ToolRisk,
    create_default_registry,
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


async def adapter_for(manager, tmp_path, *, config=None, registry=None, auth_scope="local-user"):
    config = config or server_config()
    registry = registry or create_default_registry(tmp_path / "workspace")
    client = manager([config])
    await client.start()
    adapter = McpToolAdapter(client, config, registry)
    await adapter.sync(auth_scope)
    return client, adapter, registry


def grant(registry, *, call_id: str, name: str, params: dict, run_id: str = "chat-turn:turn-1") -> None:
    """Record the user's decision the way the approval service would."""
    approvals = registry.approval_service
    approval = approvals.request(run_id, call_id, name, params)
    approvals.grant(approval.id, run_id, call_id, params)


def context(run_id: str = "chat-turn:turn-1", tool_call_id: str = "call-1") -> ToolExecutionContext:
    return ToolExecutionContext(
        owner_id="local-user", run_id=run_id, tool_call_id=tool_call_id,
        thread_id="thread-1", project_id=None,
    )


# --------------------------------------------------------------------- naming


def test_model_tool_name_is_deterministic_and_provider_safe() -> None:
    name = model_tool_name("time", "get_current_time")
    assert name == "mcp__time__get_current_time"
    assert name == model_tool_name("time", "get_current_time")
    assert all(char.isascii() and (char.isalnum() or char in "-_") for char in name)
    assert len(name) <= 64


def test_long_identities_are_truncated_with_a_stable_digest() -> None:
    first = model_tool_name("s" * 80, "t" * 80)
    second = model_tool_name("s" * 80, "t" * 80)
    assert first == second
    assert len(first) <= 64
    assert first != model_tool_name("s" * 80, "u" * 80)


def test_non_ascii_identities_are_folded_deterministically() -> None:
    name = model_tool_name("时间", "读取")
    assert name == model_tool_name("时间", "读取")
    assert all(char.isascii() for char in name)


# ------------------------------------------------------------------ selection


@pytest.mark.asyncio
async def test_only_whitelisted_tools_are_registered(manager, tmp_path) -> None:
    config = server_config(enabled_tools=["echo"])
    client, adapter, registry = await adapter_for(manager, tmp_path, config=config)
    assert adapter.model_names() == ("mcp__t__echo",)
    assert {spec.name for spec in registry.specs()} >= {"mcp__t__echo"}
    assert "sum_numbers" in adapter.rejected_tools
    assert "whitelist" in adapter.rejected_tools["sum_numbers"]


@pytest.mark.asyncio
async def test_local_risk_policy_governs_and_the_server_hint_does_not(manager, tmp_path) -> None:
    """``readOnlyHint`` is evidence, not authority: local config decides."""
    config = server_config(
        enabled_tools=["echo"], risk={"echo": "WRITE"}, allow_write_tools=True,
    )
    client, adapter, registry = await adapter_for(manager, tmp_path, config=config)
    spec = registry.spec("mcp__t__echo")
    assert spec.risk == ToolRisk.WRITE
    # The remote definition still says read-only; the local policy won.
    assert adapter._registered["mcp__t__echo"].read_only_hint is True


@pytest.mark.asyncio
async def test_write_tools_are_refused_when_the_server_is_not_trusted_for_side_effects(
    manager, tmp_path,
) -> None:
    config = server_config(allow_write_tools=False, default_risk="WRITE")
    client, adapter, registry = await adapter_for(manager, tmp_path, config=config)
    assert adapter.model_names() == ()
    assert all("not trusted for side effects" in reason for reason in adapter.rejected_tools.values())


@pytest.mark.asyncio
async def test_write_tools_are_registered_when_explicitly_trusted(manager, tmp_path) -> None:
    config = server_config(allow_write_tools=True, default_risk="WRITE")
    client, adapter, registry = await adapter_for(manager, tmp_path, config=config)
    assert "mcp__t__echo" in adapter.model_names()
    assert registry.spec("mcp__t__echo").risk == ToolRisk.WRITE


@pytest.mark.asyncio
async def test_metadata_locates_the_tool_by_source_server_and_definition(manager, tmp_path) -> None:
    client, adapter, registry = await adapter_for(manager, tmp_path)
    spec = registry.spec("mcp__t__echo")
    assert spec.source == MCP_SOURCE
    assert spec.server_id == "t"
    assert spec.remote_name == "echo"
    assert spec.definition_digest
    assert spec.definition_digest == adapter.digest_for("mcp__t__echo")
    # The description carries the provenance the model needs to cite it.
    assert "t" in spec.description and "echo" in spec.description


@pytest.mark.asyncio
async def test_name_conflict_with_an_existing_tool_is_rejected_not_overwritten(
    manager, tmp_path,
) -> None:
    registry = create_default_registry(tmp_path / "workspace")
    registry.register(_stub_spec("mcp__t__echo"))
    client, adapter, _ = await adapter_for(manager, tmp_path, registry=registry)
    assert "mcp__t__echo" not in adapter.model_names()
    assert "already in use" in adapter.rejected_tools["echo"]


def _stub_spec(name: str):
    from app.tools import ToolSpec

    return ToolSpec(
        name, "native stub", {"type": "object", "properties": {}}, ToolRisk.PURE,
        lambda params: ToolResult(True, "ok"),
    )


# ----------------------------------------------------------------- validation


def test_object_rooted_schema_is_required() -> None:
    from app.mcp_tools import _check_schema

    with pytest.raises(McpToolRejected, match="object-rooted"):
        _check_schema({"type": "array", "items": {"type": "string"}})
    _check_schema({"type": "object", "properties": {}})
    _check_schema({"properties": {}})  # an omitted type is tolerated


def test_invalid_schema_is_rejected() -> None:
    from app.mcp_tools import _check_schema

    with pytest.raises(McpToolRejected, match="not valid"):
        _check_schema({"type": "object", "properties": {"a": {"type": "nonsense"}}})


def test_defs_and_ref_semantics_are_preserved() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "$defs": {"count": {"type": "integer", "minimum": 2}},
        "properties": {"n": {"$ref": "#/$defs/count"}},
        "required": ["n"],
    }
    validate_arguments(schema, {"n": 3})
    with pytest.raises(McpToolRejected):
        validate_arguments(schema, {"n": 1})
    with pytest.raises(McpToolRejected):
        validate_arguments(schema, {"n": "3"})


def test_unresolvable_external_ref_fails_instead_of_fetching() -> None:
    schema = {
        "type": "object",
        "properties": {"x": {"$ref": "https://unreachable.invalid/schema.json"}},
    }
    # No network access is attempted; the reference simply cannot be resolved.
    with pytest.raises(McpToolRejected, match="could not be validated"):
        validate_arguments(schema, {"x": 1})


def test_argument_validation_reports_the_failing_location() -> None:
    schema = {
        "type": "object",
        "properties": {"values": {"type": "array", "items": {"type": "integer"}}},
        "required": ["values"],
        "additionalProperties": False,
    }
    with pytest.raises(McpToolRejected, match="values"):
        validate_arguments(schema, {"values": ["x"]})
    with pytest.raises(McpToolRejected):
        validate_arguments(schema, {"values": [1], "extra": 1})
    with pytest.raises(McpToolRejected):
        validate_arguments(schema, {})


# ---------------------------------------------------------------- result shape


class _Block:
    def __init__(self, type_: str, **rest):
        self.type = type_
        for key, value in rest.items():
            setattr(self, key, value)


class _Result:
    def __init__(self, content=(), structured=None, is_error=False, meta=None):
        self.content = list(content)
        self.structuredContent = structured
        self.isError = is_error
        self.meta = meta


def test_text_result_is_mapped_to_the_harness_result() -> None:
    mapped = map_call_result(
        _Result(content=[_Block("text", text="hello")]), server_id="t", remote_name="echo",
    )
    assert mapped.ok is True
    assert mapped.data["text"] == "hello"
    assert mapped.meta["source"] == MCP_SOURCE


def test_structured_content_is_carried_through() -> None:
    mapped = map_call_result(
        _Result(content=[_Block("text", text="9")], structured={"total": 9}),
        server_id="t", remote_name="sum_numbers",
    )
    assert mapped.ok is True
    assert mapped.data["structured"] == {"total": 9}


def test_tool_level_error_is_not_a_success() -> None:
    mapped = map_call_result(
        _Result(content=[_Block("text", text="refused")], is_error=True),
        server_id="t", remote_name="broken",
    )
    assert mapped.ok is False
    assert mapped.error == "MCP_TOOL_ERROR"
    assert "refused" in mapped.summary


def test_unsupported_content_blocks_fail_loudly_and_keep_the_supported_part() -> None:
    mapped = map_call_result(
        _Result(content=[_Block("text", text="caption"), _Block("image", data="x", mimeType="image/png")]),
        server_id="t", remote_name="picture",
    )
    assert mapped.ok is False
    assert mapped.error == "MCP_UNSUPPORTED_CONTENT"
    assert mapped.data["unsupported_content_types"] == ["image"]
    # Nothing was silently dropped and nothing was claimed as success.
    assert mapped.data["text"] == "caption"


def test_input_required_is_reported_as_unsupported_not_answered() -> None:
    mapped = map_call_result(
        _Result(meta={"resultType": "input_required", "requestState": "opaque"}),
        server_id="t", remote_name="ask_me",
    )
    assert mapped.ok is False
    assert mapped.error == "MCP_INPUT_REQUIRED_UNSUPPORTED"
    assert mapped.data["result_type"] == "input_required"
    assert "requestState" not in mapped.data.get("raw_meta", {})


# ------------------------------------------------------------------ execution


@pytest.mark.asyncio
async def test_read_call_flows_through_the_registry(manager, tmp_path) -> None:
    client, adapter, registry = await adapter_for(manager, tmp_path)
    call = ToolCall(id="call-1", name="mcp__t__echo", params={"text": "hi"})
    result = await registry.execute_async(
        call, context=context(), skill_tools=adapter.model_names(),
    )
    assert result.ok is True
    assert result.data["text"] == "echo:hi"
    assert result.meta["server_id"] == "t"


@pytest.mark.asyncio
async def test_invalid_arguments_are_rejected_before_execution(manager, tmp_path) -> None:
    client, adapter, registry = await adapter_for(manager, tmp_path)
    call = ToolCall(id="call-1", name="mcp__t__sum_numbers", params={"values": ["x"]})
    with pytest.raises(ToolRejected):
        await registry.execute_async(call, context=context(), skill_tools=adapter.model_names())


@pytest.mark.asyncio
async def test_unknown_tool_is_rejected(manager, tmp_path) -> None:
    client, adapter, registry = await adapter_for(manager, tmp_path)
    call = ToolCall(id="call-1", name="mcp__t__nope", params={})
    with pytest.raises(ToolRejected, match="unknown tool"):
        await registry.execute_async(call, context=context(), skill_tools=adapter.model_names())


@pytest.mark.asyncio
async def test_skill_allowance_that_excludes_the_tool_denies_it(manager, tmp_path) -> None:
    client, adapter, registry = await adapter_for(manager, tmp_path)
    call = ToolCall(id="call-1", name="mcp__t__echo", params={"text": "hi"})
    with pytest.raises(ToolRejected, match="skill does not allow tool"):
        await registry.execute_async(call, context=context(), skill_tools={"local_time"})


@pytest.mark.asyncio
async def test_synchronous_execution_path_refuses_mcp_tools(manager, tmp_path) -> None:
    client, adapter, registry = await adapter_for(manager, tmp_path)
    result = await registry.execute_async(
        ToolCall(id="call-1", name="mcp__t__echo", params={"text": "hi"}),
        context=context(), skill_tools=adapter.model_names(),
    )
    assert result.ok is True
    # The synchronous handler is a deliberate refusal, not a silent no-op.
    sync_result = registry.spec("mcp__t__echo").handler({"text": "hi"})
    assert sync_result.ok is False
    assert sync_result.error == "MCP_ASYNC_ONLY"


@pytest.mark.asyncio
async def test_remote_timeout_on_a_read_is_a_failed_result(manager, tmp_path) -> None:
    config = server_config(timeout_seconds=0.6, enabled_tools=["slow"])
    client, adapter, registry = await adapter_for(manager, tmp_path, config=config)
    result = await registry.execute_async(
        ToolCall(id="call-1", name="mcp__t__slow", params={"seconds": 5}),
        context=context(), skill_tools=adapter.model_names(),
    )
    assert result.ok is False
    assert result.error in {"MCP_TIMEOUT", "MCP_DISCONNECTED"}


@pytest.mark.asyncio
async def test_remote_write_timeout_requires_reconciliation(manager, tmp_path) -> None:
    from app.db import Database
    from app.domain import ApprovalService

    config = server_config(
        timeout_seconds=0.6, allow_write_tools=True,
        risk={"slow": "WRITE"}, enabled_tools=["slow"],
    )
    db = Database(tmp_path / "agent.db", workspace=tmp_path / "workspace")
    registry = ToolRegistry(tmp_path / "workspace", db=db, approval_service=ApprovalService(db))
    client, adapter, _ = await adapter_for(manager, tmp_path, config=config, registry=registry)
    name = "mcp__t__slow"
    params = {"seconds": 5}
    grant(registry, call_id="call-1", name=name, params=params)
    with pytest.raises(ToolReconciliationRequired):
        await registry.execute_async(
            ToolCall(id="call-1", name=name, params=params),
            context=context(), skill_tools=adapter.model_names(),
        )


@pytest.mark.asyncio
async def test_large_result_is_capped_and_saved_as_an_artifact(manager, tmp_path) -> None:
    config = server_config(max_result_bytes=2048, enabled_tools=["big_result"])
    client, adapter, registry = await adapter_for(manager, tmp_path, config=config)
    result = await registry.execute_async(
        ToolCall(id="call-1", name="mcp__t__big_result", params={"kilobytes": 32}),
        context=context(), skill_tools=adapter.model_names(),
    )
    assert result.ok is True
    assert result.meta["truncated"] is True
    assert result.meta["original_bytes"] > 2048
    assert result.artifact_ref
    stored = (registry.workspace / result.artifact_ref)
    assert stored.is_file()
    assert len(json.loads(stored.read_text(encoding="utf-8"))["data"]["text"]) == 32 * 1024
    assert len(result.data["excerpt"]["text"]) == 2000


@pytest.mark.asyncio
async def test_small_result_is_not_capped(manager, tmp_path) -> None:
    client, adapter, registry = await adapter_for(manager, tmp_path)
    result = await registry.execute_async(
        ToolCall(id="call-1", name="mcp__t__echo", params={"text": "small"}),
        context=context(), skill_tools=adapter.model_names(),
    )
    assert result.artifact_ref is None
    assert "truncated" not in result.meta


# --------------------------------------------------------- definition changes


@pytest.mark.asyncio
async def test_a_changed_definition_is_refused_instead_of_executed(manager, tmp_path) -> None:
    client, adapter, registry = await adapter_for(manager, tmp_path)
    before = adapter.digest_for("mcp__t__echo")
    # The remote definition changes under the same name.
    await client.call_tool("t", "flip_description", {})
    result = await registry.execute_async(
        ToolCall(id="call-1", name="mcp__t__echo", params={"text": "hi"}),
        context=context(), skill_tools=adapter.model_names(),
    )
    assert result.ok is False
    assert result.error == "MCP_DEFINITION_CHANGED"
    assert adapter.definition_changes >= 1
    assert adapter.digest_for("mcp__t__echo") != before


@pytest.mark.asyncio
async def test_a_removed_definition_is_refused(manager, tmp_path) -> None:
    client, adapter, registry = await adapter_for(manager, tmp_path)
    await client.call_tool("t", "drop_tool", {"name": "echo"})
    result = await registry.execute_async(
        ToolCall(id="call-1", name="mcp__t__echo", params={"text": "hi"}),
        context=context(), skill_tools=adapter.model_names(),
    )
    assert result.ok is False
    assert result.error == "MCP_DEFINITION_CHANGED"
    assert "已不存在" in result.summary


# ------------------------------------------------------------------ approval


@pytest.mark.asyncio
async def test_approval_binding_pins_configuration_and_definition(manager, tmp_path) -> None:
    config = server_config(allow_write_tools=True, default_risk="WRITE")
    client, adapter, registry = await adapter_for(manager, tmp_path, config=config)
    binding = adapter.approval_binding("mcp__t__echo")
    assert binding["mcp"]["server_id"] == "t"
    assert binding["mcp"]["config_version"] == config.config_version
    assert binding["mcp"]["remote_name"] == "echo"
    assert binding["mcp"]["definition_digest"] == adapter.digest_for("mcp__t__echo")
    assert binding["mcp"]["protocol_version"]


@pytest.mark.asyncio
async def test_registry_sync_exposes_bindings_and_enabled_names(manager, tmp_path) -> None:
    config = server_config(allow_write_tools=True, default_risk="WRITE")
    registry = create_default_registry(tmp_path / "workspace")
    client = manager([config])
    sync = McpToolRegistrySync(client, registry, [config])
    report = await sync.sync_all("local-user")
    assert report["t"]["registered"] >= 1
    assert "mcp__t__echo" in sync.enabled_model_names()
    assert sync.approval_binding_for("mcp__t__echo")["mcp"]["server_id"] == "t"
    assert sync.approval_binding_for("not_an_mcp_tool") == {}
    assert sync.adapter_for_tool("mcp__t__echo") is not None
    await sync.close()


@pytest.mark.asyncio
async def test_an_unavailable_server_registers_nothing_and_reports_it(manager, tmp_path) -> None:
    config = server_config_from_dict({
        "server_id": "ghost", "transport": "stdio", "command": sys.executable,
        "args": ["-c", "import sys; sys.exit(3)"],
    })
    registry = create_default_registry(tmp_path / "workspace")
    client = manager([config])
    sync = McpToolRegistrySync(client, registry, [config])
    report = await sync.sync_all("local-user")
    assert report["ghost"]["registered"] == 0
    assert sync.enabled_model_names() == set()
    assert sync.failures["ghost"]
    await sync.close()


# ------------------------------------------------------------------ recovery


@pytest.mark.asyncio
async def test_declared_receipt_tool_recovers_an_ambiguous_write(manager, tmp_path) -> None:
    from app.db import Database
    from app.domain import ApprovalService

    state_file = tmp_path / "state.json"
    config = server_config(
        allow_write_tools=True, default_risk="WRITE",
        enabled_tools=["write_then_disconnect", "read_marker"],
        env={"MCP_TEST_TOOLSET": "write", "MCP_TEST_STATE_FILE": str(state_file)},
        receipt_tools={"write_then_disconnect": {"tool": "read_marker", "arguments": {}}},
    )
    db = Database(tmp_path / "agent.db", workspace=tmp_path / "workspace")
    registry = ToolRegistry(tmp_path / "workspace", db=db, approval_service=ApprovalService(db))
    client, adapter, _ = await adapter_for(manager, tmp_path, config=config, registry=registry)
    name = "mcp__t__write_then_disconnect"
    params = {"value": "landed"}
    grant(registry, call_id="call-1", name=name, params=params)
    # The first attempt lands remotely and then the connection drops.
    with pytest.raises(ToolReconciliationRequired):
        await registry.execute_async(
            ToolCall(id="call-1", name=name, params=params),
            context=context(), skill_tools=adapter.model_names(), authorization=None,
        )
    assert json.loads(state_file.read_text(encoding="utf-8"))["marker"] == "landed"
    # The retry reads the receipt instead of assuming the write did not land.
    recovered = await registry.execute_async(
        ToolCall(id="call-1", name=name, params=params),
        context=context(), skill_tools=adapter.model_names(), authorization=None,
    )
    assert recovered.ok is True
    assert recovered.meta["reconciled_by"] == "read_marker"
    assert "landed" in json.dumps(recovered.data["receipt"])


@pytest.mark.asyncio
async def test_without_a_declared_receipt_an_ambiguous_write_stays_unresolved(
    manager, tmp_path,
) -> None:
    from app.db import Database
    from app.domain import ApprovalService

    state_file = tmp_path / "state.json"
    config = server_config(
        allow_write_tools=True, default_risk="WRITE",
        enabled_tools=["write_then_disconnect"],
        env={"MCP_TEST_TOOLSET": "write", "MCP_TEST_STATE_FILE": str(state_file)},
    )
    db = Database(tmp_path / "agent.db", workspace=tmp_path / "workspace")
    registry = ToolRegistry(tmp_path / "workspace", db=db, approval_service=ApprovalService(db))
    client, adapter, _ = await adapter_for(manager, tmp_path, config=config, registry=registry)
    name = "mcp__t__write_then_disconnect"
    params = {"value": "x"}
    grant(registry, call_id="call-1", name=name, params=params)
    with pytest.raises(ToolReconciliationRequired):
        await registry.execute_async(
            ToolCall(id="call-1", name=name, params=params),
            context=context(), skill_tools=adapter.model_names(), authorization=None,
        )
    # No receipt is declared, so the Harness refuses to guess.
    with pytest.raises(ToolReconciliationRequired):
        await registry.execute_async(
            ToolCall(id="call-1", name=name, params=params),
            context=context(), skill_tools=adapter.model_names(), authorization=None,
        )
