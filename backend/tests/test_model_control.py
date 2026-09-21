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
async def test_idempotent_replay_never_calls_provider_twice(tmp_path, monkeypatch) -> None:
    from app.db import Database
    from app.model_control import InvocationReplayError, ModelCallContext, ModelControlStore
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    db = Database(tmp_path / "agent.db")
    monkeypatch.setenv("MODEL_TEST_KEY", "secret")
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        )

    gateway = ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "MODEL_TEST_KEY", max_attempts=1),
        transport=httpx.MockTransport(handler),
        control_store=ModelControlStore(db),
    )
    context = ModelCallContext("conversation", "answer", idempotency_key="same-turn")
    request = ModelRequest(messages=[{"role": "user", "content": "hello"}])

    assert (await gateway.complete(request, context=context)).message == "ok"
    with pytest.raises(InvocationReplayError) as replay:
        await gateway.complete(request, context=context)

    assert replay.value.invocation_id
    assert replay.value.status == "SUCCEEDED"
    assert calls == 1
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_request_estimate_matches_transmitted_payload_and_attempt(tmp_path, monkeypatch):
    import hashlib
    from app.db import Database
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    db = Database(tmp_path / "agent.db")
    monkeypatch.setenv("MODEL_TEST_KEY", "secret")
    captured = []

    async def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

    profile = ModelProfile("https://api.deepseek.com", "deepseek-flash", "MODEL_TEST_KEY",
                           counter_id="deepseek-text-estimate", max_attempts=1)
    gateway = ModelGateway(profile, transport=httpx.MockTransport(handler), control_store=ModelControlStore(db))
    await gateway.complete(ModelRequest(messages=[{"role": "user", "content": "桂林行程", "_context_required": True}]),
                           context=ModelCallContext("conversation", "route_and_respond"))
    with db.connection() as connection:
        event = connection.execute("SELECT data_json FROM model_invocation_events WHERE event_type='model.request.estimated'").fetchone()
        attempt = connection.execute("SELECT id FROM model_attempts").fetchone()
    data = json.loads(event["data_json"])
    wire = json.dumps(captured[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert data["model_attempt_id"] == attempt["id"]
    assert data["wire_payload_digest"] == hashlib.sha256(wire).hexdigest()
    assert data["wire_payload_bytes"] == len(wire)
    assert data["estimated_input_tokens"] == (len(wire) + 3) // 4
    assert "secret" not in event["data_json"]


@pytest.mark.asyncio
async def test_idempotency_key_rejects_a_different_request_digest(tmp_path, monkeypatch) -> None:
    from app.db import Database
    from app.model_control import InvocationIdempotencyConflict, ModelCallContext, ModelControlStore
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    db = Database(tmp_path / "agent.db")
    monkeypatch.setenv("MODEL_TEST_KEY", "secret")
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        )

    gateway = ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "MODEL_TEST_KEY", max_attempts=1),
        transport=httpx.MockTransport(handler),
        control_store=ModelControlStore(db),
    )
    context = ModelCallContext("conversation", "answer", idempotency_key="same-turn")

    await gateway.complete(ModelRequest(messages=[{"role": "user", "content": "first"}]), context=context)
    with pytest.raises(InvocationIdempotencyConflict):
        await gateway.complete(ModelRequest(messages=[{"role": "user", "content": "changed"}]), context=context)

    assert calls == 1


def test_child_call_context_scopes_nested_model_purposes() -> None:
    from app.model_control import ModelCallContext, child_call_context

    parent = ModelCallContext(
        "conversation",
        "route_and_respond",
        invocation_id="conversation:turn-1",
        idempotency_key="conversation:turn-1",
    )

    classifier = child_call_context(
        parent,
        role="conversation",
        purpose="classify_research_request",
    )
    answer = child_call_context(
        parent,
        role="conversation",
        purpose="route_and_respond",
    )

    assert classifier.invocation_id == "conversation:turn-1:classify_research_request"
    assert classifier.idempotency_key == "conversation:turn-1:classify_research_request"
    assert answer.invocation_id == "conversation:turn-1"
    assert answer.idempotency_key == "conversation:turn-1"


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
        "model.route.selected",
        "model.attempt.started",
        "model.request.estimated",
        "model.attempt.finished",
        "model.attempt.retry_scheduled",
        "model.attempt.started",
        "model.request.estimated",
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


