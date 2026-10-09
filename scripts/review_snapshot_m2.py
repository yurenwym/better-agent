"""Read-only independent audit of retained M2 evidence (never rewrites it)."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def review():
    directory = ROOT / "docs/acceptance/context-snapshot-phase2/phase2b"
    evidence = json.loads((directory / "evidence.json").read_text(encoding="utf-8"))
    contracts = json.loads((directory / "test-contracts.json").read_text(encoding="utf-8"))
    callsites = json.loads((directory / "callsite-contracts.json").read_text(encoding="utf-8"))
    issues, hashes, passed = [], [], set()
    for name, expected in evidence["artifact_sha256"].items():
        path = directory / "logs" / name
        workspace = hashlib.sha256(path.read_bytes()).hexdigest()
        committed_bytes = subprocess.check_output(["git", "show", f"15b4493:{path.relative_to(ROOT).as_posix()}"], cwd=ROOT)
        committed = hashlib.sha256(committed_bytes).hexdigest()
        hashes.append({"file": name, "expected": expected, "workspace": workspace, "commit_15b4493": committed})
        if workspace != expected and committed != expected:
            issues.append(f"unexplained digest: {name}")
    for run in evidence["test_batches"]:
        cases = list(ET.parse(directory / "logs" / run["junit"]).getroot().iter("testcase"))
        if run["exit_code"] or len(cases) != run["tests"]:
            issues.append(f"invalid batch: {run['junit']}")
        for case in cases:
            if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
                issues.append(f"unsuccessful selected execution: {case.get('name')}")
            else:
                passed.add(case.get("classname").replace(".", "/") + ".py::" + case.get("name"))
    def executed(node):
        return any(item == node or item.startswith(node + "[") for item in passed)
    source_assertions = {}
    for identity, contract in contracts.items():
        nodes = contract["nodes"]
        if not nodes or not all(executed(node) for node in nodes):
            issues.append(f"unexecuted contract: {identity}")
        for node in nodes:
            file, name = node.split("::")[:2]
            name = name.split("[")[0]
            tree = ast.parse((ROOT / "backend" / file).read_text(encoding="utf-8-sig"))
            functions = [item for item in ast.walk(tree) if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name]
            if not functions:
                issues.append(f"missing test source: {node}")
            source_assertions[node] = sum(isinstance(item, ast.Assert) for func in functions for item in ast.walk(func))
    rows = [json.loads(line) for line in (directory / "logs/m2-complete-bindings.ndjson").read_text(encoding="utf-8").splitlines()]
    if len({row["send_id"] for row in rows}) != len(rows):
        issues.append("duplicate send IDs")
    for row in rows:
        if not row["valid_committed_binding"] or row["callsite_id"] not in callsites or not executed(row["pytest_node"]):
            issues.append("unbound/unregistered/unexecuted send")
    for identity, contract in callsites.items():
        observed = {row["pytest_node"] for row in rows if row["callsite_id"] == identity}
        for branch, nodes in contract["branches"].items():
            if not any(any(item == node or item.startswith(node + "[") for item in observed) for node in nodes):
                issues.append(f"unobserved branch: {identity}/{branch}")
    examples = json.loads((directory / "binding-examples.json").read_text(encoding="utf-8"))
    if isinstance(examples, dict):
        examples = examples.get("examples", [])
    for example in evidence["binding_examples"]:
        if example not in rows:
            issues.append("unlinked binding example")
    return {"kind": "historical_evidence_review", "historical_checkpoint": evidence["code_base_commit"],
            "enclosing_commit": "15b44939a4e7122e0c65e4b35bbaa76307e65d22", "current_execution": False,
            "unique_passed_nodes": len(passed), "sends": len(rows), "invocations": len({r['invocation_id'] for r in rows}),
            "entry_groups": len(callsites), "hashes": hashes, "source_assertion_counts": source_assertions,
            "issues": issues, "status": "FAIL" if issues else "PASS"}


if __name__ == "__main__":
    result = review()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(bool(result["issues"]))
