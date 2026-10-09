"""Read-only Phase 2B candidate/registration/transport/node reconciliation.

Static candidates are not a production coverage denominator: dynamic dispatch
and shared adapters still require the documented reachability audit.
"""
import ast
import json
from pathlib import Path
import re
import subprocess
import sys
import argparse
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "docs/acceptance/context-snapshot-phase2"

# Stable symbols, not line numbers. Several logical entries share one adapter.
SYMBOLS = {
    "ManagedAgentWorker._execute_claimed": ["CS-AG-02"],
    "LiveExpertModel.execute_bundle": ["CS-AG-01"],
    "LiveExpertModel.synthesize_user_task": ["CS-AG-01"],
    "LiveExpertModel.synthesize": ["CS-AG-01"],
    "ManagedTurnWorker._finish_exposure": ["CS-CA-02"],
    "LiveBehaviorRunner._run": ["CS-EV-01"],
    "LivePromptCandidateProposer._propose": ["CS-EV-04"],
    "LiveSafetyJudge.judge": ["CS-AG-02", "CS-RS-03", "CS-GR-02", "CS-CA-02"],
    "GoalProgramCompiler._validated": ["CS-GP-01", "CS-GP-02", "CS-GP-03"],
    "LearningAgent.generate_async": ["CS-LN-01"],
    "LearningJudge.compare_async": ["CS-LN-01"],
    "ConstraintExtractor.extract": ["CS-LN-01"],
    "LearningReplay.call": ["CS-EV-02"],
    "RuntimeLearningReplay.__call__.call": ["CS-EV-02"],
    "_complete_logical_call": ["CS-CA-01"],
    "LiveRuntimeModel._json": ["CS-GR-01", "CS-GR-03"],
    "LiveConversationModel._complete": ["CS-CA-01"],
    "LiveEpisodeSummarizer.__call__": ["CS-MA-01"],
    "resolve": ["CS-MR-01"],
    "ModelAdminService._verify_live": ["CS-MD-01"],
    "LiveEvaluationRunner._arm.invoke": ["CS-EV-03"],
    "LiveEvaluationRunner._judge.invoke": ["CS-EV-03"],
    "LiveResearchModel._complete": ["CS-RS-01", "CS-RS-02"],
    "ManagedResearchWorker._finish_exposure": ["CS-RS-03"],
    "ResearchEvaluationRunner.runner.invoke": ["CS-EV-03"],
    "ResearchEvaluationRunner.judge.invoke": ["CS-EV-03"],
    "SourceBoundGateway.complete": ["CS-EV-03"],
    "execute_case": ["CS-EV-03"],
    "AgentRuntime._finish_exposure": ["CS-GR-02"],
}

# Construction and wire sites are audited independently of complete() calls.
# New sites fail closed until their production reachability is reviewed.
ASSEMBLY = {
    "build_runtime": "production routed gateway with runtime-owned control_store",
    "ModelAdminService._verify_live": "controlled direct verification, server-owned service identity",
    "LiveEvaluationRunner._gateway": "controlled direct evaluation, persisted evaluation config identity",
    "run_offline": "CLI-only synthetic Research evaluation; isolated database and explicit scripted provider",
    "ResearchEvaluationRunner._preflight": "read-only routed profile validation for frozen evaluation configuration",
    "RoutedModelGateway._execute_http_attempt": "internal transport of an already bound invocation",
    "provider_payload": "pure protocol rendering, no I/O",
}
HTTP = {
    "model_gateway.py": "provider transport; reachable only through controlled complete or routed internal attempt",
    "embedding.py": "embedding vector API, not a generative completion",
    "learning_decision.py": "JEV structured decision service, excluded by Phase 2B scope",
    "notifications.py": "notification webhook, not model generation",
    "tavily.py": "source retrieval API, not model generation",
    "web.py": "search/source retrieval, not model generation",
}


