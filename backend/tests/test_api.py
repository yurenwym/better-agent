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


def test_thread_and_turn_routes_are_scoped_to_the_local_owner(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    foreign = runtime.conversation.create_thread("Private", owner_id="another-user")
    accepted = runtime.conversation.accept_turn(
        foreign.id, "foreign-turn", "secret", [], owner_id="another-user"
    )

    assert client.get(f"/api/threads/{foreign.id}", headers={"host":"127.0.0.1:8000"}).status_code == 404
    assert client.get(f"/api/threads/{foreign.id}/messages", headers={"host":"127.0.0.1:8000"}).status_code == 404
    assert client.get(f"/api/threads/{foreign.id}/events", headers={"host":"127.0.0.1:8000"}).status_code == 404
    assert client.post(f"/api/turns/{accepted.turn_id}/cancel", headers=_headers(app)).status_code == 404


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


def test_budget_route_rejects_recovery_outside_react_budget_block(tmp_path) -> None:
    import asyncio
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    run = asyncio.run(runtime.create_goal("Budget", "Do not extend a normal run"))
    app = create_app(runtime=runtime)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        f"/api/runs/{run.id}/budget",
        json={"amount": 1},
        headers=_headers(app),
    )

    assert response.status_code == 409
    assert "budget recovery" in response.json()["detail"]


def test_stats_route_returns_projected_event_metrics(tmp_path) -> None:
    import asyncio
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    run = asyncio.run(runtime.create_goal("Stats", "Project events"))
    runtime.events.append(run.id, run.goal_id, "interaction.started", "user", {})
    app = create_app(runtime=runtime)

    response = TestClient(app).get(f"/api/runs/{run.id}/stats", headers={"host": "127.0.0.1:8000"})

    assert response.status_code == 200
    assert response.json()["interactions"] == 1


def test_memory_disable_route_removes_confirmed_memory_from_context(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    created = client.post(
        "/api/memories",
        json={"kind": "preference", "content": "Use bullets", "scope": "global"},
        headers=_headers(app),
    )
    memory_id = created.json()["id"]
    client.post(f"/api/memories/{memory_id}/confirm", json={}, headers=_headers(app))

    disabled = client.post(f"/api/memories/{memory_id}/disable", json={}, headers=_headers(app))

    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert runtime.memory.context_memories(None, None) == []


def test_outcome_route_can_finish_an_awaiting_run(tmp_path) -> None:
    import asyncio
    from app.main import create_app
    from app.runtime import MockModelGateway, ModelDecision

    runtime = make_runtime(
        tmp_path,
        MockModelGateway(
            plan_steps=[{"id": "step-1", "title": "Wait"}],
            decisions=[ModelDecision.await_outcome("external result")],
        ),
    )
    run = asyncio.run(runtime.create_goal("Outcome", "Wait for result"))
    app = create_app(runtime=runtime)
    client = TestClient(app)
    client.post(f"/api/goals/{run.goal_id}/messages", json={"content": "Wait"}, headers=_headers(app))
    client.post(f"/api/runs/{run.id}/plans/1/approve", json={}, headers=_headers(app))

    response = client.post(f"/api/runs/{run.id}/outcome", json={"finished": True}, headers=_headers(app))

    assert response.status_code == 200
    assert response.json()["state"] == "COMPLETED"


def test_memory_api_exposes_evidence_event_ids_for_audit(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    created = client.post(
        "/api/memories",
        json={"kind": "preference", "content": "Use bullets", "scope": "global", "evidence_event_ids": ["evt-1"]},
        headers=_headers(app),
    )

    assert created.status_code == 200
    assert created.json()["evidence_event_ids"] == ["evt-1"]


def test_memory_versions_endpoint_exposes_markdown_history(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    created = client.post(
        "/api/memories",
        json={"kind": "preference", "content": "First", "scope": "global"},
        headers=_headers(app),
    )
    memory_id = created.json()["id"]
    client.post(f"/api/memories/{memory_id}/confirm", json={}, headers=_headers(app))
    client.patch(f"/api/memories/{memory_id}", json={"content": "Second"}, headers=_headers(app))

    response = client.get(f"/api/memories/{memory_id}/versions", headers={"host": "127.0.0.1:8000"})

    assert response.status_code == 200
    assert [item["version"] for item in response.json()["versions"]] == [1, 2]


def test_memory_create_rejects_missing_scope_identifier(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    response = TestClient(app).post(
        "/api/memories",
        json={"kind": "preference", "content": "Project only", "scope": "project"},
        headers=_headers(app),
    )

    assert response.status_code == 422
