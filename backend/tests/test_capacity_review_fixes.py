"""Regression checks through configuration, persistence and public API boundaries."""
import json

import pytest

from app.config import load_llm_ap, load_model_profile_from_env
from app.db import Database
from app.model_admin import ModelAdminService
from app.model_control import RoutedModelGateway
from app.token_budget import effective_input_budget, hot_window, static_archive_policy


def reload_profile(service, version_id):
    with service.db.connection() as connection:
        row = connection.execute("SELECT * FROM model_profile_versions WHERE id=?", (version_id,)).fetchone()
    return RoutedModelGateway._profile(row)


@pytest.fixture
def clean_env(monkeypatch):
    import os
    for key in list(os.environ):
        if key.startswith("AGENT_MODEL_"):
            monkeypatch.delenv(key)


def test_manual_tier_a_survives_registration_and_reloading(tmp_path, monkeypatch, clean_env):
    settings = dict(BASE_URL="https://api.deepseek.com", ID="deepseek-flash", API_KEY="fake-test",
        CONTEXT_WINDOW="32768", VALIDATION_TIER="A", ADMITTED_CONTEXT_LIMIT="131072",
        COUNTER_EVIDENCE_VERSION="existing-wrapper-evidence")
    for key, value in settings.items():
        monkeypatch.setenv("AGENT_MODEL_" + key, value)
    from dataclasses import replace
    from app.token_budget import ProtocolBudget
    profile = replace(load_model_profile_from_env(), protocol_budget=ProtocolBudget(evidence="existing-probe"))
    service = ModelAdminService(Database(tmp_path / "agent.db"))
    bound = service.ensure_profile(profile)
    reloaded = reload_profile(service, bound.registered_profile_version_id)
    for item in (profile, reloaded):
        assert item.soft_context_limit == 32768
        assert not item.context_window_verified
        assert effective_input_budget(item).total_limit == 32768
        assert effective_input_budget(item).input_limit == 24576
        assert hot_window(item).input_limit == 24576
        assert static_archive_policy(item).target == static_archive_policy(profile).target


def test_llm_file_wins_before_resolution_and_round_trips(tmp_path, monkeypatch, clean_env):
    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW", "32768")
    path = tmp_path / "fake-LLM_AP"
    path.write_text("LLM_API_KEY=fake\nLLM_BASE_URL=https://api.deepseek.com\nLLM_MODEL_ID=deepseek-flash\nLLM_CONTEXT_WINDOW=65536\n")
    profile = load_llm_ap(path)
    service = ModelAdminService(Database(tmp_path / "agent.db"))
    bound = service.ensure_profile(profile)
    reloaded = reload_profile(service, bound.registered_profile_version_id)
    for item in (profile, reloaded):
        record = json.loads(item.capacity_evidence)
        assert item.working_window_mode == record["mode"] == "manual"
        assert item.capacity_source == record["source"] == "llm-file"
        assert item.context_window == record["effective_context_limit"] == 65536
        assert item.soft_context_limit == record["soft_context_limit"] == 65536
        assert effective_input_budget(item) == effective_input_budget(profile)
    monkeypatch.setenv("AGENT_MODEL_ADMITTED_CONTEXT_LIMIT", "32768")
    with pytest.raises(ValueError, match="endpoint limit"):
        load_llm_ap(path)


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.startup import build_runtime
    runtime = build_runtime(tmp_path)
    app = create_app(runtime=runtime)
    http = TestClient(app)
    http.headers.update({"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000",
        "x-csrf-token": app.state.csrf_token})
    return http


def payload(**extra):
    return dict(name="capacity-review", provider_protocol="openai_compatible", provider_name="deepseek",
        base_url="https://api.deepseek.com", model_name="deepseek-flash", credential_env_ref="TEST_KEY",
        capabilities={"text": True}, max_output_tokens=8192, max_attempts=1, **extra)


@pytest.mark.parametrize("record", [None, {"mode": "auto", "status": "verified", "model_context_limit": 1000000},
    {"mode": "auto", "status": "unverified", "source": "legacy-conservative-default"}])
