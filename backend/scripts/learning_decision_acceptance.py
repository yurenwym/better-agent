"""Real JEV acceptance for the V3 learning decision layer.

Runs the human-labelled `evals/cases/learning-decision-v3.json` suite against
the live TypeSafe System One endpoint and reports the precision-oriented metrics
the V3 design asks for. Nothing here mutates production assets: the decision
service writes only its append-only audit table.

    cd backend
    RUN_EVOLUTION_V3_LIVE=1 python scripts/learning_decision_acceptance.py

Without RUN_EVOLUTION_V3_LIVE=1 the script refuses to make paid network calls.
`--dry-run` exercises the scoring path against a local stub instead.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.learning_contract import TARGET_IGNORE, TARGETS  # noqa: E402
from app.learning_decision import (  # noqa: E402
    DEFAULT_MODEL,
    REASON_PROBES,
    DecisionSchemaError,
    LearningDecision,
    TypesafeClient,
    build_questions,
    build_state,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "evals" / "cases" / "learning-decision-v3.json"
DEFAULT_REPORT = ROOT / "evals" / "results" / "learning-decision-v3-report.json"

THRESHOLDS = {
    "macro_accuracy": (0.90, "min"),
    "learn_false_positive_rate": (0.05, "max"),
    "behavior_false_positive_rate": (0.03, "max"),
    "invalid_structure_rate": (0.01, "max"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_cases(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SystemExit(f"{path} contains no cases")
    for case in cases:
        if case.get("expected") not in TARGETS:
            raise SystemExit(f"case {case.get('id')} has an unknown expected target")
    return cases


def case_state(case: dict) -> dict:
    experience = dict(case.get("experience") or {})
    experience.setdefault("id", case["id"])
    recurrence = case.get("recurrence")
    if recurrence:
        experience["recurrence"] = recurrence
    return build_state(
        experience,
        task_result=case.get("task_result") or {},
        failure_tags=case.get("failure_tags") or [],
        tool_trace=case.get("tool_trace") or [],
        user_feedback=case.get("user_feedback"),
    )


class StubClient:
    """Deterministic offline stand-in used by --dry-run only."""

    def __init__(self, cases: list[dict]) -> None:
        self.expected = {case["id"]: case["expected"] for case in cases}

    def ask(self, state, questions):
        case_id = state["experience"].get("id")
        target = self.expected.get(case_id, TARGET_IGNORE)
        payload = {
            "learn": {"noul": 0.1 if target == TARGET_IGNORE else 0.9},
            "target": {"choice": target, "probabilities": {target: 0.9}, "confidence": 0.9},
            "risk": {"choice": "low"},
            "importance": {"score": 5.0, "probabilities": {}, "confidence": 0.9},
        }
        for name, _ in REASON_PROBES:
            payload[name] = {"noul": 0.1}
        return {"answers": payload, "model": "stub"}


def classify(client, case: dict, *, attempts: int = 3) -> dict:
    state = case_state(case)
    started = time.monotonic()
    last_error = None
    for attempt in range(attempts):
        try:
            response = client.ask(state, build_questions())
            break
        except Exception as exc:  # noqa: BLE001 — surfaced in the report, never silently ignored
            last_error = f"{type(exc).__name__}: {exc}"
            response = None
            if attempt < attempts - 1:
                # The endpoint is occasionally slow to hand-shake; a transport
                # retry is safe here because no decision has been recorded yet.
                time.sleep(2 ** attempt)
    if response is None:
        return {"id": case["id"], "expected": case["expected"], "predicted": None,
                "error": last_error, "latency_seconds": time.monotonic() - started}
    try:
        decision = LearningDecision.from_answers(response["answers"], model_identity=f"typesafe:{response.get('model')}")
        error = None
    except DecisionSchemaError as exc:
        decision = LearningDecision.ignored_default(reason="schema_invalid")
        error = str(exc)
    return {
        "id": case["id"], "expected": case["expected"], "predicted": decision.target,
        "learn": decision.learn, "subtype": decision.subtype, "confidence": decision.confidence,
        "support": decision.support,
        "risk": decision.risk, "importance": decision.importance, "reason_codes": list(decision.reason_codes),
        "schema_error": error, "latency_seconds": round(time.monotonic() - started, 3),
        "model_identity": decision.model_identity, "rationale": case.get("rationale", ""),
        "label_note": case.get("label_note", ""),
    }


def score(results: list[dict]) -> dict:
    total = len(results)
    errors = [item for item in results if item.get("error")]
    schema_failures = [item for item in results if item.get("schema_error")]
    usable = [item for item in results if item.get("predicted") is not None]
    if not usable:
        raise SystemExit("no usable JEV responses; nothing to score")

    confusion = {expected: Counter() for expected in TARGETS}
    for item in usable:
        confusion[item["expected"]][item["predicted"]] += 1

    per_target = {}
    for target in TARGETS:
        row = confusion[target]
        support = sum(row.values())
        correct = row.get(target, 0)
        per_target[target] = {
            "support": support,
            "correct": correct,
            "recall": round(correct / support, 4) if support else None,
            "predicted_as": {key: value for key, value in sorted(row.items()) if value},
        }
    recalls = [value["recall"] for value in per_target.values() if value["recall"] is not None]
    macro_accuracy = sum(recalls) / len(recalls) if recalls else 0.0

    non_ignore = [item for item in usable if item["expected"] != TARGET_IGNORE]
    ignore = [item for item in usable if item["expected"] == TARGET_IGNORE]
    learn_false_positive = [item for item in ignore if item["predicted"] != TARGET_IGNORE]
    behavior_false_positive = [item for item in non_ignore if item["predicted"] == "BEHAVIOR" and item["expected"] != "BEHAVIOR"]
    ignore_to_behavior = [item for item in ignore if item["predicted"] == "BEHAVIOR"]

    return {
        "cases": total,
        "scored": len(usable),
        "transport_errors": len(errors),
        "schema_failures": len(schema_failures),
        "macro_accuracy": round(macro_accuracy, 4),
        "per_target": per_target,
        "confusion": {expected: dict(sorted(row.items())) for expected, row in confusion.items()},
        "learn_false_positive_rate": round(len(learn_false_positive) / len(ignore), 4) if ignore else None,
        "behavior_false_positive_rate": round(len(behavior_false_positive) / len(non_ignore), 4) if non_ignore else None,
        "ignore_to_behavior_count": len(ignore_to_behavior),
        "invalid_structure_rate": round(len(schema_failures) / total, 4) if total else None,
        "learn_false_positive_ids": [item["id"] for item in learn_false_positive],
        "behavior_false_positive_ids": [item["id"] for item in behavior_false_positive],
        "ignore_to_behavior_ids": [item["id"] for item in ignore_to_behavior],
        "schema_failure_ids": [item["id"] for item in schema_failures],
        "transport_error_ids": [item["id"] for item in errors],
        "mean_latency_seconds": round(sum(item["latency_seconds"] for item in results) / total, 3),
    }


def gate(metrics: dict) -> dict:
    checks = {}
    for name, (limit, direction) in THRESHOLDS.items():
        value = metrics.get(name)
        if value is None:
            checks[name] = {"limit": limit, "direction": direction, "value": None, "pass": False}
            continue
        passed = value >= limit if direction == "min" else value <= limit
        checks[name] = {"limit": limit, "direction": direction, "value": value, "pass": passed}
    # Ignore→Behavior must be near zero: it is the most expensive routing error.
    checks["ignore_to_behavior_count"] = {"limit": 1, "direction": "max", "value": metrics["ignore_to_behavior_count"],
                                          "pass": metrics["ignore_to_behavior_count"] <= 1}
    checks["transport_errors"] = {"limit": 0, "direction": "max", "value": metrics["transport_errors"],
                                  "pass": metrics["transport_errors"] == 0}
    return {"checks": checks, "pass": all(item["pass"] for item in checks.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--limit", type=int, default=0, help="score only the first N cases")
    parser.add_argument("--dry-run", action="store_true", help="use a local stub instead of the live endpoint")
    args = parser.parse_args()

    from app.config import load_env_file

    load_env_file(ROOT / ".env")

    cases = load_cases(args.cases)
    if args.limit:
        cases = cases[: args.limit]

    if args.dry_run:
        client = StubClient(cases)
        mode = "dry-run"
    else:
        if os.getenv("RUN_EVOLUTION_V3_LIVE", "").strip() not in {"1", "true", "TRUE", "yes"}:
            print("refusing to call the live JEV endpoint: set RUN_EVOLUTION_V3_LIVE=1", file=sys.stderr)
            return 2
        if not os.getenv("TYPESAFE_API_KEY"):
            print("refusing to start: TYPESAFE_API_KEY is not configured", file=sys.stderr)
            return 2
        client = TypesafeClient(model=os.getenv("TYPESAFE_MODEL", DEFAULT_MODEL))
        mode = "live"

    results = []
    for index, case in enumerate(cases, 1):
        results.append(classify(client, case))
        print(f"[{index}/{len(cases)}] {case['id']}: expected={case['expected']} predicted="
              f"{results[-1].get('predicted')}", flush=True)

    metrics = score(results)
    verdict = gate(metrics)
    report = {
        "suite": "learning-decision-v3",
        "mode": mode,
        "model": os.getenv("TYPESAFE_MODEL", DEFAULT_MODEL) if mode == "live" else "stub",
        "created_at": _now(),
        "metrics": metrics,
        "gate": verdict,
        "results": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"metrics": metrics, "gate": verdict["pass"]}, ensure_ascii=False, indent=2))
    print(f"report: {args.report}")
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
