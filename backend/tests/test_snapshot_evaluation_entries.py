"""The queued evaluation API drives real controlled direct gateways."""
import asyncio
import json

from fastapi.testclient import TestClient

from app.costs import CostService
from app.main import create_app
from app.model_admin import ModelAdminService
from app.model_control import ModelControlStore
from app.model_gateway import ModelGateway
from app.model_input_snapshot_store import ModelInputSnapshotStore
from app.real_evaluation import LiveEvaluationRunner, ManagedEvaluationWorker, RealEvaluator
from app.runtime import MockModelGateway
from snapshot_entrypoint_helpers import CommittedSnapshotTransport
from test_api import _headers
from test_evaluation_api import _control_bindings
from test_real_evaluation import _release_cases
from test_runtime import make_runtime
from test_snapshot_gateway import _answer


def test_t15_evaluation_api_freezes_both_arms_and_independent_judges(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "offline-test")
    monkeypatch.setenv("BETTER_AGENT_COST_MODE", "observe")
    runtime = make_runtime(tmp_path, MockModelGateway())
    base, candidate, models = _control_bindings(runtime, context_window=32768, max_output_tokens=4096)
    evaluator = RealEvaluator(tmp_path / "evals", db=runtime.db)
    evaluator.register_release_suite("snapshot-release", _release_cases())
    control = ModelControlStore(runtime.db, costs=CostService(runtime.db))
    evaluator.runner_factory = LiveEvaluationRunner(ModelAdminService(runtime.db), control).runners
    runtime.real_evaluator = evaluator
    runtime.evaluation_worker = ManagedEvaluationWorker(evaluator)
    observer = CommittedSnapshotTransport(runtime.db, "local-user", "CS-EV-03", None)

    async def attempt(gateway, request, *args, **kwargs):
        if request.role == "judge_quality":
            value = json.loads(request.messages[-1]["content"])
            reply = json.dumps({"winner": "right" if "improved" in value["right"] else "left"})
        elif request.role == "judge_safety":
            reply = '{"left_safe":true,"right_safe":true}'
        else:
            reply = "safe improved" if request.purpose == "evaluation_candidate" else "safe baseline"
        observer.response = _answer(reply)
        return await observer(gateway.profile, request)

    monkeypatch.setattr(ModelGateway, "_attempt", attempt)
    app = create_app(runtime=runtime)
    client = TestClient(app)
    created = client.post("/api/evaluation-runs", headers={**_headers(app), "Idempotency-Key":"snapshot-evaluation"}, json={
        "suite_id":"snapshot-release", "baseline_bundle_id":base, "candidate_bundle_id":candidate,
        "baseline_model_id":models[0], "candidate_model_id":models[1],
        "quality_judge_model_id":models[2], "safety_judge_model_id":models[3],
        "budget_microusd":1000000, "primary_objective":"quality",
    })
    assert created.status_code == 202, created.text
    assert asyncio.run(runtime.evaluation_worker.run_once())
    status = client.get(f"/api/evaluation-runs/{created.json()['id']}", headers={"host":"127.0.0.1:8000"}).json()
    assert status["status"] == "COMPLETED", status
    assert observer.send_count == 240
    assert len({row["invocation_id"] for row in observer.observations}) == 240
    with runtime.db.connection() as connection:
        rows = connection.execute("SELECT i.*,a.profile_version_id FROM model_invocations i JOIN model_attempts a ON a.invocation_id=i.id").fetchall()
    assert len(rows) == 240
    for row in rows:
        frozen = ModelInputSnapshotStore(runtime.db).load("local-user", row["context_snapshot_id"])
        index = {"evaluation_baseline":0,"evaluation_candidate":1}.get(row["purpose"], 2 if row["role"] == "judge_quality" else 3)
        assert row["profile_version_id"] == models[index]
        assert frozen.runtime_bundle_id == (candidate if index == 1 else base)
        assert row["selected_attempt_id"]
    assert not asyncio.run(runtime.evaluation_worker.run_once())
    assert observer.send_count == 240
