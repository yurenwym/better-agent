import asyncio
from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_action_context_cannot_be_dropped_to_fit_conversation(tmp_path, monkeypatch):
    from app.token_budget import pack_messages_newest
    from app.startup import build_runtime

    reached_transport = []
    class Model:
        async def route_and_respond(self, *, content, history, **kwargs):
            pack_messages_newest([*history, {"role": "user", "content": content}], budget=300)
            reached_transport.append(True)

    runtime = build_runtime(tmp_path, conversation_model=Model())
    try:
        monkeypatch.setattr(runtime.conversation.goal_context, "load_for_turn",
                            lambda *args: SimpleNamespace(context_text="bound action requirements " * 100))
        thread = runtime.conversation.create_thread("action overflow")
        accepted = runtime.conversation.accept_turn(thread.id, "action-overflow", "继续当前行动", [])
        await runtime.turn_worker.run_once()
        assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
        assert not reached_transport
    finally:
        runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["answer", "propose_execution", "propose_plan", "ask"])
async def test_incomplete_archive_only_allows_plain_answer(tmp_path, monkeypatch, policy):
    from app.memory_archive import ArchiveUnavailable
    class Model(ScriptedConversationModel):
        async def answer_without_history(self, *, history, memory_context_content, on_text_delta, **kwargs):
            assert history == [] and memory_context_content is None
            if policy == "ask":
                from app.ask import AskRequest
                return AskRequest(call_id="blocked-ask", questions=())
            on_text_delta('{"v":1,"policy":"'+policy+'","content_shape":"text","reason_code":"test"}\nLimited answer')
    runtime=make_runtime(tmp_path,Model("unused"))
    thread=runtime.conversation.create_thread("incomplete")
    accepted=runtime.conversation.accept_turn(thread.id,"m1-limited","Independent question",[])
    async def unavailable(turn, **kwargs): raise ArchiveUnavailable("dead letter")
    monkeypatch.setattr(runtime.turn_worker.primary,"_archive_history_before_generation",unavailable)
    await runtime.turn_worker.run_once()
    assert runtime.conversation.turn(accepted.turn_id).status == ("COMPLETED" if policy == "answer" else "FAILED")
    assert any(event.type == "context.incomplete" for event in runtime.conversation.events.list(thread.id))
    with runtime.db.connection() as c:
        for table in ("turn_asks", "plan_documents", "research_jobs", "agent_runs", "memory_entries"):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


class MemoryAwareConversationModel(ScriptedConversationModel):
    def __init__(self, response: str, *, applied: bool) -> None:
        super().__init__(response)
        self.applied = applied

    async def route_and_respond(self, *, on_memory_context_applied=None, **kwargs):
        await super().route_and_respond(**kwargs)
        if self.applied and on_memory_context_applied is not None:
            on_memory_context_applied()


class HistoryRecordingConversationModel(ScriptedConversationModel):
    def __init__(self, response: str) -> None:
        super().__init__(response)
        self.history: list[dict] | None = None
        self.calls = 0

    async def route_and_respond(self, *, history, **kwargs):
        self.calls += 1
        self.history = history
        return await super().route_and_respond(**kwargs)


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


