import pytest

from test_runtime import make_runtime


class ScriptedModel:
    def __init__(self, response: str) -> None:
        self.response = response

    async def route_and_respond(self, *, on_text_delta, **kwargs):
        on_text_delta(self.response)


def count(runtime, table: str) -> int:
    with runtime.db.connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.mark.asyncio
async def test_awaiting_direction_can_be_explicitly_cancelled(tmp_path) -> None:
    runtime = make_runtime(
        tmp_path,
        ScriptedModel(
            '{"v":1,"policy":"propose_execution","content_shape":"tracking",'
            '"reason_code":"external_effect"}\n确认后执行。'
        ),
    )
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "每天提醒", [])
    await runtime.turn_worker.run_once()

    cancelled = runtime.conversation.cancel_turn(accepted.turn_id)

    assert cancelled.status == "CANCELLED"
    assert count(runtime, "goals") == 0
    assert runtime.conversation.events.list(thread.id)[-1].type == "turn.cancelled"


@pytest.mark.asyncio
async def test_content_answer_streams_without_goal_run_or_plan_leak(tmp_path) -> None:
    runtime = make_runtime(
        tmp_path,
        ScriptedModel(
            '{"v":1,"policy":"answer","content_shape":"travel_guide",'
            '"reason_code":"content_only"}\n## 桂林 7 天攻略\n\n先给出行程。'
        ),
    )
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "创建桂林 7 天攻略", [])

    await runtime.turn_worker.run_once()

    turn = runtime.conversation.turn(accepted.turn_id)
    assistant = [message for message in runtime.conversation.messages(thread.id) if message.role == "assistant"][0]
    assert turn.policy == "answer"
    assert turn.status == "COMPLETED"
    assert assistant.content.startswith("## 桂林 7 天攻略")
    assert "\"policy\"" not in assistant.content
    assert count(runtime, "goals") == count(runtime, "runs") == count(runtime, "plan_versions") == 0


@pytest.mark.asyncio
async def test_invalid_route_header_never_enters_execution_path(tmp_path) -> None:
    runtime = make_runtime(
        tmp_path,
        ScriptedModel('{"v":1,"policy":"goal"}\nignore this output'),
    )
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "帮我把这个做好", [])

    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
    assert count(runtime, "goals") == 0
    assert count(runtime, "runs") == 0