def test_public_auto_cannot_be_promoted_by_client_evidence(tmp_path, record):
    from app.model_admin import ModelAdminError
    service = ModelAdminService(Database(tmp_path / "agent.db"))
    with pytest.raises(ModelAdminError, match="verified capacity evidence"):
        service.create_profile(payload(working_window_mode="auto", context_window=1000000, capacity_evidence=record))
    assert service.list_profiles() == []


def test_public_manual_does_not_need_client_capacity_evidence(tmp_path):
    from app.model_admin import ModelAdminError
    service = ModelAdminService(Database(tmp_path / "agent.db"))
    request = payload(working_window_mode="manual", context_window=32768)
    version = service.create_profile(request)["versions"][0]
    assert version["model_max_output_limit"] == 393216
    assert version["soft_context_limit"] == 32768
    assert version["capacity_status"] == "manual"
    with pytest.raises(ModelAdminError, match="model output limit"):
        service.add_version(version["profile_id"], {**request, "context_window": 1000000, "max_output_tokens": 400000})
    with pytest.raises(ModelAdminError, match="verified capacity evidence"):
        service.add_version(version["profile_id"], {**request, "working_window_mode": "auto"})


def test_manual_smaller_than_admitted_is_valid_through_admin(tmp_path):
    service = ModelAdminService(Database(tmp_path / "agent.db"))
    request = payload(working_window_mode="manual", context_window=32768, validation_tier="A",
        admitted_context_limit=131072, counter_evidence_version="existing", protocol_budget={"evidence": "probe"})
    version = service.create_profile(request)["versions"][0]
    assert effective_input_budget(reload_profile(service, version["id"])).input_limit == 24576
    from app.model_admin import ModelAdminError
    with pytest.raises(ModelAdminError, match="output reserve"):
        service.add_version(version["profile_id"], {**request, "max_output_tokens": 32768})


def test_http_create_and_new_version_reject_forged_auto(client):
    response = client.post("/api/model-profiles", json=payload(working_window_mode="auto",
        context_window=1000000, capacity_evidence={"mode": "auto", "status": "verified"}),
        headers={"idempotency-key": "forged"})
    assert response.status_code == 422
    assert "verified capacity evidence" in response.json()["detail"]
    response = client.post("/api/model-profiles", json=payload(working_window_mode="manual", context_window=32768),
        headers={"idempotency-key": "manual"})
    assert response.status_code == 201
    profile = response.json()
    response = client.post(f"/api/model-profiles/{profile['id']}/versions",
        json=payload(working_window_mode="auto"), headers={"idempotency-key": "auto-version"})
    assert response.status_code == 422


def test_http_auto_uses_server_catalog_not_client_limit(client, monkeypatch):
    from dataclasses import replace
    import app.model_capacity as catalog
    monkeypatch.setattr(catalog, "CATALOG", (replace(catalog.DEEPSEEK_FLASH,
        context_limit=131072, context_verified=True),))
    response = client.post("/api/model-profiles", json=payload(working_window_mode="auto",
        context_window=999999, capacity_evidence={"mode": "auto", "status": "verified",
            "effective_context_limit": 999999}), headers={"idempotency-key": "verified-auto"})
    assert response.status_code == 201
    version = response.json()["versions"][0]
    assert version["context_window"] == version["capacity"]["effective_context_limit"] == 131072
    response = client.post("/api/model-profiles", json={**payload(working_window_mode="auto"),
        "name": "proxy", "base_url": "https://proxy.test/v1"}, headers={"idempotency-key": "proxy"})
    assert response.status_code == 422


def test_llm_file_cannot_override_known_capacity_or_conflicting_soft_limit(tmp_path, monkeypatch, clean_env):
    from dataclasses import replace
    import app.model_capacity as catalog
    monkeypatch.setattr(catalog, "CATALOG", (replace(catalog.DEEPSEEK_FLASH,
        context_limit=32768, context_verified=True),))
    path = tmp_path / "fake-LLM_AP"
    path.write_text("LLM_API_KEY=fake\nLLM_BASE_URL=https://api.deepseek.com\nLLM_MODEL_ID=deepseek-flash\nLLM_CONTEXT_WINDOW=65536\n")
    with pytest.raises(ValueError, match="verified model capacity"):
        load_llm_ap(path)
    monkeypatch.setenv("AGENT_MODEL_SOFT_CONTEXT_LIMIT", "16384")
    with pytest.raises(ValueError, match="disagree"):
        load_llm_ap(path)
