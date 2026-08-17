from fastapi.testclient import TestClient

from test_runtime import make_runtime


def _headers(app, **extra):
    return {
        "host": "127.0.0.1:8000",
        "origin": "http://127.0.0.1:8000",
        "content-type": "application/json",
        "x-csrf-token": app.state.csrf_token,
        **extra,
    }


def test_rest_routes_drive_goal_message_plan_and_approval(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway, ModelDecision

    runtime = make_runtime(tmp_path, MockModelGateway(decisions=[ModelDecision.complete("done")]))
    app = create_app(runtime=runtime)
    client = TestClient(app)

    created = client.post(
        "/api/goals",
        json={"title": "Ship", "description": "Ship it"},
        headers=_headers(app),
    )
    assert created.status_code == 201
    goal_id = created.json()["id"]
    run_id = created.json()["run_id"]

    message = client.post(
        f"/api/goals/{goal_id}/messages",
        json={"content": "Ship it"},
        headers=_headers(app),
    )
    assert message.status_code == 200
    assert message.json()["state"] == "AWAITING_APPROVAL"

    plans = client.get(f"/api/runs/{run_id}/plans", headers={"host": "127.0.0.1:8000"})
    assert plans.status_code == 200
    assert plans.json()["current"]["version"] == 1

    approved = client.post(
        f"/api/runs/{run_id}/plans/1/approve",
        headers=_headers(app),
        json={},
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "COMPLETED"

    events = client.get(f"/api/runs/{run_id}/events", headers={"host": "127.0.0.1:8000"})
    assert events.status_code == 200
    assert [event["seq"] for event in events.json()["events"]] == list(range(1, len(events.json()["events"]) + 1))


def test_mutating_routes_require_json_and_csrf_and_plan_revision_is_optimistic(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    created = client.post("/api/goals", json={"title": "Plan", "description": ""}, headers=_headers(app))
    run_id = created.json()["run_id"]
    goal_id = created.json()["id"]
    client.post(f"/api/goals/{goal_id}/messages", json={"content": "Plan"}, headers=_headers(app))

    missing_csrf = client.post(
        f"/api/runs/{run_id}/plans/revise",
        json={"expected_version": 1, "steps": []},
        headers={"host": "127.0.0.1:8000", "content-type": "application/json"},
    )
    assert missing_csrf.status_code == 403

    conflict = client.post(
        f"/api/runs/{run_id}/plans/revise",
        json={"expected_version": 99, "steps": []},
        headers=_headers(app),
    )
    assert conflict.status_code == 409

