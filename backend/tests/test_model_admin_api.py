import os

from fastapi.testclient import TestClient


def _headers(app, key: str):
    return {
        "host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000",
        "content-type": "application/json", "x-csrf-token": app.state.csrf_token,
        "idempotency-key": key,
    }


def _profile_payload():
    return {
        "name": "规划模型", "provider_protocol": "openai_compatible", "provider_name": "测试供应商",
        "base_url": "https://api.example.com/v1", "model_name": "planner-v1", "credential_env_ref": "MODEL_ADMIN_TEST_KEY",
        "capabilities": {"text": True, "streaming": True, "tool_calling": True, "json_object": True},
        "context_window": 32768, "max_output_tokens": 4096, "timeout_seconds": 30, "max_attempts": 2,
    }


def test_environment_profile_defaults_to_text_capability_only(tmp_path, monkeypatch) -> None:
    from app.db import Database
    from app.model_admin import ModelAdminService
    from app.model_gateway import ModelProfile

    monkeypatch.delenv("AGENT_MODEL_CAPABILITIES", raising=False)
    service = ModelAdminService(Database(tmp_path / "agent.db"))

    registered = service.ensure_profile(ModelProfile("https://provider.test/v1", "demo", "MODEL_KEY"))

    assert service.version(registered.registered_profile_version_id)["capabilities"] == {
        "text": True,
        "streaming": False,
        "tool_calling": False,
        "json_object": False,
        "json_schema": False,
        "vision": False,
        "cache_usage": False,
        "reasoning_usage": False,
    }


def test_environment_profile_accepts_only_explicit_extra_capabilities(tmp_path, monkeypatch) -> None:
    from app.db import Database
    from app.model_admin import ModelAdminService
    from app.model_gateway import ModelProfile

    monkeypatch.setenv("AGENT_MODEL_CAPABILITIES", "streaming,tool_calling,json_object")
    service = ModelAdminService(Database(tmp_path / "agent.db"))

    registered = service.ensure_profile(ModelProfile("https://provider.test/v1", "demo", "MODEL_KEY"))
    capabilities = service.version(registered.registered_profile_version_id)["capabilities"]

    assert {name for name, enabled in capabilities.items() if enabled} == {
        "text", "streaming", "tool_calling", "json_object",
    }


def test_model_profile_and_routing_policy_api_are_versioned_and_secret_safe(tmp_path, monkeypatch) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway
    from test_runtime import make_runtime

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    created = client.post("/api/model-profiles", json=_profile_payload(), headers=_headers(app, "profile-1"))
    assert created.status_code == 201
    profile = created.json()
    version_id = profile["versions"][0]["id"]
    assert profile["versions"][0]["credential_env_ref"] == "MODEL_ADMIN_TEST_KEY"
    assert "secret-value" not in created.text
    assert client.get("/api/model-profiles", headers={"host": "127.0.0.1:8000"}).json()["profiles"][0]["id"] == profile["id"]

    missing = client.post(f"/api/model-profile-versions/{version_id}/verify", json={}, headers=_headers(app, "verify-missing"))
    assert missing.status_code == 422 and missing.json()["detail"]["code"] == "EXTERNAL_CREDENTIAL_MISSING"
    monkeypatch.setenv("MODEL_ADMIN_TEST_KEY", "secret-value")
    runtime.model_admin.verifier = lambda item: {"ok": True, "latency_ms": 12}
    verified = client.post(f"/api/model-profile-versions/{version_id}/verify", json={}, headers=_headers(app, "verify-ok"))
    assert verified.status_code == 200 and verified.json()["verification_status"] == "VERIFIED"
    assert "secret-value" not in verified.text

    policy = client.post("/api/model-routing-policies", json={
        "name": "默认路由", "roles": {"planner": {"primary": version_id, "fallback": []}},
    }, headers=_headers(app, "policy-1"))
    assert policy.status_code == 201 and policy.json()["version"] == 1
    assert client.get(f"/api/model-routing-policies/{policy.json()['id']}", headers={"host": "127.0.0.1:8000"}).status_code == 200

    disabled = client.post(f"/api/model-profile-versions/{version_id}/disable", json={}, headers=_headers(app, "disable-1"))
    assert disabled.status_code == 200 and disabled.json()["status"] == "DISABLED"


def test_routing_policy_rejects_unknown_role_and_incapable_model(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway
    from test_runtime import make_runtime

    runtime = make_runtime(tmp_path, MockModelGateway()); app = create_app(runtime=runtime); client = TestClient(app)
    payload = _profile_payload(); payload["capabilities"]["tool_calling"] = False
    profile = client.post("/api/model-profiles", json=payload, headers=_headers(app, "p")).json()
    version_id = profile["versions"][0]["id"]
    bad = client.post("/api/model-routing-policies", json={
        "name": "坏路由", "roles": {"executor": {"primary": version_id, "fallback": []}},
    }, headers=_headers(app, "bad-policy"))
    assert bad.status_code == 422


def test_ask_route_requires_tool_calling_capability(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway
    from test_runtime import make_runtime

    runtime = make_runtime(tmp_path, MockModelGateway()); app = create_app(runtime=runtime); client = TestClient(app)
    payload = _profile_payload()
    payload["capabilities"] = {"text": True, "json_object": True, "tool_calling": False}
    version_id = client.post("/api/model-profiles", json=payload, headers=_headers(app, "ask-profile")).json()["versions"][0]["id"]
    response = client.post("/api/model-routing-policies", json={
        "name": "ask route", "roles": {"ask": {"primary": version_id, "fallback": []}},
    }, headers=_headers(app, "ask-policy"))
    assert response.status_code == 422


def test_routing_policy_candidate_enters_existing_evolution_loop(tmp_path) -> None:
    from app.main import create_app
    from app.startup import build_runtime

    runtime=build_runtime(tmp_path);app=create_app(runtime=runtime);client=TestClient(app)
    profile=client.post("/api/model-profiles",json=_profile_payload(),headers=_headers(app,"profile")).json()
    version_id=profile["versions"][0]["id"]
    policy=client.post("/api/model-routing-policies",json={"name":"候选路由","roles":{"planner":{"primary":version_id,"fallback":[]}}},headers=_headers(app,"policy")).json()
    base=runtime.behavior.active("stable")
    evidence=[]
    for index in range(3):
        item=runtime.evolution.record_experience(task_type="plan",outcome="quality issue",lineage_group_hash=f"l{index}",source_content_hash=f"s{index}",runtime_bundle_id=base.id,dataset_partition="DISCOVERY",idempotency_key=f"e{index}")
        evidence.append(item["id"])
    candidate=client.post(f"/api/model-routing-policies/{policy['id']}/candidate",json={"experience_ids":evidence,"reason":"规划质量需要改进"},headers=_headers(app,"candidate"))
    assert candidate.status_code==201 and candidate.json()["candidate_type"]=="policy"
