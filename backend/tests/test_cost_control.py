import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest


@pytest.fixture(autouse=True)
def _enforce_cost_limits(monkeypatch):
    """These cases assert *enforcement*, so they must declare the enforcing mode.

    `monetary_limits_enabled()` reads `BETTER_AGENT_COST_MODE`, which defaults to
    `observe` — a mode where every limit is still recorded but never blocks. Under
    that default the assertions here are vacuous: `reserved_microusd` stays 0
    because the reserved amount is 0, and nothing ever raises `BudgetExceeded`.
    Declaring the mode in the fixture keeps the file self-contained instead of
    silently depending on the operator's shell environment.
    """
    monkeypatch.setenv("BETTER_AGENT_COST_MODE", "enforce")


def test_migration_10_creates_append_only_cost_tables(tmp_path) -> None:
    from app.db import Database

    db = Database(tmp_path / "agent.db")
    with db.connection() as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 10 in versions and versions[-1] >= 10
    assert {"model_price_snapshots", "cost_budgets", "cost_ledger"} <= tables


def test_cost_ledger_uniqueness_is_scoped_by_budget_period(tmp_path) -> None:
    from app.db import Database

    db = Database(tmp_path / "agent.db")
    with db.connection() as connection:
        indexes = {
            row["name"]: row["sql"]
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='cost_ledger'"
            )
        }
    assert "uq_cost_attempt_entry" not in indexes
    assert "period_kind" in indexes["uq_cost_attempt_period_entry"]
    assert "period_key" in indexes["uq_cost_attempt_period_entry"]


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


def test_concurrent_reservations_cannot_overspend_a_budget(tmp_path) -> None:
    from app.costs import BudgetExceeded, CostService
    from app.db import Database

    service = CostService(Database(tmp_path / "agent.db"))
    service.set_budget("local-user", "DAILY", "2026-08-28", 100)
    barrier = Barrier(2)

    def reserve(number: int) -> str:
        barrier.wait()
        try:
            service.reserve(
                "local-user", "DAILY", "2026-08-28", f"inv-{number}",
                f"att-{number}", 70, f"reserve-{number}",
            )
            return "reserved"
        except BudgetExceeded:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, (1, 2)))

    assert sorted(results) == ["blocked", "reserved"]
    assert service.summary("local-user", "DAILY", "2026-08-28") == {
        "limit_microusd": 100, "reserved_microusd": 70, "charged_microusd": 0,
    }


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


def test_three_level_attempt_reservation_is_atomic_and_unconfigured_levels_are_unlimited(tmp_path) -> None:
    from app.costs import BudgetExceeded, CostService
    from app.db import Database
    from app.model_control import ModelCallContext, ModelCallHandle

    db = Database(tmp_path / "agent.db")
    service = CostService(db)
    handle = _registered_cost_handle(db, service, ModelCallContext("conversation", "answer"))
    daily = service.today_period()
    monthly = daily[:7]
    service.set_budget("local-user", "INVOCATION", handle.invocation_id, 1_000)
    service.set_budget("local-user", "DAILY", daily, 1_000)
    service.set_budget("local-user", "MONTHLY", monthly, 139)

    with pytest.raises(BudgetExceeded):
        with db.transaction() as connection:
            service.reserve_attempt(connection, handle, "att-three-level")

    assert service.summary("local-user", "INVOCATION", handle.invocation_id)["reserved_microusd"] == 0
    assert service.summary("local-user", "DAILY", daily)["reserved_microusd"] == 0
    assert service.summary("local-user", "MONTHLY", monthly)["reserved_microusd"] == 0
    with db.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cost_ledger WHERE attempt_id='att-three-level'"
        ).fetchone()[0] == 0

    service.set_budget("local-user", "MONTHLY", monthly, 1_000)
    with db.transaction() as connection:
        service.reserve_attempt(connection, handle, "att-three-level")
    with db.transaction() as connection:
        service.reserve_attempt(connection, handle, "att-three-level")
    with db.connection() as connection:
        periods = connection.execute(
            "SELECT period_kind,amount_microusd FROM cost_ledger WHERE attempt_id=? ORDER BY period_kind",
            ("att-three-level",),
        ).fetchall()
    assert [tuple(row) for row in periods] == [
        ("DAILY", 140), ("INVOCATION", 140), ("MONTHLY", 140),
    ]
    assert service.summary("local-user", "INVOCATION", handle.invocation_id)["reserved_microusd"] == 140
    assert service.summary("local-user", "DAILY", daily)["reserved_microusd"] == 140
    assert service.summary("local-user", "MONTHLY", monthly)["reserved_microusd"] == 140

    unlimited = ModelCallHandle("inv-unlimited", handle.profile_version_id, handle.context)
    with db.transaction() as connection:
        service.reserve_attempt(connection, unlimited, "att-unlimited")
    with db.connection() as connection:
        kinds = connection.execute(
            "SELECT period_kind FROM cost_ledger WHERE attempt_id='att-unlimited' ORDER BY period_kind"
        ).fetchall()
    assert [row["period_kind"] for row in kinds] == ["DAILY", "MONTHLY"]

    unmetered = ModelCallHandle(
        "inv-unmetered", handle.profile_version_id,
        ModelCallContext("conversation", "answer", owner_id="unmetered-user"),
    )
    with db.transaction() as connection:
        service.reserve_attempt(connection, unmetered, "att-unmetered")
    with db.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cost_ledger WHERE attempt_id='att-unmetered'"
        ).fetchone()[0] == 0


