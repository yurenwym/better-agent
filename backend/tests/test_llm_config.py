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
    assert profile.public_view()["api_key_configured"] is True
    assert "test-secret" not in str(profile.public_view())

