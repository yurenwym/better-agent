from fastapi.testclient import TestClient
import pytest


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


def test_fastapi_serves_spa_index_for_known_frontend_routes_only(tmp_path) -> None:
    from app.main import create_app

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>local-ui</body></html>", encoding="utf-8")
    client = TestClient(create_app(static_dir=dist))
    headers = {"host": "127.0.0.1:8000"}

    for path in ("/plans", "/plans/plan-1", "/threads/thread-1", "/today", "/research", "/schedules", "/growth"):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert "local-ui" in response.text
    assert client.get("/api/not-real", headers=headers).status_code == 404


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


@pytest.mark.asyncio
async def test_runtime_without_model_fails_visibly_instead_of_echoing_user_input(tmp_path, monkeypatch) -> None:
    from app.startup import build_runtime

    for name in ("LLM_AP_PATH", "AGENT_MODEL_BASE_URL", "AGENT_MODEL_ID", "AGENT_MODEL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    runtime = build_runtime(tmp_path)
    thread = runtime.conversation.create_thread("模型配置检查")
    accepted = runtime.conversation.accept_turn(thread.id, "client-no-model", "怎么速成高等数学", [])

    await runtime.turn_worker.run_once()

    assistant = [message for message in runtime.conversation.messages(thread.id) if message.role == "assistant"]
    assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
    assert len(assistant) == 1
    assert "模型尚未配置" in assistant[0].content
    assert assistant[0].content != "怎么速成高等数学"


def test_runtime_explicitly_selects_tavily_without_search_fallback(tmp_path,monkeypatch)->None:
    from app.startup import build_runtime
    from app.research.tavily import TavilySearchRetriever
    monkeypatch.setenv("AGENT_MODEL_API_KEY","configured");monkeypatch.setenv("AGENT_MODEL_BASE_URL","https://provider.test/v1");monkeypatch.setenv("AGENT_MODEL_ID","demo")
    monkeypatch.setenv("RESEARCH_SEARCH_PROVIDER","tavily");monkeypatch.setenv("TAVILY_API_KEY","search-key")
    runtime=build_runtime(tmp_path)
    assert isinstance(runtime.research.engine.retriever.retrievers[0],TavilySearchRetriever)


def test_runtime_rejects_tavily_without_key(tmp_path,monkeypatch)->None:
    import pytest
    from app.startup import build_runtime
    monkeypatch.setenv("AGENT_MODEL_API_KEY","configured");monkeypatch.setenv("AGENT_MODEL_BASE_URL","https://provider.test/v1");monkeypatch.setenv("AGENT_MODEL_ID","demo")
    monkeypatch.setenv("RESEARCH_SEARCH_PROVIDER","tavily");monkeypatch.delenv("TAVILY_API_KEY",raising=False)
    with pytest.raises(ValueError,match="TAVILY_API_KEY"):build_runtime(tmp_path)
