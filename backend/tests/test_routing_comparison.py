from copy import deepcopy
from app.routing_comparison import compare_routing


def fixture():
    suite = {'cases': [dict(case_id=str(i), category='direct', partition='DEV', expected=['final'], forbidden=['ask']) for i in range(3)]}
    report = dict(provider='real', cases=[dict(case_id=str(i), status='valid', observed=['final'], tokens=100,
        model_calls=1, cost_microusd=1, first_token_ms=100) for i in range(3)])
    return suite, report


def test_all_tie_is_not_effectiveness_pass():
    suite, report = fixture()
    assert compare_routing(suite, report, report, safety_failures=0)['routing_quality'] == 'INSUFFICIENT_EVIDENCE'


def test_known_regression_and_missing_remain_in_denominator():
    suite, before = fixture()
    after = deepcopy(before)
    after['cases'][0]['observed'] = ['ask']
    after['cases'].pop()
    report = compare_routing(suite, before, after, safety_failures=0)
    assert report['routing_quality'] == 'FAIL'
    assert report['loop']['planned'] == 3
    assert report['loop']['invalid'] == 1
    assert report['loop']['matched'] == 1
    assert report['loop']['mean_tokens'] is None


def test_real_improvement_requires_safety_evidence():
    suite, after = fixture()
    before = deepcopy(after)
    before['cases'][0]['observed'] = ['ask']
    assert compare_routing(suite, before, after)['routing_quality'] == 'INSUFFICIENT_EVIDENCE'
    assert compare_routing(suite, before, after, safety_failures=0)['routing_quality'] == 'PASS'
    after['provider'] = 'mock'
    assert compare_routing(suite, before, after, safety_failures=0)['routing_quality'] != 'PASS'
