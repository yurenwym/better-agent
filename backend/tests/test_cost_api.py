from fastapi.testclient import TestClient

from test_runtime import make_runtime


def _headers(app, **extra):
    return {
        "host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000",
        "content-type": "application/json", "x-csrf-token": app.state.csrf_token,
        "idempotency-key": "cost-api-1", **extra,
    }


def test_cost_budget_summary_and_invocation_apis(tmp_path) -> None:
    from app.costs import CostService
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    runtime.costs = CostService(runtime.db)
    app = create_app(runtime=runtime)
    client = TestClient(app)

    saved = client.put(
        "/api/cost/budgets", json={"period_kind": "DAILY", "period_key": "2026-08-28", "limit_microusd": 1000},
        headers=_headers(app),
    )
    assert saved.status_code == 200
    assert saved.json()["limit_microusd"] == 1000
    summary = client.get("/api/cost/summary?period_kind=DAILY&period_key=2026-08-28", headers={"host": "127.0.0.1:8000"})
    assert summary.status_code == 200
    assert summary.json()["charged_microusd"] == 0


def test_cost_api_rejects_invalid_period_and_never_returns_secret_values(tmp_path, monkeypatch) -> None:
    from app.costs import CostService
    from app.main import create_app
    from app.runtime import MockModelGateway

    monkeypatch.setenv("SECRET_ENV", "secret-value")
    runtime = make_runtime(tmp_path, MockModelGateway())
    runtime.costs = CostService(runtime.db)
    app = create_app(runtime=runtime)
    client = TestClient(app)

    invalid = client.put("/api/cost/budgets", json={"period_kind": "WEEKLY", "period_key": "x", "limit_microusd": 1}, headers=_headers(app))
    assert invalid.status_code == 422
    assert "secret-value" not in client.get("/api/cost/summary?period_kind=DAILY&period_key=missing", headers={"host": "127.0.0.1:8000"}).text
