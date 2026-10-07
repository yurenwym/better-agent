"""Concurrent attempt settlement and rollback on real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import pytest

from app.costs import CostService, PriceSnapshot
from app.db import Database
from app.model_control import ModelCallContext, ModelControlStore, open_model_invocation
from app.model_gateway import ModelProfile, ModelRequest, ModelResponse, Timing, UsageBuckets


@pytest.fixture
def attempt(migrated_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("COST_MODE", "enforce")
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        costs = CostService(db)
        control = ModelControlStore(db, costs=costs)
        context = ModelCallContext(role="conversation", purpose="settlement_test", owner_id="local-user")
        handle, _, _ = open_model_invocation(
            control, ModelProfile("https://provider.test/v1", "demo", "MODEL_KEY"),
            ModelRequest(messages=[{"role": "user", "content": "hello"}], max_tokens=2000), context,
        )
        costs.register_price(handle.profile_version_id, PriceSnapshot("settlement-price", 1_000_000, 0, 0, 2_000_000, 0))
        costs.set_budget("local-user", "DAILY", costs.today_period(), 1_000_000)
        attempt_id = control.start_attempt(handle, 1, "primary")
        response = ModelResponse("answer", [], "stop", UsageBuckets(1000, 0, 0, 500, 0), Timing(0, 0, 1), 1)
        yield db, control, costs, handle, attempt_id, response
    finally:
        db.close()


def assert_settled_once(db, costs, handle, attempt_id):
    with db.connection() as connection:
        row = connection.execute("SELECT * FROM model_attempts WHERE id=?", (attempt_id,)).fetchone()
        assert (row["status"], row["error_kind"]) == ("FAILED", "asset_revoked")
        assert (row["uncached_input_tokens"], row["output_tokens"], row["usage_status"]) == (1000, 500, "COMPLETE")
        assert row["cost_microusd"] == 2000
        for event in ("model.attempt.finished", "cost.settled"):
            assert connection.execute(
                "SELECT COUNT(*) FROM model_invocation_events WHERE invocation_id=? AND event_type=?",
                (handle.invocation_id, event),
            ).fetchone()[0] == 1
        # One charge per reserved budget dimension (invocation/day/month),
        # not one ledger row across all dimensions.
        reservations = connection.execute(
            "SELECT period_kind,period_key FROM cost_ledger WHERE attempt_id=? AND entry_type='RESERVE'",
            (attempt_id,),
        ).fetchall()
        charges = connection.execute(
            "SELECT period_kind,period_key,COUNT(*),MIN(amount_microusd) FROM cost_ledger "
            "WHERE attempt_id=? AND entry_type='CHARGE' GROUP BY period_kind,period_key", (attempt_id,),
        ).fetchall()
        assert reservations
        assert {(row[0], row[1]) for row in charges} == {tuple(row) for row in reservations}
        assert all(row[2] == 1 and row[3] == 2000 for row in charges)
    summary = costs.summary("local-user", "DAILY", costs.today_period())
    assert summary["charged_microusd"] == 2000
    assert summary["reserved_microusd"] == 0


def test_concurrent_finish_has_one_settlement_and_one_event(attempt, monkeypatch):
    db, control, costs, handle, attempt_id, response = attempt
    entered, release, duplicate = Event(), Event(), Event()
    original = costs.settle_attempt
    calls = []

    def paused_settlement(*args):
        calls.append(args[2])
        if len(calls) > 1:
            duplicate.set()
        entered.set()
        assert release.wait(15), "settlement was not released"
        return original(*args)

    monkeypatch.setattr(costs, "settle_attempt", paused_settlement)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(control.finish_attempt, handle, 1, "failed", "asset_revoked", response)
        try:
            assert entered.wait(10)
            second = executor.submit(control.finish_attempt, handle, 1, "failed", "asset_revoked", response)
            # Wait for actual database contention, not a sleep-based scheduling guess.
            # Without the row lock, the second caller reaches settlement instead.
            deadline = monotonic() + 10
            blocked = False
            while monotonic() < deadline and not duplicate.is_set():
                with db.connection() as connection:
                    blocked = bool(connection.execute(
                        "SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                        "AND pid<>pg_backend_pid() AND wait_event_type='Lock' "
                        "AND query LIKE 'SELECT status FROM model_attempts%'",
                    ).fetchone())
                if blocked:
                    break
                duplicate.wait(0.02)
            assert blocked or duplicate.is_set(), "second caller did not reach the state check"
        finally:
            release.set()
        first.result(timeout=15)
        second.result(timeout=15)
    assert calls == [attempt_id]
    assert_settled_once(db, costs, handle, attempt_id)


def test_failed_finish_rolls_back_settlement_and_can_be_retried(attempt, monkeypatch):
    db, control, costs, handle, attempt_id, response = attempt
    before = costs.summary("local-user", "DAILY", costs.today_period())
    original = control._event

    def fail_finished_event(connection, context, event_type, *args, **kwargs):
        if event_type == "model.attempt.finished":
            raise RuntimeError("injected audit failure")
        return original(connection, context, event_type, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(control, "_event", fail_finished_event)
        with pytest.raises(RuntimeError, match="injected audit failure"):
            control.finish_attempt(handle, 1, "failed", "asset_revoked", response)
    with db.connection() as connection:
        row = connection.execute("SELECT status,output_tokens FROM model_attempts WHERE id=?", (attempt_id,)).fetchone()
        assert tuple(row) == ("STARTED", None)
        assert connection.execute(
            "SELECT COUNT(*) FROM cost_ledger WHERE attempt_id=? AND entry_type='CHARGE'", (attempt_id,),
        ).fetchone()[0] == 0
    assert costs.summary("local-user", "DAILY", costs.today_period()) == before
    control.finish_attempt(handle, 1, "failed", "asset_revoked", response)
    assert_settled_once(db, costs, handle, attempt_id)
