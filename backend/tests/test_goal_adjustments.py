import asyncio
from copy import deepcopy

import pytest

from test_goal_program_compiler import fixture
from test_goal_programs import service


class AdjustmentCompiler:
    def __init__(self): self.candidate=None
    async def compile(self, source_markdown, request): return fixture()
    async def adjust(self,current,reason):
        candidate=deepcopy(current)
        candidate["actions"][4]["estimated_minutes"]=30
        candidate["actions"][4]["title"]="降低训练量"
        self.candidate=candidate
        return candidate


def active_services(tmp_path):
    from app.goal_adjustments import GoalAdjustmentService
    db,conversation,goals,version=service(tmp_path)
    draft=asyncio.run(goals.preview(version.plan_document_id,start_date="2026-09-01",timezone_name="Asia/Shanghai",daily_minutes=60,requested_end_date="2026-09-07",idempotency_key="preview"))
    active=goals.activate(draft["id"],expected_version=draft["version"],idempotency_key="activate")
    compiler=AdjustmentCompiler()
    return db,conversation,goals,GoalAdjustmentService(goals,compiler,conversation.plan_documents),active


def test_accept_switches_only_future_actions_and_is_idempotent(tmp_path,monkeypatch) -> None:
    import app.goal_adjustments as module
    monkeypatch.setattr(module,"_now",lambda:"2026-09-01T00:00:00+00:00")
    monkeypatch.setattr(module,"_local_date",lambda timezone_name:"2026-09-01")
    db,_,goals,adjustments,active=active_services(tmp_path)
    first=active["actions"][0]
    goals.complete_action(first["id"],expected_version=0,idempotency_key="complete")
    current=goals.get(active["id"])
    proposal=asyncio.run(adjustments.propose(active["id"],reason="降低后半周训练量",expected_version=current["version"],idempotency_key="propose"))
    accepted=adjustments.accept(proposal["id"],expected_version=0,idempotency_key="accept")
    replay=adjustments.accept(proposal["id"],expected_version=0,idempotency_key="accept")
    assert accepted==replay
    with db.connection() as connection:
        historical=connection.execute("SELECT status,program_version_id FROM goal_actions WHERE id=?",(first["id"],)).fetchone()
        assert historical["status"]=="COMPLETED" and historical["program_version_id"]==active["current_program_version_id"]
        assert connection.execute("SELECT COUNT(*) FROM goal_program_versions WHERE program_id=?",(active["id"],)).fetchone()[0]==2


def test_stale_action_snapshot_rolls_back_accept(tmp_path,monkeypatch) -> None:
    import app.goal_adjustments as module
    monkeypatch.setattr(module,"_local_date",lambda timezone_name:"2026-09-01")
    db,_,goals,adjustments,active=active_services(tmp_path)
    proposal=asyncio.run(adjustments.propose(active["id"],reason="调整",expected_version=active["version"],idempotency_key="propose"))
    action=active["actions"][0];goals.skip_action(action["id"],expected_version=0,idempotency_key="skip")
    with pytest.raises(Exception):adjustments.accept(proposal["id"],expected_version=0,idempotency_key="accept")
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_program_versions WHERE program_id=?",(active["id"],)).fetchone()[0]==1
    assert adjustments.get(proposal["id"])["status"]=="STALE"


def test_markdown_conflict_does_not_rollback_accepted_execution(tmp_path,monkeypatch) -> None:
    import app.goal_adjustments as module
    monkeypatch.setattr(module,"_local_date",lambda timezone_name:"2026-09-01")
    db,conversation,goals,adjustments,active=active_services(tmp_path)
    proposal=asyncio.run(adjustments.propose(active["id"],reason="调整",expected_version=active["version"],idempotency_key="propose"))
    accepted=adjustments.accept(proposal["id"],expected_version=0,idempotency_key="accept")
    source=conversation.plan_documents.current_version(active["source_plan_document_id"])
    document=conversation.plan_documents.get_document(active["source_plan_document_id"])
    conversation.plan_documents.save_model_revision(thread_id=document.thread_id,title="edited",markdown_content="# edited",source_turn_id=None,source_message_id=None,actor="user",expected_version_id=source.id,expected_file_hash=source.content_hash)
    with pytest.raises(Exception):adjustments.sync_plan_document(proposal["id"],expected_version=1,idempotency_key="sync")
    current=goals.get(active["id"])
    assert current["current_program_version_id"]==accepted["program"]["current_program_version_id"]
    assert adjustments.get(proposal["id"])["plan_sync_status"]=="CONFLICT"


def test_accept_rejects_future_action_added_after_snapshot(tmp_path,monkeypatch) -> None:
    import app.goal_adjustments as module
    monkeypatch.setattr(module,"_local_date",lambda timezone_name:"2026-09-01")
    db,_,goals,adjustments,active=active_services(tmp_path)
    proposal=asyncio.run(adjustments.propose(active["id"],reason="adjust",expected_version=active["version"],idempotency_key="propose"))
    first=active["actions"][0]
    goals.defer_action(first["id"],expected_version=0,scheduled_date="2026-09-07",idempotency_key="defer")
    with pytest.raises(Exception,match="stale"):
        adjustments.accept(proposal["id"],expected_version=0,idempotency_key="accept")
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_program_versions WHERE program_id=?",(active["id"],)).fetchone()[0]==1


def test_sync_recovers_when_revision_committed_before_proposal_receipt(tmp_path,monkeypatch) -> None:
    import app.goal_adjustments as module
    monkeypatch.setattr(module,"_local_date",lambda timezone_name:"2026-09-01")
    db,conversation,goals,adjustments,active=active_services(tmp_path)
    proposal=asyncio.run(adjustments.propose(active["id"],reason="adjust",expected_version=active["version"],idempotency_key="propose"))
    accepted=adjustments.accept(proposal["id"],expected_version=0,idempotency_key="accept")
    original=adjustments.programs._save_receipt
    def crash_after_revision(*args,**kwargs): raise RuntimeError("simulated crash")
    adjustments.programs._save_receipt=crash_after_revision
    with pytest.raises(RuntimeError,match="simulated crash"):
        adjustments.sync_plan_document(proposal["id"],expected_version=accepted["proposal"]["version"],idempotency_key="sync")
    adjustments.programs._save_receipt=original
    recovered=adjustments.sync_plan_document(proposal["id"],expected_version=accepted["proposal"]["version"],idempotency_key="sync")
    assert recovered["proposal"]["plan_sync_status"]=="COMMITTED"
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM plan_document_versions WHERE plan_document_id=?",(active["source_plan_document_id"],)).fetchone()[0]==2
