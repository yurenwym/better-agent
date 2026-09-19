import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_goal_program_compiler import fixture
from test_goal_programs import preview, service

@pytest.fixture(autouse=True)
def fixed_adjustment_date(monkeypatch):
    monkeypatch.setattr("app.goal_adjustments._local_date",lambda _:"2026-09-01")


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


def test_closed_day_queues_one_review_and_worker_creates_confirmable_adjustment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.goal_reviews._local_date", lambda _timezone_name: "2026-09-02")
    db, goals, version, compiler, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    first = active["actions"][0]

    result = goals.skip_action(first["id"], expected_version=0, idempotency_key="skip")
    assert goals.skip_action(first["id"], expected_version=0, idempotency_key="skip") == result
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
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
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")

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
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    goals.feedback(action["id"], {"kind": "difficulty", "difficulty": 5, "actual_minutes": 120, "actual_date": "2026-09-01"}, expected_version=1, idempotency_key="feedback")

    assert asyncio.run(worker.run_once()) is True
    review = reviews.for_program_date(active["id"], "2026-09-01")
    assert review["signals"] == ["high_difficulty", "time_overrun"]
    assert review["proposal"] is not None
    with reviews.db.connection() as connection:
        row = connection.execute("SELECT source_hash FROM goal_daily_reviews WHERE id=?", (review["id"],)).fetchone()
        snapshots = [dict(item) for item in connection.execute(
            "SELECT action_id,action_version,status,title,estimated_minutes,actual_minutes,difficulty,feedback_json "
            "FROM goal_review_action_snapshots WHERE review_id=? ORDER BY action_id", (review["id"],)
        ).fetchall()]
    from app.goal_programs import _hash
    import json
    for item in snapshots:
        item["feedback"] = json.loads(item.pop("feedback_json"))
    assert row["source_hash"] == _hash(snapshots)


def test_expired_review_lease_is_recovered_without_duplicate_proposal(tmp_path) -> None:
    db, goals, version, _, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    goals.skip_action(active["actions"][0]["id"], expected_version=0, idempotency_key="skip")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
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
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
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
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    claimed=reviews.claim_next("broken",1); reviews.fail(claimed["id"],"broken","INVALID_MODEL_OUTPUT")
    claimed=reviews.claim_next("broken",1); reviews.fail(claimed["id"],"broken","INVALID_MODEL_OUTPUT")

    retried=reviews.retry(claimed["id"],idempotency_key="retry")

    assert retried["status"]=="QUEUED" and retried["error_code"] is None
    assert retried["revision"] == claimed["revision"]
    assert retried["history"][-1]["revision"] == claimed["revision"]
    assert reviews.retry(claimed["id"],idempotency_key="retry")["status"]=="QUEUED"
    assert asyncio.run(worker.run_once()) is True
    assert reviews.for_program_date(active["id"],"2026-09-01")["status"]=="COMPLETED"


def test_review_retry_rejects_a_review_that_has_not_failed(tmp_path) -> None:
    _, goals, version, _, reviews, _ = review_services(tmp_path)
    draft=preview(goals,version);active=goals.activate(draft["id"],expected_version=draft["version"],idempotency_key="activate")
    goals.complete_action(active["actions"][0]["id"],expected_version=0,idempotency_key="complete")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")

    with pytest.raises(ValueError,match="only failed"):
        reviews.retry(reviews.for_program_date(active["id"],"2026-09-01")["id"],idempotency_key="retry")
    with reviews.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_command_receipts WHERE idempotency_key='retry'").fetchone()[0]==0


