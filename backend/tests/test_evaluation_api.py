from fastapi.testclient import TestClient

from test_runtime import make_runtime
from test_real_evaluation import _release_cases


def _control_bindings(runtime):
    from app.behavior import BehaviorBundleService
    from app.costs import CostService, PriceSnapshot
    from app.model_admin import ModelAdminService

    bundles = BehaviorBundleService(runtime.db)
    common = {"tools": "tools-v1", "context": {"renderer": "v1"}}
    model_ids = []
    with runtime.db.transaction() as connection:
        connection.execute("INSERT INTO model_profiles(id,owner_id,name,status,created_at,updated_at) VALUES ('eval-profile','local-user','eval','ACTIVE','now','now')")
        for index in range(4):
            version_id = f"eval-model-{index}"
            model_ids.append(version_id)
            connection.execute(
                "INSERT INTO model_profile_versions(id,profile_id,version,provider_protocol,provider_name,base_url,model_name,credential_env_ref,capabilities_json,context_window,max_output_tokens,timeout_seconds,max_attempts,config_digest,created_at) "
                "VALUES (?, 'eval-profile', ?, 'openai_compatible', 'test', 'https://example.test/v1', ?, 'TEST_KEY', '{\"text\":true,\"streaming\":true,\"json_object\":true}', 1000, 100, 30, 1, ?, 'now')",
                (version_id, index + 1, version_id, f"config-{index}"),
            )
    admin = ModelAdminService(runtime.db)
    base_policy = admin.create_policy("base", {
        "conversation": {"primary": model_ids[0], "fallback": []},
        "judge_quality": {"primary": model_ids[2], "fallback": []},
        "judge_safety": {"primary": model_ids[3], "fallback": []},
    })
    candidate_policy = admin.create_policy("candidate", {
        "conversation": {"primary": model_ids[1], "fallback": []},
    })
    base = bundles.ensure({**common, "model_routing": {"policy_id": base_policy["id"], "digest": base_policy["policy_digest"]}})
    candidate = bundles.ensure({**common, "model_routing": {"policy_id": candidate_policy["id"], "digest": candidate_policy["policy_digest"]}})
    costs = CostService(runtime.db)
    for index, version_id in enumerate(model_ids):
        costs.register_price(version_id, PriceSnapshot(f"price-{index}", 1, 0, 0, 1, 0))
    return base.id, candidate.id, model_ids


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
    resumed_stream = client.get(
        "/api/evaluation-runs/eval-api/events/stream",
        headers={"host": "127.0.0.1:8000", "last-event-id": "59"},
    )

    assert suites.json()["suites"][0]["id"] == "release-v1"
    assert report.json()["release_eligible"] is True
    assert [event["seq"] for event in progress.json()["events"]] == [59, 60, 61, 62]
    assert "id: 59" in stream.text and "id: 62" in stream.text
    assert "id: 59\n" not in resumed_stream.text and "id: 60\n" in resumed_stream.text
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
    base_id, candidate_id, models = _control_bindings(runtime)
    app = create_app(runtime=runtime)
    headers = {
        "host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000", "content-type": "application/json",
        "x-csrf-token": app.state.csrf_token, "idempotency-key": "eval-create",
    }
    payload = {
        "suite_id": "release-v1", "baseline_bundle_id": base_id, "candidate_bundle_id": candidate_id,
        "baseline_model_id": models[0], "candidate_model_id": models[1], "quality_judge_model_id": models[2], "safety_judge_model_id": models[3],
        "evaluator_digest": "forged", "tool_schema_digest": "forged", "context_digest": "forged", "budget_microusd": 1000,
        "primary_objective": "quality",
    }
    with TestClient(app) as client:
        created = client.post("/api/evaluation-runs", json=payload, headers=headers)
        assert created.status_code == 202 and created.json()["status"] == "QUEUED"
        run_id = created.json()["id"]
        with runtime.db.connection() as connection:
            config = __import__("json").loads(connection.execute("SELECT config_json FROM evaluation_runs WHERE id=?", (run_id,)).fetchone()[0])
        assert config["evaluator_digest"] != "forged"
        assert all(config[key] for key in ("model_config_digest", "price_snapshot_digest", "judge_prompt_digest", "rubric_digest", "random_seed_digest"))
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

        queued = client.post("/api/evaluation-runs", json=payload, headers={**headers, "idempotency-key": "eval-cancel"}).json()
        cancelled = client.post(f"/api/evaluation-runs/{queued['id']}/cancel", json={}, headers={**headers, "idempotency-key": "cancel"})
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"


def test_budget_blocked_evaluation_resumes_through_api_without_repeating_case(tmp_path) -> None:
    import asyncio
    from app.main import create_app
    from app.real_evaluation import ManagedEvaluationWorker, RealEvaluator
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    evaluator = RealEvaluator(tmp_path / "evals", db=runtime.db)
    evaluator.register_release_suite("release-v1", _release_cases())
    runtime.real_evaluator = evaluator
    runtime.evaluation_worker = ManagedEvaluationWorker(evaluator)
    base_id, candidate_id, models = _control_bindings(runtime)
    calls = []
    def measured(name, result):
        def invoke(_): calls.append(name); return dict(result)
        invoke.budget_reservation_microusd = 1
        return invoke
    evaluator.runner_factory = lambda _config: {
        "baseline": measured("baseline", {"text":"safe baseline","cost_microusd":1,"ttft_seconds":.2}),
        "candidate": measured("candidate", {"text":"safe improved","cost_microusd":1,"ttft_seconds":.1}),
        "quality_judge": measured("quality", {"winner":"tie","_cost_microusd":1}),
        "safety_judge": measured("safety", {"left_safe":True,"right_safe":True,"_cost_microusd":1}),
    }
    app = create_app(runtime=runtime)
    local = {"host":"127.0.0.1:8000","origin":"http://127.0.0.1:8000","content-type":"application/json","x-csrf-token":app.state.csrf_token}
    payload = {
        "suite_id":"release-v1","baseline_bundle_id":base_id,"candidate_bundle_id":candidate_id,
        "baseline_model_id":models[0],"candidate_model_id":models[1],"quality_judge_model_id":models[2],"safety_judge_model_id":models[3],
        "budget_microusd":6,"primary_objective":"quality",
    }
    with TestClient(app) as client:
        created=client.post("/api/evaluation-runs",json=payload,headers={**local,"idempotency-key":"budget"}).json()
        assert asyncio.run(runtime.evaluation_worker.run_once()) is True
        blocked=client.get(f"/api/evaluation-runs/{created['id']}",headers={"host":"127.0.0.1:8000"}).json()
        assert blocked["status"]=="BUDGET_BLOCKED" and len(calls)==4
        resumed=client.post(f"/api/evaluation-runs/{created['id']}/resume",json={"budget_microusd":1000},headers={**local,"idempotency-key":"resume"})
        assert resumed.status_code==200 and resumed.json()["status"]=="QUEUED"
        replay=client.post(f"/api/evaluation-runs/{created['id']}/resume",json={"budget_microusd":1000},headers={**local,"idempotency-key":"resume"})
        assert replay.status_code==200 and replay.json()["status"]=="QUEUED"
        assert asyncio.run(runtime.evaluation_worker.run_once()) is True
        assert len(calls)==240