class ConcurrentConversationModel:
    def __init__(self, expected_concurrency: int) -> None:
        self.expected_concurrency = expected_concurrency
        self.active = 0
        self.max_active = 0
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()

    async def route_and_respond(self, *, on_text_delta, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active >= self.expected_concurrency:
            self.all_started.set()
        await self.release.wait()
        on_text_delta(
            '{"v":1,"policy":"answer","content_shape":"general",'
            '"reason_code":"content_only"}\nanswer'
        )
        self.active -= 1


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


class AskContinuationRecordingModel:
    def __init__(self) -> None:
        self.ask_parent_requests: list[str | None] = []

    async def route_and_respond(self, *, ask_parent_request=None, on_text_delta, **kwargs):
        from app.ask import AskQuestion, AskRequest

        self.ask_parent_requests.append(ask_parent_request)
        if len(self.ask_parent_requests) == 1:
            return AskRequest(
                "call-plan-document-ask",
                (
                    AskQuestion(
                        "training_level",
                        "训练水平",
                        "你目前的训练水平是什么？",
                        (),
                        False,
                        True,
                    ),
                ),
            )
        on_text_delta(
            '{"v":1,"policy":"answer","content_shape":"general",'
            '"reason_code":"content_only"}\nanswer'
        )


def _count(runtime, table: str) -> int:
    with runtime.db.connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.mark.asyncio
async def test_worker_pool_runs_different_threads_concurrently_within_cap(tmp_path) -> None:
    from app.conversation import ManagedTurnWorkerPool

    model = ConcurrentConversationModel(expected_concurrency=2)
    runtime = make_runtime(tmp_path, model)
    runtime.turn_worker = ManagedTurnWorkerPool(runtime.conversation, concurrency=2, poll_interval=0.005)
    for index in range(3):
        thread = runtime.conversation.create_thread(f"thread-{index}")
        runtime.conversation.accept_turn(thread.id, f"client-{index}", "answer", [])

    await runtime.turn_worker.start()
    await asyncio.wait_for(model.all_started.wait(), timeout=2)
    assert model.max_active == 2
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_jobs WHERE status='RUNNING'").fetchone()[0] == 2
    model.release.set()
    for _ in range(200):
        if not runtime.conversation.pending_turn_jobs():
            break
        await asyncio.sleep(0.005)
    await runtime.turn_worker.stop()

    assert model.max_active == 2
    assert not runtime.conversation.pending_turn_jobs()


def test_claim_next_never_runs_two_turns_from_the_same_thread(tmp_path) -> None:
    from app.conversation import ManagedTurnWorker

    runtime = make_runtime(tmp_path, ScriptedConversationModel("unused"))
    thread = runtime.conversation.create_thread("same-thread")
    first = runtime.conversation.accept_turn(thread.id, "first", "first", [])
    second = runtime.conversation.accept_turn(thread.id, "second", "second", [])
    worker_one = ManagedTurnWorker(runtime.conversation, owner="one")
    worker_two = ManagedTurnWorker(runtime.conversation, owner="two")

    assert worker_one.claim_next() == first.turn_id
    assert worker_two.claim_next() is None
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE turn_jobs SET status='COMPLETED',lease_owner=NULL,lease_until=NULL,finished_at=datetime('now') "
            "WHERE turn_id=?", (first.turn_id,),
        )
    assert worker_two.claim_next() == second.turn_id


def test_worker_pool_concurrency_uses_bounded_environment_value(tmp_path, monkeypatch) -> None:
    from app.conversation import ManagedTurnWorkerPool

    runtime = make_runtime(tmp_path, ScriptedConversationModel("unused"))
    monkeypatch.setenv("BETTER_AGENT_TURN_WORKER_CONCURRENCY", "32")
    assert ManagedTurnWorkerPool(runtime.conversation).concurrency == 16
    monkeypatch.setenv("BETTER_AGENT_TURN_WORKER_CONCURRENCY", "0")
    assert ManagedTurnWorkerPool(runtime.conversation).concurrency == 1
    monkeypatch.setenv("BETTER_AGENT_TURN_WORKER_CONCURRENCY", "invalid")
    with pytest.raises(ValueError, match="must be an integer"):
        ManagedTurnWorkerPool(runtime.conversation)


