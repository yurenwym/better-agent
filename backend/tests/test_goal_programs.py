import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_goal_program_compiler import fixture


def service(tmp_path, result=None):
    from app.conversation import ConversationService
    from app.db import Database
    from app.goal_program_compiler import FixedGoalProgramCompiler
    from app.goal_programs import GoalProgramService

    db = Database(tmp_path / "agent.db")
    conversation = ConversationService(db)
    thread = conversation.create_thread("训练")
    version = conversation.plan_documents.save_model_revision(
        thread_id=thread.id, title="一周力扣训练", markdown_content="# 一周力扣训练\n每天一道题",
        source_turn_id=None, source_message_id=None, actor="user",
    )
    return db, conversation, GoalProgramService(db, FixedGoalProgramCompiler(result or fixture()), plan_documents=conversation.plan_documents, conversation=conversation), version


def preview(goal_service, version, key="preview-1"):
    return asyncio.run(goal_service.preview(
        version.plan_document_id, start_date="2026-09-01", timezone_name="Asia/Shanghai",
        daily_minutes=60, requested_end_date="2026-09-07", idempotency_key=key,
    ))


def test_seven_day_lifecycle_is_durable_and_idempotent(tmp_path) -> None:
    from app.db import Database
    from app.goal_program_compiler import FixedGoalProgramCompiler
    from app.goal_programs import GoalProgramService

    db, _, goals, version = service(tmp_path)
    draft = preview(goals, version)
    assert draft["status"] == "DRAFT" and draft["compile_status"] == "READY"
    assert preview(goals, version)["id"] == draft["id"]
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate-1")
    replay = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate-1")
    assert active == replay and len(active["actions"]) == 7

    first, second, third = active["actions"][:3]
    goals.complete_action(first["id"], expected_version=0, idempotency_key="complete-1")
    goals.skip_action(second["id"], expected_version=0, idempotency_key="skip-1")
    deferred = goals.defer_action(third["id"], expected_version=0, scheduled_date="2026-09-07", idempotency_key="defer-1")
    replay_defer = goals.defer_action(third["id"], expected_version=0, scheduled_date="2026-09-07", idempotency_key="defer-1")
    assert deferred == replay_defer

    current = goals.get(draft["id"])
    paused = goals.transition(draft["id"], "pause", expected_version=current["version"], idempotency_key="pause-1")
    assert paused["status"] == "PAUSED"

    reopened = GoalProgramService(Database(db.path), FixedGoalProgramCompiler(fixture()))
    assert reopened.today(explicit_date="2026-09-02")["programs"] == []
    resumed = reopened.transition(draft["id"], "resume", expected_version=paused["version"], idempotency_key="resume-1")
    assert resumed["status"] == "ACTIVE"
    with reopened.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_actions WHERE program_id=?", (draft["id"],)).fetchone()[0] == 8
        events = connection.execute("SELECT seq FROM goal_program_events WHERE program_id=? ORDER BY seq", (draft["id"],)).fetchall()
        assert [row["seq"] for row in events] == list(range(1, len(events) + 1))


def test_cas_owner_idempotency_and_illegal_transitions_are_rejected(tmp_path) -> None:
    from app.goal_programs import GoalProgramConflict, GoalProgramNotFound

    _, _, goals, version = service(tmp_path)
    draft = preview(goals, version)
    with pytest.raises(GoalProgramConflict):
        asyncio.run(goals.preview(version.plan_document_id, start_date="2026-09-01", timezone_name="UTC", daily_minutes=60, requested_end_date="2026-09-07", idempotency_key="preview-1"))
    with pytest.raises(GoalProgramConflict): goals.activate(draft["id"], expected_version=99, idempotency_key="bad-cas")
    with pytest.raises(GoalProgramConflict): goals.transition(draft["id"], "pause", expected_version=draft["version"], idempotency_key="bad-state")
    with pytest.raises(GoalProgramNotFound): goals.get(draft["id"], owner_id="other-user")


def test_editing_markdown_does_not_change_fixed_execution_snapshot(tmp_path) -> None:
    db, conversation, goals, source = service(tmp_path)
    draft = preview(goals, source)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    document = conversation.plan_documents.get_document(source.plan_document_id)
    conversation.plan_documents.save_model_revision(
        thread_id=document.thread_id, title="Changed", markdown_content="# Changed\nnew plan",
        source_turn_id=None, source_message_id=None, actor="user", expected_version_id=source.id,
        expected_file_hash=source.content_hash,
    )
    recovered = goals.get(draft["id"])
    assert recovered["source_plan_document_version_id"] == source.id
    assert recovered["structure"] == active["structure"]
    assert recovered["actions"] == active["actions"]


