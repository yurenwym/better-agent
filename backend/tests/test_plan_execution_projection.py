from __future__ import annotations

import asyncio

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
async def test_execution_without_a_committed_plan_document_is_rejected(tmp_path) -> None:
    runtime = make_runtime(tmp_path, ExecutionModel())
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-execute-without-plan", "Track this", [])

    await runtime.turn_worker.run_once()
    turn = runtime.conversation.turn(accepted.turn_id)

    with pytest.raises(ValueError, match="committed plan document"):
        await runtime.conversation.select_direction(
            turn.id,
            "continue_execution",
            turn.version,
            "direction-without-plan",
        )

    assert _count(runtime, "goals") == 0
    assert _count(runtime, "runs") == 0
    assert _count(runtime, "plan_versions") == 0


@pytest.mark.asyncio
async def test_execution_preview_uses_the_turn_pinned_document_version_after_a_later_edit(tmp_path) -> None:
    model = ExecutionModel()
    runtime = make_runtime(tmp_path, model)
    thread = runtime.conversation.create_thread("Chat")
    first = runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Cycling plan",
        markdown_content="# v1\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "client-preview-pin", "Track this plan", [])

    await runtime.turn_worker.run_once()
    turn = runtime.conversation.turn(accepted.turn_id)
    runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Cycling plan",
        markdown_content="# v2\n",
        source_turn_id=None,
        source_message_id=None,
        actor="user",
        expected_version_id=first.id,
        expected_file_hash=first.content_hash,
    )

    materialized = await runtime.conversation.select_direction(
        turn.id, "continue_execution", turn.version, "direction-preview-pin"
    )

    assert materialized.materialized_run_id
    assert model.plan_inputs[0]["description"] == first.markdown_content
    with runtime.db.connection() as connection:
        source = connection.execute(
            "SELECT source_plan_document_version_id FROM runs WHERE id = ?",
            (materialized.materialized_run_id,),
        ).fetchone()
    assert source["source_plan_document_version_id"] == first.id


@pytest.mark.asyncio
async def test_same_execution_idempotency_key_claims_one_compilation(tmp_path) -> None:
    class ConcurrentModel(ExecutionModel):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def plan(self, goal, interactions):
            self.plan_inputs.append(goal)
            self.started.set()
            await self.release.wait()
            from app.runtime import PlanDraft

            return PlanDraft([{"id": "step-1", "title": "Track the plan"}], "Compiled once")

    model = ConcurrentModel()
    runtime = make_runtime(tmp_path, model)
    thread = runtime.conversation.create_thread("Chat")
    runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Plan",
        markdown_content="# Plan\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "client-concurrent-direction", "Track this", [])
    await runtime.turn_worker.run_once()
    turn = runtime.conversation.turn(accepted.turn_id)

    first_task = asyncio.create_task(
        runtime.conversation.select_direction(turn.id, "continue_execution", turn.version, "direction-same")
    )
    await model.started.wait()
    second_task = asyncio.create_task(
        runtime.conversation.select_direction(turn.id, "continue_execution", turn.version, "direction-same")
    )
    await asyncio.sleep(0)
    compile_calls_while_first_is_blocked = len(model.plan_inputs)
    model.release.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)

    assert compile_calls_while_first_is_blocked == 1
    assert first_result.materialized_run_id == second_result.materialized_run_id
    assert _count(runtime, "runs") == 1


@pytest.mark.asyncio
async def test_execution_claim_is_persisted_for_a_second_materializer_instance(tmp_path) -> None:
    class CrossProcessModel(ExecutionModel):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def plan(self, goal, interactions):
            self.plan_inputs.append(goal)
            self.started.set()
            await self.release.wait()
            from app.runtime import PlanDraft

            return PlanDraft([{"id": "step-1", "title": "Track the plan"}], "Compiled once")

    model = CrossProcessModel()
    runtime = make_runtime(tmp_path, model)
    thread = runtime.conversation.create_thread("Chat")
    runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Plan",
        markdown_content="# Plan\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "client-persisted-claim", "Track this", [])
    await runtime.turn_worker.run_once()
    turn = runtime.conversation.turn(accepted.turn_id)
    other_materializer = runtime.conversation.materializer.__class__(
        runtime.db,
        runtime,
        runtime.conversation.events,
    )

    first_task = asyncio.create_task(
        runtime.conversation.materializer.materialize(
            turn.id, turn.version, "direction-persisted", "continue_execution"
        )
    )
    await model.started.wait()
    second_task = asyncio.create_task(
        other_materializer.materialize(
            turn.id, turn.version, "direction-persisted", "continue_execution"
        )
    )
    await asyncio.sleep(0)
    model.release.set()
    first_result = await first_task

    with pytest.raises(ValueError, match="in progress"):
        await second_task
    assert first_result.run_id
    assert len(model.plan_inputs) == 1


@pytest.mark.asyncio
async def test_expired_execution_claim_can_be_recovered_by_a_new_materializer_instance(tmp_path) -> None:
    model = ExecutionModel()
    runtime = make_runtime(tmp_path, model)
    thread = runtime.conversation.create_thread("Chat")
    runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Plan",
        markdown_content="# Plan\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "client-expired-claim", "Track this", [])
    await runtime.turn_worker.run_once()
    turn = runtime.conversation.turn(accepted.turn_id)
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE turns SET direction_idempotency_key = ?, direction_projection_status = 'COMPILING', "
            "direction_projection_claim_owner = ?, direction_projection_lease_until = ? WHERE id = ?",
            ("direction-crashed", "dead-materializer", "2000-01-01T00:00:00+00:00", turn.id),
        )

    recovered_materializer = runtime.conversation.materializer.__class__(
        runtime.db,
        runtime,
        runtime.conversation.events,
    )
    result = await recovered_materializer.materialize(
        turn.id,
        turn.version,
        "direction-recovered",
        "continue_execution",
    )

    assert result.run_id
    assert len(model.plan_inputs) == 1
    assert _count(runtime, "runs") == 1


@pytest.mark.asyncio
async def test_active_execution_claim_is_renewed_during_a_long_compile(tmp_path, monkeypatch) -> None:
    class SlowModel(ExecutionModel):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def plan(self, goal, interactions):
            self.plan_inputs.append(goal)
            self.started.set()
            await self.release.wait()
            from app.runtime import PlanDraft

            return PlanDraft([{"id": "step-1", "title": "Track the plan"}], "Compiled once")

    monkeypatch.setattr("app.conversation.EXECUTION_PROJECTION_LEASE_SECONDS", 0.06)
    model = SlowModel()
    runtime = make_runtime(tmp_path, model)
    thread = runtime.conversation.create_thread("Chat")
    runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Plan",
        markdown_content="# Plan\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "client-renewing-claim", "Track this", [])
    await runtime.turn_worker.run_once()
    turn = runtime.conversation.turn(accepted.turn_id)
    other_materializer = runtime.conversation.materializer.__class__(
        runtime.db,
        runtime,
        runtime.conversation.events,
    )
    first_task = asyncio.create_task(
        runtime.conversation.materializer.materialize(
            turn.id, turn.version, "direction-renewing", "continue_execution"
        )
    )
    await model.started.wait()
    await asyncio.sleep(0.11)
    second_task = asyncio.create_task(
        other_materializer.materialize(
            turn.id, turn.version, "direction-renewing", "continue_execution"
        )
    )
    await asyncio.sleep(0)
    model.release.set()
    result = await first_task

    with pytest.raises(ValueError, match="in progress"):
        await second_task

    assert result.run_id
    assert len(model.plan_inputs) == 1


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