@pytest.mark.asyncio
async def test_terminal_turn_persists_latency_metrics_and_event(tmp_path) -> None:
    runtime = make_runtime(
        tmp_path,
        ScriptedConversationModel(
            '{"v":1,"policy":"answer","content_shape":"general",'
            '"reason_code":"content_only"}\nanswer'
        ),
    )
    thread = runtime.conversation.create_thread("metrics")
    accepted = runtime.conversation.accept_turn(thread.id, "metrics-turn", "answer", [])

    await runtime.turn_worker.run_once()

    turn = runtime.conversation.turn(accepted.turn_id)
    assert turn.queue_wait_ms is not None and turn.queue_wait_ms >= 0
    assert turn.context_ms is not None and turn.context_ms >= 0
    assert turn.model_ttft_ms is not None and turn.model_ttft_ms >= 0
    assert turn.stream_ms is not None and turn.stream_ms >= 0
    assert turn.answer_wait_ms is not None and turn.answer_wait_ms >= 0
    assert turn.total_ms is not None and turn.total_ms >= turn.queue_wait_ms
    assert turn.model_attempt_count == 0
    metric_events = [
        event for event in runtime.conversation.events.list(thread.id)
        if event.type == "turn.metrics.updated"
    ]
    assert len(metric_events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("model_message", [
    '```markdown\n{\n  "v": 1,\n  "policy": "answer"\n}\n\n'
    "# 学习计划\n\n每天学习两小时，并在周末复盘。\n```",
    "v=1\npolicy=answer\n\n# 学习计划\n\n每天学习两小时，并在周末复盘。",
])
async def test_missing_control_header_completes_full_conversation_flow(tmp_path, model_message) -> None:
    from app.live_model import LiveConversationModel

    class PlainAnswerGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _request, **kwargs):
            self.calls += 1
            message = model_message
            kwargs["on_text_delta"](message)
            return SimpleNamespace(message=message, tool_calls=[], finish_reason="stop")

    gateway = PlainAnswerGateway()
    runtime = make_runtime(tmp_path, LiveConversationModel(gateway))
    thread = runtime.conversation.create_thread("protocol fallback")
    accepted = runtime.conversation.accept_turn(
        thread.id,
        "protocol-fallback-turn",
        "当前数学水平：大学基础；目标：考试升学；每天两小时。",
        [],
    )

    assert await runtime.turn_worker.run_once() is True

    turn = runtime.conversation.turn(accepted.turn_id)
    messages = runtime.conversation.messages(thread.id)
    assert turn.status == "COMPLETED"
    assert gateway.calls == 1
    assert messages[-1].role == "assistant"
    assert messages[-1].content == "# 学习计划\n\n每天学习两小时，并在周末复盘。"
    deltas = [event.data["delta"] for event in runtime.conversation.events.list(thread.id)
              if event.type == "message.delta"]
    assert "v=1" not in "".join(deltas)
    assert "policy=answer" not in "".join(deltas)


@pytest.mark.asyncio
async def test_turn_metrics_count_all_auxiliary_model_attempts(tmp_path) -> None:
    runtime = make_runtime(
        tmp_path,
        ScriptedConversationModel(
            '{"v":1,"policy":"answer","content_shape":"general",'
            '"reason_code":"content_only"}\nanswer'
        ),
    )
    thread = runtime.conversation.create_thread("attempt metrics")
    accepted = runtime.conversation.accept_turn(thread.id, "attempt-metrics-turn", "answer", [])
    with runtime.db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_profiles(id,owner_id,name,status,created_at,updated_at) "
            "VALUES ('metrics-profile','local-user','metrics','ACTIVE','now','now')"
        )
        connection.execute(
            "INSERT INTO model_profile_versions(id,profile_id,version,provider_protocol,provider_name,base_url,"
            "model_name,credential_env_ref,capabilities_json,context_window,max_output_tokens,timeout_seconds,"
            "max_attempts,config_digest,created_at) VALUES ('metrics-version','metrics-profile',1,"
            "'openai_compatible','test','https://example.test','model','TEST_KEY','{}',8192,1024,30,1,'digest','now')"
        )
        for index, purpose in enumerate(("route_and_respond", "classify_plan_document_request"), start=1):
            invocation_id = f"metrics-invocation-{index}"
            connection.execute(
                "INSERT INTO model_invocations(id,owner_id,thread_id,turn_id,role,purpose,routing_policy_digest,"
                "route_snapshot_json,request_digest,tool_schema_digest,context_snapshot_digest,status,idempotency_key,"
                "created_at,finished_at) VALUES (?, 'local-user', ?, ?, 'conversation', ?, 'route', '{}', ?, 'tools',"
                "'context', 'SUCCEEDED', ?, '2026-09-07T00:00:00+00:00', '2026-09-07T00:00:01+00:00')",
                (invocation_id, thread.id, accepted.turn_id, purpose, f"request-{index}", f"key-{index}"),
            )
            connection.execute(
                "INSERT INTO model_attempts(id,invocation_id,ordinal,reason,profile_version_id,provider_protocol,"
                "request_digest,status,started_at,first_token_at,finished_at,usage_status,cost_status) VALUES "
                "(?, ?, 1, 'primary', 'metrics-version', 'openai_compatible', ?, 'SUCCEEDED', "
                "'2026-09-07T00:00:00+00:00', '2026-09-07T00:00:00.1+00:00', "
                "'2026-09-07T00:00:01+00:00', 'UNAVAILABLE', 'UNAVAILABLE')",
                (f"metrics-attempt-{index}", invocation_id, f"request-{index}"),
            )

    assert await runtime.turn_worker.run_once() is True
    assert runtime.conversation.turn(accepted.turn_id).model_attempt_count == 2


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
        "turn.metrics.updated",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("applied", [True, False])
