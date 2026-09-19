import pytest


def test_load_llm_ap_file_maps_external_profile_without_persisting_secret(tmp_path, monkeypatch) -> None:
    from app.config import load_llm_ap

    config_file = tmp_path / "LLM_AP.txt"
    config_file.write_text(
        "LLM_API_KEY=test-secret\nLLM_BASE_URL=https://provider.test\nLLM_MODEL_ID=test-model\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("AGENT_MODEL_API_KEY", raising=False)

    profile = load_llm_ap(config_file)

    assert profile.model == "test-model"
    assert profile.base_url == "https://provider.test"
    assert profile.api_key_env == "AGENT_MODEL_API_KEY"
    assert profile.context_window == 32768
    assert profile.max_output_tokens == 8192
    assert profile.public_view()["api_key_configured"] is True
    assert "test-secret" not in str(profile.public_view())


def test_model_profile_can_be_loaded_from_named_environment_settings(monkeypatch) -> None:
    from app.config import load_model_profile_from_env

    monkeypatch.setenv("AGENT_MODEL_API_KEY", "secret-value")
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("AGENT_MODEL_ID", "demo")

    profile = load_model_profile_from_env()

    assert profile.model == "demo"
    assert profile.base_url == "https://provider.test/v1"
    assert profile.context_window == 32768
    assert profile.max_output_tokens == 8192
    assert profile.public_view()["api_key_configured"] is True
    assert "secret-value" not in str(profile.public_view())


def test_llm_file_accepts_models_endpoint_but_normalizes_chat_base(tmp_path) -> None:
    from app.config import load_llm_ap
    path = tmp_path / "LLM_API.txt"
    path.write_text("LLM_API_KEY=x\nLLM_BASE_URL=https://provider.test/v1/models\nLLM_MODEL_ID=m\n", encoding="utf-8")
    assert load_llm_ap(path).base_url == "https://provider.test/v1"


def test_llm_file_can_override_context_budget(tmp_path) -> None:
    from app.config import load_llm_ap

    path = tmp_path / "LLM_API.txt"
    path.write_text(
        "LLM_API_KEY=x\nLLM_BASE_URL=https://provider.test/v1\nLLM_MODEL_ID=m\n"
        "LLM_CONTEXT_WINDOW=65536\nLLM_MAX_OUTPUT_TOKENS=8192\n",
        encoding="utf-8",
    )

    profile = load_llm_ap(path)

    assert profile.context_window == 65536
    assert profile.max_output_tokens == 8192


def test_project_env_loads_values_without_overriding_process_environment(tmp_path, monkeypatch) -> None:
    from app.config import load_env_file

    monkeypatch.setenv("OPENAI_API_KEY", "process-secret")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "# local configuration\nOPENAI_API_KEY=file-secret\nDEEPSEEK_API_KEY='deepseek-secret'\n",
        encoding="utf-8",
    )

    loaded = load_env_file(path)

    assert loaded == ("DEEPSEEK_API_KEY",)
    assert __import__("os").environ["OPENAI_API_KEY"] == "process-secret"
    assert __import__("os").environ["DEEPSEEK_API_KEY"] == "deepseek-secret"
    assert "secret" not in repr(loaded)


@pytest.mark.parametrize(
    ("provider", "key_name", "model", "protocol"),
    [
        ("openai", "OPENAI_API_KEY", "gpt-5.6-luna", "openai_compatible"),
        ("anthropic", "ANTHROPIC_API_KEY", "claude-sonnet-5", "anthropic"),
        ("deepseek", "DEEPSEEK_API_KEY", "deepseek-flash", "openai_compatible"),
    ],
)
def test_standard_provider_needs_only_its_key(monkeypatch, provider, key_name, model, protocol) -> None:
    from app.config import load_model_price, load_model_profile_from_environment

    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "AGENT_MODEL_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key_name, "secret")

    profile = load_model_profile_from_environment()

    assert profile.provider_name == provider
    assert profile.model == model
    assert profile.provider_protocol == protocol
    assert {"streaming", "tool_calling", "json_object"} <= profile.declared_capabilities
    assert load_model_price(profile) is not None


def test_multiple_standard_provider_keys_require_an_explicit_selection(monkeypatch) -> None:
    from app.config import load_model_profile_from_environment

    monkeypatch.setenv("OPENAI_API_KEY", "one")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "two")
    monkeypatch.delenv("AGENT_MODEL_PROVIDER", raising=False)

    with pytest.raises(ValueError, match="multiple provider keys"):
        load_model_profile_from_environment()


def test_custom_price_is_all_or_nothing(monkeypatch) -> None:
    from app.config import load_model_price
    from app.model_gateway import ModelProfile

    profile = ModelProfile("https://custom.test/v1", "custom", "KEY")
    monkeypatch.setenv("AGENT_MODEL_PRICE_OUTPUT", "100")

    with pytest.raises(ValueError, match="requires all five rates"):
        load_model_price(profile)
