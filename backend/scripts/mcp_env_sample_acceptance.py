"""Verify the copy-pasteable BETTER_AGENT_MCP_SERVERS example in .env.example.

The example line is extracted verbatim from the file, parsed, and driven end to
end against the real reference servers. Only the interpreter path is rewritten,
because `python` on PATH is not necessarily the one holding the SDK.

This is the "does the documented configuration actually work" gate: it proves
discovery, registration, a real read call, and that the declared resource scope
refuses a value the model was never authorised to use.

Run:  D:/pycharm/python.exe backend/scripts/mcp_env_sample_acceptance.py
Exit: 0 ok, 1 tools missing, 2 a call failed, 3 the scope guard did not refuse.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mcp_client import load_manager_from_env  # noqa: E402
from app.mcp_tools import McpToolRegistrySync, model_tool_name  # noqa: E402
from app.tools import (  # noqa: E402
    ToolCall,
    ToolExecutionContext,
    ToolRejected,
    ToolRegistry,
)

ENV_EXAMPLE = Path(__file__).resolve().parent.parent.parent / ".env.example"


def documented_example() -> list[dict]:
    """The first BETTER_AGENT_MCP_SERVERS line in .env.example, as written.

    The example is shipped commented out, so a leading ``#`` is stripped before
    parsing: what gets validated is the exact text an operator would paste.
    """
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        candidate = line.lstrip("# ").strip() if line.lstrip().startswith("#") else line
        if candidate.startswith("BETTER_AGENT_MCP_SERVERS="):
            payload = candidate.split("=", 1)[1]
            if payload.startswith("["):
                return json.loads(payload)
    raise SystemExit("no BETTER_AGENT_MCP_SERVERS example found in .env.example")


async def main() -> int:
    sample = documented_example()
    print(f"documented example: {len(sample)} servers -> {[s['server_id'] for s in sample]}")
    for server in sample:
        # Only the interpreter path is environment-specific.
        if server.get("transport", "stdio") == "stdio":
            server["command"] = sys.executable

    os.environ["BETTER_AGENT_MCP_SERVERS"] = json.dumps(sample, ensure_ascii=False)
    manager = load_manager_from_env()
    print("parsed:", manager.server_ids, "rejections:", manager.config_rejections)

    with tempfile.TemporaryDirectory() as tmp:
        registry = ToolRegistry(Path(tmp) / "workspace")
        sync = McpToolRegistrySync(manager, registry, manager.configs())
        report = await sync.sync_all("local-user")
        for server_id, entry in report.items():
            print(f"  {server_id}: registered={entry['registered']} "
                  f"whitelisted={entry['whitelisted']} rejected={entry['rejected']} "
                  f"protocol={entry['protocol_version']}")

        names = sorted(spec.name for spec in registry.specs())
        print("registered tools:", names)

        expected = {
            model_tool_name("time", "get_current_time"),
            model_tool_name("time", "convert_time"),
            model_tool_name("fetch", "fetch"),
        }
        missing = expected - set(names)
        if missing:
            print("MISSING:", sorted(missing))
            await sync.close()
            return 1

        # A real call through the registry, the way the chat loop makes one.
        context = ToolExecutionContext(
            owner_id="local-user", run_id="verify", tool_call_id="verify-1",
        )
        call = ToolCall(
            id="verify-1",
            name=model_tool_name("time", "get_current_time"),
            params={"timezone": "Asia/Shanghai"},
        )
        result = await registry.execute_async(call, context=context, skill_tools=None)
        print("time call ok:", result.ok, "|", result.summary.replace("\n", " ")[:160])

        # The declared resource scope must refuse a URL outside it. The
        # registry signals a denied authorization by raising, exactly as it
        # does for a native tool; the chat runner turns that into a failed row.
        blocked = ToolCall(
            id="verify-2",
            name=model_tool_name("fetch", "fetch"),
            params={"url": "https://evil.example/steal"},
        )
        blocked_context = ToolExecutionContext(
            owner_id="local-user", run_id="verify", tool_call_id="verify-2",
        )
        try:
            await registry.execute_async(blocked, context=blocked_context, skill_tools=None)
        except ToolRejected as exc:
            print("out-of-scope fetch refused:", exc)
        else:
            print("out-of-scope fetch was NOT refused")
            await sync.close()
            return 3

        # ...and allow the one that was authorised.
        allowed = ToolCall(
            id="verify-3",
            name=model_tool_name("fetch", "fetch"),
            params={"url": "https://example.com/"},
        )
        allowed_context = ToolExecutionContext(
            owner_id="local-user", run_id="verify", tool_call_id="verify-3",
        )
        allowed_result = await registry.execute_async(
            allowed, context=allowed_context, skill_tools=None,
        )
        print("in-scope fetch ok:", allowed_result.ok,
              "|", allowed_result.summary.replace("\n", " ")[:160])

        await sync.close()
        if not result.ok or not allowed_result.ok:
            return 2

    print("VERIFY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
