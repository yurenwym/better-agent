from fastapi.testclient import TestClient


def test_api_rejects_non_loopback_host_and_origin() -> None:
    from app.main import create_app

    client = TestClient(create_app())
    assert client.get("/api/health", headers={"host": "example.com"}).status_code == 400
    assert client.post(
        "/api/goals",
        json={"title": "x", "description": "y"},
        headers={
            "host": "127.0.0.1:8000",
            "origin": "https://evil.example",
            "content-type": "application/json",
            "x-csrf-token": "wrong",
        },
    ).status_code == 403


def test_public_api_never_returns_model_key_value() -> None:
    from app.config import AppConfig
    from app.main import create_app

    app = create_app(AppConfig(api_key_env="TEST_SECRET_KEY"))
    client = TestClient(app)
    response = client.get("/api/bootstrap", headers={"host": "127.0.0.1:8000"})

    assert response.status_code == 200
    assert "TEST_SECRET_KEY" in response.json()["api_key_env"]
    assert "secret-value" not in response.text


def test_run_api_does_not_echo_tool_action_parameters_in_budget(tmp_path) -> None:
    import asyncio

    from test_runtime import make_runtime
    from app.main import create_app
    from app.runtime import MockModelGateway, ModelDecision
    from app.tools import ToolCall

    runtime = make_runtime(
        tmp_path,
        MockModelGateway(
            plan_steps=[{"id": "step-1", "title": "Write"}],
            decisions=[ModelDecision.tool(ToolCall("call-1", "write_note", {"path": "safe.md", "content": "secret-value"}))],
        ),
    )
    run = asyncio.run(runtime.create_goal("Security", "Do not echo"))
    app = create_app(runtime=runtime)
    client = TestClient(app)
    client.post(f"/api/goals/{run.goal_id}/messages", json={"content": "Write"}, headers={"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000", "content-type": "application/json", "x-csrf-token": app.state.csrf_token})
    response = client.post(f"/api/runs/{run.id}/plans/1/approve", json={}, headers={"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000", "content-type": "application/json", "x-csrf-token": app.state.csrf_token})

    assert response.status_code == 200
    assert "secret-value" not in response.text
