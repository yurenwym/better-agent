from __future__ import annotations

import pytest

from test_runtime import make_runtime


def _answer(body: str, *, title: str | None = None) -> str:
    artifact = ""
    if title is not None:
        artifact = f',"artifact":{{"kind":"plan_document","operation":"upsert","title":"{title}"}}'
    return (
        '{"v":2,"policy":"answer","content_shape":"plan",'
        f'"reason_code":"content_only"{artifact}}}\n{body}'
    )


class ConversationModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    async def route_and_respond(self, *, on_text_delta, **kwargs):
        on_text_delta(self.responses.pop(0))

    async def needs_clarification(self, goal, interactions):
        return False

    async def plan(self, goal, interactions):
        from app.runtime import PlanDraft

        return PlanDraft([{"id": "step-1", "title": "Track the plan", "description": "Use the saved plan."}], "Compiled plan")

    async def decide(self, step, observation, iteration):
        from app.runtime import ModelDecision

        return ModelDecision.complete("done")

    async def reflect(self, goal, plan, run_id):
        return []


def _count(runtime, table: str) -> int:
    with runtime.db.connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.mark.asyncio
async def test_ordinary_guide_does_not_create_plan_or_run(tmp_path) -> None:
    runtime = make_runtime(tmp_path, ConversationModel([_answer("# A useful guide\n", title=None)]))
    thread = runtime.conversation.create_thread("Guide")
    accepted = runtime.conversation.accept_turn(thread.id, "guide-1", "Give me a guide", [])

    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    assert _count(runtime, "plan_documents") == 0
    assert _count(runtime, "goals") == 0
    assert _count(runtime, "runs") == 0
    assert _count(runtime, "plan_versions") == 0


@pytest.mark.asyncio
async def test_read_only_question_uses_current_plan_without_creating_a_revision(tmp_path) -> None:
    runtime = make_runtime(tmp_path, ConversationModel([_answer("第 3 天建议走轻松路线。")]))
    thread = runtime.conversation.create_thread("Plan")
    first = runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Travel plan",
        markdown_content="# Travel plan\n\n## Day 3\nRoute\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "question-1", "第 3 天怎么走？", [])

    await runtime.turn_worker.run_once()

    document = runtime.plan_documents.get_by_thread(thread.id)
    assert [version.version for version in runtime.plan_documents.list_versions(document.id)] == [1]
    loaded = [event for event in runtime.conversation.events.list(thread.id) if event.type == "plan.context_loaded"]
    assert loaded[-1].data["version_id"] == first.id
    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"


@pytest.mark.asyncio
async def test_explicit_plan_modification_becomes_an_approval_gated_tool_call(tmp_path) -> None:
    """D4 convergence: a legacy artifact update is suspended as a modify call."""
    runtime = make_runtime(tmp_path, ConversationModel([
        _answer("# Travel plan\n\n## Day 3\nUpdated transport\n", title="Travel plan"),
        _answer("已按确认更新计划文档。"),
    ]))
    from app.goal_tools import register_goal_tools

    register_goal_tools(
        runtime.tools,
        goal_programs=runtime.goal_programs,
        plan_documents=runtime.plan_documents,
    )
    thread = runtime.conversation.create_thread("Plan")
    first = runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Travel plan",
        markdown_content="# Travel plan\n\n## Day 3\nRoute\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "modify-1", "把第 3 天交通补充进计划", [])

    await runtime.turn_worker.run_once()

    waiting = runtime.conversation.turn(accepted.turn_id)
    assert waiting.status == "AWAITING_TOOL_APPROVAL"
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)
    assert pending.tool_name == "modify_plan_document"
    assert pending.params["document_id"] == first.plan_document_id
    assert pending.params["expected_version_id"] == first.id
    assert runtime.plan_documents.current_version(first.plan_document_id).version == 1
    # The visible answer is still delivered while the write waits for approval.
    assert runtime.conversation.messages(thread.id)[-1].content.strip() == "# Travel plan\n\n## Day 3\nUpdated transport"

    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", waiting.version, "modify-1-decision"
    )
    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(continuation.id).status == "COMPLETED"
    current = runtime.plan_documents.current_version(first.plan_document_id)
    assert current.version == 2
    assert current.base_version_id == first.id
    assert "Updated transport" in current.markdown_content


@pytest.mark.asyncio
async def test_tracking_preview_creates_no_run_until_direction_confirmation(tmp_path) -> None:
    model = ConversationModel([
        '{"v":2,"policy":"propose_execution","content_shape":"tracking","reason_code":"explicit_tracking"}\n将按计划持续跟踪。',
    ])
    runtime = make_runtime(tmp_path, model)
    thread = runtime.conversation.create_thread("Plan")
    version = runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Training plan",
        markdown_content="# Training plan\n\nWeek 1\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "track-1", "按计划持续提醒我", [])

    await runtime.turn_worker.run_once()

    waiting = runtime.conversation.turn(accepted.turn_id)
    assert waiting.status == "AWAITING_DIRECTION"
    assert _count(runtime, "runs") == 0

    materialized = await runtime.conversation.select_direction(
        waiting.id,
        "continue_execution",
        waiting.version,
        "track-direction-1",
    )

    assert materialized.materialized_run_id is not None
    assert _count(runtime, "runs") == 1
    with runtime.db.connection() as connection:
        run = connection.execute(
            "SELECT source_plan_document_version_id, state FROM runs WHERE id = ?",
            (materialized.materialized_run_id,),
        ).fetchone()
        assert run["source_plan_document_version_id"] == version.id
        assert run["state"] == "AWAITING_APPROVAL"
        assert connection.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
