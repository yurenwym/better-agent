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
    "AgentRuntime._finish_exposure": ["CS-GR-02"],
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


def audit():
    inventory = (ACCEPTANCE / "callsite-inventory.md").read_text(encoding="utf-8")
    ids = set(re.findall(r"\| (CS-[A-Z]+-\d+) \|", inventory))
    records = [json.loads(line) for line in (ACCEPTANCE / "phase2b/logs/observed-bindings.ndjson").read_text().splitlines() if line.strip()]
    seen = {row["callsite_id"] for row in records}
    evidence = json.loads((ACCEPTANCE / "phase2b/evidence.json").read_text(encoding="utf-8"))
    refs = set(re.findall(r'tests/[\w/]+\.py::[\w]+(?:\[[^"\n]+\])?', json.dumps(evidence)))
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
    report = {"static_candidates": static, "unclassified_candidates": unknown,
              "static_callsites_missing_registration": unregistered, "registered_callsites": sorted(ids),
              "observed_callsites": sorted(seen), "registered_without_recorded_send": sorted(ids - seen),
              "unregistered_sends": sorted(seen - ids), "invalid_binding_records": missing_binding,
              "unresolved_nodes": unresolved, "collection_exit": result.returncode,
              "production_denominator": None,
              "status": "incomplete: candidate reachability and complete per-entry evidence still required"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    incomplete = (report["production_denominator"] is None or unknown or unregistered
                  or ids - seen or seen - ids or missing_binding or unresolved or result.returncode)
    return int(bool(incomplete))


if __name__ == "__main__":
    raise SystemExit(audit())
