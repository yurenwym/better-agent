import os
import pytest

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
    assert runtime.model_admin.version(version_id)["verification_status"] != "VERIFIED"
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


@pytest.mark.asyncio
async def test_live_verification_freezes_the_requested_unrouted_profile_version(tmp_path, monkeypatch) -> None:
    import httpx
    from app.db import Database
    from app.model_admin import ModelAdminService
    from app.model_control import ModelControlStore
    from test_snapshot_gateway import _openai_stream

    db = Database(tmp_path / "admin-verify.db")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=_openai_stream())

    service = ModelAdminService(
        db, owner_id="trusted-admin", control_store=ModelControlStore(db),
        verification_transport=httpx.MockTransport(handler),
    )
    monkeypatch.setenv("MODEL_ADMIN_TEST_KEY", "verification-secret")
    created = service.create_profile(_profile_payload(), validate_capacity=False)
    version_id = created["versions"][0]["id"]
    verified = await service.verify(version_id)

    assert verified["verification_status"] == "VERIFIED"
    assert len(requests) == 1
    with db.connection() as connection:
        invocation = connection.execute(
            "SELECT owner_id,purpose,context_snapshot_id FROM model_invocations",
        ).fetchone()
        attempt = connection.execute(
            "SELECT profile_version_id FROM model_attempts",
        ).fetchone()
        stable = connection.execute(
            "SELECT bundle_id FROM runtime_channels WHERE name='stable'",
        ).fetchone()
    assert invocation["owner_id"] == "trusted-admin"
    assert invocation["purpose"] == "verify_model_profile_version"
    assert invocation["context_snapshot_id"]
    assert attempt["profile_version_id"] == version_id
    assert stable is None


@pytest.mark.asyncio
async def test_live_verification_failure_keeps_committed_binding_and_cross_owner_is_denied(tmp_path, monkeypatch) -> None:
    import httpx
    from app.db import Database
    from app.model_gateway import GatewayError
    from app.model_admin import ModelAdminService
    from app.model_control import ModelControlStore

    db = Database(tmp_path / "admin-verify-failure.db")
    try:
        owner_service = ModelAdminService(db, owner_id="trusted-admin", control_store=ModelControlStore(db))
        created = owner_service.create_profile(_profile_payload(), validate_capacity=False)
        version_id = created["versions"][0]["id"]

        other_owner = ModelAdminService(db, owner_id="other-admin", control_store=ModelControlStore(db))
        with pytest.raises(KeyError):
            await other_owner.verify(version_id)

        monkeypatch.setenv("MODEL_ADMIN_TEST_KEY", "verification-secret")
        observations = []

        def fail_after_auditing(request: httpx.Request) -> httpx.Response:
            with db.connection() as connection:
                row = connection.execute(
                    "SELECT a.id AS attempt_id,a.status,i.owner_id,i.purpose,i.context_snapshot_id,"
                    "i.context_snapshot_digest,i.status AS invocation_status "
                    "FROM model_attempts a JOIN model_invocations i ON i.id=a.invocation_id "
                    "WHERE a.status='STARTED' ORDER BY a.started_at DESC,a.id DESC LIMIT 1",
                ).fetchone()
            assert row is not None and row["owner_id"] == "trusted-admin"
            assert row["purpose"] == "verify_model_profile_version"
            assert row["context_snapshot_id"] and row["context_snapshot_digest"]
            assert row["invocation_status"] == "RUNNING"
            assert request.url.path.endswith("/chat/completions")
            observations.append(dict(row))
            raise httpx.ConnectError("provider unavailable", request=request)

        owner_service.verification_transport = httpx.MockTransport(fail_after_auditing)
        with pytest.raises(GatewayError, match="retry budget exhausted"):
            await owner_service.verify(version_id)

        assert len(observations) == 2
        assert len({item["context_snapshot_id"] for item in observations}) == 1
        assert len({item["context_snapshot_digest"] for item in observations}) == 1
        assert len({item["attempt_id"] for item in observations}) == 2
        assert owner_service.version(version_id)["verification_status"] == "FAILED"
        with db.connection() as connection:
            rows = connection.execute(
                "SELECT i.owner_id,i.purpose,i.context_snapshot_id,a.status "
                "FROM model_invocations i JOIN model_attempts a ON a.invocation_id=i.id "
                "ORDER BY a.started_at,a.id",
            ).fetchall()
        assert len(rows) == 2
        assert all(row["owner_id"] == "trusted-admin" for row in rows)
        assert all(row["purpose"] == "verify_model_profile_version" and row["context_snapshot_id"] for row in rows)
        assert all(row["status"] == "FAILED" for row in rows)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_live_verification_budget_rejection_happens_before_provider_send(tmp_path, monkeypatch) -> None:
    import httpx
    from app.costs import CostService, PriceSnapshot
    from app.db import Database
    from app.model_admin import ModelAdminError, ModelAdminService
    from app.model_control import ModelControlStore
    from app.model_gateway import GatewayError

    db = Database(tmp_path / "admin-verify-budget.db")
    try:
        monkeypatch.setenv("BETTER_AGENT_COST_MODE", "enforce")
        monkeypatch.setenv("MODEL_ADMIN_TEST_KEY", "verification-secret")
        costs = CostService(db)
        service = ModelAdminService(
            db, owner_id="budget-admin", control_store=ModelControlStore(db, costs=costs),
            verification_transport=httpx.MockTransport(lambda _: pytest.fail("budget must block before send")),
        )
        created = service.create_profile(_profile_payload(), validate_capacity=False)
        version_id = created["versions"][0]["id"]
        costs.register_price(version_id, PriceSnapshot("admin-budget-price", 1_000_000, 0, 0, 2_000_000, 0))
        costs.set_budget("budget-admin", "DAILY", costs.today_period(), 0)

        with pytest.raises(GatewayError) as caught:
            await service.verify(version_id)

        assert caught.value.kind == "budget"
        assert service.version(version_id)["verification_status"] == "FAILED"
        with db.connection() as connection:
            invocation = connection.execute(
                "SELECT owner_id,purpose,context_snapshot_id,status FROM model_invocations",
            ).fetchone()
            assert invocation["owner_id"] == "budget-admin"
            assert invocation["purpose"] == "verify_model_profile_version"
            assert invocation["context_snapshot_id"]
            assert invocation["status"] == "BUDGET_BLOCKED"
            assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0

        uncontrolled = ModelAdminService(db, owner_id="no-store-admin")
        uncontrolled_version = uncontrolled.create_profile(
            {**_profile_payload(), "name": "No store verification"}, validate_capacity=False,
        )["versions"][0]["id"]
        with pytest.raises(ModelAdminError, match="requires a model control store"):
            await uncontrolled.verify(uncontrolled_version)
        with db.connection() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM model_invocations WHERE owner_id='no-store-admin'",
            ).fetchone()[0] == 0
    finally:
        db.close()


def test_model_admin_startup_and_api_fallback_keep_the_control_store(tmp_path) -> None:
    from app.main import create_app
    from app.startup import build_runtime

    runtime = build_runtime(tmp_path)
    assert runtime.model_admin.control_store is runtime.model_control_store

    # Exercise the API dependency's fallback constructor without replacing the
    # runtime-owned store that production startup supplied.
    runtime.model_admin = None
    app = create_app(runtime=runtime)
    response = TestClient(app).get("/api/model-profiles", headers={"host": "127.0.0.1:8000"})

    assert response.status_code == 200
    assert runtime.model_admin is not None
    assert runtime.model_admin.control_store is runtime.model_control_store