async def test_worker_audits_memory_only_after_model_confirms_actual_use(tmp_path, applied) -> None:
    from app.memory_v2 import MemoryContextProvider, MemoryStore

    model = MemoryAwareConversationModel(
        '{"v":1,"policy":"answer","content_shape":"general","reason_code":"content_only"}\nanswer',
        applied=applied,
    )
    runtime = make_runtime(tmp_path, model)
    store = MemoryStore(runtime.db, tmp_path / "memory-v2")
    store.remember("local-user", "preference", "user", "", "Prefer concise answers", "remember-audit")
    runtime.memory_context = MemoryContextProvider(runtime.db)
    thread = runtime.conversation.create_thread("Chat")
    runtime.conversation.accept_turn(thread.id, "memory-audit", "Answer concisely", [])

    await runtime.turn_worker.run_once()

    events = [event for event in runtime.conversation.events.list(thread.id) if event.type == "memory.context_applied"]
    assert bool(events) is applied


@pytest.mark.asyncio
async def test_worker_archives_old_history_before_model_and_replaces_raw_source_with_episode(tmp_path) -> None:
    from app.memory_archive import ConversationArchiver
    from app.memory_v2 import MemoryContextProvider, MemoryStore

    async def summarize(payload):
        source_id = payload["turns"][0]["events"][0]["message_id"]
        item = {"text": "用户早先讨论了旧主题", "source_message_ids": [source_id]}
        return {
            "synopsis": [item], "topics": [], "decisions": [], "outcomes": [],
            "open_loops": [], "sensitivity": "normal",
        }

    model = HistoryRecordingConversationModel(
        '{"v":1,"policy":"answer","content_shape":"general","reason_code":"content_only"}\nanswer'
    )
    runtime = make_runtime(tmp_path, model)
    store = MemoryStore(runtime.db, tmp_path / "memory-v2")
    runtime.memory_context = MemoryContextProvider(runtime.db)
    runtime.archiver = ConversationArchiver(
        runtime.db, store, summarize, keep_tokens=1_000, max_summary_tokens=12_000,
    )
    thread = runtime.conversation.create_thread("Archive before generation")
    now = "2026-01-01T00:00:00+00:00"
    old_marker = "OLD_RAW_SOURCE_" + ("x" * 2_000)
    with runtime.db.transaction() as connection:
        # Six historical turns: the default N=5 protection must leave exactly
        # the oldest turn archivable, which is the one carrying the old marker.
        seeded = [("old-turn", old_marker, "old answer")]
        seeded.extend(
            (f"filler-{index}", f"filler user {index}", f"filler answer {index}")
            for index in range(4)
        )
        seeded.append(("recent-turn", "RECENT_RAW_SOURCE", "recent answer"))
        for index, (turn_id, user_text, assistant_text) in enumerate(seeded):
            connection.execute(
                "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
                "VALUES (?,?,?,'COMPLETED',?,?)",
                (turn_id, thread.id, turn_id, now, now),
            )
            for offset, (role, content) in enumerate((("user", user_text), ("assistant", assistant_text))):
                sequence = index * 2 + 1 + offset
                connection.execute(
                    "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
                    "content_length,message_seq,created_at,completed_at) "
                    "VALUES (?,?,?,?,?,'ready',1,?,?,?,?)",
                    (
                        f"message-{sequence}", thread.id, turn_id, role, content,
                        len(content), sequence, now, now,
                    ),
                )
    runtime.conversation.accept_turn(thread.id, "current-turn", "CURRENT_REQUEST", [])

    await runtime.turn_worker.run_once()

    assert model.calls == 1
    assert model.history is not None
    rendered = "\n".join(str(message.get("content", "")) for message in model.history)
    assert "用户早先讨论了旧主题" in rendered
    assert "OLD_RAW_SOURCE_" not in rendered
    assert "RECENT_RAW_SOURCE" in rendered
    with runtime.db.connection() as connection:
        episode = connection.execute(
            "SELECT status,start_message_seq,end_message_seq FROM memory_episodes"
        ).fetchone()
        state = connection.execute(
            "SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",
            (thread.id,),
        ).fetchone()
    assert episode["status"] == "ACTIVE"
    assert (episode["start_message_seq"], episode["end_message_seq"]) == (1, 2)
    assert state["archived_through_seq"] == 2


