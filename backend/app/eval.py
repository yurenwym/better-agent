from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import load_llm_ap
from .evals import ScenarioResult, SuiteReport, run_deterministic_suite
from .model_gateway import GatewayError, ModelGateway, ModelRequest


DEFAULT_LLM_AP = Path(r"D:\Users\王一鸣\Desktop\直到尽头\LLM_AP.txt")


@dataclass(frozen=True)
class EvaluationResult:
    report: SuiteReport
    json_path: Path
    markdown_path: Path


def run_evaluation(
    suite: str = "v1",
    mode: str = "deterministic",
    *,
    results_dir: str | Path | None = None,
    llm_ap_path: str | Path | None = None,
) -> EvaluationResult:
    if suite != "v1":
        raise ValueError("only the v1 suite is available")
    if mode == "deterministic":
        report = run_deterministic_suite()
        profile_view: dict[str, Any] = {}
    elif mode == "live":
        profile = load_llm_ap(llm_ap_path or DEFAULT_LLM_AP)
        profile_view = profile.public_view()
        report = _run_live_smoke(profile)
    else:
        raise ValueError("mode must be deterministic or live")

    root = Path(results_dir) if results_dir else Path(__file__).resolve().parents[2] / "evals" / "results"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = root / f"{stamp}.json"
    markdown_path = root / f"{stamp}.md"
    payload = {
        "suite": suite,
        "mode": mode,
        "passed": report.passed,
        "failed": report.failed,
        "profile": profile_view,
        "results": [asdict(result) for result in report.results],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    return EvaluationResult(report, json_path, markdown_path)


def compare_reports(baseline: str | Path, latest: str | Path) -> dict[str, Any]:
    old = json.loads(Path(baseline).read_text(encoding="utf-8"))
    new = json.loads(Path(latest).read_text(encoding="utf-8"))
    return {
        "baseline_passed": old.get("passed", 0),
        "latest_passed": new.get("passed", 0),
        "delta": new.get("passed", 0) - old.get("passed", 0),
    }


def _run_live_smoke(profile: Any) -> SuiteReport:
    try:
        response = asyncio.run(ModelGateway(profile).complete(ModelRequest(messages=[
            {"role": "system", "content": "返回一个只包含 status 字段的简短 JSON 对象。不要包含秘密或隐藏推理。"},
            {"role": "user", "content": "状态检查"},
        ], max_tokens=64)))
        detail = f"profile={profile.model}; attempts={response.attempts}; output_chars={len(response.message)}; manual quality scoring remains required"
        return SuiteReport((ScenarioResult(name="live-model-smoke", passed=bool(response.message.strip()), detail=detail),))
    except GatewayError as exc:
        return SuiteReport((ScenarioResult(name="live-model-smoke", passed=False, detail=f"gateway={exc.kind}; manual quality scoring required"),))


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# Better Agent {payload['suite']} evaluation",
        "",
        f"- Mode: `{payload['mode']}`",
        f"- Passed: **{payload['passed']}**",
        f"- Failed: **{payload['failed']}**",
        "",
        "| Scenario | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for result in payload["results"]:
        status = "PASS" if result["passed"] else "REVIEW"
        detail = str(result.get("detail", "")).replace("|", "\\|")
        lines.append(f"| `{result['name']}` | {status} | {detail} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Better Agent V1 evaluations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--suite", default="v1")
    run_parser.add_argument("--mode", choices=("deterministic", "live"), default="deterministic")
    run_parser.add_argument("--llm-ap", default=None)
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("baseline")
    compare_parser.add_argument("latest")
    args = parser.parse_args(argv)
    if args.command == "run":
        result = run_evaluation(args.suite, args.mode, llm_ap_path=args.llm_ap)
        print(json.dumps({"passed": result.report.passed, "failed": result.report.failed, "json": str(result.json_path), "markdown": str(result.markdown_path)}, ensure_ascii=False))
        return 0 if args.mode == "live" or result.report.failed == 0 else 1
    print(json.dumps(compare_reports(args.baseline, args.latest), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
