"""Deterministic conversation continuation acceptance tests.

These tests exercise the real ``ConversationArchiver`` commit and then capture
what ``LiveConversationModel`` actually hands to the gateway on the next turn.
A dictionary carrying ``_context_required`` is not enough: the plan's acceptance
condition is that an already-committed archive reaches the next request no
matter how unrelated the next question is.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.memory_archive import ConversationArchiver
from app.memory_v2 import (
    ArchiveCoverageMissing,
    MemoryContextProvider,
    MemoryContextRequest,
    MemoryStore,
)
from test_runtime import make_runtime


MARKER = "ARCHIVE_MARKER_BETA"


class RecordingGateway:
    supports_intent_classification = True

    def __init__(self) -> None:
        self.requests: list = []

    async def complete(self, request, **kwargs):
        self.requests.append(request)
        purpose = getattr(request, "purpose", None)
        if purpose == "classify_memory_request":
            return SimpleNamespace(message=json.dumps({
                "remember": False, "kind": "preference", "scope_type": "user",
                "scope_id": "", "content": "",
            }), tool_calls=[])
        if purpose == "classify_research_request":
            return SimpleNamespace(
                message=json.dumps({"start_research": False, "topic": ""}),
                tool_calls=[],
            )
        response = (
            '{"v":1,"policy":"answer","content_shape":"text",'
            '"reason_code":"content_only"}\n结论：好的，下面说明。'
        )
        if kwargs.get("on_text_delta"):
            kwargs["on_text_delta"](response)
        return SimpleNamespace(message=response, tool_calls=[], finish_reason="stop")


def _runtime(tmp_path, gateway):
    from app.live_model import LiveConversationModel

    model = LiveConversationModel(gateway)
    runtime = make_runtime(tmp_path, model)
    runtime.memory_store = MemoryStore(runtime.db, tmp_path / "memory-v2")
    runtime.memory_context = MemoryContextProvider(runtime.db)
    model.memory_store = runtime.memory_store
    return runtime, model


def _seed_completed_turns(runtime, thread_id: str, count: int, *, size: int = 300) -> None:
    now = "2026-01-01T00:00:00+00:00"
    sequence = 0
    with runtime.db.transaction() as connection:
        for index in range(count):
            turn_id = f"{thread_id}-seed-{index}"
            connection.execute(
                "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
                "VALUES (?,?,?,'COMPLETED',?,?)",
                (turn_id, thread_id, turn_id, now, now),
            )
            for role in ("user", "assistant"):
                sequence += 1
                connection.execute(
                    "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
                    "content_length,message_seq,created_at,completed_at) VALUES (?,?,?,?,?,'ready',1,?,?,?,?)",
                    (
                        f"{thread_id}-seed-{index}-{role}", thread_id, turn_id, role,
                        f"历史{role}{index}-" + "x" * size, size, sequence, now, now,
                    ),
                )


def _message_ids(runtime, thread_id: str) -> list[str]:
    with runtime.db.connection() as connection:
        rows = connection.execute(
            "SELECT id FROM thread_messages WHERE thread_id=? ORDER BY message_seq", (thread_id,),
        ).fetchall()
    return [row["id"] for row in rows]


async def _big_summary(payload):
    ids = [
        event["message_id"]
        for turn in payload["turns"]
        for event in turn["events"]
        if event.get("message_id")
    ]
    assert ids

    def item(text: str) -> dict:
        return {"text": text, "source_message_ids": [ids[0]]}

    return {
        "synopsis": [item(f"{MARKER} " + "详细归档" * 100)],
        "topics": [],
        "decisions": [],
        "outcomes": [],
        "open_loops": [],
        "sensitivity": "normal",
    }


def _route_requests(gateway):
    return [
        request for request in gateway.requests
        if getattr(request, "purpose", None) == "route_and_respond"
    ]


def _all_content(request) -> str:
    return "\n".join(
        str(message.get("content", ""))
        for message in request.messages
        if isinstance(message, dict)
    )


# ---------------------------------------------------------------------------
# T01 / T07: an archived summary must reach the next request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_archived_summary_reaches_an_unrelated_next_request(tmp_path) -> None:
    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("续接", owner_id="local-user")
    _seed_completed_turns(runtime, thread.id, 3, size=300)

    archiver = ConversationArchiver(
        runtime.db, runtime.memory_store, _big_summary, keep_messages=2,
    )
    runtime.archiver = archiver
    episode = await archiver.archive_thread(thread.id, "local-user")
    assert episode is not None
    assert archiver.status(thread.id, "local-user")["archived_through_seq"] > 0

    submission = runtime.conversation.accept_turn(
        thread.id, "next-question", "今天天气怎么样", owner_id="local-user",
    )
    assert await runtime.turn_worker.run_once() is True
    assert runtime.conversation.turn(submission.turn_id, "local-user").status == "COMPLETED"

    requests = _route_requests(gateway)
    assert requests, "the conversation must have produced a routed request"
    content = _all_content(requests[-1])
    assert MARKER in content, (
        "the committed archive summary must reach the next request even when the "
        "question shares no keywords with it"
    )
    # The archived raw text is replaced by the summary, the recent unarchived
    # turn survives in full, and the current input is appended exactly once.
    assert "历史user0-" not in content
    assert "历史user2-" in content
    assert content.count("今天天气怎么样") == 1


@pytest.mark.asyncio
async def test_summary_larger_than_old_recall_budget_is_still_sent(tmp_path) -> None:
    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("大摘要", owner_id="local-user")
    _seed_completed_turns(runtime, thread.id, 3, size=300)

    archiver = ConversationArchiver(
        runtime.db, runtime.memory_store, _big_summary, keep_messages=2,
    )
    runtime.archiver = archiver
    assert await archiver.archive_thread(thread.id, "local-user") is not None

    repository = runtime.memory_store
    with runtime.db.connection() as connection:
        row = connection.execute(
            "SELECT summary FROM memory_episodes WHERE thread_id=? AND status='ACTIVE'",
            (thread.id,),
        ).fetchone()
    assert row is not None and len(row["summary"].encode("utf-8")) > 1000

    submission = runtime.conversation.accept_turn(
        thread.id, "big-next", "换个话题，请推荐一部电影", owner_id="local-user",
    )
    assert await runtime.turn_worker.run_once() is True
    assert runtime.conversation.turn(submission.turn_id, "local-user").status == "COMPLETED"
    requests = _route_requests(gateway)
    assert MARKER in _all_content(requests[-1])


# ---------------------------------------------------------------------------
# T11: special send branches keep the continuation contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_existing_plan_branch_still_carries_continuation() -> None:
    import asyncio

    from app.live_model import LiveConversationModel

    gateway = RecordingGateway()
    model = LiveConversationModel(gateway)
    continuation = "MANDATORY-ARCHIVE-CONTINUATION-BODY"
    history = [
        {"role": "system", "content": continuation, "_context_required": True,
         "_context_group": "conversation-continuation"},
        {"role": "user", "content": "帮我制定训练计划"},
        {"role": "assistant", "content": "# 训练计划\n\n- 第一天：慢跑\n- 第二天：力量"},
    ]

    def noop(*args, **kwargs):
        return None

    from app.model_gateway import GatewayError

    with pytest.raises(GatewayError):
        # The stub never returns a plan artifact, so the force-plan retry ends
        # by rejecting the response. That is fine: every request it did send
        # must still have carried the continuation.
        await model.route_and_respond(
            content="把上面的计划写进计划文档",
            history=history,
            skill_names=[],
            on_text_delta=noop,
            on_text_reset=noop,
            cancel_event=asyncio.Event(),
        )
    requests = _route_requests(gateway)
    assert requests, "the plan save branch must still send a request"
    for request in requests:
        assert continuation in _all_content(request), (
            "the save-existing-plan branch must not replace history and drop the "
            "mandatory continuation"
        )


# ---------------------------------------------------------------------------
# T12: revocation, edit and concurrent commit consistency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_episode_edit_revokes_the_pin_and_new_build_uses_new_version(tmp_path) -> None:
    from app.memory_v2 import MemoryConflict

    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    provider = runtime.memory_context
    thread, episode = _seed_continuation(runtime, provider)
    first = _select_with_continuation(provider, thread)

    edited = runtime.memory_store.edit_episode(
        episode.id, "local-user", "edited synopsis", None, episode.version, "edit-episode",
    )
    assert edited.version == episode.version + 1

    # The old fixed invocation is revoked rather than reusing the old body.
    with pytest.raises(MemoryConflict):
        _select_with_continuation(provider, thread)

    # A new request builds against the edited Episode and its new version.
    second = _select_with_continuation(provider, thread, invocation="invoke-2")
    assert "edited synopsis" in second.continuation_rendered
    assert second.continuation_through_seq == 2
    assert first.continuation_rendered != second.continuation_rendered


@pytest.mark.asyncio
async def test_background_commit_after_freeze_does_not_mix_cursor_and_summary(tmp_path) -> None:
    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    provider = runtime.memory_context
    thread, first_episode = _seed_continuation(runtime, provider)
    frozen = _select_with_continuation(provider, thread, invocation="freeze-1")
    assert frozen.continuation_through_seq == 2
    assert len(frozen.continuation_episode_versions) == 1

    # A background pass commits a later prefix after the first snapshot froze.
    ids = _message_ids(runtime, thread.id)
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE conversation_archive_state SET archived_through_seq=4 "
            "WHERE owner_id=? AND thread_id=?",
            ("local-user", thread.id),
        )
    runtime.memory_store.save_episode(
        owner_id="local-user", thread_id=thread.id, project_id=None,
        start_message_seq=3, end_message_seq=4, source_hash="sha256:" + "f" * 64,
        summary="later synopsis",
        synopsis=[{"text": "later synopsis", "source_message_ids": [ids[2]]}],
    )

    # The frozen invocation keeps its old boundary and body.
    replay = _select_with_continuation(provider, thread, invocation="freeze-1")
    assert replay.trace.get("retrieval_mode") == "pin_hit"
    assert replay.continuation_through_seq == 2
    assert "later synopsis" not in replay.continuation_rendered

    # A new build sees both Episodes and the advanced cursor together.
    fresh = provider.select(MemoryContextRequest(
        "local-user", thread.id, None, "后续问题",
        purpose="route_and_respond", model_invocation_id="freeze-2",
        parent_type="turn", parent_id=thread.id,
        history_through_seq=4, include_continuation=True, episode_token_budget=0,
    ))
    assert fresh.continuation_through_seq == 4
    assert len(fresh.continuation_episode_versions) == 2
    assert "later synopsis" in fresh.continuation_rendered


# ---------------------------------------------------------------------------
# T13: build-time and dispatch-time audit can be correlated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_continuation_build_and_dispatch_are_both_audited(tmp_path) -> None:
    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("审计", owner_id="local-user")
    _seed_completed_turns(runtime, thread.id, 3, size=300)

    archiver = ConversationArchiver(
        runtime.db, runtime.memory_store, _big_summary, keep_messages=2,
    )
    runtime.archiver = archiver
    assert await archiver.archive_thread(thread.id, "local-user") is not None

    submission = runtime.conversation.accept_turn(
        thread.id, "audit-next", "无关问题", owner_id="local-user",
    )
    assert await runtime.turn_worker.run_once() is True

    events = runtime.conversation.events.list(thread.id)
    built = [
        event for event in events
        if event.type == "context.continuation_built" and event.turn_id == submission.turn_id
    ]
    dispatched = [
        event for event in events
        if event.type == "context.continuation_dispatched" and event.turn_id == submission.turn_id
    ]
    assert built and built[0].data["has_continuation"] is True
    assert built[0].data["archived_through_seq"] > 0
    assert dispatched, "the actual send path must record dispatch, not only build"
    assert dispatched[0].data["coverage_hash"] == built[0].data["coverage_hash"]


# ---------------------------------------------------------------------------
# T10: missing coverage fails explicitly instead of answering blind
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_coverage_fails_with_a_reason_and_does_not_answer(tmp_path) -> None:
    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("缺口失败", owner_id="local-user")
    _seed_completed_turns(runtime, thread.id, 2, size=120)
    ids = _message_ids(runtime, thread.id)

    # Cursor says turns 1-2 are archived, but only turn 1 has a summary: the
    # second half of the archived prefix is a silent hole.
    with runtime.db.transaction() as connection:
        connection.execute(
            "INSERT INTO conversation_archive_state(owner_id,thread_id,archived_through_seq) VALUES (?,?,?)",
            ("local-user", thread.id, 4),
        )
    runtime.memory_store.save_episode(
        owner_id="local-user", thread_id=thread.id, project_id=None,
        start_message_seq=1, end_message_seq=2, source_hash="sha256:" + "e" * 64,
        summary="只覆盖第一轮",
        synopsis=[{"text": "只覆盖第一轮", "source_message_ids": [ids[0]]}],
    )

    submission = runtime.conversation.accept_turn(
        thread.id, "hole-next", "继续刚才的话题", owner_id="local-user",
    )
    assert await runtime.turn_worker.run_once() is True
    assert runtime.conversation.turn(submission.turn_id, "local-user").status == "FAILED"

    failures = [
        event for event in runtime.conversation.events.list(thread.id)
        if event.type == "context.continuation_failed" and event.turn_id == submission.turn_id
    ]
    assert failures
    assert failures[0].data["reason_code"] in {
        "archive_coverage_missing", "archive_coverage_invalid",
    }
    # The blind fallback must never run for a mandatory-context failure.
    assert not [
        request for request in gateway.requests
        if getattr(request, "purpose", None) in {"classify_context_dependency", "answer_without_history"}
    ]
    # The user's input stays durable.
    assert any(
        message.role == "user" and message.content == "继续刚才的话题"
        for message in runtime.conversation.messages(thread.id, "local-user")
    )


# ---------------------------------------------------------------------------
# T08: complete-request packing keeps the required continuation
# ---------------------------------------------------------------------------

def test_required_continuation_survives_packing_while_optional_is_dropped() -> None:
    from app.token_budget import DEFAULT_TOKEN_COUNTER, pack_messages_newest

    continuation = "承接摘要内容" * 40
    required = [
        {"role": "system", "content": continuation, "_context_required": True,
         "_context_group": "conversation-continuation"},
        {"role": "user", "content": "old question", "_context_required": True,
         "_context_group": "conversation-turn:t1"},
        {"role": "assistant", "content": "old answer", "_context_required": True,
         "_context_group": "conversation-turn:t1"},
    ]
    optional = {"role": "system", "content": "可选长期记忆" * 200,
                "_context_required": False, "_context_group": "memory-context"}
    messages = [*required, optional, {"role": "user", "content": "current question"}]
    required_only = [*required, {"role": "user", "content": "current question"}]
    budget = DEFAULT_TOKEN_COUNTER.count_payload(required_only, None) + 8

    packed = pack_messages_newest(messages, budget=budget)
    contents = [message["content"] for message in packed]
    assert continuation in contents
    assert "old question" in contents and "old answer" in contents
    assert all("可选长期记忆" not in content for content in contents)


def test_required_continuation_overflow_is_an_explicit_error() -> None:
    from app.token_budget import ContextOverflow, pack_messages_newest

    messages = [
        {"role": "system", "content": "承接摘要" * 400, "_context_required": True,
         "_context_group": "conversation-continuation"},
        {"role": "user", "content": "current question"},
    ]
    with pytest.raises(ContextOverflow):
        pack_messages_newest(messages, budget=16)


# ---------------------------------------------------------------------------
# F01: the two P1 regressions (pre-crop and incomplete-request budget)
# ---------------------------------------------------------------------------

def test_history_keeps_all_unarchived_turns_before_required_tagging(tmp_path) -> None:
    from app.model_gateway import ModelProfile
    from app.token_budget import hot_window

    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("五轮", owner_id="local-user")
    _seed_completed_turns(runtime, thread.id, 5, size=800)
    profile = ModelProfile(
        base_url="https://api.deepseek.com", model="m", api_key_env="K",
        context_window=8_192, max_output_tokens=1_024,
    )
    window = hot_window(profile)
    assert window.min_turns == 5

    history = runtime.turn_worker.primary._history(
        thread.id, "not-a-real-turn", window,
        archived_through=0, through_sequence=None, emit_count=False,
    )
    groups = {
        message.get("_context_group") for message in history
        if str(message.get("_context_group", "")).startswith("conversation-turn:")
    }
    # Five historical turns are visible and none has a committed summary, so
    # none may be removed before the request budget decides whether it fits.
    assert len(groups) == 5
    assert all(message.get("_context_required") is True for message in history)


@pytest.mark.asyncio
async def test_foreground_archive_accounts_for_the_complete_request(tmp_path) -> None:
    from app.model_gateway import ModelProfile
    from app.memory_v2 import ContinuationContextOverflow
    from app.token_budget import (
        DEFAULT_TOKEN_COUNTER, effective_input_budget, hot_window, strip_packing_hints,
    )

    gateway = RecordingGateway()
    runtime, model = _runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("完整预算", owner_id="local-user")
    _seed_completed_turns(runtime, thread.id, 6, size=120)
    archiver = ConversationArchiver(runtime.db, runtime.memory_store, _small_summary)
    runtime.archiver = archiver

    accepted = runtime.conversation.accept_turn(
        thread.id, "budget-next", "当前问题", owner_id="local-user",
    )
    turn = runtime.conversation.turn(accepted.turn_id, "local-user")
    tools = model.mandatory_tools()

    def profile_for(context_window: int) -> ModelProfile:
        return ModelProfile(
            base_url="https://api.deepseek.com", model="m", api_key_env="K",
            context_window=context_window, max_output_tokens=1_024,
        )

    # Measure with the same construction the pre-check uses. The probe window is
    # large enough that nothing is projected; the messages are plain text.
    probe_window = hot_window(profile_for(500_000))
    history = runtime.turn_worker.primary._history(
        thread.id, turn.id, probe_window, archived_through=0,
        through_sequence=12, emit_count=False,
    )
    raw_cost = DEFAULT_TOKEN_COUNTER.count_payload(
        strip_packing_hints([*history, {"role": "user", "content": "当前问题"}]), tools,
    )
    full_messages = model.prepare_mandatory_request(
        content="当前问题", history=history, branch_state=None,
    )
    full_cost = DEFAULT_TOKEN_COUNTER.count_payload(
        strip_packing_hints(full_messages), tools,
    )

    # Pick H so the raw prefix is under the old compaction-target exit while the
    # complete request (system prompt included) still exceeds H.
    target_input = int(raw_cost * 2.2)
    low, high = 2_048, 2_000_000
    while low < high:
        middle = (low + high) // 2
        if effective_input_budget(profile_for(middle)).input_limit >= target_input:
            high = middle
        else:
            low = middle + 1
    window = hot_window(profile_for(low))
    assert raw_cost <= window.compact_target
    assert full_cost > window.input_limit

    try:
        await runtime.turn_worker.primary._archive_history_before_generation(
            turn, scope={"owner_id": "local-user", "project_id": None},
            window=window, provider=runtime.memory_context,
            history_through=12, goal_context=None,
            user_content="当前问题", human_mode=False,
        )
    except ContinuationContextOverflow:
        # After the batch, the rebuilt request may still not fit; that is an
        # explicit failure, but work must have been attempted first.
        pass

    state = archiver.status(thread.id, "local-user")
    assert state["archived_through_seq"] > 0, (
        "the complete request exceeded the budget, so a foreground archive must "
        "have run even though the raw prefix was under the compaction target"
    )


@pytest.mark.asyncio
async def test_preflight_measurement_ignores_local_packing_hints(tmp_path) -> None:
    """The pre-check must count what the packer sends, not local hints."""
    from app.model_gateway import ModelProfile
    from app.token_budget import (
        DEFAULT_TOKEN_COUNTER, effective_input_budget, hot_window, strip_packing_hints,
    )

    gateway = RecordingGateway()
    runtime, model = _runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("提示不计费", owner_id="local-user")
    _seed_completed_turns(runtime, thread.id, 6, size=120)
    archiver = ConversationArchiver(runtime.db, runtime.memory_store, _small_summary)
    runtime.archiver = archiver
    accepted = runtime.conversation.accept_turn(
        thread.id, "hint-next", "当前问题", owner_id="local-user",
    )
    turn = runtime.conversation.turn(accepted.turn_id, "local-user")
    tools = model.mandatory_tools()

    def profile_for(context_window: int) -> ModelProfile:
        return ModelProfile(
            base_url="https://api.deepseek.com", model="m", api_key_env="K",
            context_window=context_window, max_output_tokens=1_024,
        )

    probe_window = hot_window(profile_for(500_000))
    history = runtime.turn_worker.primary._history(
        thread.id, turn.id, probe_window, archived_through=0,
        through_sequence=12, emit_count=False,
    )
    messages = model.prepare_mandatory_request(
        content="当前问题", history=history, branch_state=None,
    )
    hinted_cost = DEFAULT_TOKEN_COUNTER.count_payload(messages, tools)
    stripped_cost = DEFAULT_TOKEN_COUNTER.count_payload(strip_packing_hints(messages), tools)
    assert hinted_cost > stripped_cost, "the regression needs hints to be material"

    low, high = 2_048, 2_000_000
    target = stripped_cost + max((hinted_cost - stripped_cost) // 2, 1)
    while low < high:
        middle = (low + high) // 2
        if effective_input_budget(profile_for(middle)).input_limit >= target:
            high = middle
        else:
            low = middle + 1
    window = hot_window(profile_for(low))
    assert stripped_cost <= window.input_limit < hinted_cost

    # The complete request fits once hints are stripped, so no archive may run
    # and no overflow may be reported.
    await runtime.turn_worker.primary._archive_history_before_generation(
        turn, scope={"owner_id": "local-user", "project_id": None},
        window=window, provider=runtime.memory_context,
        history_through=12, goal_context=None,
        user_content="当前问题", human_mode=False,
    )
    assert archiver.status(thread.id, "local-user")["archived_through_seq"] == 0


@pytest.mark.asyncio
async def test_preflight_measurement_includes_branch_instructions(tmp_path) -> None:
    """A required branch instruction must be measured before it is sent."""
    from app.live_model import INHERITED_PLAN_DOCUMENT_INSTRUCTION
    from app.model_gateway import ModelProfile
    from app.memory_v2 import ContinuationContextOverflow
    from app.token_budget import (
        DEFAULT_TOKEN_COUNTER, effective_input_budget, hot_window, strip_packing_hints,
    )

    gateway = RecordingGateway()
    runtime, model = _runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("分支预算", owner_id="local-user")
    _seed_completed_turns(runtime, thread.id, 6, size=120)
    archiver = ConversationArchiver(runtime.db, runtime.memory_store, _small_summary)
    runtime.archiver = archiver
    content = "继续生成完整文档"
    accepted = runtime.conversation.accept_turn(
        thread.id, "branch-next", content, owner_id="local-user",
    )
    turn = runtime.conversation.turn(accepted.turn_id, "local-user")

    def profile_for(context_window: int) -> ModelProfile:
        return ModelProfile(
            base_url="https://api.deepseek.com", model="m", api_key_env="K",
            context_window=context_window, max_output_tokens=1_024,
        )

    probe_window = hot_window(profile_for(500_000))
    history = runtime.turn_worker.primary._history(
        thread.id, turn.id, probe_window, archived_through=0,
        through_sequence=12, emit_count=False,
    )
    import asyncio

    # An answered ask whose original request demanded a plan document adds a
    # required instruction but keeps the same tools.
    branch_state = await model.resolve_branch_state(
        content=content, history=history, cancel_event=asyncio.Event(),
        ask_parent_request="请创建并保存计划文档",
    )
    assert branch_state.inherited_plan_document_intent is True
    assert branch_state.save_existing_plan is False
    tools = model.mandatory_tools(branch_state)

    plain = model.prepare_mandatory_request(
        content=content, history=history, branch_state=None,
    )
    with_branch = model.prepare_mandatory_request(
        content=content, history=history, branch_state=branch_state,
    )
    assert INHERITED_PLAN_DOCUMENT_INSTRUCTION in [m["content"] for m in with_branch]
    plain_cost = DEFAULT_TOKEN_COUNTER.count_payload(strip_packing_hints(plain), tools)
    branch_cost = DEFAULT_TOKEN_COUNTER.count_payload(strip_packing_hints(with_branch), tools)
    assert branch_cost > plain_cost

    low, high = 2_048, 2_000_000
    target = plain_cost + max((branch_cost - plain_cost) // 2, 1)
    while low < high:
        middle = (low + high) // 2
        if effective_input_budget(profile_for(middle)).input_limit >= target:
            high = middle
        else:
            low = middle + 1
    window = hot_window(profile_for(low))
    assert plain_cost <= window.input_limit < branch_cost

    # Before the fix the pre-check measured ``plain_cost`` and returned ready;
    # now the branch instruction is counted, so the loop must archive or fail.
    raised = False
    try:
        await runtime.turn_worker.primary._archive_history_before_generation(
            turn, scope={"owner_id": "local-user", "project_id": None},
            window=window, provider=runtime.memory_context,
            history_through=12, goal_context=None,
            user_content=content, human_mode=False,
            branch_state=branch_state,
        )
    except ContinuationContextOverflow:
        raised = True
    assert raised or archiver.status(thread.id, "local-user")["archived_through_seq"] > 0


@pytest.mark.asyncio
async def test_preflight_uses_branch_tool_selection_without_tools(tmp_path) -> None:
    """A save-existing-plan request is sent without tools; the pre-check must agree."""
    from app.model_gateway import ModelProfile
    from app.token_budget import (
        DEFAULT_TOKEN_COUNTER, effective_input_budget, hot_window, strip_packing_hints,
    )

    gateway = RecordingGateway()
    runtime, model = _runtime(tmp_path, gateway)
    thread = runtime.conversation.create_thread("无工具", owner_id="local-user")
    _seed_completed_turns(runtime, thread.id, 6, size=120)
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE thread_messages SET content=? WHERE thread_id=? AND id LIKE '%-seed-5-assistant'",
            ("# 训练计划\n\n- 第一天：慢跑\n- 第二天：力量", thread.id),
        )
    archiver = ConversationArchiver(runtime.db, runtime.memory_store, _small_summary)
    runtime.archiver = archiver
    accepted = runtime.conversation.accept_turn(
        thread.id, "tool-next", "把上面的计划写进计划文档", owner_id="local-user",
    )
    turn = runtime.conversation.turn(accepted.turn_id, "local-user")

    def profile_for(context_window: int) -> ModelProfile:
        return ModelProfile(
            base_url="https://api.deepseek.com", model="m", api_key_env="K",
            context_window=context_window, max_output_tokens=1_024,
        )

    probe_window = hot_window(profile_for(500_000))
    history = runtime.turn_worker.primary._history(
        thread.id, turn.id, probe_window, archived_through=0,
        through_sequence=12, emit_count=False,
    )
    import asyncio

    branch_state = await model.resolve_branch_state(
        content="把上面的计划写进计划文档", history=history,
        cancel_event=asyncio.Event(),
    )
    assert branch_state.save_existing_plan is True
    assert model.mandatory_tools(branch_state) == []

    messages = strip_packing_hints(model.prepare_mandatory_request(
        content="把上面的计划写进计划文档", history=history, branch_state=branch_state,
    ))
    with_tools = DEFAULT_TOKEN_COUNTER.count_payload(messages, model.mandatory_tools(None))
    without_tools = DEFAULT_TOKEN_COUNTER.count_payload(messages, model.mandatory_tools(branch_state))
    assert with_tools > without_tools

    low, high = 2_048, 2_000_000
    target = without_tools + max((with_tools - without_tools) // 2, 1)
    while low < high:
        middle = (low + high) // 2
        if effective_input_budget(profile_for(middle)).input_limit >= target:
            high = middle
        else:
            low = middle + 1
    window = hot_window(profile_for(low))
    assert without_tools <= window.input_limit < with_tools

    # The request as actually sent has no tools, so it fits and no archive may
    # run; counting the schemas would have forced an unnecessary archive.
    await runtime.turn_worker.primary._archive_history_before_generation(
        turn, scope={"owner_id": "local-user", "project_id": None},
        window=window, provider=runtime.memory_context,
        history_through=12, goal_context=None,
        user_content="把上面的计划写进计划文档", human_mode=False,
        branch_state=branch_state,
    )
    assert archiver.status(thread.id, "local-user")["archived_through_seq"] == 0


async def _small_summary(payload):
    ids = [
        event["message_id"]
        for turn in payload["turns"]
        for event in turn["events"]
        if event.get("message_id")
    ]
    return {
        "synopsis": [{"text": "承接摘要", "source_message_ids": [ids[0]]}],
        "topics": [], "decisions": [], "outcomes": [], "open_loops": [],
        "sensitivity": "normal",
    }


# ---------------------------------------------------------------------------
# T02-T04: snapshot selection, coverage validation and rendering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_continuation_selection_is_query_independent_and_complete(tmp_path) -> None:
    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    provider = runtime.memory_context
    thread = runtime.conversation.create_thread("选择", owner_id="local-user")
    _seed_completed_turns(runtime, thread.id, 4, size=120)
    store = runtime.memory_store
    ids = _message_ids(runtime, thread.id)

    def item(text, sid):
        return {"text": text, "source_message_ids": [sid]}

    with runtime.db.transaction() as connection:
        connection.execute(
            "INSERT INTO conversation_archive_state(owner_id,thread_id,archived_through_seq) VALUES (?,?,?)",
            ("local-user", thread.id, 4),
        )
    store.save_episode(
        owner_id="local-user", thread_id=thread.id, project_id=None,
        start_message_seq=1, end_message_seq=2, source_hash="sha256:" + "a" * 64,
        summary="第一段", synopsis=[item("第一段 synopsis", ids[0])],
    )
    store.save_episode(
        owner_id="local-user", thread_id=thread.id, project_id=None,
        start_message_seq=3, end_message_seq=4, source_hash="sha256:" + "b" * 64,
        summary="第二段", synopsis=[item("第二段 synopsis", ids[4])],
    )

    first = provider.load_continuation("local-user", thread.id, history_through_seq=8)
    second = provider.load_continuation("local-user", thread.id, history_through_seq=8)
    assert first.coverage_hash == second.coverage_hash
    assert [item.id for item in first.episodes] == [item.id for item in second.episodes]
    assert len(first.episodes) == 2
    assert "第一段" in first.rendered and "第二段" in first.rendered
    assert first.rendered.index("第一段") < first.rendered.index("第二段")


@pytest.mark.asyncio
async def test_continuation_missing_episode_reports_coverage_error(tmp_path) -> None:
    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    provider = runtime.memory_context
    thread = runtime.conversation.create_thread("缺口", owner_id="local-user")
    _seed_completed_turns(runtime, thread.id, 3, size=120)
    store = runtime.memory_store
    ids = _message_ids(runtime, thread.id)

    with runtime.db.transaction() as connection:
        connection.execute(
            "INSERT INTO conversation_archive_state(owner_id,thread_id,archived_through_seq) VALUES (?,?,?)",
            ("local-user", thread.id, 4),
        )
    store.save_episode(
        owner_id="local-user", thread_id=thread.id, project_id=None,
        start_message_seq=1, end_message_seq=2, source_hash="sha256:" + "a" * 64,
        summary="只覆盖前半", synopsis=[{"text": "只覆盖前半", "source_message_ids": [ids[0]]}],
    )
    with pytest.raises(ArchiveCoverageMissing):
        provider.load_continuation("local-user", thread.id, history_through_seq=6)


@pytest.mark.asyncio
async def test_empty_continuation_does_not_invent_a_summary(tmp_path) -> None:
    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    provider = runtime.memory_context
    thread = runtime.conversation.create_thread("空", owner_id="local-user")
    _seed_completed_turns(runtime, thread.id, 1, size=60)
    snapshot = provider.load_continuation("local-user", thread.id, history_through_seq=2)
    assert snapshot.archived_through_seq == 0
    assert snapshot.rendered == ""
    assert snapshot.has_summary is False


def _seed_continuation(runtime, provider, *, owner="local-user"):
    thread = runtime.conversation.create_thread("承接", owner_id=owner)
    _seed_completed_turns(runtime, thread.id, 2, size=120)
    ids = _message_ids(runtime, thread.id)
    with runtime.db.transaction() as connection:
        connection.execute(
            "INSERT INTO conversation_archive_state(owner_id,thread_id,archived_through_seq) VALUES (?,?,?)",
            (owner, thread.id, 2),
        )
    episode = runtime.memory_store.save_episode(
        owner_id=owner, thread_id=thread.id, project_id=None,
        start_message_seq=1, end_message_seq=2, source_hash="sha256:" + "d" * 64,
        summary="pinned synopsis",
        synopsis=[{"text": "pinned synopsis", "source_message_ids": [ids[0]]}],
    )
    return thread, episode


def _select_with_continuation(provider, thread, owner="local-user", invocation="invoke-1"):
    return provider.select(MemoryContextRequest(
        owner, thread.id, None, "无关问题",
        purpose="route_and_respond", model_invocation_id=invocation,
        parent_type="turn", parent_id=thread.id,
        history_through_seq=4, include_continuation=True, episode_token_budget=0,
    ))


@pytest.mark.asyncio
async def test_pin_round_trips_continuation_and_registers_episode(tmp_path) -> None:
    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    provider = runtime.memory_context
    thread, episode = _seed_continuation(runtime, provider)

    first = _select_with_continuation(provider, thread)
    assert first.continuation_rendered and "pinned synopsis" in first.continuation_rendered
    assert first.continuation_through_seq == 2
    assert first.continuation_episode_versions == (
        (episode.id, episode.version, 1, 2),
    )

    with runtime.db.connection() as connection:
        items = connection.execute(
            "SELECT source_type,source_id FROM memory_context_pin_items WHERE pin_invocation_id=?",
            ("invoke-1",),
        ).fetchall()
    assert ("episode", episode.id) in [(row["source_type"], row["source_id"]) for row in items]

    replay = _select_with_continuation(provider, thread)
    assert replay.trace.get("retrieval_mode") == "pin_hit"
    assert replay.continuation_rendered == first.continuation_rendered
    assert replay.continuation_hash == first.continuation_hash


@pytest.mark.asyncio
async def test_episode_delete_invalidates_the_continuation_pin(tmp_path) -> None:
    from app.memory_v2 import MemoryConflict

    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    provider = runtime.memory_context
    thread, episode = _seed_continuation(runtime, provider)
    _select_with_continuation(provider, thread)

    runtime.memory_store.delete_episode(
        episode.id, "local-user", episode.version, "delete-episode",
    )
    with pytest.raises(MemoryConflict):
        _select_with_continuation(provider, thread)


@pytest.mark.asyncio
async def test_pin_reuse_cannot_be_bound_to_a_different_history_bound(tmp_path) -> None:
    from app.memory_v2 import MemoryConflict

    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    provider = runtime.memory_context
    thread, _episode = _seed_continuation(runtime, provider)
    _select_with_continuation(provider, thread)
    with pytest.raises(MemoryConflict):
        provider.select(MemoryContextRequest(
            "local-user", thread.id, None, "无关问题",
            purpose="route_and_respond", model_invocation_id="invoke-1",
            parent_type="turn", parent_id=thread.id,
            history_through_seq=3, include_continuation=True, episode_token_budget=0,
        ))


@pytest.mark.asyncio
async def test_pin_payload_envelope_is_decoded_separately_from_optional_text(tmp_path) -> None:
    from app.memory_v2 import _decode_pin_payload, _encode_pin_payload

    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    provider = runtime.memory_context
    thread, _episode = _seed_continuation(runtime, provider)
    bundle = _select_with_continuation(provider, thread)

    rendered, continuation, through, versions, coverage_hash = _decode_pin_payload(
        _encode_pin_payload(bundle)
    )
    assert rendered == bundle.rendered
    assert continuation == bundle.continuation_rendered
    assert through == bundle.continuation_through_seq
    assert versions == bundle.continuation_episode_versions
    assert coverage_hash == bundle.continuation_hash
    # A legacy payload has no envelope; it is read back as the optional body,
    # never parsed as JSON and never injected as envelope text.
    legacy = _decode_pin_payload("plain legacy memory body")
    assert legacy[0] == "plain legacy memory body"
    assert legacy[1] == "" and legacy[2] == 0 and legacy[3] == ()


@pytest.mark.asyncio
async def test_scope_isolation_other_thread_episode_cannot_fill_gap(tmp_path) -> None:
    gateway = RecordingGateway()
    runtime, _model = _runtime(tmp_path, gateway)
    provider = runtime.memory_context
    thread = runtime.conversation.create_thread("A", owner_id="local-user")
    other = runtime.conversation.create_thread("B", owner_id="local-user")
    _seed_completed_turns(runtime, thread.id, 2, size=120)
    _seed_completed_turns(runtime, other.id, 2, size=120)
    store = runtime.memory_store
    other_ids = _message_ids(runtime, other.id)

    with runtime.db.transaction() as connection:
        connection.execute(
            "INSERT INTO conversation_archive_state(owner_id,thread_id,archived_through_seq) VALUES (?,?,?)",
            ("local-user", thread.id, 2),
        )
    store.save_episode(
        owner_id="local-user", thread_id=other.id, project_id=None,
        start_message_seq=1, end_message_seq=2, source_hash="sha256:" + "c" * 64,
        summary="别的线程", synopsis=[{"text": "别的线程", "source_message_ids": [other_ids[0]]}],
    )
    with pytest.raises(ArchiveCoverageMissing):
        provider.load_continuation("local-user", thread.id, history_through_seq=4)