@pytest.mark.asyncio
async def test_worker_does_not_generate_when_required_archive_permanently_fails(tmp_path) -> None:
    from app.memory_archive import ConversationArchiver
    from app.memory_v2 import MemoryStore

    async def invalid_summary(_payload):
        return "not-json"

    model = HistoryRecordingConversationModel(
        '{"v":1,"policy":"answer","content_shape":"general","reason_code":"content_only"}\nanswer'
    )
    runtime = make_runtime(tmp_path, model)
    runtime.archiver = ConversationArchiver(
        runtime.db, MemoryStore(runtime.db, tmp_path / "memory-v2"), invalid_summary,
        keep_tokens=1, max_attempts=1,
    )
    thread = runtime.conversation.create_thread("Archive failure")
    now = "2026-01-01T00:00:00+00:00"
    with runtime.db.transaction() as connection:
        # One archivable turn outside the protected N=5 suffix. With only one
        # visible turn there would be nothing the foreground could archive.
        for index in range(6):
            turn_id = f"old-turn-{index}"
            connection.execute(
                "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
                "VALUES (?,?,?,'COMPLETED',?,?)",
                (turn_id, thread.id, turn_id, now, now),
            )
            connection.execute(
                "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
                "content_length,message_seq,created_at,completed_at) "
                "VALUES (?,?,?,'user','old source','ready',1,10,?,?,?)",
                (f"old-message-{index}", thread.id, turn_id, index + 1, now, now),
            )
    accepted = runtime.conversation.accept_turn(thread.id, "current-turn", "CURRENT_REQUEST", [])

    await runtime.turn_worker.run_once()

    assert model.calls == 0
    assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
    with runtime.db.connection() as connection:
        job = connection.execute(
            "SELECT status,last_error_code FROM memory_archive_jobs"
        ).fetchone()
        state = connection.execute(
            "SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",
            (thread.id,),
        ).fetchone()
    assert job["status"] == "DEAD_LETTER"
    assert job["last_error_code"] == "invalid_summary"
    assert state["archived_through_seq"] == 0


@pytest.mark.asyncio
async def test_completed_conversation_finishes_canary_exposure_with_safety_judgment(tmp_path) -> None:
    runtime = make_runtime(tmp_path, ScriptedConversationModel(
        '{"v":1,"policy":"answer","content_shape":"general","reason_code":"content_only"}\nsafe answer'
    ))
    calls = []
    class Evolution:
        def assign_run(self, run_id, assignment_key, *, connection=None): return None, "deployment"
        def finish_run_exposure(self, run_id, *, success, safety_pass=None, connection=None):
            calls.append((run_id, success, safety_pass))
    class Judge:
        async def judge(self, observable):
            assert observable["output"] == "safe answer"
            return True
    runtime.evolution = Evolution()
    runtime.safety_judge = Judge()
    thread = runtime.conversation.create_thread("Canary")
    accepted = runtime.conversation.accept_turn(thread.id, "canary-turn", "answer", [])

    await runtime.turn_worker.run_once()

    assert calls == [(accepted.turn_id, True, True)]


