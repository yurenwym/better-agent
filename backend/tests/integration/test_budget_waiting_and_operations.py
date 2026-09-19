from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from threading import Barrier

import httpx
import pytest

from app.conversation import ConversationService
from app.costs import BudgetExceeded, CostService
from app.db import Database
from app.model_control import ModelCallContext, ModelControlStore
from app.model_gateway import GatewayError, ModelGateway, ModelProfile, ModelRequest
from test_cost_control import _registered_cost_handle


def test_operation_root_survives_retry_restart_and_changed_defaults(migrated_postgres_url, tmp_path, monkeypatch):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        service = CostService(db)
        root = service.ensure_default_root_budget("local-user", "goal_operation", "daily-review:1:1")
        handle = _registered_cost_handle(db, service, ModelCallContext("reflector", "daily_review", root_budget_id=root["id"]))
        with db.transaction() as connection:
            service.reserve_attempt(connection, handle, "first")
        before = service.summary("local-user", "ROOT", root["id"])
        monkeypatch.setenv("ROOT_TASK_MAX_ATTEMPTS", "99")
        recovered = CostService(db)
        resumed = recovered.ensure_default_root_budget("local-user", "goal_operation", "daily-review:1:1")
        assert resumed["id"] == root["id"]
        assert resumed["deadline_at"] == root["deadline_at"]
        assert resumed["max_attempts"] == root["max_attempts"]
        assert resumed["attempts_started"] == 1
        assert recovered.summary("local-user", "ROOT", root["id"]) == before
        assert recovered.ensure_default_root_budget("other-owner", "goal_operation", "daily-review:1:1")["id"] != root["id"]
        assert recovered.ensure_default_root_budget("local-user", "goal_operation", "daily-review:1:2")["id"] != root["id"]
    finally:
        db.close()


def test_concurrent_operation_creation_shares_one_root(migrated_postgres_url, tmp_path):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        barrier = Barrier(2)

        def create(_):
            barrier.wait()
            return CostService(db).ensure_default_root_budget("local-user", "goal_operation", "compile:1")

        with ThreadPoolExecutor(max_workers=2) as pool:
            roots = list(pool.map(create, (0, 1)))
        assert roots[0] == roots[1]
        with db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM task_budget_roots").fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unbudgeted_postgres_call_is_blocked_before_network(migrated_postgres_url, tmp_path, monkeypatch):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        monkeypatch.setenv("ISOLATED_BUDGET_KEY", "offline")
        gateway = ModelGateway(
            ModelProfile("https://example.invalid", "offline", "ISOLATED_BUDGET_KEY"),
            transport=httpx.MockTransport(lambda _: pytest.fail("network must not be called")),
            control_store=ModelControlStore(db, costs=CostService(db)),
        )
        with pytest.raises(GatewayError, match="no configured budget") as error:
            await gateway.complete(ModelRequest(messages=[]), context=ModelCallContext("planner", "compile_goal_program"))
        assert error.value.kind == "budget"
        with db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0] == 0
    finally:
        db.close()


@pytest.fixture
def pending_ask(migrated_postgres_url, tmp_path):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    service = ConversationService(db)
    costs = CostService(db)
    thread = service.create_thread("late answer")
    turn = service.accept_turn(thread.id, "request", "Make a plan", [])
    waiting = datetime.now(timezone.utc) - timedelta(days=3)
    root = costs.create_root_budget(
        "local-user", "turn", turn.turn_id, max_attempts=3,
        deadline_at=(waiting + timedelta(minutes=7)).isoformat(), limit_microusd=1000,
    )
    questions = [{"id": "level", "header": "Level", "question": "Your level?", "options": [], "multi_select": False, "allow_free_text": True}]
    with db.transaction() as connection:
        connection.execute("UPDATE turns SET status='AWAITING_INPUT',root_budget_id=? WHERE id=?", (root["id"], turn.turn_id))
        connection.execute("UPDATE turn_jobs SET status='COMPLETED' WHERE turn_id=?", (turn.turn_id,))
        connection.execute(
            "INSERT INTO turn_asks(id,turn_id,call_id,questions_json,status,created_at) VALUES ('late-ask',?,'ask-call',?,'PENDING',?)",
            (turn.turn_id, json.dumps(questions), waiting.isoformat()),
        )
        connection.execute("UPDATE task_budget_roots SET attempts_started=1 WHERE id=?", (root["id"],))
        connection.execute("UPDATE cost_budgets SET charged_microusd=200 WHERE period_key=?", (root["id"],))
    yield db, service, costs, turn, root, waiting
    db.close()


