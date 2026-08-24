from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import create_app
from app.startup import build_runtime


def headers(app, key):
    return {
        "host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000", "content-type": "application/json",
        "x-csrf-token": app.state.csrf_token, "idempotency-key": key,
    }


def test_evolution_http_closes_the_controlled_release_loop(tmp_path):
    runtime = build_runtime(tmp_path)
    base = runtime.behavior.active("stable")
    target = runtime.behavior.ensure({**base.manifest, "prompts": "candidate-v2"})
    app = create_app(runtime=runtime)
    client = TestClient(app)

    evidence = []
    for index in range(3):
        response = client.post(
            "/api/evolution/experiences", headers=headers(app, f"experience-{index}"),
            json={"task_type":"conversation", "outcome":"unnecessary clarification", "lineage_group_hash":f"lineage-{index}",
                  "source_content_hash":f"source-{index}", "runtime_bundle_id":base.id, "dataset_partition":"DISCOVERY"},
        )
        assert response.status_code == 201
        evidence.append(response.json()["id"])

    created = client.post(
        "/api/evolution/candidates", headers=headers(app, "candidate"),
        json={"candidate_type":"prompt", "experience_ids":evidence, "base_bundle_id":base.id,
              "target_bundle_id":target.id, "proposed_content":{"prompts":"candidate-v2"},
              "permission_diff":{"added":[]}, "reason":"repeated evidence"},
    )
    assert created.status_code == 201
    item = created.json()

    evaluated = client.post(
        f"/api/evolution/candidates/{item['id']}/evaluate", headers=headers(app, "evaluate"),
        json={"expected_version":item["version"]},
    ).json()
    current = client.get(f"/api/evolution/candidates/{item['id']}", headers={"host":"127.0.0.1:8000"}).json()
    approved = client.post(
        f"/api/evolution/candidates/{item['id']}/approve", headers=headers(app, "approve"),
        json={"expected_version":current["version"]},
    )
    assert approved.status_code == 200

    current = client.get(f"/api/evolution/candidates/{item['id']}", headers={"host":"127.0.0.1:8000"}).json()
    deployment = client.post(
        f"/api/evolution/candidates/{item['id']}/start-canary", headers=headers(app, "canary"),
        json={"expected_version":current["version"]},
    )
    assert deployment.status_code == 200
    deployment_id = deployment.json()["id"]
    challenger_count = 0
    for index in range(200):
        thread = client.post("/api/threads", headers=headers(app, f"thread-{index}"), json={"title":f"sample-{index}"}).json()
        run = client.post(
            f"/api/threads/{thread['id']}/expert-runs", headers=headers(app, f"run-{index}"),
            json={"objective":f"sample-{index}", "idempotency_key":f"sample-run-{index}"},
        ).json()
        runtime.agent_tasks.cancel_run(run["id"], "test terminal outcome")
        with runtime.db.connection() as connection:
            exposure = connection.execute("SELECT cohort FROM canary_exposures WHERE run_id=?", (run["id"],)).fetchone()
        challenger_count += exposure["cohort"] == "challenger"
        if challenger_count >= 3: break
    assert challenger_count >= 3
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE canary_exposures SET success=1,safety_pass=1 WHERE deployment_id=? AND cohort='challenger'",
            (deployment_id,),
        )

    current = client.get(f"/api/evolution/candidates/{item['id']}", headers={"host":"127.0.0.1:8000"}).json()
    promoted = client.post(
        f"/api/evolution/candidates/{item['id']}/promote", headers=headers(app, "promote"),
        json={"expected_version":current["version"]},
    )
    assert promoted.status_code == 200 and promoted.json()["status"] == "PROMOTED"
    assert runtime.behavior.active("stable").id == target.id
    assert client.get("/api/evolution/history", headers={"host":"127.0.0.1:8000"}).json()["events"]


def test_evolution_http_rejects_missing_idempotency_and_insufficient_evidence(tmp_path):
    runtime = build_runtime(tmp_path); app = create_app(runtime=runtime); client = TestClient(app)
    local = {"host":"127.0.0.1:8000", "origin":"http://127.0.0.1:8000", "content-type":"application/json", "x-csrf-token":app.state.csrf_token}
    base = runtime.behavior.active("stable")
    target = runtime.behavior.ensure({**base.manifest, "prompts":"v2"})
    missing = client.post(
        "/api/evolution/experiences", headers=local,
        json={"task_type":"conversation", "outcome":"failure", "lineage_group_hash":"lineage",
              "source_content_hash":"source", "runtime_bundle_id":base.id, "dataset_partition":"DISCOVERY"},
    )
    assert missing.status_code == 422 and "Idempotency-Key" in missing.json()["detail"]
    too_few = client.post(
        "/api/evolution/candidates", headers=headers(app, "too-few"),
        json={"candidate_type":"prompt", "experience_ids":[], "base_bundle_id":base.id, "target_bundle_id":target.id,
              "proposed_content":{}, "permission_diff":{"added":[]}, "reason":"none"},
    )
    assert too_few.status_code == 409


def test_browser_cannot_self_report_canary_outcomes(tmp_path):
    runtime = build_runtime(tmp_path); app = create_app(runtime=runtime); client = TestClient(app)
    response = client.post(
        "/api/evolution/canaries/fake/exposures", headers=headers(app, "forged"),
        json={"run_id":"fake", "assignment_key":"fake", "success":True, "safety_pass":True},
    )
    assert response.status_code == 404
