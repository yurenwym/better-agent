"""D2+D3 joint real-model end-to-end for plain-chat business tools.

Unlike ``followup_intent_eval.py`` this harness executes the production worker,
the production tool registry and the production approval chain against a real
model. Each scenario drives a disposable database; every model call and every
user decision is recorded to the output directory. Approval decisions are made
by this harness (test装置), never by the model.

Scenarios cover: guide delivery with no execution goal, saved-document commit,
execution preview -> confirmation -> activation, rejected write, cancelled
pending write, and restart recovery of an approved write. No provider key is
written to the artifacts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "scripts"))

from followup_intent_eval import RecordedGateway, digest, write_json  # noqa: E402

SCENARIOS: tuple[dict, ...] = (
    {
        "id": "travel_guide_delivery",
        "kind": "delivery",
        "prompt": "帮我做一份杭州三日旅游攻略，住西湖附近，预算两千元。",
    },
    {
        "id": "save_guide",
        "kind": "save",
        "prompt": "帮我做一份桂林三日旅游攻略，并保存到计划页面。",
    },
    {
        "id": "execution_management",
        "kind": "manage",
        "seed_document": True,
        "prompt": (
            "请开启这份攻略的执行管理：先生成执行预览，我确认后再激活。"
            "执行从明天开始，共三天，每天30分钟，时区 Asia/Shanghai。"
        ),
    },
    {
        "id": "modify_plan",
        "kind": "modify",
        "seed_program": True,
        "seed_program_title": "桂林三日攻略",
        "seed_program_markdown": "# 桂林三日攻略\n\n第一天：象鼻山；第二天：漓江；第三天：阳朔。",
        "prompt": "把这份攻略改简单一点：减少一个景点，改完保存到同一份计划文档。",
    },
    {
        "id": "feedback_reject",
        "kind": "feedback_reject",
        "seed_program": True,
        "prompt": "我今天完成了第一项训练，用了30分钟。",
    },
    {
        "id": "feedback_cancel",
        "kind": "feedback_cancel",
        "seed_program": True,
        "prompt": "记录一下，我今天练了20分钟。",
    },
    {
        "id": "feedback_recovery",
        "kind": "feedback_recovery",
        "seed_program": True,
        "prompt": "我今天完成了第一项训练，用了25分钟。",
    },
)

CLAIM_MARKERS = ("已保存", "已开启", "已激活", "已取消", "已延期", "已记录", "已提醒")


def build_runtime(db_path: Path, workspace: Path, gateway, *, real_compiler: bool = True):
    from app.db import Database
    from app.domain import ApprovalService, CheckpointStore, PlanVersionService
    from app.events import EventStore
    from app.goal_program_compiler import GoalProgramCompiler
    from app.goal_tools import register_goal_tools
    from app.live_model import LiveConversationModel
    from app.memory import MemoryService
    from app.runtime import AgentRuntime
    from app.tools import create_default_registry

    db = Database(db_path, workspace=workspace)
    events = EventStore(db)
    approvals = ApprovalService(db)
    model = LiveConversationModel(gateway)
    runtime = AgentRuntime(
        db=db,
        events=events,
        plans=PlanVersionService(db),
        approvals=approvals,
        checkpoints=CheckpointStore(db),
        memory=MemoryService(db, events, workspace / "memory"),
        tools=create_default_registry(workspace, db=db, approval_service=approvals),
        model=model,
        conversation_model=model,
    )
    if real_compiler:
        runtime.goal_programs.compiler = GoalProgramCompiler(gateway)
    register_goal_tools(
        runtime.tools,
        goal_programs=runtime.goal_programs,
        plan_documents=runtime.plan_documents,
    )
    return runtime


def count(runtime, table: str, where: str = "", params: tuple = ()) -> int:
    clause = f" WHERE {where}" if where else ""
    with runtime.db.connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) AS total FROM {table}{clause}", params).fetchone()["total"])


def seed_document(runtime, thread_id: str) -> None:
    runtime.plan_documents.save_model_revision(
        thread_id=thread_id,
        title="桂林三日攻略",
        markdown_content="# 桂林三日攻略\n\n第一天：象鼻山；第二天：漓江；第三天：阳朔。",
        source_turn_id=None,
        source_message_id=None,
        actor="user",
    )


async def seed_program(
    runtime, thread_id: str, gateway, scenario_id: str,
    *, title: str = "四周力量计划", markdown: str = "# 四周力量计划\n\n- 力量训练",
) -> str:
    from app.goal_program_compiler import FixedGoalProgramCompiler, GoalProgramCompiler

    original = runtime.goal_programs.compiler
    runtime.goal_programs.compiler = FixedGoalProgramCompiler()
    try:
        version = runtime.plan_documents.save_model_revision(
            thread_id=thread_id,
            title=title,
            markdown_content=markdown,
            source_turn_id=None,
            source_message_id=None,
            actor="user",
        )
        local_today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        program = await runtime.goal_programs.preview(
            version.plan_document_id,
            start_date=local_today.isoformat(),
            timezone_name="Asia/Shanghai",
            daily_minutes=30,
            requested_end_date=(local_today + timedelta(days=2)).isoformat(),
            idempotency_key=f"e2e-{scenario_id}-seed-preview",
            owner_id="local-user",
        )
        snapshot = runtime.goal_programs.activation_snapshot_hash(program)
        runtime.goal_programs.record_preview_snapshot(
            program["id"], snapshot,
            source_version_id=program["source_plan_document_version_id"],
            source_content_hash=program["source_plan_content_hash"],
            owner_id="local-user",
        )
        runtime.goal_programs.activate(
            program["id"], expected_version=program["version"],
            idempotency_key=f"e2e-{scenario_id}-seed-activate", owner_id="local-user",
            expected_snapshot_hash=snapshot,
        )
        return program["id"]
    finally:
        runtime.goal_programs.compiler = original if original is not None else GoalProgramCompiler(gateway)


def default_answers(ask) -> list[dict]:
    answers = []
    for question in ask.questions:
        if question.options:
            answers.append({
                "question_id": question.id,
                "selected_options": [question.options[0]["label"]],
                "free_text": "",
            })
        else:
            answers.append({
                "question_id": question.id,
                "selected_options": [],
                "free_text": "按合理默认：明天开始，共三天，每天30分钟，Asia/Shanghai。",
            })
    return answers


def final_assistant_content(runtime, thread_id: str) -> str:
    messages = [m for m in runtime.conversation.messages(thread_id) if m.role == "assistant" and m.status == "ready"]
    return messages[-1].content if messages else ""


def decide(scenario: dict, pending) -> str:
    kind = scenario["kind"]
    if kind == "delivery":
        return "reject"
    if kind in {"feedback_reject"}:
        return "reject" if pending.tool_name == "record_action_feedback" else "approve"
    if kind in {"feedback_cancel"}:
        return "cancel" if pending.tool_name == "record_action_feedback" else "approve"
    return "approve"


async def drive(runtime_holder: dict, thread_id: str, scenario: dict, gateway) -> dict:
    """Run to quiescence, wiring approvals/asks; returns the decision log."""
    decisions: list[dict] = []
    state = {"activation_confirmed": False}
    deadline = time.monotonic() + 420
    for _step in range(40):
        if time.monotonic() > deadline:
            raise TimeoutError("scenario wall time exceeded")
        runtime = runtime_holder["runtime"]
        active_id = runtime.conversation.thread(thread_id, "local-user").active_turn_id
        if active_id is None:
            break
        turn = runtime.conversation.turn(active_id, "local-user")
        if turn.status == "AWAITING_TOOL_APPROVAL":
            pending = runtime.conversation.pending_tool_call(active_id, "local-user")
            action = decide(scenario, pending)
            decisions.append({
                "turn_id": active_id, "tool": pending.tool_name, "action": action,
                "params": pending.params, "binding": pending.binding,
            })
            if action == "cancel":
                runtime.conversation.cancel_turn(active_id, "local-user")
            else:
                runtime.conversation.decide_tool_call(
                    active_id, action, turn.version,
                    f"e2e-{scenario['id']}-{len(decisions)}", "local-user",
                )
            if action == "approve" and scenario["kind"] == "feedback_recovery" and not runtime_holder.get("restarted"):
                # Simulate a process restart while the approved write is resumed.
                runtime.db.close()
                runtime_holder["runtime"] = build_runtime(
                    runtime_holder["db_path"], runtime_holder["workspace"], gateway,
                )
                runtime_holder["restarted"] = True
                decisions[-1]["restart_after_decision"] = True
            continue
        if turn.status == "AWAITING_INPUT":
            ask = runtime.conversation.pending_ask(active_id, "local-user")
            decisions.append({"turn_id": active_id, "ask": ask.call_id, "action": "answer"})
            runtime.conversation.answer_ask(
                active_id, turn.version, f"e2e-ask-{scenario['id']}-{len(decisions)}",
                default_answers(ask), "local-user",
            )
            continue
        if turn.status == "AWAITING_DIRECTION":
            decisions.append({"turn_id": active_id, "action": "unexpected_legacy_direction"})
            runtime.conversation.select_direction(
                active_id, "modify_plan", turn.version, f"e2e-dir-{len(decisions)}", "local-user",
            )
            continue
        if turn.status in {"COMPLETED", "FAILED", "CANCELLED"} and not runtime.conversation.pending_turn_jobs():
            if scenario["kind"] == "manage" and not state["activation_confirmed"]:
                with runtime.db.connection() as connection:
                    program = connection.execute(
                        "SELECT status FROM goal_programs ORDER BY updated_at DESC LIMIT 1"
                    ).fetchone()
                if program is not None and program["status"] == "DRAFT":
                    # The preview is ready; the user now confirms the exact
                    # compiled version, exactly as the scenario text promises.
                    decisions.append({"turn_id": active_id, "action": "user_confirms_activation"})
                    runtime.conversation.accept_turn(
                        thread_id, f"{scenario['id']}-confirm", "确认，按这个版本激活执行管理。", [],
                        owner_id="local-user",
                    )
                    state["activation_confirmed"] = True
                    continue
            break
        worked = await runtime.turn_worker.run_once()
        if not worked and not runtime.conversation.pending_turn_jobs():
            break
    return {"decisions": decisions, "restarted": bool(runtime_holder.get("restarted"))}


def evaluate(scenario: dict, runtime, thread_id: str, decision_log: dict) -> dict:
    kind = scenario["kind"]
    answer = final_assistant_content(runtime, thread_id)
    checks: dict[str, bool] = {"answer_nonempty": bool(answer.strip())}
    if kind == "delivery":
        checks["no_plan_documents"] = count(runtime, "plan_documents") == 0
        checks["no_goal_programs"] = count(runtime, "goal_programs") == 0
        checks["no_goals"] = count(runtime, "goals") == 0
        checks["no_runs"] = count(runtime, "runs") == 0
    elif kind == "save":
        checks["one_plan_document"] = count(runtime, "plan_documents") == 1
        checks["one_committed_version"] = count(runtime, "plan_document_versions", "status='committed'") == 1
        checks["no_goal_programs"] = count(runtime, "goal_programs") == 0
    elif kind == "manage":
        checks["one_goal_program"] = count(runtime, "goal_programs") == 1
        with runtime.db.connection() as connection:
            row = connection.execute("SELECT status,start_date,end_date,timezone,daily_minutes FROM goal_programs LIMIT 1").fetchone()
            action_count = connection.execute("SELECT COUNT(*) AS total FROM goal_actions").fetchone()["total"]
        checks["program_active"] = row is not None and row["status"] == "ACTIVE"
        checks["has_actions"] = int(action_count) >= 1
        expected_start = decision_log["expected_start"]
        checks["relative_dates_correct"] = bool(row is not None
            and row["start_date"] == expected_start.isoformat()
            and row["end_date"] == (expected_start + timedelta(days=2)).isoformat())
        checks["schedule_constraints_correct"] = bool(row is not None
            and row["timezone"] == "Asia/Shanghai" and row["daily_minutes"] == 30)
    elif kind == "modify":
        checks["two_committed_versions"] = count(
            runtime, "plan_document_versions", "status='committed'",
        ) == 2
        with runtime.db.connection() as connection:
            program = connection.execute(
                "SELECT status,source_plan_document_version_id FROM goal_programs LIMIT 1"
            ).fetchone()
            first = connection.execute(
                "SELECT id FROM plan_document_versions ORDER BY version LIMIT 1"
            ).fetchone()
            tool_row = connection.execute(
                "SELECT result_json FROM turn_tool_calls WHERE tool_name='modify_plan_document' "
                "AND status='EXECUTED' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        checks["program_active"] = program is not None and program["status"] == "ACTIVE"
        checks["program_source_unchanged"] = (
            program is not None and first is not None
            and program["source_plan_document_version_id"] == first["id"]
        )
        checks["execution_updated_false"] = bool(
            tool_row is not None
            and (json.loads(tool_row["result_json"]).get("data") or {}).get("execution_updated") is False
        )
    elif kind in {"feedback_reject", "feedback_cancel"}:
        checks["no_feedback_rows"] = count(runtime, "goal_action_feedback") == 0
        if kind == "feedback_reject":
            checks["answer_does_not_claim_recorded"] = "已记录" not in answer
    elif kind == "feedback_recovery":
        checks["one_feedback_row"] = count(runtime, "goal_action_feedback") == 1
        checks["process_restarted"] = bool(decision_log.get("restarted"))
    # A successful write may legitimately be described with a receipt; every
    # other scenario must not claim an effect the tools never proved.
    if kind not in {"save", "manage", "modify", "feedback_recovery"}:
        checks["no_false_claims"] = not any(marker in answer for marker in CLAIM_MARKERS)
    return {"checks": checks, "passed": all(checks.values()), "final_answer": answer}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ids", nargs="*")
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute required for paid real-model calls")
    scenarios = [s for s in SCENARIOS if not args.ids or s["id"] in args.ids]
    if args.ids and len(scenarios) != len(set(args.ids)):
        parser.error("unknown scenario id")

    from app.config import load_model_profile_from_environment, load_user_model_environment

    load_user_model_environment()
    profile = load_model_profile_from_environment()
    if profile is None:
        raise RuntimeError("No configured real model")
    profile = replace(profile, max_attempts=1, network_retries=0, timeout_seconds=120)

    os.environ.setdefault("BETTER_AGENT_TEST_ALLOW_SQLITE", "1")
    args.out.mkdir(parents=True, exist_ok=False)
    semaphore = asyncio.Semaphore(1)
    budget = {"calls": 0, "limit": max(60, len(scenarios) * 12)}
    rows = []
    started_at = datetime.now(timezone.utc).isoformat()

    for scenario in scenarios:
        gateway = RecordedGateway(profile, args.out, f"{scenario['id']}", semaphore, budget)
        scenario_dir = args.out / "scenarios" / scenario["id"]
        scenario_dir.mkdir(parents=True, exist_ok=True)
        db_path = scenario_dir / "agent.db"
        workspace = scenario_dir / "artifacts"
        runtime = build_runtime(db_path, workspace, gateway)
        thread = runtime.conversation.create_thread(f"e2e-{scenario['id']}")
        if scenario.get("seed_document"):
            seed_document(runtime, thread.id)
        if scenario.get("seed_program"):
            await seed_program(
                runtime, thread.id, gateway, scenario["id"],
                title=scenario.get("seed_program_title", "四周力量计划"),
                markdown=scenario.get("seed_program_markdown", "# 四周力量计划\n\n- 力量训练"),
            )
        runtime.conversation.accept_turn(thread.id, f"{scenario['id']}-turn-1", scenario["prompt"], [], owner_id="local-user")
        holder = {"runtime": runtime, "db_path": db_path, "workspace": workspace, "restarted": False}
        holder["expected_start"] = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
        row = {"id": scenario["id"], "input": scenario["prompt"], "status": "started"}
        started = time.perf_counter()
        try:
            decision_log = await drive(holder, thread.id, scenario, gateway)
            runtime = holder["runtime"]
            row["decisions"] = decision_log["decisions"]
            row.update(evaluate(scenario, runtime, thread.id, holder))
            row["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - failures stay in the report
            row.update(status="error", error_type=type(exc).__name__, error=str(exc)[:500])
        finally:
            row["elapsed_seconds"] = time.perf_counter() - started
            row["model_calls"] = len(gateway.records)
            row["usage"] = {
                key: sum(rec.get("response", {}).get("usage", {}).get(key) or 0 for rec in gateway.records)
                for key in ("uncached_input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")
            }
            rows.append(row)
            holder["runtime"].db.close()
            print(f"{scenario['id']} {row['status']} calls={row['model_calls']} passed={row.get('passed')}", flush=True)

    report = {
        "status": "completed",
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "model": profile.model,
        "provider": profile.provider_name,
        "configuration": {
            "temperature": 0, "thinking": False, "max_output_tokens": profile.max_output_tokens,
            "max_attempts": 1, "network_retries": 0, "timeout_seconds": 120,
            "database": "one disposable sqlite file per scenario in the output directory",
        },
        "source_hashes": {
            name: digest((ROOT / name).read_bytes())
            for name in (
                "backend/app/live_model.py", "backend/app/conversation.py", "backend/app/chat_tools.py",
                "backend/app/goal_tools.py", "backend/scripts/chat_tool_e2e.py",
            )
        },
        "limitations": [
            "Approval decisions are made by the harness, not by the model.",
            "Each scenario uses its own disposable SQLite database; PostgreSQL write semantics are covered by integration tests.",
            "One run per scenario; passing does not imply production reliability.",
        ],
        "rows": rows,
        "passed": sum(1 for row in rows if row.get("passed")),
        "failed": sum(1 for row in rows if not row.get("passed")),
        "total_model_calls": budget["calls"],
    }
    report["usage"] = {
        key: sum(row["usage"][key] for row in rows)
        for key in ("uncached_input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")
    }
    write_json(args.out / "results.json", report)
    print(json.dumps({"passed": report["passed"], "failed": report["failed"], "calls": report["total_model_calls"]}))


if __name__ == "__main__":
    asyncio.run(main())
