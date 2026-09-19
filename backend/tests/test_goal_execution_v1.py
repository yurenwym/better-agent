import asyncio
import json

import pytest

from app.goal_programs import GoalProgramConflict
from test_goal_programs import preview
from test_goal_reviews import review_services


def active_services(tmp_path):
    db, goals, version, compiler, reviews, worker = review_services(tmp_path, needs_adjustment=False)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    return db, goals, active, compiler, reviews, worker


def test_only_explicit_close_dispatches_and_repeated_close_reuses_review(tmp_path):
    _, goals, active, compiler, reviews, worker = active_services(tmp_path)
    action = active["actions"][0]
    with pytest.raises(ValueError, match="record progress"):
        goals.close_day(active["id"], "2026-09-01", idempotency_key="empty-close")
    goals.feedback(action["id"], {"kind": "partial", "actual_date": "2026-09-01", "actual_minutes": 45, "remaining_work": "read csv"}, expected_version=0, idempotency_key="partial")
    assert asyncio.run(worker.run_once()) is False
    goals.complete_action(action["id"], expected_version=1, idempotency_key="complete", feedback={"actual_date": "2026-09-01", "actual_minutes": 60})
    reviews.queue_due_reviews()
    assert asyncio.run(worker.run_once()) is False
    result = goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    assert goals.close_day(active["id"], "2026-09-01", idempotency_key="again")["id"] == result["id"]
    assert asyncio.run(worker.run_once()) is True
    assert compiler.review_calls == 1
    assert asyncio.run(worker.run_once()) is False


def test_daily_cumulative_correction_clear_and_reopen(tmp_path):
    _, goals, active, _, _, _ = active_services(tmp_path)
    action = active["actions"][0]
    goals.feedback(action["id"], {"kind": "partial", "actual_date": "2026-09-01", "actual_minutes": 45}, expected_version=0, idempotency_key="partial")
    goals.complete_action(action["id"], expected_version=1, idempotency_key="complete", feedback={"actual_date": "2026-09-01", "actual_minutes": 60})
    assert goals.day_time(active["id"], "2026-09-01")["spent_minutes"] == 60
    goals.reopen_action(action["id"], expected_version=2, idempotency_key="reopen")
    assert goals.day_time(active["id"], "2026-09-01")["spent_minutes"] == 60
    goals.feedback(action["id"], {"kind": "correction", "actual_date": "2026-09-01", "actual_minutes": 30}, expected_version=3, idempotency_key="correct")
    assert goals.day_time(active["id"], "2026-09-01")["spent_minutes"] == 30
    with pytest.raises(GoalProgramConflict):
        goals.feedback(action["id"], {"kind": "correction", "actual_minutes": 4}, expected_version=3, idempotency_key="stale")
    goals.feedback(action["id"], {"kind": "correction", "actual_date": "2026-09-01", "cleared_fields": ["actual_minutes"]}, expected_version=4, idempotency_key="clear")
    timing = goals.day_time(active["id"], "2026-09-01")
    assert timing["spent_minutes"] == 0 and timing["remaining_minutes"] is None


def test_actual_date_early_completion_and_legacy_unknown(tmp_path):
    db, goals, active, _, _, _ = active_services(tmp_path)
    action = active["actions"][1]
    goals.complete_action(action["id"], expected_version=0, idempotency_key="early", feedback={"actual_date": "2026-09-01", "actual_minutes": 25})
    group = goals.today(explicit_date="2026-09-01")["programs"][0]
    assert action["id"] in [item["id"] for item in group["completed"]]
    assert group["day_time"]["spent_minutes"] == 25
    assert goals.day_time(active["id"], "2026-09-02")["spent_minutes"] == 0
    with db.transaction() as connection:
        connection.execute("UPDATE goal_action_feedback SET details_json=? WHERE action_id=?", (json.dumps({}), action["id"]))
    assert not goals.day_time(active["id"], "2026-09-02")["time_complete"]


def test_correction_invalidates_running_review_and_terminal_program_rejects(tmp_path):
    _, goals, active, _, reviews, _ = active_services(tmp_path)
    action = active["actions"][0]
    goals.complete_action(action["id"], expected_version=0, idempotency_key="complete", feedback={"actual_date": "2026-09-01", "actual_minutes": 45})
    review = goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    reviews.claim_next("worker", 30)
    goals.feedback(action["id"], {"kind": "correction", "actual_date": "2026-09-01", "actual_minutes": 30}, expected_version=1, idempotency_key="correct")
    assert reviews.complete(review["id"], "worker", {}, None) is False
    current = goals.get(active["id"])
    goals.transition(active["id"], "pause", expected_version=current["version"], idempotency_key="pause")
    with pytest.raises(GoalProgramConflict):
        goals.reopen_action(action["id"], expected_version=2, idempotency_key="no-reopen")
    with pytest.raises(GoalProgramConflict):
        goals.feedback(action["id"], {"kind": "correction", "note": "x"}, expected_version=2, idempotency_key="no-correct")


def test_help_uses_effective_feedback_and_never_dispatches_expert_by_default(tmp_path):
    from app.goal_context import GoalContextProvider
    _, goals, active, _, _, _ = active_services(tmp_path)
    action = active["actions"][0]
    goals.feedback(action["id"], {"kind": "partial", "actual_minutes": 45, "note": "incorrect old blocker"}, expected_version=0, idempotency_key="partial")
    goals.feedback(action["id"], {"kind": "correction", "actual_minutes": 30, "cleared_fields": ["note"]}, expected_version=1, idempotency_key="correct")
    class ForbiddenAdvisor:
        def __getattr__(self, name):
            raise AssertionError("ordinary help must not call expert advisor")
    goals.expert_advisor = ForbiddenAdvisor()
    help_result = goals.request_help(action["id"], content="Where can I get a CSV?", expected_version=2, idempotency_key="help")
    context = GoalContextProvider(goals.db).load_for_turn(help_result["thread_id"], help_result["turn_id"])
    assert "incorrect old blocker" not in context.context_text
    assert "30" in context.time_context


def test_completed_review_correction_marks_adjustment_stale(tmp_path, monkeypatch):
    monkeypatch.setattr("app.goal_adjustments._local_date", lambda _: "2026-09-01")
    _, goals, version, _, reviews, worker = review_services(tmp_path, needs_adjustment=True)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    action = active["actions"][0]
    goals.complete_action(action["id"], expected_version=0, idempotency_key="complete", feedback={"actual_date": "2026-09-01", "actual_minutes": 100, "difficulty": 5})
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    assert asyncio.run(worker.run_once())
    original = reviews.for_program_date(active["id"], "2026-09-01")
    assert original["proposal"]["status"] == "PENDING"
    goals.feedback(action["id"], {"kind": "correction", "actual_date": "2026-09-01", "actual_minutes": 20}, expected_version=1, idempotency_key="correct")
    stale = reviews.for_program_date(active["id"], "2026-09-01")
    assert stale["evidence_stale"] and stale["proposal"]["status"] == "STALE"
