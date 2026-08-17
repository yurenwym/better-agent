from fastapi.testclient import TestClient


def test_health_returns_local_service_metadata() -> None:
    from app.main import create_app

    client = TestClient(create_app())
    response = client.get("/api/health", headers={"host": "127.0.0.1:8000"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "better-agent",
        "version": "1.0.0",
        "api_key_configured": False,
    }


def test_health_rejects_non_loopback_host() -> None:
    from app.main import create_app

    client = TestClient(create_app())

    response = client.get("/api/health", headers={"host": "example.com"})

    assert response.status_code == 400