def test_three_level_settlement_and_release_are_append_only_idempotent_and_replayable(tmp_path) -> None:
    from app.costs import CostService
    from app.db import Database
    from app.model_control import ModelCallContext
    from app.model_gateway import UsageBuckets

    db = Database(tmp_path / "agent.db")
    service = CostService(db)
    handle = _registered_cost_handle(db, service, ModelCallContext("conversation", "answer"))
    periods = {
        "INVOCATION": handle.invocation_id,
        "DAILY": service.today_period(),
        "MONTHLY": service.today_period()[:7],
    }
    for kind, key in periods.items():
        service.set_budget("local-user", kind, key, 1_000)
    with db.transaction() as connection:
        service.reserve_attempt(connection, handle, "att-settle-three")
        first = service.settle_attempt(
            connection, handle, "att-settle-three", UsageBuckets(10, 0, 0, 5, 0)
        )
    with db.transaction() as connection:
        replay = service.settle_attempt(
            connection, handle, "att-settle-three", UsageBuckets(10, 0, 0, 5, 0)
        )

    assert first == replay
    assert first.microusd == 20
    for kind, key in periods.items():
        assert service.summary("local-user", kind, key) == {
            "limit_microusd": 1_000, "reserved_microusd": 0, "charged_microusd": 20,
        }
    with db.connection() as connection:
        entries = connection.execute(
            "SELECT period_kind,entry_type,amount_microusd FROM cost_ledger "
            "WHERE attempt_id=? ORDER BY period_kind,row_id",
            ("att-settle-three",),
        ).fetchall()
        with pytest.raises(Exception, match="append-only"):
            connection.execute(
                "UPDATE cost_ledger SET amount_microusd=0 WHERE attempt_id='att-settle-three'"
            )
    assert [tuple(row) for row in entries] == [
        ("DAILY", "RESERVE", 140), ("DAILY", "CHARGE", 20), ("DAILY", "RELEASE", 120),
        ("INVOCATION", "RESERVE", 140), ("INVOCATION", "CHARGE", 20), ("INVOCATION", "RELEASE", 120),
        ("MONTHLY", "RESERVE", 140), ("MONTHLY", "CHARGE", 20), ("MONTHLY", "RELEASE", 120),
    ]


def test_missing_usage_conservatively_charges_all_configured_levels(tmp_path) -> None:
    from app.costs import CostService
    from app.db import Database
    from app.model_control import ModelCallContext

    db = Database(tmp_path / "agent.db")
    service = CostService(db)
    handle = _registered_cost_handle(db, service, ModelCallContext("conversation", "answer"))
    periods = {
        "INVOCATION": handle.invocation_id,
        "DAILY": service.today_period(),
        "MONTHLY": service.today_period()[:7],
    }
    for kind, key in periods.items():
        service.set_budget("local-user", kind, key, 1_000)
    with db.transaction() as connection:
        service.reserve_attempt(connection, handle, "att-no-usage")
        result = service.settle_attempt(connection, handle, "att-no-usage", None)

    assert result.microusd == 140
    assert result.status == "ESTIMATED_PARTIAL"
    for kind, key in periods.items():
        assert service.summary("local-user", kind, key) == {
            "limit_microusd": 1_000, "reserved_microusd": 0, "charged_microusd": 140,
        }
    with db.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cost_ledger WHERE attempt_id=? AND entry_type='RELEASE'",
            ("att-no-usage",),
        ).fetchone()[0] == 0


def _registered_cost_handle(db, service, context):
    from app.costs import PriceSnapshot
    from app.model_control import ModelCallHandle

    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_profiles(id,owner_id,name,status,created_at,updated_at) "
            "VALUES ('profile','local-user','test','ACTIVE','now','now')"
        )
        connection.execute(
            "INSERT INTO model_profile_versions("
            "id,profile_id,version,provider_protocol,provider_name,base_url,model_name,credential_env_ref,"
            "capabilities_json,context_window,max_output_tokens,timeout_seconds,max_attempts,config_digest,created_at"
            ") VALUES ('profile-v1','profile',1,'openai_compatible','test','https://test.invalid','model','TEST_KEY',"
            "'{}',100,20,1,1,'digest','now')"
        )
    service.register_price(
        "profile-v1", PriceSnapshot("price-v1", 1_000_000, 0, 0, 2_000_000, 0)
    )
    return ModelCallHandle("inv-three-level", "profile-v1", context)


