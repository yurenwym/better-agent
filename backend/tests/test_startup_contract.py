from fastapi.testclient import TestClient


def test_start_script_uses_windows_npm_command(monkeypatch) -> None:
    import importlib.util
    from pathlib import Path

    script_path = Path(__file__).parents[2] / "scripts" / "start.py"
    spec = importlib.util.spec_from_file_location("start_script", script_path)
    assert spec is not None and spec.loader is not None
    start_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(start_script)

    monkeypatch.setattr(start_script.sys, "platform", "win32")

    assert start_script.npm_command() == "npm.cmd"


def test_fastapi_serves_built_frontend_from_same_origin(tmp_path) -> None:
    from app.main import create_app

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>local-ui</body></html>", encoding="utf-8")
    client = TestClient(create_app(static_dir=dist))

    response = client.get("/", headers={"host": "127.0.0.1:8000"})

    assert response.status_code == 200
    assert "local-ui" in response.text


def test_start_script_defaults_to_loopback() -> None:
    from app.startup import DEFAULT_HOST, DEFAULT_PORT

    assert DEFAULT_HOST == "127.0.0.1"
    assert DEFAULT_PORT == 8000


def test_runtime_uses_one_configured_profile_without_cross_vendor_fallback(tmp_path, monkeypatch) -> None:
    from app.startup import build_runtime
    from app.live_model import LiveRuntimeModel

    monkeypatch.setenv("AGENT_MODEL_API_KEY", "configured")
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("AGENT_MODEL_ID", "demo")

    runtime = build_runtime(tmp_path)

    assert isinstance(runtime.model, LiveRuntimeModel)
