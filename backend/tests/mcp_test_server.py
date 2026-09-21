"""A real MCP server over stdio, driven by environment variables.

It is a genuine server speaking the real protocol through the official SDK - it
exists so the tests can exercise catalogue caching, pagination, definition
changes, unsupported content, large results, timeouts, disconnects and remote
writes deterministically. It is not a mock of the transport: the client really
spawns this process, completes the real handshake and exchanges real messages.

Environment switches (all optional):

``MCP_TEST_TOOLSET``      ``basic`` (default) | ``write`` | ``paged`` | ``empty``
``MCP_TEST_TTL_MS``       cache hint placed in the ``tools/list`` result ``_meta``
``MCP_TEST_DESCRIPTION``  description text of the ``echo`` tool
``MCP_TEST_STATE_FILE``   file backing the write/receipt and crash scenarios
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

STATE_FILE = Path(os.getenv("MCP_TEST_STATE_FILE", "")).resolve() if os.getenv("MCP_TEST_STATE_FILE") else None
PAGE_SIZE = int(os.getenv("MCP_TEST_PAGE_SIZE", "2"))


def _tool(
    name: str,
    description: str,
    schema: dict[str, Any],
    *,
    read_only: bool = True,
) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        inputSchema=schema,
        annotations=types.ToolAnnotations(readOnlyHint=read_only),
    )


def _apply_description_overrides(tools: list[types.Tool]) -> list[types.Tool]:
    """Let a test change one named tool's definition between discoveries."""
    echo_override = os.getenv("MCP_TEST_DESCRIPTION")
    for tool in tools:
        override = os.getenv(f"MCP_TEST_DESC_{tool.name.upper()}")
        if override:
            tool.description = override
        elif echo_override and tool.name == "echo":
            tool.description = echo_override
    return tools


