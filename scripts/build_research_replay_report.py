"""Assemble Replay-M3 evidence from actual executions, preserving failed batches."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "docs/acceptance/research-snapshot-replay"
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]
from app.research_replay import digest, _write, load_suite, freeze_suite, comparison_markdown
from app.real_evaluation import ResearchRoleReplayEvaluator


def main():
    suite_path = ROOT / "backend/tests/fixtures/research-snapshot-replay-v3/case-manifest.json"
    suite, fixtures = load_suite(suite_path)
    runs = [json.loads(line) for line in (ACCEPTANCE / "logs/runs.ndjson").read_text(encoding="utf-8").splitlines()]
    selected = [run for run in runs if run["batch"] == "release"]
    if len(selected) != 2 or any(run["exit_code"] or run["failures"] or run["errors"] or run["skipped"] or not run["source_unchanged_during_execution"] for run in selected):
        raise SystemExit("Required actual acceptance batches have not passed")
    mapping = {f"RP{n:02}": [] for n in range(1, 19)}
    actual_nodes = []
    for run in selected:
        for case in ET.parse(ACCEPTANCE / "logs" / run["junit"]).getroot().iter("testcase"):
            node = case.get("classname").replace(".", "/") + ".py::" + case.get("name")
            actual_nodes.append(node)
            for identity in re.findall(r"rp(\d{2})", case.get("name").lower()):
                mapping["RP" + identity].append(node)
    if any(not nodes for nodes in mapping.values()):
        raise SystemExit("Unexecuted RP contract")
    # Retain real prompt/identity/Judge evidence separately from scenario rules.
    from pytest import MonkeyPatch
    from test_research_snapshot_replay import paired_setup
    with tempfile.TemporaryDirectory(prefix="research-replay-judge-") as temp:
        monkeypatch = MonkeyPatch()
        try:
            monkeypatch.setenv("BETTER_AGENT_COST_MODE", "observe")
            db, base, candidate, runner, config, observers, case, _ = paired_setup(Path(temp), monkeypatch)
            report = ResearchRoleReplayEvaluator().evaluate(base_manifest=base.manifest, candidate_manifest=candidate.manifest,
                baseline_bundle_id=base.id, candidate_bundle_id=candidate.id, cases=[case], runner=runner.runner(config), judge=runner.judge(config), suite_digest=suite["suite_digest"])
            summarized = {"status": report["outcome"], "report_digest": report["report_digest"], "cost_source": "fixture/simulated",
                "cost_microusd": report["cost_microusd"], "bindings": runner.bindings, "blind_judges": runner.blind_records,
                "prompt_inputs": [{key: row[key] for key in ("case_id", "base_prompt_digest", "candidate_prompt_digest", "input_digest", "single_allowed_fragment")} for row in report["records"]]}
            _write(ACCEPTANCE / "logs/paired-judge-bindings.json", summarized)
            db.close()
        finally:
            monkeypatch.undo()
    first = json.loads((ACCEPTANCE / "baseline-final-01/report.json").read_text(encoding="utf-8"))
    second = json.loads((ACCEPTANCE / "baseline-final-02/report.json").read_text(encoding="utf-8"))
    deterministic = all(a["semantic_digest"] == b["semantic_digest"] and a["input_digests"] == b["input_digests"] for a, b in zip(first["records"], second["records"]))
    actual = json.loads((ACCEPTANCE / "actual-comparison/comparison.json").read_text(encoding="utf-8"))
    regression = json.loads((ACCEPTANCE / "regression-comparison/comparison.json").read_text(encoding="utf-8"))
    if not deterministic or first["passed"] != 8 or second["passed"] != 8 or regression["outcome"] != "FAIL":
        raise SystemExit("Replay semantic/regression gate failed")
    freeze_suite(ACCEPTANCE, suite["cases"], fixtures)
    _write(ACCEPTANCE / "comparison.json", actual)
    (ACCEPTANCE / "comparison.md").write_text(comparison_markdown(actual), encoding="utf-8")
    evidence = {"date": "2026-10-09", "engineering_status": "PASS", "candidate_outcome": actual["outcome"],
        "release_eligible": False, "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty_at_report": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
        "version_binding": "Execution source hashes in each run are authoritative; enclosing implementation commit is recorded separately.",
        "test_batches": selected, "unique_passed_nodes": len(set(actual_nodes)), "rp_contracts": mapping,
        "suite_digest": suite["suite_digest"], "candidate_manifest_digest": json.loads((ACCEPTANCE / "candidate-final/report.json").read_text(encoding="utf-8"))["manifest_digest"],
        "two_runs_semantically_identical": deterministic, "paired_judge_evidence": "logs/paired-judge-bindings.json",
        "paid_model_calls": 0, "offline_cost_source": "fixture/simulated", "unknown_cost": None,
        "pg_cost_evidence": "test_rp12_rp15_rp18_research_pair_pg_owner_root_and_cost: four authoritative fixture attempts total 8 microusd",
        "regression_outcome": regression["outcome"], "regression_counts": regression["counts"], "actual_candidate_counts": actual["counts"],
        "limitations": ["No paid provider evaluation; no real-model effect or latency claim.", "No full repository test run.",
            "Legacy M2 dirty execution lacks per-source execution hashes; independent review documents this limit.",
            "controlled CLI refuses unauthorised paid launch; runner seam requires authorised managed evaluation context.",
            "SQLite evaluation roots are identity references; authoritative budgets tested only on guarded PostgreSQL."]}
    evidence["artifact_sha256"] = {path.relative_to(ACCEPTANCE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(ACCEPTANCE.rglob("*")) if path.is_file() and path.suffix in {".json", ".ndjson", ".log", ".xml", ".md"} and path.name != "evidence.json"}
    _write(ACCEPTANCE / "evidence.json", evidence)
    print(json.dumps({"engineering_status": "PASS", "tests": evidence["unique_passed_nodes"], "candidate_outcome": actual["outcome"]}))


if __name__ == "__main__":
    main()
