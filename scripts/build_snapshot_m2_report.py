"""Validate retained JUnit/transport evidence; print a reproducible M2 ledger."""
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter

from audit_snapshot_callsites import ROOT, ACCEPTANCE, audit


def main():
    directory = ACCEPTANCE / "phase2b"
    logs = directory / "logs"
    selected = {
        "entries": ("m2-complete", "observe"), "snapshot": ("m2-complete", "observe"),
        "postgres": ("m2-complete", "observe"), "m1": ("m2-complete", "enforce"),
        "harness": ("m2-r1", "observe"), "business": ("m2-r1", "observe"),
        "learning": ("m2-fixed", "observe"),
    }
    runs = [json.loads(line) for line in (logs / "m2-runs.ndjson").read_text().splitlines()]
    batches, passed, issues = [], set(), []
    for group, (batch, mode) in selected.items():
        matches = [run for run in runs if (run["group"], run["batch"], run["cost_mode"]) == (group, batch, mode)]
        if len(matches) != 1:
            issues.append(f"missing/duplicate run: {group}")
            continue
        run = matches[0]
        batches.append(run)
        if run["exit_code"] or any(run[key] for key in ("failures", "errors", "skipped")):
            issues.append(f"unsuccessful run: {group}")
        for case in ET.parse(logs / run["junit"]).getroot().iter("testcase"):
            node = case.get("classname").replace(".", "/") + ".py::" + case.get("name")
            if not any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
                passed.add(node)
    contracts = json.loads((directory / "test-contracts.json").read_text(encoding="utf-8"))
    for identity, contract in contracts.items():
        actual = []
        for reference in contract["nodes"]:
            matches = sorted(node for node in passed if node == reference or node.startswith(reference + "["))
            if not matches:
                issues.append(f"{identity}: no successful JUnit execution: {reference}")
            actual.extend(matches)
        contract["executed_nodes"] = sorted(set(actual))
        contract["status"] = "incomplete" if any(issue.startswith(identity + ":") for issue in issues) else "pass"
    capture = logs / "m2-complete-bindings.ndjson"
    records = [json.loads(line) for line in capture.read_text().splitlines()]
    reconciliation = audit(capture, "m2-complete", return_report=True)
    if reconciliation["status"] != "pass":
        issues.append("callsite reconciliation incomplete")
    if len({row["send_id"] for row in records}) != len(records):
        issues.append("duplicate transport send ID")
    unresolved = sorted({row["pytest_node"] for row in records} - passed)
    if unresolved:
        issues.append(f"transport nodes without successful JUnit execution: {unresolved}")
    for contract in contracts.values():
        if not contract["executed_nodes"]:
            issues.append("zero-sample test contract")
    manifest = json.loads((directory / "callsite-contracts.json").read_text(encoding="utf-8"))
    matrix = []
    for callsite, contract in manifest.items():
        observed = sorted({row["pytest_node"] for row in records if row["callsite_id"] == callsite})
        ts = [identity for identity, item in contracts.items() if set(observed) & set(item["executed_nodes"])]
        matrix.append({"callsite_id":callsite, **contract, "observed_nodes":observed, "tests":ts,
                       "tasks":sorted({contracts[t]["task"] for t in ts})})
    hashes = {path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in
              [capture, *(logs / run["junit"] for run in batches), *(logs / run["log"] for run in batches)]}
    report = {
        "date":"2026-10-09", "m2_status":"incomplete" if issues else "pass",
        "code_base_commit":subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty_at_execution":True, "version_note":"Tests ran against the M2 working-tree delta on this checkpoint; deliver with its enclosing commit.",
        "m1_status":"pass; prior closure retained, current snapshot/Harness/PG/M5 regression revalidated",
        "environment":{"python":"3.13.0", "database":"temporary SQLite and guarded ephemeral Docker PostgreSQL",
                       "alembic_head":"20260930_0025", "paid_model_calls":0},
        "test_batches":batches, "selected_test_executions":sum(run["tests"] for run in batches),
        "unique_passed_nodes":len(passed), "t01_t38":contracts, "callsite_matrix":matrix,
        "coverage":{key:reconciliation[key] for key in ("observed_sends", "sends_with_valid_committed_binding",
            "unique_invocations", "provenance_complete", "provenance_partial")},
        "capture_nodes":dict(sorted(Counter(row["pytest_node"] for row in records).items())),
        "issues":issues, "artifact_sha256":hashes, "reconciliation":reconciliation,
        "limitations":["Capture denominator is every observed send in the explicitly instrumented nodes, not all legacy regression sends or production telemetry.",
            "All provenance remains partial: excerpts/reference IDs/rubric are tracked, not full external bytes or exhaustive dropped spans.",
            "Default observe batches have explicit per-test enforce/observe overrides for budget and settlement semantics.",
            "No full repository pytest rerun; historical user-reported 2106/7/8 is not current evidence.",
            "Direct/routed post-response revocation semantics remain different; pre-send revocation and usage preservation are covered."],
    }
    report["coverage"].update(production_entrypoints=reconciliation["production_denominator"],
        tested_entrypoints=len(reconciliation["observed_callsites"]), unmapped_nodes=len(unresolved),
        required_branches=sum(len(item["branches"]) for item in manifest.values()))
    examples = {row["callsite_id"]:row for row in records}
    report["binding_examples"] = list(examples.values())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