def test_review_retry_is_idempotent_under_concurrency(tmp_path) -> None:
    _,goals,version,_,reviews,_=review_services(tmp_path)
    draft=preview(goals,version);active=goals.activate(draft["id"],expected_version=draft["version"],idempotency_key="activate")
    goals.complete_action(active["actions"][0]["id"],expected_version=0,idempotency_key="complete")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    claimed=reviews.claim_next("broken",1);reviews.fail(claimed["id"],"broken","INVALID_MODEL_OUTPUT")
    claimed=reviews.claim_next("broken",1);reviews.fail(claimed["id"],"broken","INVALID_MODEL_OUTPUT")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:reviews.retry(claimed["id"],idempotency_key="same-retry"),range(2)))

    assert results[0]==results[1]
    with reviews.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_program_events WHERE program_id=? AND type='review.retry_queued'",(active["id"],)).fetchone()[0]==1
        assert connection.execute("SELECT COUNT(*) FROM goal_command_receipts WHERE idempotency_key='same-retry'").fetchone()[0]==1


def test_worker_does_not_review_a_missed_past_day_after_restart(tmp_path, monkeypatch) -> None:
    import app.goal_reviews as module
    monkeypatch.setattr("app.goal_adjustments._local_date", lambda timezone_name: "2026-09-02")

    _, goals, version, _, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    monkeypatch.setattr(module, "_local_date", lambda timezone_name: "2026-09-02")

    assert asyncio.run(worker.run_once()) is False
    assert reviews.for_program_date(active["id"], "2026-09-01") is None


def test_adjustment_failure_preserves_review_and_retries_only_adjustment(tmp_path, monkeypatch):
    monkeypatch.setattr("app.goal_reviews._local_date", lambda _: "2026-09-01")
    _, goals, version, compiler, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    goals.skip_action(active["actions"][0]["id"], expected_version=0, idempotency_key="skip")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    original = reviews.adjustments.propose
    async def broken(*args, **kwargs):
        raise RuntimeError("temporary provider failure")
    monkeypatch.setattr(reviews.adjustments, "propose", broken)
    assert asyncio.run(worker.run_once())
    first = reviews.for_program_date(active["id"], "2026-09-01")
    assert first["status"] == "COMPLETED" and first["summary"]
    assert first["adjustment_status"] == "PENDING"
    assert asyncio.run(worker.run_once())
    failed = reviews.for_program_date(active["id"], "2026-09-01")
    assert failed["adjustment_status"] == "FAILED"
    retry = reviews.retry(failed["id"], idempotency_key="retry-proposal")
    assert retry["revision"] == first["revision"] and retry["summary"] == first["summary"]
    monkeypatch.setattr(reviews.adjustments, "propose", original)
    assert asyncio.run(worker.run_once())
    final = reviews.for_program_date(active["id"], "2026-09-01")
    assert final["adjustment_status"] == "COMPLETED" and final["proposal"]
    assert compiler.review_calls == 1


def test_no_change_is_success_without_forcing_model_to_invent_adjustment(tmp_path, monkeypatch):
    monkeypatch.setattr("app.goal_reviews._local_date", lambda _: "2026-09-01")
    _, goals, version, compiler, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    goals.skip_action(active["actions"][0]["id"], expected_version=0, idempotency_key="skip")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    async def unchanged(current, reason):
        compiler.adjust_calls += 1
        return deepcopy(current)
    monkeypatch.setattr(compiler, "adjust", unchanged)
    assert asyncio.run(worker.run_once())
    review = reviews.for_program_date(active["id"], "2026-09-01")
    assert review["status"] == "COMPLETED"
    assert review["adjustment_status"] == "NO_CHANGE" and review["proposal"] is None
    assert compiler.adjust_calls == 1 and compiler.review_calls == 1


def test_completed_program_does_not_generate_adjustment(tmp_path, monkeypatch):
    monkeypatch.setattr("app.goal_reviews._local_date", lambda _: "2026-09-01")
    _, goals, version, compiler, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    for action in active["actions"]:
        goals.complete_action(action["id"], expected_version=0, idempotency_key=action["id"])
    goals.feedback(active["actions"][0]["id"], {"kind": "difficulty", "difficulty": 5, "actual_date": "2026-09-01"}, expected_version=1, idempotency_key="difficulty")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    assert asyncio.run(worker.run_once())
    review = reviews.for_program_date(active["id"], "2026-09-01")
    assert review["status"] == "COMPLETED" and review["adjustment_status"] == "NO_CHANGE"
    assert compiler.adjust_calls == 0


