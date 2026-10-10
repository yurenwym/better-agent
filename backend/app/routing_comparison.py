"""Compare complete routing runs without dropping failed or missing cases."""
from statistics import mean, median


def compare_routing(suite, legacy, loop, *, safety_failures=None, token_explanation=None):
    def rows(report):
        indexed = {r['case_id']: r for r in report['cases']}
        if len(indexed) != len(report['cases']):
            raise ValueError('duplicate case ids')
        if set(indexed) - {c['case_id'] for c in suite['cases']}:
            raise ValueError('unknown case ids')
        result = []
        for case in suite['cases']:
            row = dict(indexed.get(case['case_id'], {}))
            observed = row.get('observed', [])
            row.update(case_id=case['case_id'], category=case['category'], partition=case['partition'],
                matched=row.get('status') == 'valid' and set(case['expected']) <= set(observed)
                    and not bool(set(case['forbidden']) & set(observed)),
                forbidden_hit=bool(set(case['forbidden']) & set(observed)))
            result.append(row)
        return result

    def summary(items):
        def average(field):
            values = [r.get(field) for r in items]
            return mean(values) if values and all(v is not None for v in values) else None
        direct = [r.get('first_token_ms') for r in items if r['category'] == 'direct']
        return dict(planned=len(items), matched=sum(r['matched'] for r in items),
            forbidden_hits=sum(r['forbidden_hit'] for r in items),
            invalid=sum(r.get('status') not in {'valid', 'cancelled'} for r in items),
            cancelled=sum(r.get('status') == 'cancelled' for r in items),
            mean_tokens=average('tokens'), mean_cost_microusd=average('cost_microusd'),
            mean_model_calls=average('model_calls'),
            direct_ttft_p50_ms=median(direct) if direct and all(v is not None for v in direct) else None)

    left, right = rows(legacy), rows(loop)
    a, b = summary(left), summary(right)
    categories = {c: {'legacy': summary([r for r in left if r['category'] == c]),
                       'loop': summary([r for r in right if r['category'] == c])}
                  for c in sorted({r['category'] for r in left})}
    def within(field):
        return None if a[field] is None or b[field] is None else b[field] <= a[field] * 1.2
    gates = dict(accuracy=b['matched'] >= a['matched'],
        categories=all(v['loop']['matched'] >= v['legacy']['matched'] - 1 for v in categories.values()),
        forbidden=b['forbidden_hits'] <= a['forbidden_hits'], safety=None if safety_failures is None else safety_failures == 0,
        tokens=within('mean_tokens') or bool(token_explanation) if within('mean_tokens') is not None else None,
        cost=within('mean_cost_microusd') or bool(token_explanation) if within('mean_cost_microusd') is not None else None,
        direct_ttft=within('direct_ttft_p50_ms'))
    differences = [dict(case_id=x['case_id'], partition=x['partition'], category=x['category'],
        legacy_status=x.get('status', 'missing'), loop_status=y.get('status', 'missing'),
        legacy_observed=x.get('observed', []), loop_observed=y.get('observed', []),
        legacy_matched=x['matched'], loop_matched=y['matched']) for x, y in zip(left, right)]
    all_tie = all(x['matched'] == y['matched'] and x.get('observed') == y.get('observed') for x, y in zip(left, right))
    real = legacy.get('provider') == loop.get('provider') == 'real'
    quality = ('FAIL' if any(v is False for v in gates.values()) else
               'INSUFFICIENT_EVIDENCE' if not real or all_tie or any(v is None for v in gates.values()) else 'PASS')
    return dict(engineering='NOT_ASSESSED', routing_quality=quality, gates=gates,
        all_tie=all_tie, legacy=a, loop=b, by_category=categories, cases=differences,
        token_explanation=token_explanation, safety_failures=safety_failures)
