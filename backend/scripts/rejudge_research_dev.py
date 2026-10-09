"""Rejudge saved answers in a restored isolated evaluation DB; never regenerate answers."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from app.behavior import BehaviorBundleService
from app.config import load_env_file, load_user_model_environment
from app.costs import CostService
from app.db import Database
from app.model_admin import ModelAdminService
from app.model_control import ModelControlStore
from app.model_input_snapshot_store import ModelInputSnapshotStore
from app.real_evaluation import SAFETY_JUDGE_PROMPT
from app.research_judgment import QUALITY_JUDGE_PROMPT, CORRECTNESS_JUDGE_PROMPT, JUDGE_VERSION, correctness_summary
from app.research_replay import ResearchEvaluationRunner, ReplayError, digest, SEED
from app.research_judge_checkpoint import JudgeCheckpoint
from run_research_dev import save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, help="Agent-authored contrast cases; expected labels are never sent")
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--budget-microusd", type=int, default=50_000)
    parser.add_argument("--continue-from", type=Path)
    parser.add_argument("--retry-invalid-case", action="append", default=[])
    parser.add_argument("--quality-max-tokens", type=int, default=4096)
    parser.add_argument("--retry-failed-invocation", action="append", default=[],
                        help="Explicitly retry a settled FAILED invocation in the copied checkpoint; preserves its history")
    args = parser.parse_args()
    if args.retry_failed_invocation and not args.continue_from:
        raise ValueError("explicit failed-call retry requires --continue-from")
    if not 0 < args.budget_microusd <= 300_000 or args.quality_max_tokens not in (4096, 8192):
        raise ValueError("budget/output limit exceeds controlled experiment remainder")
    load_env_file(Path(__file__).resolve().parents[2] / ".env")
    load_user_model_environment()
    os.environ["BETTER_AGENT_COST_MODE"] = "enforce"
    target = os.environ["RESEARCH_DEV_DATABASE_URL"]
    parsed = urlsplit(target)
    if (parsed.scheme != "postgresql" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not parsed.path.endswith("_test") or parsed.query):
        raise ValueError("isolated local *_test database required")
    old = json.loads((args.source / "results.json").read_text(encoding="utf-8"))
    frozen = json.loads((args.source / "frozen-input.json").read_text(encoding="utf-8"))
    cases = frozen["cases"]
    if digest(cases) != old["suite_digest"] or len(cases) != 20:
        raise ValueError("original frozen cases do not match")
    records = {row["case_id"]: row for row in old["records"]}
    if len(records) != len(cases) or any(case["id"] not in records for case in cases):
        raise ValueError("incomplete original answers")
    expectations = {}
    if args.diagnostics:
        diagnostics = json.loads(args.diagnostics.read_text(encoding="utf-8"))
        original_cases = {case["id"]: case for case in cases}
        new_cases, new_records = [], {}
        for spec in diagnostics["cases"]:
            for reverse in (False, True):
                identity = spec["id"] + ("-reversed" if reverse else "-forward")
                # Keep arm-to-side mapping identical so reversing answers really
                # tests left/right position rather than cancelling randomization.
                while int(digest([SEED, identity])[:8], 16) % 2 != 0:
                    identity += "x"
                case = {**original_cases[spec["source_case"]], "id": identity}
                case.update(spec.get("context_override", {}))
                answers = [spec["a"], spec["b"]]
                answers = [records[spec["source_case"]][x["original_arm"]]["text"] if "original_arm" in x else x["text"] for x in answers]
                expected = list(spec["expected"])
                if reverse:
                    answers.reverse()
                    expected.reverse()
                new_cases.append(case)
                new_records[identity] = {"case_id": identity, "baseline": {"text": answers[0]}, "candidate": {"text": answers[1]}, "winner": None}
                expectations[identity] = dict(zip(("baseline", "candidate"), expected))
        cases, records = new_cases, new_records
        frozen = {"description": "代理整理的诊断对照，非真人标签，非独立泛化评测", "cases": cases}
    args.out.mkdir(parents=True, exist_ok=False)
    previous = json.loads((args.continue_from / "results.json").read_text(encoding="utf-8")) if args.continue_from else None
    if previous and (previous["status"] not in {"STOPPED", "COMPLETED_DEV_HUMAN_REVIEW_PENDING"} or previous["suite_digest"] != digest(cases)
                     or previous["judge_version"] != JUDGE_VERSION or not args.thinking):
        raise ValueError("continuation requires matching stopped thinking experiment")
    db = Database(target, workspace=args.out / "workspace")
    config = dict(old["config"])
    config.update(quality_thinking=args.thinking, quality_max_tokens=args.quality_max_tokens)
    owner = config["owner_id"]
    costs = CostService(db)
    budget = costs.create_root_budget(owner, "evaluation", owner + ":" + JUDGE_VERSION + ":" + args.out.name,
        max_attempts=4 * len(cases), limit_microusd=args.budget_microusd,
        deadline_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    config["root_budget_id"] = budget["id"]
    bundles = BehaviorBundleService(db)
    manifest = dict(bundles.get(config["evaluator_bundle_id"]).manifest)
    if args.quality_max_tokens > 4096:
        from app.costs import PriceSnapshot
        admin = ModelAdminService(db, owner_id=owner)
        profile = admin.version(config["quality_judge_model_id"])
        profile.update(max_output_tokens=args.quality_max_tokens, timeout_seconds=180)
        version = next((item for item in admin.get_profile(profile["profile_id"])["versions"]
                        if item["max_output_tokens"] == args.quality_max_tokens and item["timeout_seconds"] == 180
                        and item["status"] == "ACTIVE"), None)
        if version is None:
            version = admin.add_version(profile["profile_id"], profile)
        config["quality_judge_model_id"] = version["id"]
        price_id = args.out.name + ":quality-price"
        costs.register_price(version["id"], PriceSnapshot(price_id, 300_000, 6_000, 0, 1_200_000, 0),
                             source_url="https://api-docs.deepseek.com/quick_start/pricing")
        with db.connection() as connection:
            price_id = connection.execute("SELECT id FROM model_price_snapshots WHERE profile_version_id=? ORDER BY effective_at DESC LIMIT 1", (version["id"],)).fetchone()[0]
        config["price_snapshot_ids"] = {**config["price_snapshot_ids"], "quality_judge": price_id}
        route = admin.create_policy(args.out.name, {role: {"primary": config[key], "fallback": []}
            for role, key in (("researcher", "baseline_model_id"), ("judge_quality", "quality_judge_model_id"), ("judge_safety", "safety_judge_model_id"))})
        routing = {"policy_id": route["id"], "digest": route["policy_digest"]}
        manifest["model_routing"] = routing
        for arm in ("baseline", "candidate"):
            writer = dict(bundles.get(config[arm + "_bundle_id"]).manifest)
            writer["model_routing"] = routing
            config[arm + "_bundle_id"] = bundles.ensure(writer).id
    manifest.update(judge_prompts=[CORRECTNESS_JUDGE_PROMPT, QUALITY_JUDGE_PROMPT, SAFETY_JUDGE_PROMPT], judge_version=JUDGE_VERSION)
    manifest["judge_parameters"] = {"quality_thinking": args.thinking, "quality_max_tokens": args.quality_max_tokens}
    config["evaluator_bundle_id"] = bundles.ensure(manifest).id
    checkpoint_path = args.out / "judge-checkpoint.sqlite3"
    if previous and (args.continue_from / "judge-checkpoint.sqlite3").exists():
        if (previous["config"].get("quality_thinking") != args.thinking
                or previous["config"].get("quality_max_tokens") != args.quality_max_tokens):
            raise ValueError("checkpoint continuation requires identical Judge parameters")
        previous_manifest = bundles.get(previous["config"]["evaluator_bundle_id"]).manifest
        if previous_manifest.get("judge_prompts") != manifest["judge_prompts"]:
            raise ValueError("checkpoint continuation requires identical Judge prompts")
        config = {**previous["config"], "root_budget_id": budget["id"]}
        JudgeCheckpoint(args.continue_from / "judge-checkpoint.sqlite3").copy_to(checkpoint_path)
    runner = ResearchEvaluationRunner(ModelAdminService(db, owner_id=owner), ModelControlStore(db, costs=costs),
                                     judge_checkpoint=JudgeCheckpoint(checkpoint_path))
    for identity in args.retry_failed_invocation:
        runner.judge_checkpoint.retry_failed(identity, db, owner)
    for case_id in args.retry_invalid_case:
        if not previous or not any(r["case_id"] == case_id and r.get("evaluation_error") for r in previous["records"]):
            raise ValueError("only explicitly recorded invalid cases can be retried")
        runner.judge_checkpoint.retry_invalid_case(case_id)
    if previous:
        runner.blind_records.extend(previous["blind_records"])
    judge = runner.judge(config)
    report = {"status": "RUNNING", "scope": "人工合成的章节写作测试；仅重评已有回答",
        "source_results_digest": digest(old), "suite_digest": digest(cases), "config": config,
        "judge_version": JUDGE_VERSION, "cost_cap_microusd": args.budget_microusd, "attempt_cap": 4 * len(cases),
        "human_calibration": "PENDING", "release_eligible": False, "records": []}
    if previous:
        report.update(records=[r for r in previous["records"] if r["case_id"] not in args.retry_invalid_case], continued_from=str(args.continue_from),
                      previous_results_digest=digest(previous), previous_cost_microusd=previous["charged_or_reserved_microusd"])
    save(args.out / "frozen-input.json", frozen)
    try:
        priority = {"meal-waste": 0, "membership-usage": 1, "appliance-energy": 2}
        for case in sorted(cases, key=lambda item: priority.get(item["id"], 3)):
            if any(row["case_id"] == case["id"] for row in report["records"]):
                continue
            report["current_case"] = case["id"]
            save(args.out / "results.json", report)
            original = records[case["id"]]
            try:
                decision = judge({"case": case, "baseline": original["baseline"]["text"], "candidate": original["candidate"]["text"]})
            except ReplayError as exc:
                if str(exc) not in {"INVALID_BLIND_JUDGE", "JUDGE_OUTPUT_TRUNCATED"}:
                    raise
                report["records"].append({**original, "winner": None, "evaluation_error": str(exc),
                    "baseline_correctness": {"verdict": "unassessed"}, "candidate_correctness": {"verdict": "unassessed"}})
                print(json.dumps({"case": case["id"], "error": str(exc)}), flush=True)
                continue
            report["records"].append({**original, **decision, "previous_winner": original["winner"],
                "judge_cost_microusd": decision["cost_microusd"]})
            print(json.dumps({"case": case["id"], "done": len(report["records"]),
                "baseline": decision["baseline_correctness"]["verdict"],
                "candidate": decision["candidate_correctness"]["verdict"]}), flush=True)
        report["status"] = "COMPLETED_DEV_HUMAN_REVIEW_PENDING"
    except BaseException as exc:
        report.update(status="STOPPED", error_type=type(exc).__name__)
        raise
    finally:
        report["correctness"] = correctness_summary(report["records"])
        if expectations:
            report["diagnostics"] = {"label_origin": "coding_agent_not_human", "expected": expectations,
                "mismatches": [{"case_id": row["case_id"], "arm": arm, "expected": expectations[row["case_id"]][arm],
                    "actual": row[arm + "_correctness"]["verdict"]} for row in report["records"]
                    for arm in ("baseline", "candidate") if row[arm + "_correctness"]["verdict"] != expectations[row["case_id"]][arm]]}
        report["blind_records"] = runner.blind_records
        report["bindings"] = runner.bindings
        with db.connection() as connection:
            report["attempts"] = [dict(row) for row in connection.execute(
                "SELECT a.id,a.invocation_id,a.status,a.cost_microusd,a.cost_status FROM model_attempts a "
                "JOIN model_invocations i ON i.id=a.invocation_id WHERE i.root_budget_id=?", (budget["id"],))]
            report["charged_or_reserved_microusd"] = int(connection.execute(
                "SELECT COALESCE(SUM(reserved_microusd+charged_microusd),0) FROM cost_budgets WHERE owner_id=? AND period_kind='ROOT' AND period_key=?",
                (owner, budget["id"])).fetchone()[0])
        store = ModelInputSnapshotStore(db)
        save(args.out / "trajectories.json", [{"invocation_id": row["id"],
            "input": store.load_for_invocation(owner, row["id"]).envelope()} for row in runner.bindings])
        save(args.out / "results.json", report)
        mapping = {row["case_id"]: row["left_is_baseline"] for row in runner.blind_records}
        labels, worksheet = [], ["# 人工核对：已有回答", "先分别判定 pass/fail/uncertain，再比较 left/right/tie；记录错误原句和依据。"]
        for case in cases:
            if case["id"] not in mapping:
                continue
            row = records[case["id"]]
            left, right = (row["baseline"], row["candidate"]) if mapping[case["id"]] else (row["candidate"], row["baseline"])
            worksheet.extend([f"## {case['id']}：{case['heading']}", case["thesis"], case["prior_summary"],
                json.dumps(case["evidence"], ensure_ascii=False), case["rubric"]["quality"],
                "### left", left["text"], "### right", right["text"],
                "left 正确性：____；right 正确性：____；胜负：____；错误原句及依据：____"])
            labels.append({"case_id": case["id"], "winner": None, "left_correctness": None, "right_correctness": None, "reason": ""})
        save(args.out / "human-labels.json", labels)
        (args.out / "human-review.md").write_text("\n\n".join(worksheet), encoding="utf-8")
        db.close()


if __name__ == "__main__":
    main()