@pytest.mark.asyncio
@pytest.mark.parametrize("cost_mode", ["enforce", "observe"])
async def test_evaluation_attempt_pins_price_snapshot_while_regular_call_uses_current_price(tmp_path, monkeypatch, cost_mode) -> None:
    monkeypatch.setenv("BETTER_AGENT_COST_MODE", cost_mode)
    from app.costs import CostService, PriceSnapshot
    from app.db import Database
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    db = Database(tmp_path / "agent.db")
    costs = CostService(db)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_profiles(id,owner_id,name,status,created_at,updated_at) "
            "VALUES ('price-profile','local-user','prices','ACTIVE','now','now')"
        )
        connection.execute(
            "INSERT INTO model_profile_versions("
            "id,profile_id,version,provider_protocol,provider_name,base_url,model_name,credential_env_ref,"
            "capabilities_json,context_window,max_output_tokens,timeout_seconds,max_attempts,config_digest,created_at"
            ") VALUES ('price-version','price-profile',1,'openai_compatible','test','https://provider.test/v1','demo',"
            "'MODEL_TEST_KEY','{}',100,20,30,1,'price-config','now')"
        )
    costs.register_price(
        "price-version", PriceSnapshot("price-frozen", 1_000_000, 0, 0, 2_000_000, 0),
        "2026-01-01T00:00:00+00:00",
    )
    costs.register_price(
        "price-version", PriceSnapshot("price-current", 10_000_000, 0, 0, 20_000_000, 0),
        "2026-02-01T00:00:00+00:00",
    )
    costs.set_budget("local-user", "DAILY", costs.today_period(), 10_000)
    monkeypatch.setenv("MODEL_TEST_KEY", "secret")

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=(
            'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            'data: {"usage":{"prompt_tokens":10,"completion_tokens":5,"cached_tokens":0}}\n\n'
            'data: [DONE]\n\n'
        ).encode())

    gateway = ModelGateway(
        ModelProfile(
            "https://provider.test/v1", "demo", "MODEL_TEST_KEY", context_window=100, max_output_tokens=20,
            provider_name="test", registered_profile_version_id="price-version",
        ),
        transport=httpx.MockTransport(handler),
        control_store=ModelControlStore(db, costs=costs),
    )
    await gateway.complete(
        ModelRequest(messages=[]),
        context=ModelCallContext(
            "conversation", "evaluation_baseline", invocation_id="eval-priced",
            price_snapshot_id="price-frozen",
        ),
    )
    await gateway.complete(
        ModelRequest(messages=[]),
        context=ModelCallContext("conversation", "answer", invocation_id="regular-priced"),
    )

    with db.connection() as connection:
        attempts = connection.execute(
            "SELECT invocation_id,price_snapshot_id,cost_microusd FROM model_attempts ORDER BY invocation_id"
        ).fetchall()
        reserves = connection.execute(
            "SELECT invocation_id,price_snapshot_id,amount_microusd FROM cost_ledger "
            "WHERE entry_type='RESERVE' AND period_kind='DAILY' ORDER BY invocation_id"
        ).fetchall()
    assert [tuple(row) for row in attempts] == [
        ("eval-priced", "price-frozen", 20),
        ("regular-priced", "price-current", 200),
    ]
    assert [tuple(row) for row in reserves] == [
        ("eval-priced", "price-frozen", 140 if cost_mode == "enforce" else 0),
        ("regular-priced", "price-current", 1400 if cost_mode == "enforce" else 0),
    ]


    with db.connection() as connection:
        charges = connection.execute(
            "SELECT invocation_id,price_snapshot_id,amount_microusd FROM cost_ledger "
            "WHERE entry_type='CHARGE' AND period_kind='DAILY' ORDER BY invocation_id"
        ).fetchall()
        balance = connection.execute(
            "SELECT reserved_microusd,charged_microusd FROM cost_budgets WHERE period_kind='DAILY'"
        ).fetchone()
        all_reserves = connection.execute("SELECT * FROM cost_ledger WHERE entry_type='RESERVE'").fetchall()
    assert [tuple(row) for row in charges] == [("eval-priced", "price-frozen", 20), ("regular-priced", "price-current", 200)]
    assert tuple(balance) == (0, 220)
    assert len(all_reserves) == (2 if cost_mode == "enforce" else 6)
    assert len({(r["attempt_id"], r["period_kind"], r["period_key"]) for r in all_reserves}) == len(all_reserves)