@pytest.mark.asyncio
async def test_worker_routes_start_expert_to_bounded_agent_run_without_visible_raw_header(tmp_path, monkeypatch) -> None:
    from app.startup import build_runtime

    class ExpertRouteModel:
        async def route_and_respond(self, *, on_text_delta, **kwargs):
            on_text_delta('{"v":4,"policy":"start_expert","content_shape":"expert","reason_code":"complex_compare","expert":{"objective":"比较训练方案","roles":["planner","critic"]}}\n')

    runtime = build_runtime(tmp_path)
    runtime.conversation.route_model = ExpertRouteModel()
    history = [{"role": "system", "content": "bounded plan source v1"}] + [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"history-{index}"}
        for index in range(14)
    ]
    monkeypatch.setattr(runtime.turn_worker.primary, "_history", lambda *args, **kwargs: history.copy())
    thread = runtime.conversation.create_thread("专家路由")
    accepted = runtime.conversation.accept_turn(thread.id, "client-expert-route", "比较两个训练方案", [])
    pinned = runtime.conversation.turn(accepted.turn_id).runtime_bundle_id
    replacement = runtime.behavior.ensure({"prompt": "changed-after-turn-acceptance"})
    runtime.behavior.activate("stable", replacement.id, "expert-route-version-switch")

    await runtime.turn_worker.run_once()

    turn = runtime.conversation.turn(accepted.turn_id)
    assert turn.status == "COMPLETED"
    assert turn.policy == "start_expert"
    routed_run = runtime.agent_tasks.latest_run_for_thread(thread.id)
    assert routed_run["runtime_bundle_id"] == pinned
    assert routed_run["runtime_bundle_id"] != replacement.id
    from app.agents import AgentTaskConflict
    with pytest.raises(AgentTaskConflict, match="accessible pinned"):
        runtime.agent_tasks.create_run(
            "another-owner", "unauthorized parent", {}, replacement.id,
            thread_id=thread.id, parent_turn_id=accepted.turn_id,
            idempotency_key="wrong-parent-owner",
        )
    assert runtime.agent_tasks.context(routed_run["context_snapshot_id"])["expert_roles"] == ["planner", "critic"]
    snapshot = runtime.agent_tasks.context(routed_run["context_snapshot_id"])
    assert snapshot["history"] == history
    assert snapshot["request"] == "比较两个训练方案"
    assert snapshot["source_turn_id"] == accepted.turn_id
    assert snapshot["source_message_id"] == runtime.conversation.messages(thread.id)[0].id
    assert runtime.agent_tasks.latest_run_for_thread(thread.id)["objective"] == "比较训练方案"
    assert [message.content for message in runtime.conversation.messages(thread.id)] == ["比较两个训练方案"]


@pytest.mark.asyncio
async def test_cancel_accepted_before_research_commit_prevents_durable_side_effect(
    tmp_path, monkeypatch,
) -> None:
    from app.conversation import ControlHeadDecoder
    from app.startup import build_runtime

    runtime = build_runtime(tmp_path)
    runtime.conversation.route_model = ScriptedConversationModel(
        '{"v":3,"policy":"start_research","content_shape":"research",'
        '"reason_code":"explicit_deep_research","research":{"topic":"topic","scope":"web"}}\n'
    )
    thread = runtime.conversation.create_thread("research cancellation fence")
    accepted = runtime.conversation.accept_turn(thread.id, "cancel-research", "research", [])
    original_finish = ControlHeadDecoder.finish

    def cancel_after_decode(decoder):
        original_finish(decoder)
        runtime.conversation.cancel_turn(accepted.turn_id)

    monkeypatch.setattr(ControlHeadDecoder, "finish", cancel_after_decode)

    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(accepted.turn_id).status == "CANCELLED"
    assert _count(runtime, "research_jobs") == 0


