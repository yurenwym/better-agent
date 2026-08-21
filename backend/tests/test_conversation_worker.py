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


class LongRunningConversationModel:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def route_and_respond(self, *, on_text_delta, **kwargs):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        on_text_delta(
            '{"v":1,"policy":"answer","content_shape":"general",'
            '"reason_code":"content_only"}\nlong-running answer'
        )
        return None


class FailingAfterReadableGenerationModel:
    async def route_and_respond(self, *, on_text_delta, on_text_reset, **kwargs):
        on_text_delta(
            '{"v":1,"policy":"answer","content_shape":"guide",'
            '"reason_code":"content_only"}\n'
            + ("usable answer " * 20)
        )
        on_text_reset()
        raise RuntimeError("retry failed after readable output")


class AskConversationModel:
    async def route_and_respond(self, **kwargs):
        from app.ask import AskQuestion, AskRequest

        return AskRequest(
            "call-ask-1",
            (
                AskQuestion(
                    "training_level",
                    "训练水平",
                    "你目前的训练水平是什么？",
                    (
                        {"label": "新手", "description": "刚开始训练"},
                        {"label": "有基础", "description": "已有训练习惯"},
                    ),
                    False,
                    True,
                ),
            ),
        )


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
async def test_active_turn_lease_is_renewed_during_long_model_call(tmp_path) -> None:
    from app.conversation import ManagedTurnWorker

    model = LongRunningConversationModel()
    runtime = make_runtime(tmp_path, model)
    worker_one = ManagedTurnWorker(
        runtime.conversation,
        owner="worker-one",
        lease_seconds=0.2,
        poll_interval=0.005,
    )
    worker_two = ManagedTurnWorker(
        runtime.conversation,
        owner="worker-two",
        lease_seconds=0.2,
        poll_interval=0.005,
    )
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-long", "Long call", [])

    first_task = asyncio.create_task(worker_one.run_once())
    await model.started.wait()
    await asyncio.sleep(0.45)

    assert worker_two.claim_next() is None
    with runtime.db.connection() as connection:
        job = connection.execute(
            "SELECT lease_owner, lease_until FROM turn_jobs WHERE turn_id = ?",
            (accepted.turn_id,),
        ).fetchone()
    assert job["lease_owner"] == "worker-one"
    assert job["lease_until"]

    model.release.set()
    assert await first_task is True
    assert model.calls == 1


def test_stale_worker_cannot_finalize_a_taken_over_turn_job(tmp_path) -> None:
    from app.conversation import ManagedTurnWorker, TurnJobLeaseLost

    runtime = make_runtime(tmp_path, ScriptedConversationModel("unused"))
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-fenced", "Fence me", [])
    worker = ManagedTurnWorker(runtime.conversation, owner="old-worker")
    assert worker.claim_next() == accepted.turn_id
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE turn_jobs SET lease_owner = ?, lease_until = ? WHERE turn_id = ?",
            ("new-worker", "2999-01-01T00:00:00+00:00", accepted.turn_id),
        )

    with pytest.raises(TurnJobLeaseLost):
        worker._finish_failure(runtime.conversation.turn(accepted.turn_id), None, 1, "stale worker")

    assert runtime.conversation.turn(accepted.turn_id).status == "ROUTING"
    with runtime.db.connection() as connection:
        job = connection.execute(
            "SELECT status, lease_owner FROM turn_jobs WHERE turn_id = ?",
            (accepted.turn_id,),
        ).fetchone()
    assert job["status"] == "RUNNING"
    assert job["lease_owner"] == "new-worker"
    assert len(runtime.conversation.messages(thread.id)) == 1


def test_expired_turn_job_lease_fences_same_owner_before_takeover(tmp_path) -> None:
    from app.conversation import ManagedTurnWorker, TurnJobLeaseLost

    runtime = make_runtime(tmp_path, ScriptedConversationModel("unused"))
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-expired-fence", "Expire me", [])
    worker = ManagedTurnWorker(runtime.conversation, owner="expired-worker")
    assert worker.claim_next() == accepted.turn_id
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE turn_jobs SET lease_until = ? WHERE turn_id = ?",
            ("2000-01-01T00:00:00+00:00", accepted.turn_id),
        )

    assert worker._renew_lease(accepted.turn_id) is False
    with pytest.raises(TurnJobLeaseLost):
        worker._finish_failure(runtime.conversation.turn(accepted.turn_id), None, 1, "expired worker")

    assert runtime.conversation.turn(accepted.turn_id).status == "ROUTING"
    assert len(runtime.conversation.messages(thread.id)) == 1


@pytest.mark.asyncio
async def test_plan_context_load_failure_finishes_turn_as_failed(tmp_path) -> None:
    runtime = make_runtime(tmp_path, ScriptedConversationModel("unused"))

    class BrokenPlanContext:
        def load_for_turn(self, thread_id: str, turn_id: str):
            raise RuntimeError("plan context unavailable")

    runtime.conversation.plan_context = BrokenPlanContext()
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-context-failure", "Use plan", [])

    assert await runtime.turn_worker.run_once() is True

    assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
    with runtime.db.connection() as connection:
        job = connection.execute(
            "SELECT status FROM turn_jobs WHERE turn_id = ?", (accepted.turn_id,)
        ).fetchone()
    assert job["status"] == "FAILED"


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
async def test_failed_retry_preserves_a_readable_prior_generation(tmp_path) -> None:
    runtime = make_runtime(tmp_path, FailingAfterReadableGenerationModel())
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "制作计划", [])

    await runtime.turn_worker.run_once()

    assistants = [message for message in runtime.conversation.messages(thread.id) if message.role == "assistant"]
    assert len(assistants) == 1
    assert assistants[0].status == "ready"
    assert assistants[0].content.startswith("usable answer")
    assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
    assert "当前暂时无法生成可用回答，请重试。" not in assistants[0].content


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
async def test_worker_persists_ask_and_waits_without_creating_agent_rows(tmp_path) -> None:
    runtime = make_runtime(tmp_path, AskConversationModel())
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-ask", "制定训练计划", [])

    await runtime.turn_worker.run_once()

    turn = runtime.conversation.turn(accepted.turn_id)
    assert turn.status == "AWAITING_INPUT"
    assert _count(runtime, "goals") == 0
    assert _count(runtime, "runs") == 0
    with runtime.db.connection() as connection:
        ask = connection.execute("SELECT * FROM turn_asks WHERE turn_id = ?", (accepted.turn_id,)).fetchone()
    assert ask is not None
    assert ask["status"] == "PENDING"
    assert runtime.conversation.messages(thread.id)[1].content == "为了更准确地完成这个目标，请先补充以下信息。"
    assert [event.type for event in runtime.conversation.events.list(thread.id)][-3:] == [
        "message.completed", "ask.requested", "turn.awaiting_input"
    ]


