import pytest

from test_runtime import make_runtime


class MaterializerModel:
    async def route_and_respond(self, *, on_text_delta, **kwargs):
        on_text_delta(
            '{"v":1,"policy":"propose_execution","content_shape":"tracking",'
            '"reason_code":"external_effect"}\n确认后我会更新清单。'
        )

    async def needs_clarification(self, goal, interactions):
        return False

    async def plan(self, goal, interactions):
        from app.runtime import PlanDraft

        return PlanDraft([{"id": "step-1", "title": "Update list"}], "Update the list")

    async def decide(self, step, observation, iteration):
        from app.runtime import ModelDecision

        return ModelDecision.complete("done")

    async def reflect(self, goal, plan, run_id):
        return []


def _count(runtime, table: str) -> int:
    with runtime.db.connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.mark.asyncio
async def test_execution_materializes_once_only_after_confirmed_direction(tmp_path) -> None:
    runtime = make_runtime(tmp_path, MaterializerModel())
    thread = runtime.conversation.create_thread("Chat")
    runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Execution plan",
        markdown_content="# Execution plan\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "每天更新清单", [])
    await runtime.turn_worker.run_once()
    turn = runtime.conversation.turn(accepted.turn_id)

    assert turn.status == "AWAITING_DIRECTION"
    assert _count(runtime, "goals") == 0
    first = await runtime.conversation.select_direction(
        turn.id, "continue_execution", turn.version, "action-1"
    )
    second = await runtime.conversation.select_direction(
        turn.id, "continue_execution", first.version, "action-1"
    )

    assert first.materialized_run_id == second.materialized_run_id
    assert _count(runtime, "goals") == 1
    assert _count(runtime, "sessions") == 1
    assert _count(runtime, "runs") == 1
    with runtime.db.connection() as connection:
        row = connection.execute(
            "SELECT source_turn_id FROM runs WHERE id = ?", (first.materialized_run_id,)
        ).fetchone()
    assert row["source_turn_id"] == turn.id
    assert any(
        event.type == "execution.materialized"
        for event in runtime.conversation.events.list(thread.id)
    )


@pytest.mark.asyncio
async def test_direction_version_conflict_and_answer_upgrade_are_rejected(tmp_path) -> None:
    runtime = make_runtime(tmp_path, MaterializerModel())
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "执行", [])
    await runtime.turn_worker.run_once()
    turn = runtime.conversation.turn(accepted.turn_id)

    with pytest.raises(ValueError, match="turn version conflict"):
        await runtime.conversation.select_direction(
            turn.id, "continue_execution", turn.version - 1, "action-1"
        )

    answer_runtime = make_runtime(tmp_path / "answer", MaterializerModel())
    answer_thread = answer_runtime.conversation.create_thread("Chat")
    answer_accepted = answer_runtime.conversation.accept_turn(answer_thread.id, "client-1", "回答", [])
    await answer_runtime.turn_worker.run_once()
    with answer_runtime.db.connection() as connection:
        connection.execute(
            "UPDATE turns SET policy = 'answer', status = 'COMPLETED' WHERE id = ?",
            (answer_accepted.turn_id,),
        )
    with pytest.raises(ValueError, match="execution direction is not available"):
        await answer_runtime.conversation.select_direction(
            answer_accepted.turn_id, "continue_execution", 0, "action-2"
        )


@pytest.mark.asyncio
async def test_modify_plan_completes_turn_without_materializing_agent(tmp_path) -> None:
    runtime = make_runtime(tmp_path, MaterializerModel())
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "执行", [])
    await runtime.turn_worker.run_once()
    turn = runtime.conversation.turn(accepted.turn_id)

    modified = await runtime.conversation.select_direction(
        turn.id, "modify_plan", turn.version, "action-modify"
    )

    assert modified.status == "COMPLETED"
    assert modified.direction_action == "modify_plan"
    assert _count(runtime, "goals") == 0
    assert any(
        event.type == "turn.direction_selected" and event.data["action"] == "modify_plan"
        for event in runtime.conversation.events.list(thread.id)
    )
