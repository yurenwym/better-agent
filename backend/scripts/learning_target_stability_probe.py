"""Target-stability probe: is a SKILL/IGNORE flip sampling noise or a contract defect?

Reproduces the exact JEV state `learning_pipeline._load_experience` builds for the
acceptance script's Case B (three failures that converge on one triage order), then
asks JEV the same question set N times and reports the target distribution.

The baseline question set (before the precedence rules were added to
`build_questions`) is kept here so the before/after difference stays reproducible
after the fix lands: `--baseline` uses the old `target` wording.

Usage:
    python scripts/learning_target_stability_probe.py                 # current contract
    python scripts/learning_target_stability_probe.py --baseline      # pre-fix wording
    PROBE_SAMPLES=10 python scripts/learning_target_stability_probe.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.learning_decision import (  # noqa: E402
    LearningDecision, TypesafeClient, build_questions, build_state, decision_from_experience_row,
)

SKILL_FAILURES = [
    "线上 HTTP 500，按 部署变更 → 慢查询 → 连接池 的顺序排查，最后停在连接池。",
    "又一次 database timeout，走同一套排查顺序，最后还是停在连接池。",
    "database timeout 再次出现，第三次走同样的排查顺序，结果仍然在连接池。",
]
SKILL_FAILURE_TRACES = [
    [{"step": "check_recent_deploy", "result": "no_recent_change"},
     {"step": "check_slow_queries", "result": "no_slow_query"},
     {"step": "check_connection_pool", "result": "pool_exhausted"}],
    [{"step": "check_recent_deploy", "result": "no_recent_change"},
     {"step": "check_slow_queries", "result": "no_slow_query"},
     {"step": "check_connection_pool", "result": "pool_too_small"}],
    [{"step": "check_recent_deploy", "result": "no_recent_change"},
     {"step": "check_slow_queries", "result": "no_slow_query"},
     {"step": "check_connection_pool", "result": "pool_exhausted"}],
]

#: The `target` question as it stood before the precedence rules were added. Kept
#: so the regression this probe found stays demonstrable.
BASELINE_TARGET = {
    "type": "choice",
    "instructions": "Choose the single surface the lesson belongs to. MEMORY = a fact, preference, constraint, decision or lesson the agent should know later. SKILL = a repeatable method or ordered procedure for a class of tasks. BEHAVIOR = a general defect in how the agent decides or acts, not tied to one fact or one procedure. IGNORE = nothing durable should be learned.",
    "criteria": {
        "MEMORY": "A durable fact, user preference, constraint or lesson to recall later.",
        "SKILL": "A repeatable method or ordered procedure reusable on similar future tasks.",
        "BEHAVIOR": "A general decision-making or acting defect that applies across tasks.",
        "IGNORE": "Nothing durable is worth learning from this experience.",
    },
}


def build_skill_state() -> dict:
    index = len(SKILL_FAILURES)
    row = {
        "id": f"experience_{index}",
        "task_type": "database_timeout_diagnosis",
        "outcome": "failure",
        "signal_type": "failure",
        "severity": "medium",
        "failure_tags_json": "[]",
        "root_task_id": f"experience_{index}",
        "runtime_bundle_id": "bundle_probe",
        "observed_at": "2026-09-21T00:00:00+00:00",
        "provenance": "observed",
        "evidence_json": json.dumps({"note": SKILL_FAILURES[index - 1],
                                     "tool_trace": SKILL_FAILURE_TRACES[index - 1]}, ensure_ascii=False),
        "source_content_hash": "probe",
    }
    summary = decision_from_experience_row(row)
    summary["task_result"] = {"outcome": "failure", "signal_type": "failure"}
    summary["recurrence"] = {"count": len(SKILL_FAILURES), "task_type": "database_timeout_diagnosis"}
    return build_state(
        summary,
        task_result=summary["task_result"],
        failure_tags=[],
        tool_trace=summary["tool_trace"],
        user_feedback=None,
        assets_summary={"skills": [], "behaviors": [], "memory": []},
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true",
                        help="use the pre-fix `target` wording instead of the live contract")
    parser.add_argument("--samples", type=int, default=int(os.getenv("PROBE_SAMPLES", "6")))
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    client = TypesafeClient(model=os.getenv("TYPESAFE_MODEL", "jev-latest"))
    state = build_skill_state()
    questions = build_questions()
    if args.baseline:
        questions = {**questions, "target": BASELINE_TARGET}

    rows = []
    for _ in range(args.samples):
        try:
            response = client.ask(state, questions)
            decision = LearningDecision.from_answers(
                response["answers"], model_identity=response["model"], min_target_confidence=0.5)
            rows.append({"target": decision.target, "confidence": round(decision.confidence, 3),
                         "learn": decision.learn, "probabilities": dict(decision.probabilities),
                         "reasons": list(decision.reason_codes)})
        except Exception as exc:  # noqa: BLE001 — a probe reports, it does not raise
            rows.append({"error": f"{type(exc).__name__}: {exc}"})

    counts: dict[str, int] = {}
    for row in rows:
        key = row.get("target") or row.get("error", "?")
        counts[key] = counts.get(key, 0) + 1

    label = "baseline (pre-fix wording)" if args.baseline else "current contract"
    print(f"=== {label}: {json.dumps(counts, ensure_ascii=False)}")
    for row in rows:
        print("   ", json.dumps(row, ensure_ascii=False))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({"label": label, "samples": args.samples, "counts": counts, "rows": rows},
                      handle, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
