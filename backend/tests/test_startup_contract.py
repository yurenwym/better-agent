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
    monkeypatch.setenv("AGENT_MODEL_CAPABILITIES", "streaming,tool_calling,json_object")

    runtime = build_runtime(tmp_path)

    assert isinstance(runtime.model, LiveRuntimeModel)
    assert runtime.memory_context.ranking_strategy == "rrf"
    assert runtime.memory_context.semantic_candidate_limit == 50
    assert runtime.memory_context.lexical_candidate_limit == 50


def test_runtime_routes_all_roles_to_an_explicit_fallback_after_primary(tmp_path, monkeypatch) -> None:
    from app.startup import build_runtime

    monkeypatch.setenv("AGENT_MODEL_API_KEY", "primary-secret")
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://primary.test/v1")
    monkeypatch.setenv("AGENT_MODEL_ID", "primary-model")
    monkeypatch.setenv("AGENT_MODEL_CAPABILITIES", "streaming,tool_calling,json_object")
    monkeypatch.setenv("AGENT_FALLBACK_MODEL_API_KEY", "fallback-secret")
    monkeypatch.setenv("AGENT_FALLBACK_MODEL_BASE_URL", "https://api.siliconflow.cn/v1")
    monkeypatch.setenv("AGENT_FALLBACK_MODEL_ID", "Qwen/Qwen3.5-122B-A10B")
    monkeypatch.setenv("AGENT_FALLBACK_MODEL_PROVIDER_NAME", "siliconflow")
    monkeypatch.setenv("AGENT_FALLBACK_MODEL_CAPABILITIES", "streaming,tool_calling,json_object")

    runtime = build_runtime(tmp_path)
    stable = runtime.behavior.active("stable").manifest

    assert stable["model"]["model"] == "primary-model"
    assert [item["model"] for item in stable["fallback_models"]] == ["Qwen/Qwen3.5-122B-A10B"]
    with runtime.db.connection() as connection:
        for route in stable["model_role_bindings"].values():
            assert len(route["fallback"]) == 1
            primary = connection.execute(
                "SELECT model_name FROM model_profile_versions WHERE id=?", (route["primary"],),
            ).fetchone()
            fallback = connection.execute(
                "SELECT model_name,credential_env_ref FROM model_profile_versions WHERE id=?",
                (route["fallback"][0],),
            ).fetchone()
            assert primary["model_name"] == "primary-model"
            assert fallback["model_name"] == "Qwen/Qwen3.5-122B-A10B"
            assert fallback["credential_env_ref"] == "AGENT_FALLBACK_MODEL_API_KEY"
        dump = "\n".join(connection.iterdump())
    assert "primary-secret" not in dump
    assert "fallback-secret" not in dump