def test_duration_timezone_progress_and_feedback_privacy_invariants(tmp_path) -> None:
    from app.goal_programs import GoalProgramService

    db, _, goals, version = service(tmp_path)
    with pytest.raises(ValueError, match="28 days"):
        asyncio.run(goals.preview(version.plan_document_id, start_date="2026-09-01", timezone_name="Asia/Shanghai", daily_minutes=60, requested_end_date="2026-09-30", idempotency_key="30-days"))
    with pytest.raises(ValueError, match="IANA"):
        asyncio.run(goals.preview(version.plan_document_id, start_date="2026-09-01", timezone_name="Mars/Olympus", daily_minutes=60, requested_end_date="2026-09-07", idempotency_key="tz"))
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    action = active["actions"][0]
    completed = goals.complete_action(action["id"], expected_version=0, idempotency_key="complete")
    goals.feedback(action["id"], {"kind": "difficulty", "difficulty": 4, "note": "private health detail", "sensitivity": "sensitive"}, expected_version=1, idempotency_key="feedback")
    assert completed["progress"]["required_completed"] == 1
    with db.connection() as connection:
        events = "\n".join(row[0] for row in connection.execute("SELECT data_json FROM goal_program_events"))
    assert "private health detail" not in events


def test_request_help_creates_action_linked_turn_without_run_and_bounded_owner_context(tmp_path) -> None:
    from app.goal_context import GoalContextProvider

    db, conversation, goals, version = service(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    action = active["actions"][0]
    goals.feedback(action["id"], {"kind":"difficulty","difficulty":5,"note":"x"*1000}, expected_version=0, idempotency_key="feedback")
    result = goals.request_help(action["id"], content="这道题怎么拆解？", expected_version=0, idempotency_key="help")
    assert goals.request_help(action["id"], content="这道题怎么拆解？", expected_version=0, idempotency_key="help") == result
    context = GoalContextProvider(db).load_for_turn(result["thread_id"], result["turn_id"])
    assert context is not None and len(context.recent_feedback[0]) < 300
    assert "当前行动" in context.context_text
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        connection.execute("UPDATE threads SET owner_id='other-user' WHERE id=?", (result["thread_id"],))
    assert GoalContextProvider(db).load_for_turn(result["thread_id"], result["turn_id"]) is None


def test_compile_failure_retry_creates_one_initial_version_and_no_actions(tmp_path) -> None:
    from app.goal_program_compiler import FixedGoalProgramCompiler, GoalCompilationError

    db, _, goals, version = service(tmp_path)
    goals.compiler = FixedGoalProgramCompiler(error=GoalCompilationError("INVALID_MODEL_OUTPUT", "bad"))
    with pytest.raises(GoalCompilationError):
        preview(goals, version)
    with db.connection() as connection:
        program_id = connection.execute("SELECT id FROM goal_programs").fetchone()["id"]
        assert connection.execute("SELECT COUNT(*) FROM goal_actions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM goal_program_versions").fetchone()[0] == 0
    failed = goals.get(program_id)
    goals.compiler = FixedGoalProgramCompiler(fixture())
    ready = asyncio.run(goals.retry_compile(program_id, expected_version=failed["version"], idempotency_key="retry"))
    replay = asyncio.run(goals.retry_compile(program_id, expected_version=failed["version"], idempotency_key="retry"))
    assert ready == replay and ready["compile_status"] == "READY"
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_program_versions WHERE program_id=?", (program_id,)).fetchone()[0] == 1


def test_progress_excludes_optional_skipped_deferred_and_cancelled_sources(tmp_path) -> None:
    value = fixture()
    value["actions"][1]["required"] = False
    _, _, goals, version = service(tmp_path, value)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    goals.skip_action(active["actions"][1]["id"], expected_version=0, idempotency_key="skip-optional")
    goals.defer_action(active["actions"][2]["id"], expected_version=0, scheduled_date="2026-09-07", idempotency_key="defer")
    progress = goals.get(draft["id"])["progress"]
    assert progress["required_total"] == 6
    assert progress["required_completed"] == 0
    assert progress["completion_rate"] == 0


def test_concurrent_activate_cas_materializes_exactly_one_action_set(tmp_path) -> None:
    from app.goal_programs import GoalProgramConflict

    db, _, goals, version = service(tmp_path)
    draft = preview(goals, version)
    def activate(key):
        try: return goals.activate(draft["id"], expected_version=draft["version"], idempotency_key=key)["status"]
        except GoalProgramConflict: return "CONFLICT"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(activate, ("activate-a", "activate-b")))
    assert sorted(results) == ["ACTIVE", "CONFLICT"]
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_actions WHERE program_id=?", (draft["id"],)).fetchone()[0] == 7


def test_concurrent_preview_same_key_creates_one_program(tmp_path) -> None:
    db, _, goals, version = service(tmp_path)
    original = goals.compiler.compile
    async def blocked_compile(source, request):
        await asyncio.sleep(.2)
        return await original(source, request)
    goals.compiler.compile = blocked_compile
    def run():
        return asyncio.run(goals.preview(
            version.plan_document_id,start_date="2026-09-01",timezone_name="Asia/Shanghai",
            daily_minutes=60,requested_end_date="2026-09-07",idempotency_key="same-preview",
        ))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda _:run(),range(2)))
    assert results[0]==results[1]
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_programs").fetchone()[0]==1
