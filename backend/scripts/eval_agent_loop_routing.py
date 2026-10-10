"""Offline T00 routing report scaffold; never starts a model or a business service."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evals import load_routing_suite, run_routing_mock


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["legacy"], default="legacy")
    parser.add_argument("--provider", choices=["mock"], required=True)
    parser.add_argument("--cases", type=Path, default=root / "evals/cases/agent-loop-routing-v1.json")
    parser.add_argument("--freeze", type=Path, default=root / "docs/acceptance/agent-loop-m1/holdout-freeze.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    suite = load_routing_suite(args.cases, args.freeze)
    report = run_routing_mock(suite, mode=args.mode)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "planned": report["summary"]["planned"],
                      "routing_quality": report["routing_quality"]}))


if __name__ == "__main__":
    main()
