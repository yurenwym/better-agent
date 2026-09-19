"""Offline business acceptance of captured application outputs, not model smoke tests."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

from .goal_program_compiler import validate_program_structure, validate_daily_review, validate_period_review


DEFAULT_SUITE = Path(__file__).resolve().parents[2] / "evals" / "cases" / "planning-v1.json"
DIMENSIONS = ("actionability", "faithfulness", "personalization")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_suite(path=DEFAULT_SUITE):
    suite = json.loads(Path(path).read_text(encoding="utf-8"))
    ids = [case["id"] for case in suite["cases"]]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("case IDs must be nonempty and unique")
    return suite


def model_inputs(suite):
    # Never send rubrics or expected state to the model under evaluation.
    return [{"id": case["id"], "input": case["input"], "context": case["context"]} for case in suite["cases"]]


def check_case(case, output):
    errors = []
    if not isinstance(output.get("text"), str) or not output["text"].strip():
        errors.append("missing_text")
    if not isinstance(output.get("trace_ref"), str) or not output["trace_ref"].strip():
        errors.append("missing_trace")
    expected = case["expected"]
    observed = output.get("observed")
    if not isinstance(observed, dict):
        return [*errors, "missing_observed_state"]
    for key, value in expected.get("state", {}).items():
        if key not in observed or type(observed[key]) is not type(value) or observed[key] != value:
            errors.append(f"state:{key}")
    kind = expected.get("schema")
    try:
        if kind == "program":
            limits = case["context"]["limits"]
            validate_program_structure(observed.get("program"), limits["start_date"], limits["end_date"],
                                       limits["daily_minutes"], constraints=limits.get("schedule_constraints"))
        elif kind == "daily_review":
            validate_daily_review(observed.get("review"))
        elif kind == "period_review":
            validate_period_review(observed.get("review"))
    except (ValueError, TypeError, KeyError) as exc:
        errors.append(f"schema:{getattr(exc, 'code', type(exc).__name__)}")
    return errors


def score(suite, capture, ratings=None):
    if capture.get("suite_digest") != digest(suite):
        raise ValueError("capture suite digest mismatch")
    metadata = capture.get("metadata", {})
    if any(not isinstance(metadata.get(key), str) or not metadata[key].strip()
           for key in ("code_version", "model", "prompt_version")):
        raise ValueError("capture requires code_version, model and prompt_version")
    records = capture.get("outputs", [])
    outputs = {item["id"]: item for item in records}
    if len(outputs) != len(records) or set(outputs) - {case["id"] for case in suite["cases"]}:
        raise ValueError("duplicate or unknown output IDs")
    if ratings is not None and ratings.get("capture_digest") != digest(capture):
        raise ValueError("ratings must bind to the exact captured outputs")
    rating_items = (ratings or {}).get("ratings", [])
    by_id = {item["id"]: item for item in rating_items}
    if len(by_id) != len(rating_items) or set(by_id) - set(outputs):
        raise ValueError("duplicate or unknown rating IDs")
    results = []
    for case in suite["cases"]:
        output = outputs.get(case["id"])
        errors = check_case(case, output) if output is not None else ["missing_output"]
        rating = by_id.get(case["id"], {})
        scored = bool(rating.get("reviewer") and rating.get("rationale")) and all(
            type(rating.get(key)) is int and 1 <= rating[key] <= 5 for key in DIMENSIONS)
        status = "FAIL" if errors or (scored and min(rating[key] for key in DIMENSIONS) < 3) else "PASS" if scored else "NEEDS_REVIEW"
        results.append({"id": case["id"], "category": case["category"], "partition": case["partition"],
                        "status": status, "hard_errors": errors, "human_scored": scored})
    metrics = {}
    for field in ("latency_seconds", "cost_microusd", "model_calls"):
        values = [item.get(field) for item in outputs.values()]
        known = sorted(value for value in values if type(value) in (int, float) and math.isfinite(value) and value >= 0)
        metrics[field] = {"known": len(known), "missing": len(suite["cases"]) - len(known),
                          "p50": known[math.ceil(len(known)*.5)-1] if known else None,
                          "p95": known[math.ceil(len(known)*.95)-1] if known else None}
    return {"suite": suite["id"], "suite_digest": digest(suite), "capture_digest": digest(capture),
            "metadata": metadata, "results": results, "metrics": metrics,
            "passed": sum(item["status"] == "PASS" for item in results),
            "failed": sum(item["status"] == "FAIL" for item in results),
            "needs_review": sum(item["status"] == "NEEDS_REVIEW" for item in results),
            "quality_verified": all(item["status"] == "PASS" for item in results)}


def compare(baseline, candidate):
    if baseline["suite_digest"] != candidate["suite_digest"]:
        raise ValueError("cannot compare different suites")
    old = {item["id"]: item for item in baseline["results"]}
    new = {item["id"]: item for item in candidate["results"]}
    if set(old) != set(new):
        raise ValueError("cannot compare different case sets")
    return {"baseline_verified": baseline["quality_verified"], "candidate_verified": candidate["quality_verified"],
            "regressions": [key for key in old if old[key]["status"] == "PASS" and new[key]["status"] != "PASS"],
            "improvements": [key for key in old if old[key]["status"] != "PASS" and new[key]["status"] == "PASS"],
            "baseline_metrics": baseline["metrics"], "candidate_metrics": candidate["metrics"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    commands = parser.add_subparsers(dest="command", required=True)
    template = commands.add_parser("template")
    template.add_argument("--output", type=Path, required=True)
    scoring = commands.add_parser("score")
    scoring.add_argument("capture", type=Path)
    scoring.add_argument("--ratings", type=Path)
    scoring.add_argument("--output", type=Path, required=True)
    comparing = commands.add_parser("compare")
    comparing.add_argument("baseline", type=Path)
    comparing.add_argument("candidate", type=Path)
    args = parser.parse_args(argv)
    if args.command == "compare":
        report = compare(json.loads(args.baseline.read_text(encoding="utf-8")), json.loads(args.candidate.read_text(encoding="utf-8")))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["regressions"] or not report["candidate_verified"] else 0
    suite = load_suite(args.suite)
    if args.command == "template":
        report = {"suite_digest": digest(suite), "metadata": {"code_version": "", "model": "", "prompt_version": ""},
                  "inputs": model_inputs(suite),
                  "outputs": [{"id": case["id"], "text": "", "observed": {}, "trace_ref": ""} for case in suite["cases"]]}
        exit_code = 0
    else:
        report = score(suite, json.loads(args.capture.read_text(encoding="utf-8")),
                       json.loads(args.ratings.read_text(encoding="utf-8")) if args.ratings else None)
        exit_code = 0 if report["quality_verified"] else 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "quality_verified": report.get("quality_verified", False)}))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
