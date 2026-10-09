"""Run an explicit offline M2 test group, retaining logs and JUnit evidence."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/acceptance/context-snapshot-phase2/phase2b/logs"
GROUPS = {
    "m1": "test_m5_release_gates test_m5_controlled_acceptance test_m5_controlled_evolution test_eval_scenarios integration/test_m1_live_harness",
    "entries": "test_snapshot_research_entries test_snapshot_agent_entries test_snapshot_learning_entries test_snapshot_goal_runtime_entries test_snapshot_auxiliary_entries test_snapshot_admin_entries test_snapshot_production_boundary test_snapshot_evaluation_entries",
    "snapshot": "test_model_input_snapshot test_model_input_snapshot_store test_snapshot_gateway test_snapshot_flow test_snapshot_recovery",
    "harness": "test_execution_context test_harness_context_flow test_harness_context_approval test_model_control test_model_gateway test_routed_model_gateway test_chat_goal_tools test_goal_tools test_goal_tool_recovery test_conversation test_conversation_worker test_conversation_protocol_v2 test_goal_programs test_goal_program_compiler",
    "business": "test_research_engine test_research_service test_research_api test_agent_tasks test_agent_worker test_agent_api test_model_admin_api test_startup_contract test_runtime test_runtime_prompt_policy test_materializer test_plan_execution_projection test_goal_reviews test_goal_period_review test_goal_adjustments test_goal_adjustment_api test_live_model",
    "learning": "test_learning_agent test_learning_eval test_learning_judge_reliability test_learning_pipeline_v3 test_learning_v3_wiring test_learning_v3_fixes test_evolution test_evolution_api test_real_evaluation test_evaluation_api test_memory_archive test_memory_reference test_memory_reference_gateway",
    "postgres": "integration/test_snapshot_entrypoints_postgres integration/test_model_input_snapshot_postgres integration/test_harness_context_postgres integration/test_chat_goal_tools_postgres integration/test_goal_tool_recovery_postgres integration/test_root_task_budgets integration/test_attempt_settlement_postgres integration/test_learning_v2_postgres integration/test_budget_waiting_and_operations integration/test_db_target_guard",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("group", choices=GROUPS)
    parser.add_argument("--mode", choices=("observe", "enforce"), default="observe")
    parser.add_argument("--batch", required=True)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"{args.batch}-{args.group}-{args.mode}"
    log, junit = OUT / f"{tag}.log", OUT / f"{tag}.xml"
    if log.exists() or junit.exists():
        raise SystemExit("batch/group already recorded; use a fresh batch to retain failed rounds")
    files = [f"tests/{name}.py" for name in GROUPS[args.group].split()]
    command = [sys.executable, "-m", "pytest", *files, "-q", "--tb=short", f"--junitxml={junit}"]
    env = {**os.environ, "PYTHONIOENCODING":"utf-8", "BETTER_AGENT_COST_MODE":args.mode,
           "BETTER_SNAPSHOT_BATCH_ID":args.batch, "BETTER_SNAPSHOT_EVIDENCE_PATH":str(OUT / f"{args.batch}-bindings.ndjson")}
    started = datetime.now(timezone.utc).isoformat()
    with log.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps({"command":command,"cwd":str(ROOT / "backend"),"started_at":started,
            "cost_mode":args.mode,"batch":args.batch}, ensure_ascii=False) + "\n")
        stream.flush()
        result = subprocess.run(command, cwd=ROOT / "backend", env=env, stdout=stream, stderr=subprocess.STDOUT)
    summary = {"group":args.group,"batch":args.batch,"cost_mode":args.mode,"exit_code":result.returncode,
        "started_at":started,"finished_at":datetime.now(timezone.utc).isoformat(),"log":log.name,"junit":junit.name}
    if junit.exists():
        suites = ET.parse(junit).getroot().findall("testsuite")
        summary.update({key:sum(int(suite.get(key, "0")) for suite in suites) for key in ("tests","failures","errors","skipped")})
    with (OUT / "m2-runs.ndjson").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(summary) + "\n")
    print(json.dumps(summary))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
