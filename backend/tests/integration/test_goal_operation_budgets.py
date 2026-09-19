from app.db import Database
from app.goal_programs import GoalProgramService
from app.goal_program_compiler import FixedGoalProgramCompiler
from app.conversation import ConversationService
import asyncio


def test_goal_context_creates_and_reuses_independent_operation_budget(migrated_postgres_url,tmp_path):
    db=Database(migrated_postgres_url,workspace=tmp_path/"artifacts")
    try:
        conversation=ConversationService(db)
        thread=conversation.create_thread("budgeted goal")
        plan=conversation.plan_documents.save_model_revision(thread_id=thread.id,title="Plan",markdown_content="# Plan\nPractice daily",source_turn_id=None,source_message_id=None,actor="user")
        goals=GoalProgramService(db,FixedGoalProgramCompiler(),plan_documents=conversation.plan_documents)
        draft=asyncio.run(goals.preview(plan.plan_document_id,start_date="2026-09-14",requested_end_date="2026-09-15",timezone_name="Asia/Shanghai",daily_minutes=60,idempotency_key="preview"))
        compile_context=goals._model_context(draft["id"],"planner","compile_goal_program")
        again=goals._model_context(draft["id"],"planner","compile_goal_program")
        daily=goals._model_context(draft["id"],"reflector","daily_review",operation_id="review:0")
        adjustment=goals._model_context(draft["id"],"planner","adjust_goal_program",operation_id="adjust:0",root_budget_id=daily.root_budget_id)
        assert compile_context.root_budget_id==again.root_budget_id
        assert daily.root_budget_id!=compile_context.root_budget_id
        assert adjustment.root_budget_id==daily.root_budget_id
        with db.connection() as connection:
            roots=connection.execute("SELECT * FROM task_budget_roots").fetchall()
            assert len(roots)==2
            assert all(row["root_kind"]=="goal_operation" for row in roots)
    finally:db.close()


def test_period_review_retry_keeps_operation_budget(migrated_postgres_url, tmp_path):
    from app.goal_program_compiler import GoalCompilationError

    db = Database(migrated_postgres_url, workspace=tmp_path / "artifacts")
    try:
        conversation = ConversationService(db)
        thread = conversation.create_thread("period review budget")
        plan = conversation.plan_documents.save_model_revision(
            thread_id=thread.id, title="Plan", markdown_content="# Plan\nPractice daily",
            source_turn_id=None, source_message_id=None, actor="user")
        goals = GoalProgramService(db, FixedGoalProgramCompiler(), plan_documents=conversation.plan_documents)
        draft = asyncio.run(goals.preview(plan.plan_document_id, start_date="2026-09-14",
                            requested_end_date="2026-09-15", timezone_name="Asia/Shanghai",
                            daily_minutes=60, idempotency_key="period-preview"))
        active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="period-activate")
        for action in active["actions"]:
            goals.complete_action(action["id"], expected_version=0, idempotency_key=action["id"])
        goals.transition(draft["id"], "complete", expected_version=active["version"], idempotency_key="period-complete")
        contexts = []
        original_call = goals._call_model
        async def record(context, invocation):
            contexts.append(context)
            return await original_call(context, invocation)
        goals._call_model = record
        class Compiler:
            calls = 0
            async def period_review(self, evidence):
                self.calls += 1
                if self.calls == 1:
                    raise GoalCompilationError("MODEL_UNAVAILABLE", "unavailable", temporary=True)
                return {"summary": "Recovered"}
        goals.compiler = Compiler()
        assert asyncio.run(goals.period_summary(draft["id"])) is None
        assert asyncio.run(goals.period_summary(draft["id"], retry_key="retry")) == "Recovered"
        assert len(contexts) == 2
        assert contexts[0].root_budget_id == contexts[1].root_budget_id
        assert contexts[0].root_budget_id is not None
    finally:
        db.close()