def _basic_tools() -> list[types.Tool]:
    return [
        _tool(
            "echo",
            os.getenv("MCP_TEST_DESCRIPTION", "Echo the supplied text back."),
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "sum_numbers",
            "Sum a list of integers and return a structured result.",
            {
                "type": "object",
                "properties": {"values": {"type": "array", "items": {"type": "integer"}}},
                "required": ["values"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "big_result",
            "Return a payload of approximately the requested size in kilobytes.",
            {
                "type": "object",
                "properties": {"kilobytes": {"type": "integer", "minimum": 1, "maximum": 512}},
                "required": ["kilobytes"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "picture",
            "Return an image content block, which this Harness cannot carry.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _tool(
            "broken",
            "Always report a tool-level error.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _tool(
            "slow",
            "Sleep for the requested number of seconds before answering.",
            {
                "type": "object",
                "properties": {"seconds": {"type": "number", "minimum": 0, "maximum": 600}},
                "required": ["seconds"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "flip_description",
            "Change this server's own tool definitions and notify the client.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _tool(
            "drop_tool",
            "Stop advertising one tool, then notify the client.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "read_repo_file",
            "Read a file from one of the repositories this server can see.",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["repo", "path"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "change_definition",
            "Change one named tool's definition, then notify the client.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
    ]


def _write_tools() -> list[types.Tool]:
    return [
        _tool(
            "write_marker",
            "Persist a value on the server, then answer with a receipt id.",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            read_only=False,
        ),
        _tool(
            "read_marker",
            "Read the value most recently persisted on the server.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _tool(
            "write_then_disconnect",
            "Persist a value, then drop the connection without answering.",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            read_only=False,
        ),
    ]


def _paged_tools() -> list[types.Tool]:
    return [
        _tool(
            f"page_tool_{index:02d}",
            f"Read-only tool number {index} used to exercise catalogue pagination.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        )
        for index in range(PAGE_SIZE * 2 + 1)
    ]


def _tools() -> list[types.Tool]:
    toolset = os.getenv("MCP_TEST_TOOLSET", "basic")
    hidden = {name for name in os.getenv("MCP_TEST_HIDDEN", "").split(",") if name}
    if toolset == "empty":
        return []
    if toolset == "paged":
        return _apply_description_overrides([tool for tool in _paged_tools() if tool.name not in hidden])
    if toolset == "write":
        available = _write_tools() + [
            tool for tool in _basic_tools() if tool.name in {"echo", "change_definition"}
        ]
    else:
        available = _basic_tools()
    return _apply_description_overrides([tool for tool in available if tool.name not in hidden])


def _write_state(key: str, value: str) -> None:
    if STATE_FILE is None:
        # Without a state file the write has no observable effect, but the call
        # still succeeds so the protocol shape under test is unchanged.
        return
    payload: dict[str, Any] = {}
    if STATE_FILE.exists():
        try:
            payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
    payload[key] = value
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")


def _read_state(key: str) -> str | None:
    if STATE_FILE is None or not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get(key)
    except json.JSONDecodeError:
        return None


server = Server("better-mcp-test-server")


@server.list_tools()
async def list_tools(request: types.ListToolsRequest) -> types.ListToolsResult:
    tools = _tools()
    meta: dict[str, Any] = {}
    ttl = os.getenv("MCP_TEST_TTL_MS")
    if ttl is not None:
        meta["ttlMs"] = int(ttl)
        meta["cacheScope"] = os.getenv("MCP_TEST_CACHE_SCOPE", "private")
    # ``Result.meta`` carries the alias ``_meta`` and pydantic is not configured
    # with populate_by_name, so the alias key is the only one that fills the
    # field. Passing ``meta=`` would land in ``model_extra`` and never reach the
    # wire.
    extra = {"_meta": meta} if meta else {}
    if os.getenv("MCP_TEST_TOOLSET", "basic") != "paged":
        return types.ListToolsResult(tools=tools, **extra)
    cursor = request.params.cursor if request.params is not None else None
    start = 0 if cursor is None else int(cursor)
    page = tools[start:start + PAGE_SIZE]
    next_start = start + PAGE_SIZE
    next_cursor = str(next_start) if next_start < len(tools) else None
    return types.ListToolsResult(tools=page, nextCursor=next_cursor, **extra)


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
    if name == "echo":
        return [types.TextContent(type="text", text=f"echo:{arguments.get('text')}")]
    if name == "sum_numbers":
        total = sum(int(value) for value in arguments.get("values", []))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(total))],
            structuredContent={"total": total, "count": len(arguments.get("values", []))},
        )
    if name == "big_result":
        kilobytes = int(arguments.get("kilobytes", 64))
        return [types.TextContent(type="text", text="x" * (kilobytes * 1024))]
    if name == "picture":
        return [types.ImageContent(type="image", data="aGVsbG8=", mimeType="image/png")]
    if name == "broken":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="remote tool refused the request")],
            isError=True,
        )
    if name == "slow":
        await asyncio.sleep(float(arguments.get("seconds", 1)))
        return [types.TextContent(type="text", text="slept")]
    if name in {"flip_description", "change_definition"}:
        target = "echo" if name == "flip_description" else str(arguments.get("name", ""))
        if not target:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="a tool name is required")],
                isError=True,
            )
        variable = f"MCP_TEST_DESC_{target.upper()}"
        current = os.getenv(variable) or next(
            (tool.description for tool in _tools() if tool.name == target), target
        )
        os.environ[variable] = (current or "") + " (changed)"
        if target == "echo":
            os.environ["MCP_TEST_DESCRIPTION"] = os.environ[variable]
        await server.request_context.session.send_tool_list_changed()
        return [types.TextContent(type="text", text=f"definitions changed: {target}")]
    if name == "read_repo_file":
        return [types.TextContent(
            type="text",
            text=f"{arguments.get('repo')}:{arguments.get('path')}",
        )]
    if name == "drop_tool":
        dropped = str(arguments.get("name", ""))
        hidden = {entry for entry in os.getenv("MCP_TEST_HIDDEN", "").split(",") if entry}
        hidden.add(dropped)
        os.environ["MCP_TEST_HIDDEN"] = ",".join(sorted(hidden))
        await server.request_context.session.send_tool_list_changed()
        return [types.TextContent(type="text", text=f"dropped:{dropped}")]
    if name == "write_marker":
        value = str(arguments.get("value", ""))
        _write_state("marker", value)
        return [types.TextContent(type="text", text=json.dumps({"receipt": f"receipt:{value}"}))]
    if name == "read_marker":
        return [types.TextContent(type="text", text=json.dumps({"marker": _read_state("marker")}))]
    if name == "write_then_disconnect":
        _write_state("marker", str(arguments.get("value", "")))
        # The effect landed remotely and no answer was sent. The client must not
        # be able to claim either success or failure.
        os._exit(17)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"unknown tool: {name}")],
        isError=True,
    )


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
