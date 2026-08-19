import asyncio

import pytest

from test_runtime import make_runtime


class ScriptedConversationModel:
    def __init__(self, response: str) -> None:
        self.response = response
        self.skill_names: list[str] = []

    async def route_and_respond(self, *, on_text_delta, skill_names, **kwargs):
        self.skill_names = list(skill_names)
        midpoint = max(len(self.response) // 2, 1)
        on_text_delta(self.response[:midpoint])
        on_text_delta(self.response[midpoint:])
        return None


class BlockingConversationModel:
    def __init__(self) -> None:
        import asyncio

        self.started = asyncio.Event()

    async def route_and_respond(self, *, cancel_event, **kwargs):
        self.started.set()
        while not cancel_event.is_set():
            await asyncio.sleep(0.005)
        raise RuntimeError("cancelled")


def _count(runtime, table: str) -> int:
    with runtime.db.connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.mark.asyncio
async def test_worker_streams_markdown_and_finishes_answer_without_agent_rows(tmp_path) -> None:
    from app.conversation import TurnSnapshot

    runtime = make_runtime(
        tmp_path,
        ScriptedConversationModel(
            '{"v":1,"policy":"answer","content_shape":"guide",'
            '"reason_code":"content_only"}\n## 桂林\n第一天…'
        ),
    )
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "给我攻略", [])

    await runtime.turn_worker.run_once()

    messages = runtime.conversation.messages(thread.id)
    assert len(messages) == 2
    assert messages[1].content == "## 桂林\n第一天…"
    assert messages[1].status == "ready"
    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    assert _count(runtime, "goals") == 0
    assert _count(runtime, "runs") == 0
    assert [event.type for event in runtime.conversation.events.list(thread.id)] == [
        "turn.accepted",
        "turn.started",
        "turn.policy_decided",
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]


@pytest.mark.asyncio
async def test_propose_execution_waits_for_direction_without_agent_rows(tmp_path) -> None:
    runtime = make_runtime(
        tmp_path,
        ScriptedConversationModel(
            '{"v":1,"policy":"propose_execution","content_shape":"tracking",'
            '"reason_code":"external_effect"}\n我可以在确认后更新清单。'
        ),
    )
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "每天更新清单", [])

    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(accepted.turn_id).status == "AWAITING_DIRECTION"
    assert _count(runtime, "goals") == 0
    assert [event.type for event in runtime.conversation.events.list(thread.id)][-2:] == [
        "message.completed", "turn.awaiting_direction"
    ]


@pytest.mark.asyncio
async def test_cancel_queued_turn_is_persisted_and_not_claimed(tmp_path) -> None:
    runtime = make_runtime(tmp_path, ScriptedConversationModel("unused"))
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "停止", [])

    cancelled = runtime.conversation.cancel_turn(accepted.turn_id)

    assert cancelled.status == "CANCELLED"
    assert await runtime.turn_worker.run_once() is False
    assert [event.type for event in runtime.conversation.events.list(thread.id)][-2:] == [
        "turn.cancel_requested", "turn.cancelled"
    ]


@pytest.mark.asyncio
async def test_cancel_active_turn_stops_model_and_keeps_terminal_state(tmp_path) -> None:
    model = BlockingConversationModel()
    runtime = make_runtime(tmp_path, model)
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "开始", [])
    task = asyncio.create_task(runtime.turn_worker.run_once())

    await model.started.wait()
    runtime.conversation.cancel_turn(accepted.turn_id)
    await task

    assert runtime.conversation.turn(accepted.turn_id).status == "CANCELLED"
    assert "turn.cancelled" in [event.type for event in runtime.conversation.events.list(thread.id)]


@pytest.mark.asyncio
async def test_expired_running_job_recovers_partial_generation(tmp_path) -> None:
    runtime = make_runtime(
        tmp_path,
        ScriptedConversationModel(
            '{"v":1,"policy":"answer","content_shape":"general",'
            '"reason_code":"content_only"}\nnew answer'
        ),
    )
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "继续", [])
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE turns SET status = 'STREAMING' WHERE id = ?", (accepted.turn_id,)
        )
        connection.execute(
            "UPDATE turn_jobs SET status = 'RUNNING', attempts = 1, lease_until = '2000-01-01T00:00:00+00:00' "
            "WHERE turn_id = ?", (accepted.turn_id,)
        )
        connection.execute(
            "INSERT INTO thread_messages(id, thread_id, turn_id, role, content, status, generation, content_length, created_at) "
            "VALUES (?, ?, ?, 'assistant', ?, 'streaming', 1, ?, ?)",
            ("message-old", thread.id, accepted.turn_id, "partial", 7, "2000-01-01T00:00:00+00:00"),
        )

    await runtime.turn_worker.run_once()

    messages = runtime.conversation.messages(thread.id)
    assistants = [message for message in messages if message.role == "assistant"]
    assert [(message.generation, message.status, message.content) for message in assistants] == [
        (1, "interrupted", "partial"),
        (2, "ready", "new answer"),
    ]
    started = [
        event for event in runtime.conversation.events.list(thread.id)
        if event.type == "turn.started"
    ]
    assert started[-1].data["attempt"] == 2


@pytest.mark.asyncio
async def test_invalid_control_head_fails_safely_without_persisting_raw_model_output(tmp_path) -> None:
    from app.conversation import SAFE_FAILURE_MESSAGE

    secret = '{"policy":"goal","token":"secret"}\nraw provider output'
    runtime = make_runtime(tmp_path, ScriptedConversationModel(secret))
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "继续", [])

    await runtime.turn_worker.run_once()

    assistant = [message for message in runtime.conversation.messages(thread.id) if message.role == "assistant"][0]
    assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
    assert assistant.content == "当前暂时无法生成可用回答，请重试。"
    assert "secret" not in assistant.content
    assert any(event.type == "turn.failed" for event in runtime.conversation.events.list(thread.id))
    snapshot = [
        event for event in runtime.conversation.events.list(thread.id)
        if event.type == "message.snapshot"
    ]
    assert len(snapshot) == 1
    assert snapshot[0].data["content"] == SAFE_FAILURE_MESSAGE


@pytest.mark.asyncio
async def test_clarify_completes_turn_without_agent_rows(tmp_path) -> None:
    runtime = make_runtime(
        tmp_path,
        ScriptedConversationModel(
            '{"v":1,"policy":"clarify","content_shape":"general",'
            '"reason_code":"missing_object"}\n请补充要处理的对象。'
        ),
    )
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "帮我处理", [])

    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    assert _count(runtime, "goals") == 0
    assert _count(runtime, "runs") == 0


@pytest.mark.asyncio
async def test_selected_skills_survive_turn_worker(tmp_path) -> None:
    model = ScriptedConversationModel(
        '{"v":1,"policy":"answer","content_shape":"general",'
        '"reason_code":"content_only"}\nanswer'
    )
    runtime = make_runtime(tmp_path, model)
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "回答", ["reflection"])

    await runtime.turn_worker.run_once()

    assert model.skill_names == ["reflection"]
    assert runtime.conversation.turn(accepted.turn_id).skill_names == ("reflection",)
