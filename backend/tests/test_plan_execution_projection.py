from __future__ import annotations

import pytest

from test_runtime import make_runtime


class ExecutionModel:
    def __init__(self, fail_compile: bool = False) -> None:
        self.fail_compile = fail_compile
        self.plan_inputs: list[dict] = []

    async def route_and_respond(self, *, on_text_delta, **kwargs):
        on_text_delta(
            '{"v":1,"policy":"propose_execution","content_shape":"tracking",'
            '"reason_code":"explicit_tracking"}\nI can track this after confirmation.'
        )

    async def needs_clarification(self, goal, interactions):
        return False

    async def plan(self, goal, interactions):
        self.plan_inputs.append(goal)
        if self.fail_compile:
            raise RuntimeError("compiler unavailable")
        from app.runtime import PlanDraft

        return PlanDraft([{"id": "step-1", "title": "Track the plan", "description": "Use the saved document."}], "Compiled from document")

    async def decide(self, step, observation, iteration):
        from app.runtime import ModelDecision

        return ModelDecision.complete("done")

    async def reflect(self, goal, plan, run_id):
        return []


def _count(runtime, table: str) -> int:
    with runtime.db.connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.mark.asyncio
async def test_confirmed_execution_compiles_exact_committed_document_version(tmp_path) -> None:
    model = ExecutionModel()
    runtime = make_runtime(tmp_path, model)
    thread = runtime.conversation.create_thread("Chat")
    document_version = runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Cycling plan",
        markdown_content="# Cycling plan\n\nWeek 1: easy rides\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "client-execute", "Track this plan", [])

    await runtime.turn_worker.run_once()
    turn = runtime.conversation.turn(accepted.turn_id)
    materialized = await runtime.conversation.select_direction(turn.id, "continue_execution", turn.version, "direction-1")

    assert materialized.materialized_run_id
    assert model.plan_inputs[0]["description"] == document_version.markdown_content
    with runtime.db.connection() as connection:
        run = connection.execute(
            "SELECT source_plan_document_id, source_plan_document_version_id, source_plan_content_hash, state "
            "FROM runs WHERE id = ?",
            (materialized.materialized_run_id,),
        ).fetchone()
        plan = connection.execute(
            "SELECT source_document_version_id FROM plan_versions WHERE run_id = ?",
            (materialized.materialized_run_id,),
        ).fetchone()
    assert run["source_plan_document_id"] == document_version.plan_document_id
    assert run["source_plan_document_version_id"] == document_version.id
    assert run["source_plan_content_hash"] == document_version.content_hash
    assert plan["source_document_version_id"] == document_version.id
    assert run["state"] == "AWAITING_APPROVAL"


@pytest.mark.asyncio
async def test_compiler_failure_leaves_no_execution_projection_or_agent_rows(tmp_path) -> None:
    runtime = make_runtime(tmp_path, ExecutionModel(fail_compile=True))
    thread = runtime.conversation.create_thread("Chat")
    runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Plan",
        markdown_content="# Plan\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "client-fail-compile", "Track this", [])
    await runtime.turn_worker.run_once()
    turn = runtime.conversation.turn(accepted.turn_id)

    with pytest.raises(RuntimeError, match="compiler unavailable"):
        await runtime.conversation.select_direction(turn.id, "continue_execution", turn.version, "direction-fail")

    assert runtime.conversation.turn(turn.id).status == "AWAITING_DIRECTION"
    assert _count(runtime, "goals") == 0
    assert _count(runtime, "runs") == 0
    assert _count(runtime, "plan_versions") == 0


def test_execution_compiler_rejects_non_string_step_fields() -> None:
    from app.plan_execution import PlanCompilationError, PlanExecutionCompiler
    from app.runtime import PlanDraft

    with pytest.raises(PlanCompilationError):
        PlanExecutionCompiler._validate(PlanDraft([{
            "id": "step-1",
            "title": {"unexpected": "object"},
            "description": "description",
            "status": "pending",
        }], "summary"))

    with pytest.raises(PlanCompilationError):
        PlanExecutionCompiler._validate(PlanDraft([{
            "id": "step-1",
            "title": "Valid",
            "description": ["not", "text"],
            "status": "pending",
        }], "summary"))
