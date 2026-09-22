"""Does JEV's `choice` field agree with its own `probabilities` distribution?

Runs the frozen 80-case decision dataset once, records both the point estimate
(`choice`) and the full distribution, and scores each against the expected target.
One JEV call per case. Prints where they disagree.

This is a diagnostic, not part of the acceptance gate: it exists to decide whether
`from_answers` should trust `choice` or the distribution.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.learning_decision import (  # noqa: E402
    TypesafeClient, build_questions, build_state,
)

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "evals" / "cases" / "learning-decision-v3.json"
OUT = ROOT / "evals" / "results" / "learning-choice-vs-distribution.json"


def case_state(case: dict) -> dict:
    experience = dict(case.get("experience") or {})
    if case.get("recurrence"):
        experience["recurrence"] = case["recurrence"]
    return build_state(
        experience,
        task_result={"outcome": experience.get("outcome", ""), "signal_type": experience.get("signal_type", "")},
        failure_tags=case.get("failure_tags") or [],
        tool_trace=case.get("tool_trace") or [],
        user_feedback=case.get("user_feedback"),
        assets_summary={"skills": [], "behaviors": [], "memory": []},
    )


def argmax(probabilities: dict) -> str:
    if not probabilities:
        return ""
    return max(probabilities.items(), key=lambda item: (float(item[1]), item[0] == "IGNORE"))[0]


def main() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    client = TypesafeClient(model=os.getenv("TYPESAFE_MODEL", "jev-latest"))
    questions = build_questions()

    rows = []
    for case in cases:
        try:
            response = client.ask(case_state(case), questions)
            answers = response["answers"]
            target_answer = answers.get("target") or {}
            probabilities = {str(k): float(v) for k, v in (target_answer.get("probabilities") or {}).items()}
            rows.append({
                "id": case["id"],
                "expected": case["expected"],
                "choice": target_answer.get("choice"),
                "argmax": argmax(probabilities),
                "confidence": target_answer.get("confidence"),
                "probabilities": probabilities,
            })
        except Exception as exc:  # noqa: BLE001
            rows.append({"id": case["id"], "expected": case["expected"], "error": f"{type(exc).__name__}: {exc}"})

    usable = [row for row in rows if "error" not in row]
    agree = [row for row in usable if row["choice"] == row["argmax"]]
    choice_hits = sum(1 for row in usable if row["choice"] == row["expected"])
    argmax_hits = sum(1 for row in usable if row["argmax"] == row["expected"])

    print(f"cases={len(rows)} usable={len(usable)}")
    print(f"choice == argmax(probabilities): {len(agree)}/{len(usable)}")
    print(f"accuracy by choice : {choice_hits}/{len(usable)} = {choice_hits / max(len(usable), 1):.4f}")
    print(f"accuracy by argmax : {argmax_hits}/{len(usable)} = {argmax_hits / max(len(usable), 1):.4f}")

    disagreements = [row for row in usable if row["choice"] != row["argmax"]]
    print(f"\n--- disagreements ({len(disagreements)}) ---")
    for row in disagreements[:25]:
        print(f"  {row['id']:>14} expected={row['expected']:<8} choice={row['choice']:<8} "
              f"argmax={row['argmax']:<8} conf={row['confidence']} probs={row['probabilities']}")

    print("\n--- per-target choice vs argmax ---")
    for target in ("MEMORY", "SKILL", "BEHAVIOR", "IGNORE"):
        subset = [row for row in usable if row["expected"] == target]
        if not subset:
            continue
        c = sum(1 for row in subset if row["choice"] == target)
        a = sum(1 for row in subset if row["argmax"] == target)
        print(f"  {target:<9} n={len(subset):<3} choice={c}/{len(subset)}  argmax={a}/{len(subset)}")

    print("\nchoice distribution:", Counter(row["choice"] for row in usable))
    OUT.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
