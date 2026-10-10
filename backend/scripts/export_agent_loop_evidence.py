"""Export non-secret ledger metadata for the isolated routing evaluation."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import Database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / 'backend/tests/integration'))
    from db_target_guard import assert_isolated_test_database
    url = os.environ['AGENT_LOOP_EVAL_DATABASE_URL']
    assert_isolated_test_database(url)
    db = Database(url, workspace=args.input / 'workspace')
    try:
        with db.connection() as connection:
            costs = dict(connection.execute('SELECT SUM(cost_microusd) known_cost_microusd,COUNT(*) attempts,COUNT(*) FILTER (WHERE cost_microusd IS NULL) unknown_cost_attempts FROM model_attempts').fetchone())
            prices = [dict(r) for r in connection.execute('SELECT DISTINCT p.* FROM model_price_snapshots p JOIN model_attempts a ON a.price_snapshot_id=p.id')]
            for mode in ('legacy', 'loop'):
                report = json.loads((args.input / (mode + '.json')).read_text(encoding='utf-8'))
                report['prices'] = prices
                report['model'] = 'deepseek-flash'
                report['reasoning'] = False
                report['max_output_tokens'] = 2048
                report['max_attempts'] = 1
                report['network_retries'] = 0
                report['cost_source'] = 'model_attempts; ESTIMATED_COMPLETE is conservative catalog pricing, not provider invoice'
                for row in report['cases']:
                    turn = row.get('turn_id')
                    if not turn:
                        continue
                    row['model_calls'] = connection.execute('SELECT COUNT(*) FROM model_invocations WHERE turn_id=?', (turn,)).fetchone()[0]
                    attempt_rows = [dict(r) for r in connection.execute('SELECT a.cost_microusd,a.cost_status,a.uncached_input_tokens,a.cache_read_tokens,a.cache_write_tokens,a.output_tokens,a.reasoning_tokens FROM model_attempts a JOIN model_invocations i ON i.id=a.invocation_id WHERE i.turn_id=?', (turn,))]
                    row['attempts'] = attempt_rows
                    row['tokens'] = sum(r[k] for r in attempt_rows for k in ('uncached_input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'output_tokens')) if all(r[k] is not None for r in attempt_rows for k in ('uncached_input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'output_tokens')) else None
                    row['cost_microusd'] = sum(r['cost_microusd'] for r in attempt_rows) if all(r['cost_microusd'] is not None for r in attempt_rows) else None
                args.output.mkdir(parents=True, exist_ok=True)
                (args.output / (mode + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
            (args.output / 'budget.json').write_text(json.dumps(costs, indent=2) + '\n', encoding='utf-8')
    finally:
        db.close()


if __name__ == '__main__':
    main()
