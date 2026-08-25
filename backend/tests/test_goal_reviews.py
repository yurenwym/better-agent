import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_goal_program_compiler import fixture
from test_goal_programs import preview, service


class ReviewCompiler:
    def __init__(self, *, needs_adjustment: bool) -> None:
        self.needs_adjustment = needs_adjustment
        self.review_calls = 0
        self.adjust_calls = 0

    async def compile(self, source_markdown, request):
        return fixture()

    async def review(self, evidence):
        self.review_calls += 1
        return {
            "summary": "今天的行动已经完成复盘。",
            "encouragement": "保持当前节奏即可。",
            "needs_adjustment": self.needs_adjustment,
            "adjustment_reason": "今天出现跳过，建议降低下一次任务强度。" if self.needs_adjustment else "",
        }

    async def adjust(self, current, reason):
        self.adjust_calls += 1
        candidate = deepcopy(current)
        candidate["actions"][1]["estimated_minutes"] = 30
        candidate["actions"][1]["title"] = "降低强度后的训练"
        return candidate


def review_services(tmp_path, *, needs_adjustment=True):
    from app.goal_adjustments import GoalAdjustmentService
    from app.goal_reviews import GoalReviewService, ManagedGoalReviewWorker

    db, conversation, goals, version = service(tmp_path)
    compiler = ReviewCompiler(needs_adjustment=needs_adjustment)
    goals.compiler = compiler
    adjustments = GoalAdjustmentService(goals, compiler, conversation.plan_documents)
    reviews = GoalReviewService(goals, compiler, adjustments, queue_delay_seconds=0)
    goals.reviews = reviews
    return db, goals, version, compiler, reviews, ManagedGoalReviewWorker(reviews, poll_interval=.01, lease_seconds=1)


def test_closed_day_queues_one_review_and_worker_creates_confirmable_adjustment(tmp_path) -> None:
    db, goals, version, compiler, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    first = active["actions"][0]

    result = goals.skip_action(first["id"], expected_version=0, idempotency_key="skip")
    assert goals.skip_action(first["id"], expected_version=0, idempotency_key="skip") == result
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_daily_reviews").fetchone()[0] == 1

    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is False
    review = reviews.for_program_date(active["id"], "2026-09-01")

    assert review["status"] == "COMPLETED"
    assert review["signals"] == ["day_interrupted"]
    assert review["proposal"]["status"] == "PENDING"
    assert compiler.review_calls == 1
    assert compiler.adjust_calls == 1
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_adjustment_proposals").fetchone()[0] == 1

    accepted = reviews.adjustments.accept(review["proposal"]["id"], expected_version=0, idempotency_key="accept-auto")
    historical = next(item for item in accepted["program"]["actions"] if item["id"] == first["id"])
    assert historical["status"] == "SKIPPED"
    assert accepted["proposal"]["status"] == "ACCEPTED"


def test_review_without_risk_signal_does_not_create_adjustment(tmp_path) -> None:
    db, goals, version, compiler, reviews, worker = review_services(tmp_path, needs_adjustment=True)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    goals.complete_action(active["actions"][0]["id"], expected_version=0, idempotency_key="complete")

    assert asyncio.run(worker.run_once()) is True
    review = reviews.for_program_date(active["id"], "2026-09-01")
    assert review["status"] == "COMPLETED"
    assert review["signals"] == []
    assert review["proposal"] is None
    assert compiler.review_calls == 1
    assert compiler.adjust_calls == 0


def test_feedback_submitted_after_completion_updates_queued_review_evidence(tmp_path) -> None:
    _, goals, version, _, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    action = active["actions"][0]
    goals.complete_action(action["id"], expected_version=0, idempotency_key="complete")
    goals.feedback(action["id"], {"kind": "difficulty", "difficulty": 5, "actual_minutes": 120}, expected_version=1, idempotency_key="feedback")

    assert asyncio.run(worker.run_once()) is True
    review = reviews.for_program_date(active["id"], "2026-09-01")
    assert review["signals"] == ["high_difficulty", "time_overrun"]
    assert review["proposal"] is not None
    with reviews.db.connection() as connection:
        row = connection.execute("SELECT source_hash FROM goal_daily_reviews WHERE id=?", (review["id"],)).fetchone()
        snapshots = [dict(item) for item in connection.execute(
            "SELECT action_id,action_version,status,title,estimated_minutes,actual_minutes,difficulty "
            "FROM goal_review_action_snapshots WHERE review_id=? ORDER BY action_id", (review["id"],)
        ).fetchall()]
    from app.goal_programs import _hash
    assert row["source_hash"] == _hash(snapshots)