@pytest.mark.asyncio
async def test_cancel_accepted_before_expert_commit_prevents_durable_side_effect(
    tmp_path, monkeypatch,
) -> None:
    from app.conversation import ControlHeadDecoder
    from app.startup import build_runtime

    runtime = build_runtime(tmp_path)
    runtime.conversation.route_model = ScriptedConversationModel(
        '{"v":4,"policy":"start_expert","content_shape":"expert",'
        '"reason_code":"complex_compare","expert":{"objective":"compare","roles":["critic"]}}\n'
    )
    thread = runtime.conversation.create_thread("expert cancellation fence")
    accepted = runtime.conversation.accept_turn(thread.id, "cancel-expert", "compare", [])
    original_finish = ControlHeadDecoder.finish

    def cancel_after_decode(decoder):
        original_finish(decoder)
        runtime.conversation.cancel_turn(accepted.turn_id)

    monkeypatch.setattr(ControlHeadDecoder, "finish", cancel_after_decode)

    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(accepted.turn_id).status == "CANCELLED"
    assert _count(runtime, "agent_runs") == 0


@pytest.mark.asyncio
async def test_stale_epoch_cannot_write_terminal_metrics(tmp_path) -> None:
    class TakenOverConversationModel(ScriptedConversationModel):
        async def route_and_respond(self, **kwargs):
            await super().route_and_respond(**kwargs)
            now = "2099-01-01T00:00:00+00:00"
            with runtime.db.transaction() as connection:
                connection.execute(
                    "UPDATE turns SET status='COMPLETED',updated_at=? WHERE id=?",
                    (now, accepted.turn_id),
                )
                connection.execute(
                    "UPDATE turn_jobs SET status='COMPLETED',lease_epoch=lease_epoch+1,"
                    "lease_owner=NULL,lease_until=NULL,finished_at=? WHERE turn_id=?",
                    (now, accepted.turn_id),
                )

    model = TakenOverConversationModel(
        '{"v":1,"policy":"answer","content_shape":"general",'
        '"reason_code":"content_only"}\nanswer'
    )
    runtime = make_runtime(tmp_path, model)
    thread = runtime.conversation.create_thread("metrics epoch fence")
    accepted = runtime.conversation.accept_turn(thread.id, "stale-metrics", "answer", [])

    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    assert _count(runtime, "turn_metrics") == 0


@pytest.mark.asyncio
async def test_worker_routes_expert_with_authoritative_non_default_owner(tmp_path) -> None:
    from app.startup import build_runtime

    class ExpertRouteModel:
        async def route_and_respond(self, *, on_text_delta, **kwargs):
            on_text_delta('{"v":4,"policy":"start_expert","content_shape":"expert","reason_code":"complex_compare","expert":{"objective":"compare","roles":["planner"]}}\n')

    runtime = build_runtime(tmp_path)
    runtime.conversation.route_model = ExpertRouteModel()
    thread = runtime.conversation.create_thread("owned", owner_id="alice")
    accepted = runtime.conversation.accept_turn(thread.id, "alice-turn", "compare", [], owner_id="alice")
    await runtime.turn_worker.run_once()
    assert runtime.conversation.turn(accepted.turn_id, "alice").status == "COMPLETED"
    assert runtime.agent_tasks.latest_run_for_thread(thread.id)["owner_id"] == "alice"


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
    assert [event.type for event in runtime.conversation.events.list(thread.id)][-3:] == [
        "message.completed", "turn.awaiting_direction", "turn.metrics.updated"
    ]


