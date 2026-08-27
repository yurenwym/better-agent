import asyncio
import json

import httpx
import pytest


def test_migration_10_creates_append_only_cost_tables(tmp_path) -> None:
    from app.db import Database

    db = Database(tmp_path / "agent.db")
    with db.connection() as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert versions[-1] == 10
    assert {"model_price_snapshots", "cost_budgets", "cost_ledger"} <= tables


def test_integer_cost_formula_and_unknown_price_are_explicit() -> None:
    from app.costs import PriceSnapshot, estimate_cost
    from app.model_gateway import UsageBuckets

    price = PriceSnapshot("price-1", 1_000_000, 100_000, 1_250_000, 2_000_000, 3_000_000)
    result = estimate_cost(UsageBuckets(10, 4, 2, 5, 1), price)
    assert result.microusd == 26
    assert result.status == "ESTIMATED_COMPLETE"

    unavailable = estimate_cost(UsageBuckets(10, 0, 0, 5, 0), None)
    assert unavailable.microusd is None
    assert unavailable.status == "UNAVAILABLE"


def test_budget_reservation_is_atomic_and_replay_safe(tmp_path) -> None:
    from app.costs import BudgetExceeded, CostService
    from app.db import Database

    service = CostService(Database(tmp_path / "agent.db"))
    service.set_budget("local-user", "DAILY", "2026-08-28", 100)

    first = service.reserve("local-user", "DAILY", "2026-08-28", "inv-1", "att-1", 70, "reserve-1")
    replay = service.reserve("local-user", "DAILY", "2026-08-28", "inv-1", "att-1", 70, "reserve-1")
    assert first == replay
    with pytest.raises(BudgetExceeded):
        service.reserve("local-user", "DAILY", "2026-08-28", "inv-2", "att-2", 40, "reserve-2")
    assert service.summary("local-user", "DAILY", "2026-08-28") == {"limit_microusd": 100, "reserved_microusd": 70, "charged_microusd": 0}


def test_settlement_charges_once_and_releases_the_difference(tmp_path) -> None:
    from app.costs import CostService
    from app.db import Database

    service = CostService(Database(tmp_path / "agent.db"))
    service.set_budget("local-user", "DAILY", "2026-08-28", 100)
    service.reserve("local-user", "DAILY", "2026-08-28", "inv-1", "att-1", 70, "reserve-1")
    first = service.settle("local-user", "DAILY", "2026-08-28", "inv-1", "att-1", "price-1", 30, "ESTIMATED_COMPLETE")
    replay = service.settle("local-user", "DAILY", "2026-08-28", "inv-1", "att-1", "price-1", 30, "ESTIMATED_COMPLETE")

    assert first == replay
    assert service.summary("local-user", "DAILY", "2026-08-28") == {"limit_microusd": 100, "reserved_microusd": 0, "charged_microusd": 30}
    with service.db.connection() as connection:
        entries = connection.execute("SELECT entry_type,amount_microusd FROM cost_ledger ORDER BY row_id").fetchall()
    assert [tuple(row) for row in entries] == [("RESERVE", 70), ("CHARGE", 30), ("RELEASE", 40)]


@pytest.mark.asyncio
async def test_budget_block_happens_before_network(tmp_path, monkeypatch) -> None:
    from app.costs import CostService
    from app.db import Database
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import GatewayError, ModelGateway, ModelProfile, ModelRequest

    db = Database(tmp_path / "agent.db")
    costs = CostService(db)
    costs.set_budget("local-user", "DAILY", costs.today_period(), 0)
    monkeypatch.setenv("MODEL_TEST_KEY", "secret")
    gateway = ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "MODEL_TEST_KEY"),
        transport=httpx.MockTransport(lambda _: pytest.fail("network must not be called")),
        control_store=ModelControlStore(db, costs=costs),
    )

    with pytest.raises(GatewayError) as caught:
        await gateway.complete(ModelRequest(messages=[]), context=ModelCallContext("conversation", "answer"))
    assert caught.value.kind == "budget"
    with db.connection() as connection:
        invocation = connection.execute("SELECT status FROM model_invocations").fetchone()
        attempts = connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0]
    assert invocation["status"] == "BUDGET_BLOCKED"
    assert attempts == 0


@pytest.mark.asyncio
async def test_gateway_charges_each_retry_attempt_without_double_counting(tmp_path, monkeypatch) -> None:
    from app.costs import CostService, PriceSnapshot
    from app.db import Database
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    db = Database(tmp_path / "agent.db")
    costs = CostService(db)
    monkeypatch.setenv("MODEL_TEST_KEY", "secret")
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "retry"}})
        return httpx.Response(200, content=(
            'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            'data: {"usage":{"prompt_tokens":10,"completion_tokens":5,"cached_tokens":0}}\n\n'
            'data: [DONE]\n\n'
        ).encode())

    profile = ModelProfile(
        "https://provider.test/v1", "demo", "MODEL_TEST_KEY", max_attempts=2, network_retries=1,
        retry_base_seconds=0, context_window=100, max_output_tokens=20,
    )
    control = ModelControlStore(db, costs=costs)
    gateway = ModelGateway(profile, transport=httpx.MockTransport(handler), control_store=control)
    await gateway.complete(ModelRequest(messages=[]), context=ModelCallContext("conversation", "unmetered_setup"))
    with db.connection() as connection:
        profile_version_id = connection.execute("SELECT id FROM model_profile_versions").fetchone()[0]
        connection.execute("DELETE FROM model_attempts")
        connection.execute("DELETE FROM model_invocations")
    calls = 0
    costs.register_price(profile_version_id, PriceSnapshot("price-1", 1_000_000, 0, 0, 2_000_000, 0))
    costs.set_budget("local-user", "DAILY", costs.today_period(), 1_000)

    result = await gateway.complete(ModelRequest(messages=[]), context=ModelCallContext("conversation", "metered_answer"))
    assert result.message == "ok"
    with db.connection() as connection:
        attempts = connection.execute("SELECT ordinal,status,cost_status,cost_microusd,price_snapshot_id FROM model_attempts ORDER BY ordinal").fetchall()
        ledger = connection.execute("SELECT attempt_id,entry_type,amount_microusd FROM cost_ledger ORDER BY row_id").fetchall()
    assert [tuple(row) for row in attempts] == [
        (1, "FAILED", "ESTIMATED_PARTIAL", 140, "price-1"),
        (2, "SUCCEEDED", "ESTIMATED_COMPLETE", 20, "price-1"),
    ]
    assert [row["entry_type"] for row in ledger] == ["RESERVE", "CHARGE", "RESERVE", "CHARGE", "RELEASE"]
    assert costs.summary("local-user", "DAILY", costs.today_period()) == {
        "limit_microusd": 1_000, "reserved_microusd": 0, "charged_microusd": 160,
    }