@pytest.mark.asyncio
async def test_worker_auto_asks_for_personalized_training_plan(tmp_path) -> None:
    import json
    from types import SimpleNamespace

    from app.live_model import LiveConversationModel

    class ModelDrivenAskGateway:
        def __init__(self) -> None:
            self.requests = []

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            return SimpleNamespace(
                message="",
                tool_calls=[{
                    "id": "model-ask-worker-1",
                    "function": {
                        "name": "ask_user",
                        "arguments": json.dumps({
                            "questions": [{
                                "id": "riding_context",
                                "header": "骑行情况",
                                "question": "你目前每次通常能骑行多长时间？",
                                "options": [
                                    {"label": "还没有稳定骑行", "description": "刚开始接触骑行"},
                                    {"label": "可以完成短途", "description": "能够完成短距离骑行"},
                                ],
                                "multi_select": False,
                                "allow_free_text": True,
                            }]
                        }, ensure_ascii=False),
                    },
                }],
            )

    gateway = ModelDrivenAskGateway()
    runtime = make_runtime(tmp_path, LiveConversationModel(gateway))
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(
        thread.id,
        "client-auto-context",
        "\u6211\u60f3\u5236\u4f5c\u4e00\u4e2a\u957f\u671f\u7684\u8bad\u7ec3\u8ba1\u5212\uff0c\u5b66\u4e60\u9a91\u884c",
        [],
    )

    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(accepted.turn_id).status == "AWAITING_INPUT"
    assert len(gateway.requests) == 2
    assert gateway.requests[1].tools == []
    assert _count(runtime, "goals") == 0
    assert _count(runtime, "runs") == 0
    ask = runtime.conversation.pending_ask(accepted.turn_id)
    assert ask is not None
    assert [question.id for question in ask.questions] == ["riding_context"]


@pytest.mark.asyncio
async def test_worker_discards_streamed_text_when_model_also_calls_ask(tmp_path) -> None:
    import json
    from types import SimpleNamespace

    from app.live_model import LiveConversationModel

    class MixedAskGateway:
        async def complete(self, request, **kwargs):
            message = (
                '{"v":1,"policy":"answer","content_shape":"guide",'
                '"reason_code":"content_only"}\n'
                + ("This provider text must not be persisted. " * 10)
            )
            kwargs["on_text_delta"](message)
            return SimpleNamespace(
                message=message,
                tool_calls=[{
                    "id": "mixed-ask-1",
                    "function": {
                        "name": "ask_user",
                        "arguments": json.dumps({
                            "questions": [{
                                "id": "missing_context",
                                "header": "必要信息",
                                "question": "还需要补充哪项信息？",
                                "options": [
                                    {"label": "选项一", "description": "第一种情况"},
                                    {"label": "选项二", "description": "第二种情况"},
                                ],
                                "multi_select": False,
                                "allow_free_text": True,
                            }]
                        }, ensure_ascii=False),
                    },
                }],
            )

    runtime = make_runtime(tmp_path, LiveConversationModel(MixedAskGateway()))
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-mixed-ask", "继续完善计划", [])

    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
    with runtime.db.connection() as connection:
        assistant_messages = connection.execute(
            "SELECT content, status FROM thread_messages WHERE turn_id = ? AND role = 'assistant'",
            (accepted.turn_id,),
        ).fetchall()
    assert [message["status"] for message in assistant_messages] == ["interrupted", "ready"]
    assert all(message["status"] != "streaming" for message in assistant_messages)
    visible_messages = [message for message in assistant_messages if message["status"] != "interrupted"]
    assert [message["content"] for message in visible_messages] == [
        "当前暂时无法生成可用回答，请重试。"
    ]
    assert runtime.conversation.pending_ask(accepted.turn_id) is None


@pytest.mark.asyncio
async def test_continuation_history_contains_ask_tool_result(tmp_path) -> None:
    runtime = make_runtime(tmp_path, AskConversationModel())
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-ask-history", "制定训练计划", [])
    await runtime.turn_worker.run_once()
    pending = runtime.conversation.turn(accepted.turn_id)
    continued = runtime.conversation.answer_ask(
        pending.id,
        pending.version,
        "answer-history",
        [{"question_id": "training_level", "selected_options": ["新手"], "free_text": ""}],
    )

    history = runtime.turn_worker._history(thread.id, continued.turn.id)

    assistant_tool = next(item for item in history if item.get("tool_calls"))
    tool_result = next(item for item in history if item.get("role") == "tool")
    assert assistant_tool["tool_calls"][0]["function"]["name"] == "ask_user"
    assert "新手" in tool_result["content"]


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