@pytest.mark.asyncio
async def test_cancel_queued_turn_is_persisted_and_not_claimed(tmp_path) -> None:
    runtime = make_runtime(tmp_path, ScriptedConversationModel("unused"))
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "停止", [])

    cancelled = runtime.conversation.cancel_turn(accepted.turn_id)

    assert cancelled.status == "CANCELLED"
    assert await runtime.turn_worker.run_once() is False
    assert [event.type for event in runtime.conversation.events.list(thread.id)][-3:] == [
        "turn.cancel_requested", "turn.cancelled", "turn.metrics.updated"
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
async def test_cancel_accepted_before_answer_commit_cannot_be_overwritten(
    tmp_path, monkeypatch,
) -> None:
    from app.conversation import ManagedTurnWorker

    runtime = make_runtime(
        tmp_path,
        ScriptedConversationModel(
            '{"v":1,"policy":"answer","content_shape":"general",'
            '"reason_code":"content_only"}\nanswer'
        ),
    )
    thread = runtime.conversation.create_thread("answer cancellation fence")
    accepted = runtime.conversation.accept_turn(thread.id, "cancel-answer", "answer", [])
    original_finish = ManagedTurnWorker._finish_success

    def cancel_before_commit(worker, *args, **kwargs):
        runtime.conversation.cancel_turn(accepted.turn_id)
        return original_finish(worker, *args, **kwargs)

    monkeypatch.setattr(ManagedTurnWorker, "_finish_success", cancel_before_commit)

    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(accepted.turn_id).status == "CANCELLED"
    assert "turn.completed" not in [
        event.type for event in runtime.conversation.events.list(thread.id)
    ]


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
    assert [event.type for event in runtime.conversation.events.list(thread.id)][-4:] == [
        "message.completed", "ask.requested", "turn.awaiting_input", "turn.metrics.updated"
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
    assert len(gateway.requests) == 1
    assert gateway.requests[0].thinking is False
    assert gateway.requests[0].tools
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
            if request.tools == []:
                return SimpleNamespace(
                    message='{"plan_document_request":false}',
                    tool_calls=[],
                )
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

    assert runtime.conversation.turn(accepted.turn_id).status == "AWAITING_INPUT"
    with runtime.db.connection() as connection:
        assistant_messages = connection.execute(
            "SELECT content, status FROM thread_messages WHERE turn_id = ? AND role = 'assistant'",
            (accepted.turn_id,),
        ).fetchall()
    assert [message["status"] for message in assistant_messages] == ["interrupted", "ready"]
    assert all(message["status"] != "streaming" for message in assistant_messages)
    visible_messages = [message for message in assistant_messages if message["status"] != "interrupted"]
    assert [message["content"] for message in visible_messages] == [
        "为了更准确地完成这个目标，请先补充以下信息。"
    ]
    ask = runtime.conversation.pending_ask(accepted.turn_id)
    assert ask is not None
    assert ask.call_id == "mixed-ask-1"
    assert [question.id for question in ask.questions] == ["missing_context"]


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
async def test_only_answered_ask_continuation_inherits_parent_request_and_pins(tmp_path) -> None:
    model = AskContinuationRecordingModel()
    runtime = make_runtime(tmp_path, model)
    thread = runtime.conversation.create_thread("Chat")
    original_request = "请创建并保存一份骑行训练计划文档"
    accepted = runtime.conversation.accept_turn(
        thread.id, "client-plan-document-ask", original_request, [],
    )
    await runtime.turn_worker.run_once()
    parent = runtime.conversation.turn(accepted.turn_id)
    continued = runtime.conversation.answer_ask(
        parent.id,
        parent.version,
        "answer-plan-document-ask",
        [{"question_id": "training_level", "selected_options": [], "free_text": "新手"}],
    )

    assert continued.turn.runtime_bundle_id == parent.runtime_bundle_id
    assert continued.turn.root_budget_id == parent.root_budget_id
    await runtime.turn_worker.run_once()

    ordinary = runtime.conversation.accept_turn(thread.id, "client-ordinary", "谢谢", [])
    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(continued.turn.id).status == "COMPLETED"
    assert runtime.conversation.turn(ordinary.turn_id).status == "COMPLETED"
    assert model.ask_parent_requests == [None, original_request, None]


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