def test_configured_startup_repairs_an_unroutable_stable_bundle(tmp_path, monkeypatch) -> None:
    from app.startup import build_runtime

    for name in (
        "LLM_AP_PATH",
        "AGENT_MODEL_BASE_URL",
        "AGENT_MODEL_ID",
        "AGENT_MODEL_API_KEY",
        "AGENT_FALLBACK_MODEL_BASE_URL",
        "AGENT_FALLBACK_MODEL_ID",
        "AGENT_FALLBACK_MODEL_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "AGENT_MODEL_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    build_runtime(tmp_path)

    monkeypatch.setenv("AGENT_MODEL_API_KEY", "configured")
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("AGENT_MODEL_ID", "demo")
    monkeypatch.setenv("AGENT_MODEL_CAPABILITIES", "streaming,tool_calling,json_object")
    runtime = build_runtime(tmp_path)

    routing = runtime.behavior.active("stable").manifest["model_routing"]
    assert routing["policy_id"] != "unconfigured"
    with runtime.db.connection() as connection:
        policy = connection.execute(
            "SELECT policy_digest FROM model_routing_policies WHERE id=?",
            (routing["policy_id"],),
        ).fetchone()
    assert policy is not None
    assert policy["policy_digest"] == routing["digest"]


def test_model_routing_requires_conversation_and_ask_routes(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from app.startup import _has_valid_model_routing, build_runtime

    monkeypatch.setenv("AGENT_MODEL_API_KEY", "configured")
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("AGENT_MODEL_ID", "demo")
    monkeypatch.setenv("AGENT_MODEL_CAPABILITIES", "streaming,tool_calling,json_object")
    runtime = build_runtime(tmp_path)
    stable = runtime.behavior.active("stable")
    primary = stable.manifest["model_role_bindings"]["conversation"]["primary"]

    with runtime.db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_routing_policies(id,owner_id,version,name,roles_json,policy_digest,created_at) "
            "VALUES (?, 'local-user', 1, 'expert only', ?, 'digest-expert-only', datetime('now'))",
            ("policy_expert_only", '{"expert":{"primary":"%s","fallback":[]}}' % primary),
        )

    expert_only = SimpleNamespace(manifest={
        "model_routing": {"policy_id": "policy_expert_only", "digest": "digest-expert-only"},
    })
    assert _has_valid_model_routing(runtime.db, expert_only) is False
    assert _has_valid_model_routing(runtime.db, stable) is True


def test_configured_startup_activates_a_changed_model_profile(tmp_path, monkeypatch) -> None:
    from app.startup import build_runtime

    monkeypatch.setenv("AGENT_MODEL_API_KEY", "configured")
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://provider-one.test/v1")
    monkeypatch.setenv("AGENT_MODEL_ID", "first")
    monkeypatch.setenv("AGENT_MODEL_CAPABILITIES", "streaming,tool_calling,json_object")
    first = build_runtime(tmp_path)
    stable_id = first.behavior.active("stable").id

    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://provider-two.test/v1")
    monkeypatch.setenv("AGENT_MODEL_ID", "second")
    second = build_runtime(tmp_path)

    stable = second.behavior.active("stable")
    assert stable.id != stable_id
    assert stable.manifest["model"]["base_url"] == "https://provider-two.test/v1"
    with second.db.connection() as connection:
        selected = connection.execute(
            "SELECT base_url FROM model_profile_versions WHERE id=?",
            (stable.manifest["model_role_bindings"]["conversation"]["primary"],),
        ).fetchone()
    assert selected is not None
    assert selected["base_url"] == "https://provider-two.test/v1"


@pytest.mark.asyncio
async def test_runtime_without_model_fails_visibly_instead_of_echoing_user_input(tmp_path, monkeypatch) -> None:
    from app.startup import build_runtime

    for name in (
        "LLM_AP_PATH",
        "AGENT_MODEL_BASE_URL",
        "AGENT_MODEL_ID",
        "AGENT_MODEL_API_KEY",
        "AGENT_FALLBACK_MODEL_BASE_URL",
        "AGENT_FALLBACK_MODEL_ID",
        "AGENT_FALLBACK_MODEL_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "AGENT_MODEL_PROVIDER",
    ):
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
    monkeypatch.setenv("AGENT_MODEL_API_KEY","configured");monkeypatch.setenv("AGENT_MODEL_BASE_URL","https://provider.test/v1");monkeypatch.setenv("AGENT_MODEL_ID","demo");monkeypatch.setenv("AGENT_MODEL_CAPABILITIES","streaming,tool_calling,json_object")
    monkeypatch.setenv("RESEARCH_SEARCH_PROVIDER","tavily");monkeypatch.setenv("TAVILY_API_KEY","search-key")
    runtime=build_runtime(tmp_path)
    assert isinstance(runtime.research.engine.retriever.retrievers[0],TavilySearchRetriever)


def test_runtime_rejects_tavily_without_key(tmp_path,monkeypatch)->None:
    import pytest
    from app.startup import build_runtime
    monkeypatch.setenv("AGENT_MODEL_API_KEY","configured");monkeypatch.setenv("AGENT_MODEL_BASE_URL","https://provider.test/v1");monkeypatch.setenv("AGENT_MODEL_ID","demo")
    monkeypatch.setenv("RESEARCH_SEARCH_PROVIDER","tavily");monkeypatch.delenv("TAVILY_API_KEY",raising=False)
    with pytest.raises(ValueError,match="TAVILY_API_KEY"):build_runtime(tmp_path)


def test_official_deepseek_startup_registers_price_and_is_ready(tmp_path, monkeypatch) -> None:
    from app.main import create_app
    from app.startup import build_runtime

    for name in ("AGENT_MODEL_BASE_URL", "AGENT_MODEL_ID", "AGENT_MODEL_API_KEY", "LLM_AP_PATH"):
        monkeypatch.delenv(name, raising=False)
    for name in ("AGENT_FALLBACK_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_ID", "AGENT_FALLBACK_MODEL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "deepseek")

    runtime = build_runtime(tmp_path)
    readiness = runtime.model_readiness.check()
    bootstrap = TestClient(create_app(runtime=runtime)).get(
        "/api/bootstrap", headers={"host": "127.0.0.1:8000"},
    ).json()

    assert readiness["ready"] is True
    assert readiness["network_verified"] is False
    assert bootstrap["api_key_env"] == "DEEPSEEK_API_KEY"
    assert set(readiness["required_roles"]) == {"conversation", "ask"}
    assert all(item["source_url"].startswith("https://api-docs.deepseek.com/") for item in readiness["prices"].values())


def test_custom_unpriced_model_is_rejected_before_turn_is_accepted(tmp_path, monkeypatch) -> None:
    from app.main import create_app
    from app.startup import build_runtime

    # This contract is about enforcement mode; the ambient default is observe.
    monkeypatch.setenv("BETTER_AGENT_COST_MODE", "enforce")
    monkeypatch.setenv("AGENT_MODEL_API_KEY", "configured")
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://custom.test/v1")
    monkeypatch.setenv("AGENT_MODEL_ID", "custom")
    monkeypatch.setenv("AGENT_MODEL_CAPABILITIES", "streaming,tool_calling,json_object")
    runtime = build_runtime(tmp_path)
    app = create_app(runtime=runtime)
    client = TestClient(app)
    headers = {
        "host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000",
        "content-type": "application/json", "x-csrf-token": app.state.csrf_token,
    }
    thread = client.post("/api/threads", headers=headers, json={"title": "readiness"}).json()

    readiness = client.get("/api/model-readiness", headers={"host": "127.0.0.1:8000"})
    response = client.post(
        f"/api/threads/{thread['id']}/turns", headers=headers,
        json={"client_turn_id": "not-accepted", "content": "hello"},
    )

    assert readiness.status_code == 200
    assert readiness.json()["errors"][0]["code"] == "MODEL_PRICE_MISSING"
    assert response.status_code == 503
    assert response.json()["detail"]["errors"][0]["retryable"] is False
    assert runtime.conversation.turns(thread["id"]) == []
