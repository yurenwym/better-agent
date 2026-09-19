from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from app.costs import BudgetExceeded, CostService
from app.db import Database
from app.model_control import ModelCallContext
from test_cost_control import _registered_cost_handle


@pytest.fixture
def root_budget(migrated_postgres_url, tmp_path):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    costs = CostService(db)
    root = costs.create_root_budget(
        "local-user", "turn", "root-turn", max_attempts=1,
        deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), limit_microusd=1000,
    )
    handle = _registered_cost_handle(db, costs, ModelCallContext("conversation", "answer", root_budget_id=root["id"]))
    yield db, costs, root, handle
    db.close()


def test_concurrent_roles_share_last_root_attempt(root_budget):
    db, costs, root, handle = root_budget
    barrier = Barrier(2)
    def reserve(number):
        call = replace(handle, invocation_id=f"inv-{number}",
                       context=replace(handle.context, role="expert" if number else "conversation"))
        barrier.wait()
        try:
            with db.transaction() as connection:
                costs.reserve_attempt(connection, call, f"attempt-{number}")
            return "reserved"
        except BudgetExceeded:
            return "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, (0, 1))) == ["blocked", "reserved"]
    with db.connection() as connection:
        assert connection.execute("SELECT attempts_started FROM task_budget_roots WHERE id=?", (root["id"],)).fetchone()[0] == 1
    assert costs.summary("local-user", "ROOT", root["id"])["reserved_microusd"] == 140


def test_failed_money_reservation_rolls_back_root_count_and_all_ledgers(root_budget):
    db, costs, root, handle = root_budget
    costs.set_budget("local-user", "DAILY", costs.today_period(), 0)
    with pytest.raises(BudgetExceeded):
        with db.transaction() as connection:
            costs.reserve_attempt(connection, handle, "blocked")
    with db.connection() as connection:
        assert connection.execute("SELECT attempts_started FROM task_budget_roots WHERE id=?", (root["id"],)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0] == 0
    assert costs.summary("local-user", "ROOT", root["id"])["reserved_microusd"] == 0


def test_duplicate_reservation_and_new_service_do_not_reset_root(root_budget):
    db, costs, root, handle = root_budget
    for _ in range(2):
        with db.transaction() as connection:
            costs.reserve_attempt(connection, handle, "same-attempt")
    recovered_db = Database(db.database_url, workspace=db.workspace)
    try:
        recovered = CostService(recovered_db)
        with pytest.raises(BudgetExceeded, match="attempt limit"):
            with recovered_db.transaction() as connection:
                recovered.reserve_attempt(connection, replace(handle, invocation_id="retry"), "new-attempt")
        assert recovered.summary("local-user", "ROOT", root["id"])["reserved_microusd"] == 140
    finally:
        recovered_db.close()


@pytest.mark.parametrize("reason", ["deadline", "owner"])
def test_expired_or_foreign_root_is_rejected_before_cost_reservation(root_budget, reason):
    db, costs, root, handle = root_budget
    if reason == "deadline":
        with db.transaction() as connection:
            connection.execute("UPDATE task_budget_roots SET deadline_at='2000-01-01T00:00:00Z' WHERE id=?", (root["id"],))
    else:
        handle = replace(handle, context=replace(handle.context, owner_id="other-owner"))
    with pytest.raises(BudgetExceeded):
        with db.transaction() as connection:
            costs.reserve_attempt(connection, handle, "blocked")
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0] == 0