def candidates(root=ROOT):
    found = []
    for path in sorted((root / "backend/app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))

        class Visitor(ast.NodeVisitor):
            scope = ()

            def visit_ClassDef(self, node):
                self.enter(node)

            def visit_FunctionDef(self, node):
                self.enter(node)

            visit_AsyncFunctionDef = visit_FunctionDef

            def enter(self, node):
                old = self.scope
                self.scope = (*old, node.name)
                self.generic_visit(node)
                self.scope = old

            def visit_Call(self, node):
                name = ast.unparse(node.func)
                if (isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "complete", "route_and_respond", "judge", "_attempt", "_execute_http_attempt",
                }) or name == "_complete_logical_call":
                    symbol = ".".join(self.scope)
                    exclusion = None
                    if name in {"self.service.complete", "ModelDecision.complete"}:
                        exclusion = "state/artifact completion, not a provider call"
                    elif path.name in {"eval.py", "evals.py"}:
                        exclusion = "CLI-only offline/evaluation entry, not API production assembly"
                    elif symbol in {"RoutedModelGateway._execute_http_attempt", "ModelGateway.complete"}:
                        exclusion = "shared internal transport; counted at logical caller"
                    found.append({"file": path.relative_to(root).as_posix(), "symbol": symbol,
                                  "line": node.lineno, "call": name, "excluded_reason": exclusion,
                                  "callsites": [] if exclusion else SYMBOLS.get(symbol, [])})
                self.generic_visit(node)

        Visitor().visit(tree)
    return found


def assembly_candidates(root=ROOT):
    found = []
    for path in sorted((root / "backend/app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            constructor = name in {"ModelGateway", "RoutedModelGateway"}
            wire = isinstance(node.func, ast.Attribute) and node.func.attr in {"post", "stream", "request", "get"} and (
                ast.unparse(node.func.value) in {"client", "self._client", "session", "httpx", "requests"})
            if not constructor and not wire:
                continue
            scope, ancestor = [], parents.get(node)
            while ancestor is not None:
                if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    scope.insert(0, ancestor.name)
                ancestor = parents.get(ancestor)
            symbol = ".".join(scope)
            reason = ("explicit CLI/test-only assembly, not reachable from API startup" if path.name in {"eval.py", "evals.py"}
                      else ASSEMBLY.get(symbol) if constructor else HTTP.get(path.name))
            keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in node.keywords}
            found.append({"file":path.relative_to(root).as_posix(), "symbol":symbol,"line":node.lineno,
                "call":name,"disposition":reason,"keywords":keywords if constructor else {}})
    return found


def audit(records_path=None, batch_id=None, *, return_report=False):
    inventory = (ACCEPTANCE / "callsite-inventory.md").read_text(encoding="utf-8")
    ids = set(re.findall(r"\| (CS-[A-Z]+-\d+) \|", inventory))
    paths = records_path if isinstance(records_path, list) else [records_path or ACCEPTANCE / "phase2b/logs/observed-bindings.ndjson"]
    records = [json.loads(line) for path in paths for line in path.read_text().splitlines() if line.strip()]
    if batch_id:
        records = [row for row in records if row.get("batch_id") == batch_id]
    seen = {row["callsite_id"] for row in records}
    evidence = json.loads((ACCEPTANCE / "phase2b/test-contracts.json").read_text(encoding="utf-8"))
    refs = set(re.findall(r'tests/[\w/]+\.py::[\w]+(?:\[[^"\n]+\])?', json.dumps(evidence)))
    refs.update(row["pytest_node"] for row in records if row.get("pytest_node"))
    files = sorted({ref.split("::")[0] for ref in refs})
    result = subprocess.run([sys.executable, "-m", "pytest", *files, "--collect-only", "-q"],
                            cwd=ROOT / "backend", capture_output=True, text=True, encoding="utf-8", errors="replace")
    nodes = {line.strip() for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line}
    unresolved = sorted(ref for ref in refs if not any(node == ref or node.startswith(ref + "[") for node in nodes))
    missing_binding = [index for index, row in enumerate(records) if not all(row.get(key) for key in
                       ("invocation_id", "attempt_id", "snapshot_id", "snapshot_digest", "owner_id", "profile_version_id"))]
    static = candidates()
    unknown = [row for row in static if not row["excluded_reason"] and not row["callsites"]]
    unregistered = sorted({entry for row in static for entry in row["callsites"]} - ids)
    assembly = assembly_candidates()
    unknown_assembly = [row for row in assembly if not row["disposition"]]
    unique = {row["invocation_id"]: row for row in records if row.get("invocation_id")}
    provenance = Counter(row["provenance_status"] for row in unique.values())
    invalid = [index for index, row in enumerate(records) if row.get("valid_committed_binding") is not True]
    reachability = json.loads((ACCEPTANCE / "phase2b/callsite-contracts.json").read_text(encoding="utf-8"))
    manifest_ids = set(reachability)
    missing_branches = []
    for callsite, contract in reachability.items():
        for branch, required in contract["branches"].items():
            if not any(row.get("callsite_id") == callsite and any(row.get("pytest_node") == node or row.get("pytest_node", "").startswith(node + "[") for node in required) for row in records):
                missing_branches.append(f"{callsite}:{branch}")
    report = {"static_candidates": static, "unclassified_candidates": unknown,
              "static_callsites_missing_registration": unregistered, "registered_callsites": sorted(ids),
              "observed_callsites": sorted(seen), "registered_without_recorded_send": sorted(ids - seen),
              "unregistered_sends": sorted(seen - ids), "invalid_binding_records": missing_binding,
              "unresolved_nodes": unresolved, "collection_exit": result.returncode,
              "assembly_candidates": assembly, "unclassified_assembly": unknown_assembly,
              "manifest_registration_difference": sorted(manifest_ids ^ ids),
              "missing_branch_evidence": missing_branches,
              "production_denominator": len(ids) if not (unknown or unknown_assembly or manifest_ids ^ ids) else None,
              "observed_sends": len(records), "sends_with_valid_committed_binding": len(records)-len(invalid),
              "invalid_audits": invalid, "unique_invocations": len(unique),
              "provenance_complete": provenance["complete"], "provenance_partial": provenance["partial"],
              "scope": "explicit acceptance capture batch, not long-term production telemetry"}
    incomplete = (report["production_denominator"] is None or unknown or unregistered
                  or ids - seen or seen - ids or missing_binding or unresolved or result.returncode
                  or missing_branches or invalid or not records)
    report["status"] = "incomplete" if incomplete else "pass"
    if return_report:
        return report
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(bool(incomplete))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=Path)
    parser.add_argument("--batch-id")
    args = parser.parse_args()
    raise SystemExit(audit(args.records, args.batch_id))
