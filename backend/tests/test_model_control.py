import json
import sqlite3

import pytest
import httpx


def test_migration_9_creates_model_control_tables_and_freezes_profile_versions(tmp_path) -> None:
    from app.db import Database

    db = Database(tmp_path / "agent.db")
    with db.connection() as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert 9 in versions and versions[-1] >= 9
    assert {
        "model_profiles",
        "model_profile_versions",
        "model_routing_policies",
        "model_invocations",
        "model_attempts",
    } <= tables

    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_profiles(id,owner_id,name,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("profile-1", "local-user", "主要模型", "ACTIVE", "now", "now"),
        )
        connection.execute(
            "INSERT INTO model_profile_versions("
            "id,profile_id,version,provider_protocol,provider_name,base_url,model_name,credential_env_ref,"
            "capabilities_json,context_window,max_output_tokens,timeout_seconds,max_attempts,config_digest,created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "profile-version-1", "profile-1", 1, "openai_compatible", "test", "https://example.test/v1",
                "demo", "TEST_KEY", json.dumps({"text": True}), 8192, 1024, 30.0, 2, "digest-1", "now",
            ),
        )

    with db.connection() as connection, pytest.raises(sqlite3.IntegrityError, match="frozen"):
        connection.execute("UPDATE model_profile_versions SET model_name='changed' WHERE id='profile-version-1'")


def test_router_filters_capabilities_and_uses_stable_priority() -> None:
    from app.model_control import ModelCandidate, ModelRouter, RoutingError

    router = ModelRouter()
    candidates = [
        ModelCandidate("text-only", 1, {"text": True, "streaming": True, "tool_calling": False}),
        ModelCandidate("tools-b", 2, {"text": True, "streaming": True, "tool_calling": True}),
        ModelCandidate("tools-a", 2, {"text": True, "streaming": True, "tool_calling": True}),
    ]

    selected = router.select(candidates, {"text", "streaming", "tool_calling"})

    assert selected.profile_version_id == "tools-a"
    with pytest.raises(RoutingError, match="capabilities"):
        router.select(candidates, {"vision"})


@pytest.mark.asyncio
async def test_gateway_persists_real_attempts_and_emits_contiguous_run_events(tmp_path, monkeypatch) -> None:
    from app.db import Database
    from app.events import EventStore
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        connection.execute("INSERT INTO goals(id,title,created_at,updated_at) VALUES ('goal-1','Goal','now','now')")
        connection.execute("INSERT INTO runs(id,goal_id,state,created_at,updated_at) VALUES ('run-1','goal-1','RECEIVED','now','now')")
    events = EventStore(db)
    store = ModelControlStore(db, events=events)
    monkeypatch.setenv("MODEL_TEST_KEY", "secret")
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(429, json={"error": {"message": "retry"}})
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

    gateway = ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "MODEL_TEST_KEY", max_attempts=2, network_retries=1, retry_base_seconds=0),
        transport=httpx.MockTransport(handler),
        control_store=store,
    )
    result = await gateway.complete(
        ModelRequest(messages=[{"role": "user", "content": "hi"}]),
        context=ModelCallContext(role="planner", purpose="create_plan", run_id="run-1", goal_id="goal-1"),
    )

    assert result.message == "ok"
    with db.connection() as connection:
        invocation = connection.execute("SELECT * FROM model_invocations").fetchone()
        attempts = connection.execute("SELECT * FROM model_attempts ORDER BY ordinal").fetchall()
    assert invocation["role"] == "planner"
    assert invocation["status"] == "SUCCEEDED"
    assert [(row["ordinal"], row["reason"], row["status"], row["error_kind"]) for row in attempts] == [
        (1, "primary", "FAILED", "rate_limit"),
        (2, "retry", "SUCCEEDED", None),
    ]
    run_events = events.list("run-1")
    assert [event.seq for event in run_events] == list(range(1, len(run_events) + 1))
    assert [event.type for event in run_events] == [
        "model.invocation.created",
        "model.attempt.started",
        "model.attempt.finished",
        "model.attempt.started",
        "model.output.started",
        "model.attempt.finished",
        "model.invocation.finished",
    ]


@pytest.mark.asyncio
async def test_gateway_persists_cancel_before_network_as_cancelled_invocation(tmp_path, monkeypatch) -> None:
    import asyncio

    from app.db import Database
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import GatewayError, ModelGateway, ModelProfile, ModelRequest

    db = Database(tmp_path / "agent.db")
    monkeypatch.setenv("MODEL_TEST_KEY", "secret")
    cancelled = asyncio.Event()
    cancelled.set()
    gateway = ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "MODEL_TEST_KEY"),
        transport=httpx.MockTransport(lambda _: pytest.fail("network must not be called")),
        control_store=ModelControlStore(db),
    )

    with pytest.raises(GatewayError, match="cancelled"):
        await gateway.complete(
            ModelRequest(messages=[]), cancel_event=cancelled,
            context=ModelCallContext(role="conversation", purpose="answer"),
        )
    with db.connection() as connection:
        invocation = connection.execute("SELECT status FROM model_invocations").fetchone()
        attempt_count = connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0]
    assert invocation["status"] == "CANCELLED"
    assert attempt_count == 0


@pytest.mark.asyncio
async def test_connect_failure_closes_attempt_and_invocation(tmp_path, monkeypatch) -> None:
    from app.db import Database
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import GatewayError, ModelGateway, ModelProfile, ModelRequest

    db = Database(tmp_path / "agent.db")
    monkeypatch.setenv("MODEL_TEST_KEY", "secret")

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    gateway = ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "MODEL_TEST_KEY", max_attempts=1, network_retries=0),
        transport=httpx.MockTransport(handler),
        control_store=ModelControlStore(db),
    )
    with pytest.raises(GatewayError) as caught:
        await gateway.complete(ModelRequest(messages=[]), context=ModelCallContext("conversation", "answer"))

    assert caught.value.kind == "provider_unavailable"
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM model_invocations").fetchone()[0] == "FAILED"
        attempt = connection.execute("SELECT status,error_kind FROM model_attempts").fetchone()
    assert tuple(attempt) == ("FAILED", "provider_unavailable")


@pytest.mark.asyncio
async def test_connect_failure_is_retryable_with_separate_attempts(tmp_path, monkeypatch) -> None:
    from app.db import Database
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    db = Database(tmp_path / "agent.db")
    monkeypatch.setenv("MODEL_TEST_KEY", "secret")
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

    gateway = ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "MODEL_TEST_KEY", max_attempts=2, network_retries=1, retry_base_seconds=0),
        transport=httpx.MockTransport(handler), control_store=ModelControlStore(db),
    )
    result = await gateway.complete(ModelRequest(messages=[]), context=ModelCallContext("conversation", "answer"))

    assert result.message == "ok"
    with db.connection() as connection:
        attempts = connection.execute("SELECT ordinal,status,error_kind FROM model_attempts ORDER BY ordinal").fetchall()
    assert [tuple(row) for row in attempts] == [(1, "FAILED", "provider_unavailable"), (2, "SUCCEEDED", None)]
