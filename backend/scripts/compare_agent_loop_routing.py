"""Produce the complete routing comparison from durable run reports."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.evals import load_routing_suite
from app.routing_comparison import compare_routing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--safety-failures', type=int)
    parser.add_argument('--token-explanation')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    suite = load_routing_suite(root / 'evals/cases/agent-loop-routing-v1.json', root / 'docs/acceptance/agent-loop-m1/holdout-freeze.json')
    reports = [json.loads((args.input / (mode + '.json')).read_text(encoding='utf-8')) for mode in ('legacy', 'loop')]
    report = compare_routing(suite, *reports, safety_failures=args.safety_failures, token_explanation=args.token_explanation)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = ['# AgentLoop 路由对比', '', '效果结论：' + report['routing_quality'], '',
             '工程结论：由独立回归结果记录，本报告不推断。', '', '| 指标 | legacy | loop |', '|---|---:|---:|']
    lines.extend(f'| {key} | {value} | {report["loop"][key]} |' for key, value in report['legacy'].items())
    lines += ['', '## 门禁', ''] + [f'- {key}: {value}' for key, value in report['gates'].items()]
    lines += ['', '## 逐案例', '', '| 案例 | legacy | loop | legacy命中 | loop命中 |', '|---|---|---|---|---|']
    lines.extend(f'| {r["case_id"]} | {r["legacy_observed"]} | {r["loop_observed"]} | {r["legacy_matched"]} | {r["loop_matched"]} |' for r in report['cases'])
    (args.output / 'comparison.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('routing_quality', 'gates', 'legacy', 'loop')}, indent=2))


if __name__ == '__main__':
    main()