def _answer(service, turn):
    return service.answer_ask(turn.turn_id, turn.version, "answer-once", [{"question_id": "level", "selected_options": [], "free_text": "beginner"}])


def test_late_ask_excludes_wait_without_resetting_spend_or_attempts(pending_ask):
    db, service, costs, turn, root, waiting = pending_ask
    answer = _answer(service, turn)
    assert answer.turn.root_budget_id == root["id"]
    assert 415 < costs.root_seconds_remaining("local-user", root["id"]) <= 420
    before = costs.summary("local-user", "ROOT", root["id"])
    assert before["charged_microusd"] == 200
    with db.connection() as connection:
        resumed = dict(connection.execute("SELECT * FROM task_budget_roots WHERE id=?", (root["id"],)).fetchone())
        event = json.loads(connection.execute("SELECT data_json FROM thread_events WHERE type='ask.answered'").fetchone()[0])
    assert resumed["attempts_started"] == 1
    assert event["excluded_wait_seconds"] >= 3 * 86400
    repeated = _answer(ConversationService(db), turn)
    assert repeated.turn.id == answer.turn.id
    with db.connection() as connection:
        assert dict(connection.execute("SELECT * FROM task_budget_roots WHERE id=?", (root["id"],)).fetchone()) == resumed
    assert costs.summary("local-user", "ROOT", root["id"]) == before


def test_concurrent_answers_only_resume_once(pending_ask):
    db, service, costs, turn, root, waiting = pending_ask
    barrier = Barrier(2)

    def answer(_):
        barrier.wait()
        return _answer(ConversationService(db), turn)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(answer, (0, 1)))
    assert results[0].turn.id == results[1].turn.id
    assert 415 < costs.root_seconds_remaining("local-user", root["id"]) <= 420
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns WHERE parent_turn_id=?", (turn.turn_id,)).fetchone()[0] == 1


@pytest.mark.parametrize("exhausted", ["deadline", "attempts", "money"])
def test_ask_does_not_revive_exhausted_execution_or_reset_limits(pending_ask, exhausted):
    db, service, costs, turn, root, waiting = pending_ask
    with db.transaction() as connection:
        if exhausted == "deadline":
            connection.execute("UPDATE task_budget_roots SET deadline_at=? WHERE id=?", ((waiting - timedelta(seconds=1)).isoformat(), root["id"]))
        elif exhausted == "attempts":
            connection.execute("UPDATE task_budget_roots SET attempts_started=max_attempts WHERE id=?", (root["id"],))
        else:
            connection.execute("UPDATE cost_budgets SET charged_microusd=limit_microusd WHERE period_key=?", (root["id"],))
    result = _answer(service, turn)
    assert result.turn.status == "ACCEPTED"
    handle = _registered_cost_handle(db, costs, ModelCallContext("conversation", "answer", root_budget_id=root["id"]))
    with pytest.raises(BudgetExceeded):
        with db.transaction() as connection:
            costs.reserve_attempt(connection, handle, "continuation-attempt")


def test_historical_ask_without_root_gets_bounded_continuation(pending_ask):
    db, service, costs, turn, root, waiting = pending_ask
    with db.transaction() as connection:
        connection.execute("UPDATE turns SET root_budget_id=NULL WHERE id=?", (turn.turn_id,))
    answer = _answer(service, turn)
    assert answer.turn.root_budget_id and answer.turn.root_budget_id != root["id"]
    assert costs.root_seconds_remaining("local-user", answer.turn.root_budget_id) > 0
    assert costs.summary("local-user", "ROOT", answer.turn.root_budget_id)["limit_microusd"] > 0