def test_expired_review_lease_is_recovered_without_duplicate_proposal(tmp_path) -> None:
    db, goals, version, _, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    goals.skip_action(active["actions"][0]["id"], expected_version=0, idempotency_key="skip")
    claimed = reviews.claim_next("dead-worker", 1)
    assert claimed is not None
    with db.transaction() as connection:
        connection.execute("UPDATE goal_daily_reviews SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (claimed["id"],))

    assert asyncio.run(worker.run_once()) is True
    assert reviews.for_program_date(active["id"], "2026-09-01")["status"] == "COMPLETED"
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_adjustment_proposals").fetchone()[0] == 1


def test_expired_review_lease_cannot_complete_or_fail(tmp_path) -> None:
    db, goals, version, _, reviews, _ = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    goals.skip_action(active["actions"][0]["id"], expected_version=0, idempotency_key="skip")
    claimed = reviews.claim_next("slow-worker", 1)
    with db.transaction() as connection:
        connection.execute("UPDATE goal_daily_reviews SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (claimed["id"],))
    result = {"summary":"done","encouragement":"keep going","needs_adjustment":False,"adjustment_reason":""}
    with pytest.raises(PermissionError, match="lease lost"):
        reviews.complete(claimed["id"], "slow-worker", result, None)
    with pytest.raises(PermissionError, match="lease lost"):
        reviews.fail(claimed["id"], "slow-worker", "TIMEOUT")


def test_failed_review_can_be_requeued_without_losing_its_evidence(tmp_path) -> None:
    _, goals, version, _, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version); active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    action = active["actions"][0]; goals.complete_action(action["id"], expected_version=0, idempotency_key="complete")
    claimed=reviews.claim_next("broken",1); reviews.fail(claimed["id"],"broken","INVALID_MODEL_OUTPUT")
    claimed=reviews.claim_next("broken",1); reviews.fail(claimed["id"],"broken","INVALID_MODEL_OUTPUT")

    retried=reviews.retry(claimed["id"],idempotency_key="retry")

    assert retried["status"]=="QUEUED" and retried["error_code"] is None
    assert reviews.retry(claimed["id"],idempotency_key="retry")["status"]=="QUEUED"
    assert asyncio.run(worker.run_once()) is True
    assert reviews.for_program_date(active["id"],"2026-09-01")["status"]=="COMPLETED"


def test_review_retry_rejects_a_review_that_has_not_failed(tmp_path) -> None:
    _, goals, version, _, reviews, _ = review_services(tmp_path)
    draft=preview(goals,version);active=goals.activate(draft["id"],expected_version=draft["version"],idempotency_key="activate")
    goals.complete_action(active["actions"][0]["id"],expected_version=0,idempotency_key="complete")

    with pytest.raises(ValueError,match="only failed"):
        reviews.retry(reviews.for_program_date(active["id"],"2026-09-01")["id"],idempotency_key="retry")
    with reviews.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_command_receipts WHERE idempotency_key='retry'").fetchone()[0]==0


def test_review_retry_is_idempotent_under_concurrency(tmp_path) -> None:
    _,goals,version,_,reviews,_=review_services(tmp_path)
    draft=preview(goals,version);active=goals.activate(draft["id"],expected_version=draft["version"],idempotency_key="activate")
    goals.complete_action(active["actions"][0]["id"],expected_version=0,idempotency_key="complete")
    claimed=reviews.claim_next("broken",1);reviews.fail(claimed["id"],"broken","INVALID_MODEL_OUTPUT")
    claimed=reviews.claim_next("broken",1);reviews.fail(claimed["id"],"broken","INVALID_MODEL_OUTPUT")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:reviews.retry(claimed["id"],idempotency_key="same-retry"),range(2)))

    assert results[0]==results[1]
    with reviews.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_program_events WHERE program_id=? AND type='review.retry_queued'",(active["id"],)).fetchone()[0]==1
        assert connection.execute("SELECT COUNT(*) FROM goal_command_receipts WHERE idempotency_key='same-retry'").fetchone()[0]==1


def test_worker_reviews_a_missed_past_day_after_restart(tmp_path, monkeypatch) -> None:
    import app.goal_reviews as module

    _, goals, version, _, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    monkeypatch.setattr(module, "_local_date", lambda timezone_name: "2026-09-02")

    assert asyncio.run(worker.run_once()) is True
    review = reviews.for_program_date(active["id"], "2026-09-01")
    assert review is not None
    assert review["status"] == "COMPLETED"
    assert review["signals"] == ["unfinished_actions"]
