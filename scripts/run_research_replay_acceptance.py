"""Execute Replay-M3 gates and retain immutable command/JUnit evidence."""
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "docs/acceptance/research-snapshot-replay/logs"


def main():
    batch = sys.argv[1] if len(sys.argv) > 1 else "verified"
    groups = {
        "local": ["tests/test_research_snapshot_replay.py", "tests/test_model_input_snapshot.py", "tests/test_model_input_snapshot_store.py",
                  "tests/test_snapshot_research_entries.py", "tests/test_research_engine.py", "tests/test_real_evaluation.py",
                  "tests/test_m5_release_gates.py", "tests/test_m5_controlled_acceptance.py", "tests/test_snapshot_production_boundary.py"],
        "postgres": ["tests/integration/test_snapshot_entrypoints_postgres.py", "tests/integration/test_model_input_snapshot_postgres.py",
                     "tests/integration/test_harness_context_postgres.py", "tests/integration/test_root_task_budgets.py"],
    }
    failed = False
    for group, files in groups.items():
        stem = f"{batch}-{group}"
        log, junit = LOGS / (stem + ".log"), LOGS / (stem + ".xml")
        if log.exists() or junit.exists():
            raise SystemExit("Batch already exists; choose a new batch ID")
        command = [sys.executable, "-m", "pytest", *files, "-q", "--tb=short", "--junitxml=" + str(junit)]
        environment = os.environ.copy()
        environment["BETTER_AGENT_COST_MODE"] = "enforce"
        environment.pop("BETTER_SNAPSHOT_EVIDENCE_PATH", None)
        code_paths = [*files, "app/research_replay.py", "app/research/live.py", "app/research/engine.py", "app/real_evaluation.py"]
        source_hashes = {path: hashlib.sha256((ROOT / "backend" / path).read_bytes()).hexdigest() for path in code_paths}
        started = datetime.now(timezone.utc).isoformat()
        with log.open("w", encoding="utf-8") as stream:
            process = subprocess.run(command, cwd=ROOT / "backend", env=environment, stdout=stream, stderr=subprocess.STDOUT)
        cases = list(ET.parse(junit).getroot().iter("testcase")) if junit.exists() else []
        result = {"batch": batch, "group": group, "command": command, "cost_mode": "enforce",
                  "flags": {"model_provider": "scripted", "paid_model_calls": 0, "postgres": "guarded ephemeral Docker"},
                  "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(), "exit_code": process.returncode,
                  "tests": len(cases), "failures": sum(case.find("failure") is not None for case in cases),
                  "errors": sum(case.find("error") is not None for case in cases), "skipped": sum(case.find("skipped") is not None for case in cases),
                  "log": log.name, "junit": junit.name,
                  "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                  "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
                  "source_sha256_at_start": source_hashes,
                  "source_unchanged_during_execution": all(hashlib.sha256((ROOT / "backend" / path).read_bytes()).hexdigest() == sha for path, sha in source_hashes.items())}
        with (LOGS / "runs.ndjson").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(json.dumps({key: result[key] for key in ("batch", "group", "tests", "failures", "errors", "skipped", "exit_code")}), flush=True)
        failed |= bool(process.returncode)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