@pytest.mark.parametrize("operation", ["queue_due_reviews", "claim_next"])
def test_worker_recovers_after_queue_or_claim_exception(tmp_path, monkeypatch, operation):
    _, _, _, _, reviews, worker = review_services(tmp_path)
    original = getattr(reviews, operation)
    calls = 0
    def flaky(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(reviews, operation, flaky)
    async def exercise():
        await worker.start()
        for _ in range(100):
            if worker.last_success_at:
                break
            await asyncio.sleep(.01)
        assert worker.health_view()["running"]
        assert worker.last_success_at and worker.last_error_code == "RUNTIMEERROR"
        await worker.stop()
        assert not worker.health_view()["running"]
    asyncio.run(exercise())


def test_worker_stop_cancels_blocked_model_within_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr("app.goal_reviews._local_date", lambda _: "2026-09-01")
    _, goals, version, compiler, _, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    goals.complete_action(active["actions"][0]["id"], expected_version=0, idempotency_key="done")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    async def exercise():
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def blocked(evidence):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        monkeypatch.setattr(compiler, "review", blocked)
        worker.shutdown_timeout = .01
        await worker.start()
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(worker.stop(), .5)
        assert cancelled.is_set()
    asyncio.run(exercise())


def test_worker_lease_loss_cancels_model_and_cannot_publish(tmp_path, monkeypatch):
    monkeypatch.setattr("app.goal_reviews._local_date", lambda _: "2026-09-01")
    _, goals, version, compiler, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    goals.complete_action(active["actions"][0]["id"], expected_version=0, idempotency_key="done")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    async def exercise():
        cancelled = asyncio.Event()
        async def blocked(evidence):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        monkeypatch.setattr(compiler, "review", blocked)
        monkeypatch.setattr(reviews, "renew", lambda *args: False)
        worker.lease_seconds = .15
        assert await asyncio.wait_for(worker.run_once(), 1)
        assert cancelled.is_set()
        assert reviews.for_program_date(active["id"], "2026-09-01")["summary"] is None
    asyncio.run(exercise())


def test_refresh_unchanged_completed_evidence_does_not_mark_stale(tmp_path, monkeypatch):
    monkeypatch.setattr("app.goal_reviews._local_date", lambda _: "2026-09-01")
    db, goals, version, _, reviews, worker = review_services(tmp_path, needs_adjustment=False)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    action = active["actions"][0]
    goals.complete_action(action["id"], expected_version=0, idempotency_key="done")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    assert asyncio.run(worker.run_once())
    with db.transaction() as connection:
        reviews.refresh_queued_feedback(connection, action["id"])
    assert not reviews.for_program_date(active["id"], "2026-09-01")["evidence_stale"]


def test_worker_stop_is_bounded_even_when_provider_suppresses_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr("app.goal_reviews._local_date", lambda _: "2026-09-01")
    _, goals, version, compiler, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    goals.complete_action(active["actions"][0]["id"], expected_version=0, idempotency_key="done")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    async def exercise():
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def stubborn(evidence):
            started.set()
            try:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                return {"summary": "Must not publish", "encouragement": "", "needs_adjustment": False, "adjustment_reason": ""}
            finally:
                finished.set()
        monkeypatch.setattr(compiler, "review", stubborn)
        worker.shutdown_timeout = .01
        await worker.start()
        await asyncio.wait_for(started.wait(), 1)
        try:
            await asyncio.wait_for(worker.stop(), .3)
            assert worker.last_error_code in {"WORKER_SHUTDOWN_TIMEOUT", "MODEL_CANCELLATION_TIMEOUT"}
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), 1)
            await asyncio.sleep(.01)
        assert reviews.for_program_date(active["id"], "2026-09-01")["summary"] is None
    asyncio.run(exercise())


