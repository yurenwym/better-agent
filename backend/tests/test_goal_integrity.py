"""Execution contracts shared by SQLite and isolated PostgreSQL acceptance."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest

from app.conversation import ConversationService
from app.db import Database
from app.goal_adjustments import GoalAdjustmentService
from app.goal_program_compiler import FixedGoalProgramCompiler, GoalCompilationError
from app.goal_programs import GoalProgramConflict, GoalProgramService
from app.goal_reviews import GoalReviewService


@pytest.fixture
def integrity_database(tmp_path):
    db = Database(tmp_path / "agent.db")
    yield db
    db.close()


@pytest.fixture
def scenario(integrity_database, monkeypatch):
    monkeypatch.setattr("app.goal_adjustments._local_date",lambda _:"2026-09-14")
    monkeypatch.setattr("app.goal_programs._execution_date",lambda _:"2026-09-15")
    monkeypatch.setattr("app.goal_reviews._local_date",lambda _:"2026-09-15")
    db=integrity_database
    conversation=ConversationService(db)
    thread=conversation.create_thread("Sales analysis")
    plan=conversation.plan_documents.save_model_revision(thread_id=thread.id,title="Sales analysis",markdown_content="# Sales analysis\n30 calendar days; Sunday off; 60 minutes/day.",source_turn_id=None,source_message_id=None,actor="user")
    compiler=FixedGoalProgramCompiler()
    goals=GoalProgramService(db,compiler,plan_documents=conversation.plan_documents,conversation=conversation)
    adjustments=GoalAdjustmentService(goals,compiler,conversation.plan_documents)
    reviews=GoalReviewService(goals,compiler,adjustments,queue_delay_seconds=0)
    goals.reviews=reviews
    return goals,adjustments,reviews,plan


def preview(scenario,days=7):
    goals,_,_,plan=scenario
    return asyncio.run(goals.preview(plan.plan_document_id,start_date="2026-09-14",requested_end_date=(date(2026,9,14)+timedelta(days=days-1)).isoformat(),timezone_name="Asia/Shanghai",daily_minutes=60,constraints={"available_weekdays":[0,1,2,3,4,5],"excluded_dates":[]},idempotency_key="preview"))


def active(scenario,minutes=60):
    goals=scenario[0]
    if minutes!=60:
        compile_program=goals.compiler.compile
        async def shorter(markdown,request):
            value=await compile_program(markdown,request)
            for action in value["actions"]:action["estimated_minutes"]=minutes
            return value
        goals.compiler.compile=shorter
    draft=preview(scenario)
    return goals.activate(draft["id"],expected_version=draft["version"],idempotency_key="activate")


def test_thirty_days_persist_rest_days_and_restart(scenario):
    goals=scenario[0]
    draft=preview(scenario,30)
    assert draft["calendar"]["natural_days"]==30
    assert draft["calendar"]["study_days"]==26
    assert len(draft["structure"]["actions"])==26
    assert draft["calendar"]["rest_dates"]==["2026-09-20","2026-09-27","2026-10-04","2026-10-11"]
    assert preview(scenario,30)==draft
    program=goals.activate(draft["id"],expected_version=draft["version"],idempotency_key="activate")
    assert goals.activate(draft["id"],expected_version=draft["version"],idempotency_key="activate")==program
    reopened=Database(goals.db.database_url or goals.db.path,workspace=goals.db.workspace)
    try:
        assert GoalProgramService(reopened,FixedGoalProgramCompiler()).get(program["id"])==program
    finally:reopened.close()


def test_untrusted_compiler_cannot_ignore_rest_days(scenario):
    goals=scenario[0]
    compile_program=goals.compiler.compile
    async def bad_compiler(markdown,request):
        value=await compile_program(markdown,request)
        value["actions"][-1]["scheduled_date"]="2026-09-20"
        return value
    goals.compiler.compile=bad_compiler
    with pytest.raises(GoalCompilationError,match="rest day"):preview(scenario)
    assert goals.list()[0]["compile_status"]=="FAILED"


def test_deferral_rejects_over_budget_and_rest_day(scenario):
    program=active(scenario)
    goals=scenario[0]; first,second=program["actions"][:2]
    for target in (second["scheduled_date"],"2026-09-20"):
        with pytest.raises((GoalProgramConflict,ValueError)):
            goals.defer_action(first["id"],expected_version=0,scheduled_date=target,idempotency_key=target)
    assert goals.get(program["id"])["actions"]==program["actions"]


def test_concurrent_deferrals_cannot_overfill_one_day(scenario):
    program=active(scenario,30);goals=scenario[0]
    first,second,target=program["actions"][:3]
    def defer(action):
        try:
            return goals.defer_action(action["id"],expected_version=0,scheduled_date=target["scheduled_date"],idempotency_key=action["id"])
        except GoalProgramConflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(defer,[first,second]))
    assert sum(item is not None for item in results)==1
    total=sum(item["estimated_minutes"] for item in goals.get(program["id"])["actions"] if item["scheduled_date"]==target["scheduled_date"] and item["status"]=="SCHEDULED")
    assert total==60


def test_adjustment_preserves_deferred_task_identity(scenario):
    program=active(scenario,30);goals,adjustments,_,_=scenario
    first,second=program["actions"][:2]
    replacement=goals.defer_action(first["id"],expected_version=0,scheduled_date=second["scheduled_date"],idempotency_key="defer")["replacement"]
    current=goals.get(program["id"])
    proposal=asyncio.run(adjustments.propose(program["id"],reason="Clarify the last task only",expected_version=current["version"],idempotency_key="adjust"))
    assert replacement["logical_key"] in {item["logical_key"] for item in proposal["candidate"]["actions"]}
    accepted=adjustments.accept(proposal["id"],expected_version=0,idempotency_key="accept")["program"]
    retained=next(item for item in accepted["actions"] if item["id"]==replacement["id"])
    assert retained["status"]=="SCHEDULED"
    assert accepted["progress"]["required_total"]==program["progress"]["required_total"]


def test_completion_feedback_is_atomic_and_note_preserves_metrics(scenario):
    program=active(scenario);goals,_,reviews,_=scenario;first=program["actions"][0]
    feedback={"actual_minutes":80,"difficulty":5,"actual_date":first["scheduled_date"]}
    result=goals.complete_action(first["id"],expected_version=0,idempotency_key="complete",feedback=feedback)
    assert goals.complete_action(first["id"],expected_version=0,idempotency_key="complete",feedback=feedback)==result
    goals.feedback(first["id"],{"kind":"note","note":"Jupyter installation blocked verification","actual_date":first["scheduled_date"]},expected_version=1,idempotency_key="note")
    goals.close_day(program["id"],first["scheduled_date"],idempotency_key="close")
    review=reviews.claim_next("test",60);evidence=reviews.evidence(review["id"],"test")
    assert (evidence["actions"][0]["actual_minutes"],evidence["actions"][0]["difficulty"])==(80,5)
    assert evidence["user_notes"][0]["note"]=="Jupyter installation blocked verification"


def test_invalid_atomic_feedback_leaves_action_pending(scenario):
    program=active(scenario);goals=scenario[0];first=program["actions"][0]
    with pytest.raises(ValueError):
        goals.complete_action(first["id"],expected_version=0,idempotency_key="bad",feedback={"actual_minutes":-1})
    assert goals.get(program["id"])["actions"][0]["status"]=="SCHEDULED"
    with goals.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_action_feedback").fetchone()[0]==0


def test_late_feedback_requires_new_review_and_keeps_old_evidence(scenario):
    program=active(scenario);goals,_,reviews,_=scenario;first=program["actions"][0]
    goals.complete_action(first["id"],expected_version=0,idempotency_key="complete")
    goals.close_day(program["id"],first["scheduled_date"],idempotency_key="close")
    review=reviews.claim_next("test",60)
    goals.feedback(first["id"],{"kind":"completion","actual_minutes":80,"difficulty":5,"actual_date":first["scheduled_date"]},expected_version=1,idempotency_key="late")
    assert reviews.evidence(review["id"],"test")["evidence_stale"]
    reviews.complete(review["id"],"test",{},None)
    assert reviews.for_program_date(program["id"],first["scheduled_date"])["error_code"]=="REVIEW_EVIDENCE_CHANGED"
    retried=reviews.retry(review["id"],idempotency_key="retry")
    assert len(retried["history"])==1
    assert retried["history"][0]["snapshots"][0]["actual_minutes"] is None
    reviews.claim_next("test",60)
    assert reviews.evidence(review["id"],"test")["actions"][0]["actual_minutes"]==80


def test_partial_progress_remains_required_and_survives_reload(scenario):
    program=active(scenario);goals,_,reviews,_=scenario;first=program["actions"][0]
    goals.feedback(first["id"],{"kind":"partial","actual_minutes":80,"actual_date":first["scheduled_date"],"completed_work":"Installed Jupyter","remaining_work":"Run pandas verification","output":"Notebook opens"},expected_version=0,idempotency_key="partial")
    goals.close_day(program["id"],first["scheduled_date"],idempotency_key="close")
    current=goals.get(program["id"])
    assert current["actions"][0]["status"]=="SCHEDULED"
    assert current["actions"][0]["progress"]["state"]=="PARTIAL"
    assert current["actions"][0]["progress"]["actual_minutes"]==80
    assert not current["progress"]["completion_ready"]
    review=reviews.claim_next("test",60)
    evidence=reviews.evidence(review["id"],"test")
    assert evidence["actions"][0]["status"]=="PARTIAL"
    assert evidence["actions"][0]["feedback"]["remaining_work"]=="Run pandas verification"


@pytest.mark.parametrize("other_resource",[True,False])
def test_idempotency_cannot_cross_resource_or_operation(scenario,other_resource):
    program=active(scenario);goals=scenario[0];first,second=program["actions"][:2]
    goals.complete_action(first["id"],expected_version=0,idempotency_key="same")
    with pytest.raises(GoalProgramConflict):
        if other_resource:goals.complete_action(second["id"],expected_version=0,idempotency_key="same")
        else:goals.skip_action(first["id"],expected_version=0,idempotency_key="same")


def test_concurrent_same_command_has_one_effect(scenario):
    program=active(scenario);goals=scenario[0];first=program["actions"][0]
    def complete(_):return goals.complete_action(first["id"],expected_version=0,idempotency_key="same")
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(complete,range(2)))
    assert results[0]==results[1]
    with goals.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_program_events WHERE type='action.completed'").fetchone()[0]==1


@pytest.mark.parametrize("review_started", [False, True])
def test_partial_deferral_keeps_work_context_and_refreshes_both_dates(scenario, review_started):
    program = active(scenario, 30)
    goals, _, reviews, _ = scenario
    first, second = program["actions"][:2]
    goals.feedback(first["id"], {"kind": "partial", "actual_minutes": 25, "actual_date": first["scheduled_date"],
                   "completed_work": "Notebook ready", "remaining_work": "Verify dataframe", "output": "sample.ipynb"},
                   expected_version=0, idempotency_key="partial")
    goals.complete_action(second["id"], expected_version=0, idempotency_key="second-done")
    goals.close_day(program["id"], first["scheduled_date"], idempotency_key="close-first")
    goals.close_day(program["id"], second["scheduled_date"], idempotency_key="close-second")
    if review_started:
        for _ in range(2):
            claimed = reviews.claim_next("test", 60)
            reviews.complete(claimed["id"], "test", {"summary": "Original evidence", "encouragement": "Continue",
                             "needs_adjustment": False, "adjustment_reason": ""}, None)
    result = goals.defer_action(first["id"], expected_version=1, scheduled_date=second["scheduled_date"], idempotency_key="defer-partial")
    progress = result["replacement"]["progress"]
    assert progress["remaining_work"] == "Verify dataframe" and progress["source_action_id"] == first["id"]
    assert "actual_minutes" not in progress and "difficulty" not in progress
    source = reviews.for_program_date(program["id"], first["scheduled_date"])
    target = reviews.for_program_date(program["id"], second["scheduled_date"])
    assert source["evidence_stale"] is review_started and target["evidence_stale"] is review_started
    with goals.db.connection() as connection:
        rows = connection.execute("SELECT * FROM goal_review_action_snapshots WHERE review_id=?", (source["id"],)).fetchall()
        assert rows[0]["status"] == ("PARTIAL" if review_started else "DEFERRED")
        assert rows[0]["actual_minutes"] == 25
        target_rows = connection.execute("SELECT * FROM goal_review_action_snapshots WHERE review_id=?", (target["id"],)).fetchall()
        assert len(target_rows) == (1 if review_started else 2)
        if not review_started:
            carried = next(row for row in target_rows if row["action_id"] == result["replacement"]["id"])
            assert carried["actual_minutes"] is None
            import json
            assert json.loads(carried["feedback_json"])["carried_progress"]["remaining_work"] == "Verify dataframe"