@pytest.mark.asyncio
@pytest.mark.parametrize("period_kind", ["INVOCATION", "DAILY", "MONTHLY"])
async def test_budget_block_happens_before_network(tmp_path, monkeypatch, period_kind) -> None:
    from app.costs import CostService
    from app.db import Database
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import GatewayError, ModelGateway, ModelProfile, ModelRequest

    db = Database(tmp_path / "agent.db")
    costs = CostService(db)
    invocation_id = f"inv-blocked-{period_kind.lower()}"
    period_key = {
        "INVOCATION": invocation_id,
        "DAILY": costs.today_period(),
        "MONTHLY": costs.today_period()[:7],
    }[period_kind]
    costs.set_budget("local-user", period_kind, period_key, 0)
    monkeypatch.setenv("MODEL_TEST_KEY", "secret")
    gateway = ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "MODEL_TEST_KEY"),
        transport=httpx.MockTransport(lambda _: pytest.fail("network must not be called")),
        control_store=ModelControlStore(db, costs=costs),
    )

    with pytest.raises(GatewayError) as caught:
        await gateway.complete(
            ModelRequest(messages=[]),
            context=ModelCallContext("conversation", "answer", invocation_id=invocation_id),
        )
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
        # `model_invocation_events` (migration 37) also references `model_invocations`,
        # so it must be cleared first or the delete below trips a foreign key constraint.
        connection.execute("DELETE FROM model_invocation_events")
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


def test_usage_summary_groups_authoritative_attempt_metrics_and_preserves_unknown_cost(tmp_path) -> None:
    from app.costs import CostService
    from app.db import Database
    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        connection.execute("INSERT INTO model_profiles(id,owner_id,name,status,created_at,updated_at) VALUES ('p','local-user','m','ACTIVE','n','n')")
        connection.execute("INSERT INTO model_profile_versions(id,profile_id,version,provider_protocol,provider_name,base_url,model_name,credential_env_ref,capabilities_json,context_window,max_output_tokens,timeout_seconds,max_attempts,config_digest,created_at) VALUES ('v','p',1,'openai_compatible','provider','https://x','m','KEY','{}',1,1,1,1,'d','n')")
        connection.execute("INSERT INTO model_invocations(id,owner_id,role,purpose,routing_policy_digest,route_snapshot_json,request_digest,tool_schema_digest,context_snapshot_digest,status,idempotency_key,created_at) VALUES ('i','local-user','planner','plan','d','{}','r','t','c','SUCCEEDED','i','2026-08-28T00:00:00+00:00')")
        connection.execute("INSERT INTO model_attempts(id,invocation_id,ordinal,reason,profile_version_id,provider_protocol,request_digest,status,output_tokens,started_at,first_token_at,finished_at,usage_status,cost_status,cost_microusd) VALUES ('a','i',1,'fallback','v','openai_compatible','r','SUCCEEDED',10,'2026-08-28T00:00:00+00:00','2026-08-28T00:00:01+00:00','2026-08-28T00:00:03+00:00','COMPLETE','UNAVAILABLE',NULL)")
    assert CostService(db).usage_summary("local-user")["groups"] == [{
        "role":"planner","provider":"provider","profile_version_id":"v","attempts":1,"succeeded":1,
        "fallbacks":1,"cost_microusd":0,"unknown_cost_attempts":1,"success_rate":1.0,"fallback_rate":1.0,
        "ttft_seconds":1.0,"tps":5.0,"p95_latency_seconds":3.0,
    }]


def test_default_invocation_budget_is_materialized_for_each_real_invocation(tmp_path) -> None:
    from app.costs import CostService
    from app.db import Database
    from app.model_control import ModelCallContext, ModelCallHandle
    db = Database(tmp_path / "agent.db")
    service = CostService(db)
    base = _registered_cost_handle(db, service, ModelCallContext("conversation", "answer"))
    service.set_budget("local-user", "INVOCATION", "default", 1_000)
    handle = ModelCallHandle("inv-from-template", base.profile_version_id, base.context)
    with db.transaction() as connection:
        service.reserve_attempt(connection, handle, "attempt-from-template")
    assert service.summary("local-user", "INVOCATION", "inv-from-template") == {
        "limit_microusd":1_000,"reserved_microusd":140,"charged_microusd":0,
    }
