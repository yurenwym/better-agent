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

