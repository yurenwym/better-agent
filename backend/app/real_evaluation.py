from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable


PARTITIONS = {"DISCOVERY", "DEV", "HOLDOUT", "SAFETY"}


class EvaluationAccessError(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256((value if isinstance(value, str) else _json(value)).encode("utf-8")).hexdigest()


class RealEvaluator:
    """Small, deterministic baseline/challenger evaluator with partition boundaries."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.suites = self.root / "suites"
        self.suites.mkdir(parents=True, exist_ok=True)

    def register_suite(self, suite_id: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
        if not suite_id.strip() or not cases:
            raise ValueError("suite_id and cases are required")
        normalized = []
        seen: set[str] = set()
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not isinstance(case.get("input"), str):
                raise ValueError("evaluation case requires id and input")
            case_id = case["id"]
            partition = str(case.get("partition", "DEV")).upper()
            if partition not in PARTITIONS or case_id in seen:
                raise ValueError("invalid or duplicate evaluation case")
            seen.add(case_id)
            normalized.append({"id": case_id, "partition": partition, "input": case["input"], "expected": case.get("expected")})
        normalized.sort(key=lambda item: item["id"])
        digest = _digest(normalized)
        payload = {"id": suite_id, "digest": digest, "cases": normalized}
        path = self.suites / f"{suite_id}.json"
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) != payload:
            raise ValueError("evaluation suite is immutable")
        path.write_text(_json(payload) + "\n", encoding="utf-8")
        return {"id": suite_id, "digest": digest, "case_count": len(normalized)}

    def _suite(self, suite_id: str) -> dict[str, Any]:
        path = self.suites / f"{suite_id}.json"
        if not path.exists():
            raise KeyError(suite_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def candidate_cases(self, suite_id: str) -> list[dict[str, Any]]:
        suite = self._suite(suite_id)
        return [case for case in suite["cases"] if case["partition"] in {"DISCOVERY", "DEV"}]

    def evaluate(
        self, *, suite_id: str, baseline_bundle_id: str, candidate_bundle_id: str, candidate_id: str,
        evaluator_digest: str, budget_units: int, baseline: Callable[[dict[str, Any]], Any], candidate: Callable[[dict[str, Any]], Any],
        model_config_digest: str = "", tool_schema_digest: str = "",
    ) -> dict[str, Any]:
        if budget_units <= 0:
            raise ValueError("budget_units must be positive")
        suite = self._suite(suite_id)
        baseline_results: list[tuple[dict[str, Any], Any]] = []
        candidate_results: list[tuple[dict[str, Any], Any]] = []
        for case in suite["cases"]:
            # Evaluated behavior receives only public input; partitions and answers stay evaluator-side.
            baseline_results.append((case, baseline({"input": case["input"]})))
            candidate_results.append((case, candidate({"input": case["input"]})))
        baseline_correct = sum(self._correct(case, value) for case, value in baseline_results)
        candidate_correct = sum(self._correct(case, value) for case, value in candidate_results)
        regressions = [case["id"] for (case, old), (_, new) in zip(baseline_results, candidate_results) if old != new and not self._correct(case, new)]
        safety_violations = sum(not self._correct(case, value) for case, value in candidate_results if case["partition"] == "SAFETY")
        report = {
            "bindings": {
                "candidate_id": candidate_id, "baseline_bundle_id": baseline_bundle_id, "candidate_bundle_id": candidate_bundle_id,
                "eval_set_digest": suite["digest"], "evaluator_digest": evaluator_digest, "budget_units": budget_units,
                "model_config_digest": model_config_digest, "tool_schema_digest": tool_schema_digest,
            },
            "baseline": {"cases": len(baseline_results), "correct": baseline_correct},
            "candidate": {"cases": len(candidate_results), "correct": candidate_correct},
            "metrics": {"quality_delta": candidate_correct - baseline_correct, "regressions": regressions, "safety_violations": safety_violations},
            "deterministic_pass": not regressions and safety_violations == 0 and candidate_correct >= baseline_correct,
            "trace_digest": _digest([[case["id"], str(old), str(new)] for (case, old), (_, new) in zip(baseline_results, candidate_results)]),
        }
        report["report_digest"] = _digest(report)
        return report

    @staticmethod
    def assert_approvable(report: dict[str, Any]) -> None:
        if not report.get("baseline"):
            raise EvaluationAccessError("baseline report is required")
        bindings = report.get("bindings", {})
        required = {"candidate_id", "baseline_bundle_id", "candidate_bundle_id", "eval_set_digest", "evaluator_digest", "model_config_digest", "tool_schema_digest", "budget_units"}
        if not required.issubset(bindings) or not report.get("report_digest"):
            raise EvaluationAccessError("evaluation binding is incomplete")
        if not report.get("deterministic_pass"):
            raise EvaluationAccessError("evaluation gates did not pass")

    @staticmethod
    def _correct(case: dict[str, Any], value: Any) -> bool:
        expected = case.get("expected")
        return expected is None or value == expected
