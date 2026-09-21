"""Real provider + real stdio MCP through the production conversation worker.

Uses an isolated SQLite runtime, not the running HTTP/PostgreSQL deployment.
No model responses or tool calls are scripted. Run explicitly: this costs tokens.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import load_env_file, load_user_model_environment, load_model_profile_from_environment, resolve_credential
from app.db import Database
from app.domain import ApprovalService, CheckpointStore, PlanVersionService
from app.events import EventStore
from app.live_model import LiveConversationModel, LiveRuntimeModel
from app.mcp_client import McpClientManager, server_config_from_dict
from app.mcp_tools import McpToolRegistrySync
from app.memory import MemoryService
from app.model_gateway import ModelGateway
from app.runtime import AgentRuntime
from app.tools import create_default_registry


class ObservedGateway(ModelGateway):
    """Record observations while forwarding unchanged to the real gateway."""

    def __init__(self, profile):
        super().__init__(profile)
        self.calls = []

    async def complete(self, request, **kwargs):
        entry = {
            "purpose": request.purpose,
            "offered_tools": [t["function"]["name"] for t in request.tools or []],
            "tool_observations": [
                {"tool_call_id": m.get("tool_call_id"),
                 "content_sha256": hashlib.sha256(str(m.get("content", "")).encode()).hexdigest()}
                for m in request.messages if m.get("role") == "tool"
            ],
        }
        self.calls.append(entry)
        response = await super().complete(request, **kwargs)
        entry.update(tool_calls=response.tool_calls, usage=asdict(response.usage),
                     actual_input_tokens=response.usage.input_tokens,
                     attempts=response.attempts, finish_reason=response.finish_reason)
        return response


async def run(out: Path) -> dict:
    load_env_file(ROOT / ".env")
    load_user_model_environment()
    profile = load_model_profile_from_environment()
    if profile is None or not resolve_credential(profile.api_key_env):
        raise RuntimeError("No model credential available through the application config loader")
    out.mkdir(parents=True, exist_ok=False)
    db = Database(out / "runtime" / "agent.db", workspace=out / "runtime" / "workspace")
    events = EventStore(db)
    approvals = ApprovalService(db)
    gateway = ObservedGateway(profile)
    runtime = AgentRuntime(
        db=db, events=events, plans=PlanVersionService(db), approvals=approvals,
        checkpoints=CheckpointStore(db), memory=MemoryService(db, events, out / "runtime" / "memory"),
        tools=create_default_registry(out / "runtime" / "workspace", db=db, approval_service=approvals),
        model=LiveRuntimeModel(gateway), conversation_model=LiveConversationModel(gateway),
    )
    configs = [server_config_from_dict({
        "server_id": "time", "transport": "stdio", "command": sys.executable,
        "args": ["-m", "mcp_server_time", "--local-timezone", "Asia/Shanghai"],
        "inherit_env": False, "enabled_tools": ["get_current_time", "convert_time"],
        "risk": {"get_current_time": "READ", "convert_time": "READ"}, "timeout_seconds": 30,
    }), server_config_from_dict({
        "server_id": "check", "transport": "stdio", "command": sys.executable,
        "args": [str(ROOT / "backend/tests/mcp_test_server.py")], "inherit_env": False,
        "enabled_tools": ["echo", "sum_numbers"],
        "risk": {"echo": "READ", "sum_numbers": "READ"}, "timeout_seconds": 30,
    })]
    manager = McpClientManager(configs)
    runtime.mcp_manager = manager
    runtime.mcp_sync = McpToolRegistrySync(manager, runtime.tools, configs)
    mcp_events = []
    manager.event_sink = lambda kind, data: mcp_events.append({"type": kind, "data": data})
    report = {
        "status": "running", "mode": "real provider + real stdio MCP; isolated SQLite production worker",
        "model": profile.model, "provider_endpoint": profile.base_url,
        "sdk_version": importlib.metadata.version("mcp"), "rounds": [], "errors": [],
    }
    marker = "mcp-live-" + uuid.uuid4().hex[:16]
    scenarios = [
        ("请通过 time 服务实际查询 Asia/Shanghai 的当前时间，回答具体时间和时区。不要凭记忆猜测。", "mcp__time__get_current_time", None),
        ("请通过 time 服务将上海上午09:30换算到 America/New_York，调用 convert_time 后依据结果回答。", "mcp__time__convert_time", None),
        ("请调用 check 服务的 sum_numbers 计算 [173, 281, 547]，必须根据远端工具结果回答总和。", "mcp__check__sum_numbers", "1001"),
        (f"请调用 check 服务的 echo 回显这段文本：{marker}。随后完整输出远端返回的文本。", "mcp__check__echo", marker),
        ("请再次通过 time 服务查询 UTC 当前时间，并明确这是新查询的结果。", "mcp__time__get_current_time", "UTC"),
    ]
    try:
        report["discovery"] = await runtime.mcp_sync.sync_all("local-user")
        expected = {name for _, name, _ in scenarios}
        if not expected <= runtime.mcp_sync.enabled_model_names():
            raise RuntimeError("Required MCP tools were not registered")
        thread = runtime.conversation.create_thread("真实模型 MCP 验收")
        report["thread_id"] = thread.id
        for index, (prompt, expected_tool, expected_text) in enumerate(scenarios, 1):
            start = len(gateway.calls)
            turn = runtime.conversation.accept_turn(thread.id, uuid.uuid4().hex, prompt, [])
            await asyncio.wait_for(runtime.turn_worker.run_once(), timeout=180)
            status = runtime.conversation.turn(turn.turn_id).status
            messages = runtime.conversation.messages(thread.id)
            answer = next((m.content for m in reversed(messages)
                           if m.turn_id == turn.turn_id and m.role == "assistant"), "")
            with db.connection() as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT id,tool_name,status,result_json FROM turn_tool_calls WHERE turn_id=? ORDER BY created_at",
                    (turn.turn_id,),
                ).fetchall()]
            for row in rows:
                row["result"] = json.loads(row.pop("result_json") or "null")
            calls = gateway.calls[start:]
            observations = {m["tool_call_id"] for call in calls for m in call["tool_observations"]}
            model_selected = {t["id"] for call in calls for t in call.get("tool_calls", [])
                              if t.get("function", {}).get("name") == expected_tool}
            successful = [r for r in rows if r["tool_name"] == expected_tool and r["status"] == "EXECUTED"
                          and (r["result"] or {}).get("ok")]
            # The Harness rewrites both sides of the exchange to its durable call ID.
            checks = {
                "completed_answer": status == "COMPLETED" and bool(answer.strip()),
                "real_model_selected_expected_tool": bool(model_selected),
                "remote_execution_succeeded": bool(successful),
                "tool_result_returned_to_model": any(r["id"] in observations for r in successful),
                "provider_usage_recorded": bool(calls) and all(c.get("actual_input_tokens") is not None for c in calls),
                "expected_answer_content": expected_text is None or expected_text in answer,
            }
            entry = {"index": index, "turn_id": turn.turn_id, "prompt": prompt,
                     "status": status, "answer": answer, "tool_records": rows, "model_calls": calls, "checks": checks}
            report["rounds"].append(entry)
            if not all(checks.values()):
                report["errors"].append({"round": index, "failed_checks": [k for k, v in checks.items() if not v]})
            (out / "progress.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"round {index}: {status}, tools={len(rows)}, checks={checks}", flush=True)
            if status != "COMPLETED":
                break
        if len(report["rounds"]) != len(scenarios):
            report["errors"].append("Not all planned rounds completed")
    except Exception as exc:
        # Avoid emitting provider headers, credentials, or arbitrary exception bodies.
        report["errors"].append({"exception_type": type(exc).__name__})
    finally:
        report["mcp_metrics"] = manager.metrics.as_dict()
        report["protocol_versions"] = {s: manager.protocol_version(s) for s in manager.server_ids}
        report["mcp_events"] = mcp_events
        with db.connection() as c:
            report["thread_events"] = [dict(r) for r in c.execute(
                "SELECT type,turn_id,data_json FROM thread_events WHERE thread_id=? ORDER BY seq",
                (report.get("thread_id", ""),),
            ).fetchall()]
        await runtime.mcp_sync.close()
        db.close()
        report["status"] = "passed" if not report["errors"] else "failed"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.out.resolve()))
    print(json.dumps({"status": result["status"], "rounds": len(result["rounds"]), "errors": result["errors"]}))
    raise SystemExit(0 if result["status"] == "passed" else 1)
