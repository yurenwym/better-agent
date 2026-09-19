from copy import deepcopy

import pytest

from app.business_eval import compare, digest, load_suite, model_inputs, score


def fixture():
    suite = {"id": "tiny", "cases": [load_suite()["cases"][0]]}
    capture = {"suite_digest": digest(suite), "metadata": {"code_version": "test", "model": "fake", "prompt_version": "v1"},
               "outputs": [{"id": "clarify-01", "text": "Which role and deadline?", "trace_ref": "trace-1",
                            "observed": {"policy": "clarify", "documents_created": 0}}]}
    ratings = {"capture_digest": digest(capture), "ratings": [{"id": "clarify-01", "reviewer": "human", "rationale": "Requests relevant constraints",
               "actionability": 4, "faithfulness": 4, "personalization": 4}]}
    return suite, capture, ratings


def test_suite_has_40_unique_cases_and_does_not_leak_rubrics_to_inputs():
    suite = load_suite()
    assert len(suite["cases"]) == 40
    assert len({case["category"] for case in suite["cases"]}) == 8
    assert {case["partition"] for case in suite["cases"]} == {"DEV", "HOLDOUT", "SAFETY"}
    assert all(set(case) == {"id", "input", "context"} for case in model_inputs(suite))
    assert all(case["rubric"] for case in suite["cases"])


def test_no_human_score_is_not_a_quality_pass():
    suite, capture, ratings = fixture()
    report = score(suite, capture)
    assert report["needs_review"] == 1 and not report["quality_verified"]
    assert report["metrics"]["cost_microusd"]["p50"] is None
    assert score(suite, capture, ratings)["quality_verified"]


def test_missing_outputs_fail_and_capture_changes_invalidate_ratings():
    suite, capture, ratings = fixture()
    capture["outputs"] = []
    assert score(suite, capture)["failed"] == 1
    with pytest.raises(ValueError, match="exact captured"):
        score(suite, capture, ratings)


def test_state_regression_fails_even_with_good_human_scores():
    suite, capture, ratings = fixture()
    baseline = score(suite, capture, ratings)
    capture["outputs"][0]["observed"]["documents_created"] = 1
    ratings["capture_digest"] = digest(capture)
    candidate = score(suite, capture, ratings)
    assert candidate["failed"] == 1
    assert compare(baseline, candidate)["regressions"] == ["clarify-01"]
    candidate["suite_digest"] = "changed"
    with pytest.raises(ValueError):
        compare(baseline, candidate)


def test_program_schema_uses_real_business_validator():
    suite, capture, _ = fixture()
    case = deepcopy(next(case for case in load_suite()["cases"] if case["id"] == "plan-01"))
    suite["cases"] = [case]
    capture["suite_digest"] = digest(suite)
    capture["outputs"][0].update(id="plan-01", observed={"program": {}})
    assert any(error.startswith("schema:") for error in score(suite, capture)["results"][0]["hard_errors"])


def test_live_smoke_failure_has_nonzero_exit(monkeypatch, tmp_path):
    from app import eval as evaluation
    from app.evals import ScenarioResult, SuiteReport
    monkeypatch.setattr(evaluation, "run_evaluation", lambda *args, **kwargs: evaluation.EvaluationResult(
        SuiteReport((ScenarioResult("live", False),)), tmp_path / "a.json", tmp_path / "a.md"))
    assert evaluation.main(["run", "--mode", "live"]) == 1


def test_cli_template_and_unreviewed_capture_cannot_pass(tmp_path):
    import json
    from app.business_eval import main
    template = tmp_path / "template.json"
    report = tmp_path / "report.json"
    assert main(["template", "--output", str(template)]) == 0
    capture = json.loads(template.read_text(encoding="utf-8"))
    capture["metadata"] = {"code_version": "test", "model": "fake", "prompt_version": "v1"}
    template.write_text(json.dumps(capture), encoding="utf-8")
    assert main(["score", str(template), "--output", str(report)]) == 1
    assert json.loads(report.read_text(encoding="utf-8"))["failed"] == 40
