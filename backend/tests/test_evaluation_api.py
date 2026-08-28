from fastapi.testclient import TestClient

from test_runtime import make_runtime
from test_real_evaluation import _release_cases


def test_evaluation_report_and_cursor_api(tmp_path) -> None:
    from app.main import create_app
    from app.real_evaluation import RealEvaluator
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    evaluator = RealEvaluator(tmp_path / "evals", db=runtime.db)
    runtime.real_evaluator = evaluator
    evaluator.register_release_suite("release-v1", _release_cases())
    evaluator.evaluate_paired(
        suite_id="release-v1", evaluation_run_id="eval-api", baseline_bundle_id="base", candidate_bundle_id="candidate",
        baseline=lambda case: {"text": "safe baseline", "cost_microusd": 1, "ttft_seconds": .2},
        candidate=lambda case: {"text": "safe improved", "cost_microusd": 1, "ttft_seconds": .1},
        quality_judge=lambda payload: {"winner": "right" if "improved" in payload["right"] else "left"},
        safety_judge=lambda payload: {"left_safe": True, "right_safe": True},
        baseline_model_id="a", candidate_model_id="b", quality_judge_model_id="q", safety_judge_model_id="s",
        evaluator_digest="e", tool_schema_digest="t", context_digest="c", budget_microusd=1000, primary_objective="quality",
    )
    app = create_app(runtime=runtime)
    client = TestClient(app)

    suites = client.get("/api/evaluation-suites", headers={"host": "127.0.0.1:8000"})
    report = client.get("/api/evaluation-runs/eval-api/report", headers={"host": "127.0.0.1:8000"})
    progress = client.get("/api/evaluation-runs/eval-api/events?after_seq=58", headers={"host": "127.0.0.1:8000"})
    stream = client.get("/api/evaluation-runs/eval-api/events/stream?after_seq=58", headers={"host": "127.0.0.1:8000"})

    assert suites.json()["suites"][0]["id"] == "release-v1"
    assert report.json()["release_eligible"] is True
    assert [event["seq"] for event in progress.json()["events"]] == [59, 60]
    assert "id: 59" in stream.text and "id: 60" in stream.text
    assert client.post("/api/evaluation-runs/eval-api/cancel", json={}, headers={
        "host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000", "content-type": "application/json",
        "x-csrf-token": app.state.csrf_token,
    }).status_code == 409


def test_post_evaluation_run_is_queued_worker_completes_and_cancel_is_observed(tmp_path) -> None:
    from app.main import create_app
    from app.real_evaluation import ManagedEvaluationWorker, RealEvaluator
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    evaluator = RealEvaluator(tmp_path / "evals", db=runtime.db)
    evaluator.register_release_suite("release-v1", _release_cases())
    runtime.real_evaluator = evaluator
    runtime.evaluation_worker = ManagedEvaluationWorker(evaluator)
    app = create_app(runtime=runtime)
    headers = {
        "host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000", "content-type": "application/json",
        "x-csrf-token": app.state.csrf_token, "idempotency-key": "eval-create",
    }
    payload = {
        "suite_id": "release-v1", "baseline_bundle_id": "base", "candidate_bundle_id": "candidate",
        "baseline_model_id": "a", "candidate_model_id": "b", "quality_judge_model_id": "q", "safety_judge_model_id": "s",
        "evaluator_digest": "e", "tool_schema_digest": "t", "context_digest": "c", "budget_microusd": 1000,
        "primary_objective": "quality",
    }
    with TestClient(app) as client:
        created = client.post("/api/evaluation-runs", json=payload, headers=headers)
        assert created.status_code == 202 and created.json()["status"] == "QUEUED"
        run_id = created.json()["id"]
        evaluator.runner_factory = lambda config: {
            "baseline": lambda case: {"text": "safe baseline", "cost_microusd": 1, "ttft_seconds": .2},
            "candidate": lambda case: {"text": "safe improved", "cost_microusd": 1, "ttft_seconds": .1},
            "quality_judge": lambda value: {"winner": "right" if "improved" in value["right"] else "left"},
            "safety_judge": lambda value: {"left_safe": True, "right_safe": True},
        }
        assert __import__("asyncio").run(runtime.evaluation_worker.run_once()) is True
        status = client.get(f"/api/evaluation-runs/{run_id}", headers={"host": "127.0.0.1:8000"})
        assert status.json()["status"] == "COMPLETED"
        assert client.get(f"/api/evaluation-runs/{run_id}/report", headers={"host": "127.0.0.1:8000"}).json()["release_eligible"] is True

        queued = client.post("/api/evaluation-runs", json={**payload, "candidate_bundle_id": "other"}, headers={**headers, "idempotency-key": "eval-cancel"}).json()
        cancelled = client.post(f"/api/evaluation-runs/{queued['id']}/cancel", json={}, headers={**headers, "idempotency-key": "cancel"})
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
