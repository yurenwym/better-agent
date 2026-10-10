"""Explicitly authorized, isolated production routing evaluation; no background jobs."""
from __future__ import annotations

import argparse
import hashlib
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_env_file, load_user_model_environment
from app.evals import load_routing_suite, routing_digest
from app.model_gateway import ModelProfile
from app.startup import build_runtime


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    for attempt in range(10):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(.1)


async def run(args):
    root = Path(__file__).resolve().parents[2]
    load_env_file(root / ".env")
    load_user_model_environment()
    sys.path.insert(0, str(root / "backend/tests/integration"))
    from db_target_guard import assert_isolated_test_database
    database = os.environ["AGENT_LOOP_EVAL_DATABASE_URL"]
    assert_isolated_test_database(database)
    os.environ["BETTER_AGENT_LOOP_MODE"] = args.mode
    os.environ["BETTER_AGENT_COST_MODE"] = "enforce"
    os.environ["EMBEDDING_API_KEY_ENV"] = "AGENT_LOOP_UNUSED_EMBEDDING_KEY"
    os.environ.pop("AGENT_LOOP_UNUSED_EMBEDDING_KEY", None)
    for name in tuple(os.environ):
        if name.startswith("AGENT_FALLBACK_MODEL_"):
            os.environ.pop(name)
    suite = load_routing_suite(root / "evals/cases/agent-loop-routing-v1.json",
                               root / "docs/acceptance/agent-loop-m1/holdout-freeze.json")
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    code_files = sorted((root / 'backend/app').rglob('*.py'))
    code_digest = hashlib.sha256(b''.join(str(p.relative_to(root)).encode() + b'\0' + p.read_bytes() for p in code_files)).hexdigest()
    path = output / (args.mode + ".json")
    if path.exists():
        report = json.loads(path.read_text(encoding="utf-8"))
        if report["suite_digest"] != routing_digest(suite):
            raise ValueError("suite changed")
        if report.get('code_digest') not in (None, code_digest):
            raise ValueError('code changed: use a new output directory')
        if any(row["status"] == "STARTED" for row in report["cases"]):
            raise ValueError("ambiguous started case: inspect database before retrying")
    else:
        report = {"mode": args.mode, "provider": "real", "suite_digest": routing_digest(suite),
            "code": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
            "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip()),
            "cost_mode": "enforce", "budget_cap_microusd": args.budget_microusd,
            "code_digest": code_digest,
            "scope": "routing_only; research/expert workers and exposure judging disabled",
            "planned": len(suite["cases"]), "cases": [], "status": "RUNNING"}
    print(json.dumps({"mode": args.mode, "planned": 70, "estimated_calls": "70-210", "shared_budget_usd": args.budget_microusd / 1e6}), flush=True)
    profile = ModelProfile("https://api.deepseek.com", "deepseek-flash", "DEEPSEEK_API_KEY",
        timeout_seconds=90, max_attempts=1, network_retries=0, provider_name="deepseek",
        context_window=65536, max_output_tokens=2048,
        declared_capabilities=frozenset({"text", "streaming", "tool_calling", "json_object"}))
    runtime = build_runtime(output / "workspace", profile=profile, database_url=database)
    # One shared daily budget covers both paths, including classification calls.
    today = runtime.costs.today_period()
    runtime.costs.set_budget("local-user", "DAILY", today, args.budget_microusd)
    runtime.costs.set_budget("local-user", "MONTHLY", today[:7], args.budget_microusd)
    runtime.costs.set_budget("local-user", "INVOCATION", "default", 100_000)
    gateway = runtime.conversation.route_model.gateway
    original = gateway.complete
    measured = []
    async def measured_complete(request, **kwargs):
        result = await original(request, **kwargs)
        row = {"case_id": case['case_id'], "mode": args.mode, "purpose": request.purpose, "ttft_seconds": result.timing.ttft_seconds,
            "usage": vars(result.usage), "response": result.message, "tool_calls": result.tool_calls}
        measured.append(row)
        with (output / (args.mode + "-responses.ndjson")).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return result
    gateway.complete = measured_complete
    # Evaluation measures route choice; services create jobs, but workers never run search/experts.
    runtime.turn_worker.primary._finish_exposure = _no_exposure
    completed = {row["case_id"] for row in report["cases"]}
    try:
        for case in suite["cases"]:
            if case["case_id"] in completed:
                continue
            row = {key: case[key] for key in ("case_id", "partition", "category", "expected", "forbidden")}
            row.update(status="STARTED", observed=[], model_calls=0, tokens=None, cost_microusd=None, first_token_ms=None)
            report["cases"].append(row)
            save(path, report)
            measured.clear()
            thread = runtime.conversation.create_thread("routing:" + case["case_id"])
            # Fixture history is synthetic and independent of provider output.
            for index, message in enumerate(case["history"]):
                fixture_turn = runtime.conversation.accept_turn(thread.id, f"fixture-{index}", "合成历史夹具", [])
                with runtime.db.transaction() as connection:
                    connection.execute("UPDATE turns SET status='COMPLETED',policy='answer',content_shape='text' WHERE id=?", (fixture_turn.turn_id,))
                    connection.execute("UPDATE turn_jobs SET status='COMPLETED' WHERE turn_id=?", (fixture_turn.turn_id,))
                    connection.execute("UPDATE thread_messages SET role=?,content=?,content_length=?,status='ready' WHERE turn_id=?",
                        (message["role"], message["content"], len(message["content"]), fixture_turn.turn_id))
            if case["category"] == "goal_write":
                runtime.plan_documents.save_model_revision(thread_id=thread.id, title="当前阅读计划",
                    markdown_content="# 四周阅读计划\n\n每天阅读二十分钟，周末同样阅读。第二周继续阅读，最后一天读新章节。",
                    source_turn_id=None, source_message_id=None, actor="model")
            accepted = runtime.conversation.accept_turn(thread.id, "eval", case["input"], [])
            row["turn_id"] = accepted.turn_id
            save(path, report)
            await runtime.turn_worker.run_once()
            turn = runtime.conversation.turn(accepted.turn_id)
            observed = []
            if turn.policy == "start_research": observed.append("handoff:research")
            elif turn.policy == "start_expert": observed.append("handoff:expert")
            elif turn.status == "AWAITING_INPUT": observed.append("ask")
            elif turn.status == "AWAITING_TOOL_APPROVAL": observed.append("approval")
            elif turn.status == "COMPLETED": observed.append("final+plan_document" if turn.content_shape == "plan_document" else "final")
            with runtime.db.connection() as connection:
                calls = connection.execute("SELECT tool_name,status FROM turn_tool_calls WHERE turn_id=?", (turn.id,)).fetchall()
                for call in calls:
                    if call["status"] == "EXECUTED" and call["tool_name"] in {"remember", "query_goals"}:
                        observed.append("tool:" + call["tool_name"])
                if turn.reason_code == "memory_saved": observed.append("tool:remember")
                attempts = [dict(item) for item in connection.execute("SELECT a.cost_microusd,a.cost_status,a.uncached_input_tokens,a.cache_read_tokens,a.cache_write_tokens,a.output_tokens,a.reasoning_tokens FROM model_attempts a JOIN model_invocations i ON i.id=a.invocation_id WHERE i.turn_id=?", (turn.id,))]
                invocation_count = connection.execute('SELECT COUNT(*) FROM model_invocations WHERE turn_id=?', (turn.id,)).fetchone()[0]
            valid = turn.status in {"COMPLETED", "AWAITING_INPUT", "AWAITING_TOOL_APPROVAL"}
            row.update(status="valid" if valid else "cancelled" if turn.status == "CANCELLED" else "invalid",
                observed=observed, model_calls=invocation_count, attempts=attempts,
                matched=valid and set(case["expected"]) <= set(observed) and not bool(set(case["forbidden"]) & set(observed)),
                forbidden_hit=bool(set(case["forbidden"]) & set(observed)),
                cost_microusd=sum(item["cost_microusd"] for item in attempts) if all(item["cost_microusd"] is not None for item in attempts) else None,
                first_token_ms=next((item["ttft_seconds"] * 1000 for item in measured if item["purpose"] == "route_and_respond" and item["ttft_seconds"] is not None), None),
                tokens=sum(item[key] for item in attempts for key in ('uncached_input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'output_tokens'))
                    if all(item[key] is not None for item in attempts for key in ('uncached_input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'output_tokens')) else None,
                bundle_id=turn.runtime_bundle_id)
            save(path, report)
            print(json.dumps({"mode": args.mode, "done": len(report["cases"]), "status": row["status"], "matched": row["matched"]}), flush=True)
            if args.limit and len(report["cases"]) >= args.limit:
                break
        report["status"] = "COMPLETED" if len(report["cases"]) == report["planned"] else "PARTIAL"
        save(path, report)
    finally:
        runtime.db.close()


async def _no_exposure(*args, **kwargs):
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["legacy", "loop"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--authorize-paid", action="store_true", required=True)
    parser.add_argument("--budget-microusd", type=int, default=1_000_000)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    asyncio.run(run(args))
