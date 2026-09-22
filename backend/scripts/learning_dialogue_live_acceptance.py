"""Frozen conversation acceptance on a fresh PostgreSQL database and live models.

Run from backend: python scripts/learning_dialogue_live_acceptance.py --live
Creates its own test database; never migrates or truncates the development DB.
Discovery conversations are authored fixtures; every assistant/replay/judge
response is live. This is learning acceptance, not a production release test.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend/tests/integration"))


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def snapshot(runtime):
    with runtime.db.connection() as connection:
        return {
            "memory": [tuple(row) for row in connection.execute("SELECT id,current_revision_id,status FROM memory_entries ORDER BY id")],
            "skills": [tuple(row) for row in connection.execute("SELECT id,version,status FROM skill_versions WHERE status='ENABLED' ORDER BY id")],
            "channels": [tuple(row) for row in connection.execute("SELECT name,bundle_id FROM runtime_channels ORDER BY name")],
        }


def test_database():
    import psycopg
    from psycopg import sql
    from db_target_guard import assert_isolated_test_database
    secret = subprocess.run(["docker", "exec", "better-postgres-1", "printenv", "POSTGRES_PASSWORD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    if not secret:
        raise RuntimeError("PostgreSQL test credential unavailable")
    name = "better_learning_live_" + uuid.uuid4().hex[:10] + "_test"
    url = f"postgresql://better_agent:{quote(secret, safe='')}@127.0.0.1:5432/{name}"
    assert_isolated_test_database(url)
    with psycopg.connect(host="127.0.0.1", user="better_agent", password=secret, dbname="postgres", autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    environment = {**os.environ, "DATABASE_URL": url}
    migration = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT / "backend",
                               env=environment, capture_output=True, text=True)
    if migration.returncode:
        raise RuntimeError("isolated test database migration failed: " + migration.stderr.replace(secret, "<redacted>")[-1000:])
    return name, url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/cases/learning-live-dialogues-v1.json")
    parser.add_argument("--replay", type=Path, default=ROOT / "evals/cases/learning-runtime-replay-v1.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    replay_data = json.loads(args.replay.read_text(encoding="utf-8"))
    if not args.live:
        print(json.dumps({"conversations": len(dataset["discovery"]), "replay_cases": sum(len(s["cases"]) for s in replay_data["suites"]),
                          "network": False, "next": "pass --live to run"}))
        return 0
    from app.config import load_env_file
    load_env_file(ROOT / ".env")
    if not os.getenv("TYPESAFE_API_KEY"):
        raise RuntimeError("TYPESAFE_API_KEY unavailable")
    os.environ["BETTER_AGENT_LEARNING_V3"] = "SHADOW"
    os.environ["BETTER_AGENT_LEARNING_REPLAY_FILE"] = str(args.replay.resolve())
    os.environ["BETTER_AGENT_COST_MODE"] = "enforce"
    os.environ["BETTER_AGENT_LEARNING_CANARY_BUDGET_MICROUSD"] = "1000000"
    output = args.output or ROOT / "evals/results" / ("learning-dialogue-live-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    output.mkdir(parents=True, exist_ok=False)
    write(output / "dataset.json", dataset)
    write(output / "replay.json", replay_data)
    name, url = test_database()
    from app.startup import build_runtime
    from app.model_control import ModelCallContext
    from app.model_gateway import ModelRequest
    from app.learning import digest as source_digest
    runtime = build_runtime(Path(tempfile.mkdtemp(prefix="v3live-")), database_url=url)
    pipeline = runtime.learning.pipeline
    gateway = pipeline.agent.gateway
    if not pipeline.replay or not pipeline.replay.suites:
        raise RuntimeError("runtime replay suites were not wired")
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["memory", "skill", "prompt"],
        cycle_microusd=1_000_000, daily_microusd=10_000_000, monthly_microusd=10_000_000, max_attempts=200)
    today = runtime.costs.today_period()
    for kind, period in (("DAILY", today), ("MONTHLY", today[:7])):
        runtime.costs.set_budget("local-user", kind, period, 10_000_000)
    plan = {"created_at": now(), "dataset_digest": digest(dataset), "replay_digest": digest(replay_data),
        "database": name, "model": pipeline.agent.identity(), "judge": pipeline.judge.identity(),
        "jev": pipeline.decisions.client.model, "max_gateway_calls": 300, "llm_budget_microusd": 10_000_000,
        "jev_budget": "excluded by user instruction", "baseline_bundle": runtime.behavior.active("stable").id,
        "thresholds": dataset["acceptance"], "provenance": "authored scenarios and feedback; all responses generated live",
        "code": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in [*sorted((ROOT / 'backend/app').glob('learning*.py')), Path(__file__)]}}
    write(output / "plan.json", plan)  # Before the first remote call; never overwritten.
    print(f"FROZEN {output}", flush=True)
    report = {"plan": plan, "conversations": [], "cycles": [], "checks": {}, "errors": [], "calls": 0}
    started = time.monotonic()
    original_complete = gateway.complete
    call_records = output / "model-calls.jsonl"

    async def observed_complete(request, *positional, **kwargs):
        if report["calls"] >= plan["max_gateway_calls"]:
            raise RuntimeError("frozen model call limit exhausted")
        report["calls"] += 1
        sequence = report["calls"]
        record = {"sequence": sequence, "role": request.role, "purpose": request.purpose,
                  "messages": request.messages, "started_at": now()}
        try:
            response = await original_complete(request, *positional, **kwargs)
            record["response"] = response.message
            return response
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"[:500]
            raise
        finally:
            record["finished_at"] = now()
            with call_records.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            if sequence % 10 == 0:
                print(f"CALL {sequence} {request.role}/{request.purpose}", flush=True)
    gateway.complete = observed_complete
    conversation_root = runtime.costs.create_root_budget("local-user", "evaluation", "discovery-dialogues",
        max_attempts=60, deadline_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), limit_microusd=1_000_000)

    def save():
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        write(output / "report.json", report)

    def dialogue(case):
        thread = runtime.conversation.create_thread("[ACCEPT] " + case["id"])
        messages = [{"role": "system", "content": "你是技术助手。准确、简洁地回答用户，不执行工具或生产操作。"}]
        transcript = {"id": case["id"], "group": case["group"], "thread_id": thread.id, "turns": []}
        last = None
        for i, turn in enumerate(case["turns"]):
            accepted = runtime.conversation.accept_turn(thread.id, f"turn-{i}", turn["content"])
            messages.append(turn)
            response = asyncio.run(gateway.complete(ModelRequest(messages=list(messages), tools=[], temperature=0,
                max_tokens=900, role="conversation", purpose="acceptance_dialogue", thinking=False),
                context=ModelCallContext("conversation", "acceptance_dialogue", owner_id="local-user",
                    root_budget_id=conversation_root["id"], runtime_bundle_id=plan["baseline_bundle"])))
            messages.append({"role": "assistant", "content": response.message})
            with runtime.db.transaction() as connection:
                last = connection.execute("SELECT id FROM thread_messages WHERE turn_id=? AND role='user'", (accepted.turn_id,)).fetchone()[0]
                # Persist the actual reply, not a canned assistant fixture. This
                # harness drives model+storage directly; it is not a UI/router test.
                connection.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at) "
                    "VALUES (?,?,?,'assistant',?,'ready',1,?,(SELECT COALESCE(MAX(message_seq),0)+1 FROM thread_messages WHERE thread_id=?),?)",
                    ("message_" + uuid.uuid4().hex, thread.id, accepted.turn_id, response.message, len(response.message), thread.id, now()))
                connection.execute("UPDATE turns SET status='COMPLETED',updated_at=? WHERE id=?", (now(), accepted.turn_id))
                connection.execute("UPDATE threads SET active_turn_id=NULL WHERE id=?", (thread.id,))
            transcript["turns"].extend([turn, {"role": "assistant", "content": response.message, "origin": "live_model"}])
        report["conversations"].append(transcript)
        save()
        return transcript, last, accepted.turn_id

    def cycle(case_id, source_kind, source_id, source_hash, mode):
        pipeline.set_mode(mode)
        job_id = runtime.learning.enqueue("local-user", source_kind, source_id, source_hash, source_id, provenance="acceptance")
        # accept_turn also enqueues learning. Claim only the selected evaluation
        # job, suspending other pending authored turns without touching their data.
        with runtime.db.transaction() as connection:
            connection.execute("UPDATE learning_jobs SET status='SUSPENDED' WHERE status='QUEUED' AND id<>?", (job_id,))
        job = runtime.learning.claim("local-user", lease_seconds=3600)
        if job is None or job["id"] != job_id:
            raise RuntimeError("acceptance job was not claimable")
        before = snapshot(runtime)
        result = pipeline.run_job(job)
        item = {"case_id": case_id, **result.to_dict(), "production_unchanged": before == snapshot(runtime)}
        report["cycles"].append(item)
        save()
        print(f"CYCLE {case_id}: {result.target} {result.outcome} {result.reason[:180]}", flush=True)
        return result

    try:
        evidence = {"skill": [], "behavior": []}
        for case in dataset["discovery"]:
            transcript, message, turn_id = dialogue(case)
            group = case["group"]
            if group in {"memory", "ignore"}:
                cycle(case["id"], "thread_message", message, source_digest(case["turns"][-1]["content"]),
                      "ACTIVE" if group == "memory" else "SHADOW")
            else:
                experience = runtime.evolution.record_experience(task_type=case["task_type"], outcome="failure",
                    lineage_group_hash=digest(case["id"]), root_task_id=turn_id, source_content_hash=digest(transcript),
                    runtime_bundle_id=plan["baseline_bundle"], dataset_partition="DISCOVERY", source_kind="turn", source_id=turn_id,
                    signal_type="user_feedback", severity="warning", provenance="acceptance", idempotency_key=case["id"],
                    evidence={"note": case["turns"][-1]["content"], "dialogue": transcript["turns"],
                              "tool_trace": case.get("observed_trace", []), "origin": case["observation_origin"]},
                    failure_tags=["repeatable_timeout_triage" if group == "skill" else "premature_root_cause"])
                evidence[group].append(experience)
        for group in ("skill", "behavior"):
            item = evidence[group][-1]
            cycle(group + "-aggregate", "experience", item["id"], item["source_content_hash"], "SHADOW")
        checks = report["checks"]
        memories = [item for item in report["cycles"] if item["case_id"].startswith("memory-")]
        ignored = [item for item in report["cycles"] if item["case_id"].startswith("ignore-")]
        skill = next(item for item in report["cycles"] if item["case_id"] == "skill-aggregate")
        behavior = next(item for item in report["cycles"] if item["case_id"] == "behavior-aggregate")
        checks.update(memory_promoted=sum(item["outcome"] == "PROMOTED" for item in memories) >= 2,
            ignores_correct=all(item["target"] == "IGNORE" and not item["candidate_id"] for item in ignored),
            skill_target=skill["target"] == "SKILL", skill_candidate=bool(skill["candidate_id"]),
            skill_live_replay=bool((skill.get("evaluation") or {}).get("metrics", {}).get("records")),
            skill_eval_pass=(skill.get("evaluation") or {}).get("outcome") == "PASS",
            skill_shadow_gate=skill["outcome"] == "SHADOW",
            behavior_target=behavior["target"] == "BEHAVIOR", behavior_candidate=bool(behavior["candidate_id"]),
            behavior_live_replay=bool((behavior.get("evaluation") or {}).get("metrics", {}).get("records")),
            behavior_eval_pass=(behavior.get("evaluation") or {}).get("outcome") == "PASS",
            behavior_shadow_gate=behavior["outcome"] == "SHADOW",
            shadow_production_unchanged=all(item["production_unchanged"] for item in report["cycles"] if item["observability"]["mode"] == "SHADOW"),
            no_unknown=all(item["status"] != "UNKNOWN" for item in report["cycles"]))
        with runtime.db.connection() as connection:
            report["model_usage"] = dict(connection.execute("SELECT COUNT(*) calls,COALESCE(SUM(cost_microusd),0) cost_microusd FROM model_attempts").fetchone())
            checks["owner_isolation"] = connection.execute("SELECT COUNT(*) FROM memory_entries WHERE owner_id<>'local-user'").fetchone()[0] == 0
            checks["no_unauthorized_promotion"] = connection.execute("SELECT COUNT(*) FROM learning_promotions WHERE target<>'MEMORY' AND outcome='PROMOTED'").fetchone()[0] == 0
        checks["calls_within_limit"] = report["calls"] <= plan["max_gateway_calls"]
        report["gate"] = {"pass": all(checks.values()), "failed": [key for key, value in checks.items() if not value]}
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}"[:1000])
        report["gate"] = {"pass": False, "failed": ["execution_error"]}
    finally:
        save()
        runtime.db._pool.close()
    print(json.dumps({"gate": report["gate"], "calls": report["calls"], "output": str(output)}, ensure_ascii=False), flush=True)
    return 0 if report["gate"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