def test_failure_persistence_outage_does_not_kill_worker(tmp_path, monkeypatch):
    monkeypatch.setattr("app.goal_reviews._local_date", lambda _: "2026-09-01")
    _, goals, version, compiler, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    goals.complete_action(active["actions"][0]["id"], expected_version=0, idempotency_key="done")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    async def broken(evidence):
        raise RuntimeError("provider failed")
    def fail(*args):
        raise OSError("database temporarily offline")
    monkeypatch.setattr(compiler, "review", broken)
    monkeypatch.setattr(reviews, "fail", fail)
    async def exercise():
        await worker.start()
        for _ in range(100):
            if worker.last_error_code and worker.last_success_at:
                break
            await asyncio.sleep(.01)
        assert worker.health_view()["running"] and worker.last_error_code == "OSERROR"
        assert worker.last_success_at
        await worker.stop()
    asyncio.run(exercise())


def test_heartbeat_database_failure_cancels_model_and_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr("app.goal_reviews._local_date", lambda _: "2026-09-01")
    _, goals, version, compiler, reviews, worker = review_services(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    goals.complete_action(active["actions"][0]["id"], expected_version=0, idempotency_key="done")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    async def exercise():
        cancelled = asyncio.Event()
        async def blocked(evidence):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        def unavailable(*args):
            raise OSError("lease renewal unavailable")
        monkeypatch.setattr(compiler, "review", blocked)
        monkeypatch.setattr(reviews, "renew", unavailable)
        worker.lease_seconds = 1
        assert await asyncio.wait_for(worker.run_once(), 1)
        assert cancelled.is_set()
        row = reviews.for_program_date(active["id"], "2026-09-01")
        assert row["status"] == "QUEUED" and row["error_code"] == "OSERROR"
    asyncio.run(exercise())


@pytest.mark.parametrize("changed_evidence", [False, True])
def test_review_retry_budget_identity_changes_only_with_evidence(tmp_path, monkeypatch, changed_evidence):
    monkeypatch.setattr("app.goal_reviews._local_date", lambda _: "2026-09-01")
    _, goals, version, compiler, reviews, worker = review_services(tmp_path, needs_adjustment=False)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    action = active["actions"][0]
    goals.complete_action(action["id"], expected_version=0, idempotency_key="done")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")
    operations = []
    original_context = goals._model_context
    def tracked_context(*args, **kwargs):
        operations.append(kwargs["operation_id"])
        return original_context(*args, **kwargs)
    monkeypatch.setattr(goals, "_model_context", tracked_context)
    class ExhaustedBudget(RuntimeError):
        code = "TASK_BUDGET_EXCEEDED"
    async def denied(evidence):
        raise ExhaustedBudget("budget remains exhausted")
    monkeypatch.setattr(compiler, "review", denied)
    assert asyncio.run(worker.run_once())
    assert asyncio.run(worker.run_once())
    failed = reviews.for_program_date(active["id"], "2026-09-01")
    assert failed["status"] == "FAILED"
    if changed_evidence:
        goals.feedback(action["id"], {"kind": "note", "note": "Corrected execution evidence"}, expected_version=1, idempotency_key="new-evidence")
    retried = reviews.retry(failed["id"], idempotency_key="retry")
    assert retried["revision"] == failed["revision"] + int(changed_evidence)
    assert len(retried["history"]) == 1
    assert asyncio.run(worker.run_once())
    assert operations[:2] == [f"{failed['id']}:{failed['revision']}"] * 2
    assert operations[-1] == f"{failed['id']}:{retried['revision']}"
    assert (operations[-1] != operations[0]) is changed_evidence
    assert reviews.for_program_date(active["id"], "2026-09-01")["error_code"] == "TASK_BUDGET_EXCEEDED"
