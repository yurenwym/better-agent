"""Background archive entrypoint and conversation reference call boundaries."""
from __future__ import annotations

import json

import pytest

from app.memory_archive import ConversationArchiver, LiveEpisodeSummarizer
from app.memory_reference import resolve
from app.memory_v2 import MemoryStore
from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
from snapshot_entrypoint_helpers import CommittedSnapshotTransport
from test_memory_archive import _completed_thread
from test_snapshot_gateway import _answer, _configured_control_plane


@pytest.mark.asyncio
async def test_t18_clarification_uses_separate_ask_binding(tmp_path, monkeypatch):
    from dataclasses import replace
    from app.live_model import LiveConversationModel
    from test_snapshot_flow import _drive

    db, _, _ = _configured_control_plane(tmp_path, monkeypatch)
    def answer(identity):
        call = {"id":identity,"function":{"name":"ask_user","arguments":json.dumps({"questions":[{
            "id":"context","header":"Details","question":"What is the target?","options":[],
            "multi_select":False,"allow_free_text":True}]})}}
        return replace(_answer(""), tool_calls=[call])
    observer = CommittedSnapshotTransport(db, "local-user", "CS-CA-01", [answer("draft"), answer("final")])
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
    result = await _drive(LiveConversationModel(gateway), "Help")
    assert result.call_id == "final"
    with db.connection() as connection:
        rows = connection.execute("SELECT role,purpose,context_snapshot_id FROM model_invocations ORDER BY created_at,id").fetchall()
    assert [row["role"] for row in rows] == ["conversation", "ask"]
    assert rows[1]["purpose"] == "generate_clarification"
    assert observer.send_count == len({row["context_snapshot_id"] for row in rows}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["plan", "existing_plan", "research", "memory_repair", "no_history", "dependent", "short_circuit"])
async def test_t18_auxiliary_branches_have_independent_bindings(tmp_path, monkeypatch, branch):
    import asyncio
    from app.live_model import LiveConversationModel
    from app.memory_archive import ArchiveUnavailable

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, owner_id="aux-owner")
    replies = {
        "plan": ['{"plan_document_request":false}'],
        "existing_plan": ['{"save_existing_plan":false}'],
        "research": ['{"start_research":false}'],
        "memory_repair": ['{"remember":false}', '{"remember":true,"kind":"preference","scope_type":"user","scope_id":"","content":"简洁回答"}'],
        "no_history": ['{"independent":true}', "Answer"],
        "dependent": ['{"independent":false}'], "short_circuit": [],
    }
    observer = CommittedSnapshotTransport(db, "aux-owner", "CS-CA-01", [_answer(text) for text in replies[branch]])
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
    model = LiveConversationModel(gateway)
    token = gateway.set_call_context(ModelCallContext("conversation", "aux", owner_id="aux-owner", runtime_bundle_id=bundle.id))
    cancel = asyncio.Event()
    try:
        if branch == "plan":
            assert not await model._classify_explicit_plan_document_request("请提供建议", [], cancel)
        elif branch == "existing_plan":
            assert not await model._classify_existing_plan_save("请提供建议", [
                {"role":"assistant", "content":"# 学习计划\n- 每天阅读\n- 每周复习"}], cancel)
        elif branch == "research":
            assert (await model._classify_explicit_research_request("普通问题", cancel))[0] is False
        elif branch == "memory_repair":
            assert (await model._classify_explicit_remember("请记住我喜欢简洁回答", cancel))["remember"] is True
        elif branch == "short_circuit":
            assert await model._classify_explicit_remember("你好", cancel) is None
            assert not await model._classify_existing_plan_save("你好", [], cancel)
        else:
            async def answer():
                return await model.answer_without_history(content="普通问题", on_text_delta=lambda _: None, cancel_event=cancel)
            if branch == "dependent":
                with pytest.raises(ArchiveUnavailable):
                    await answer()
            else:
                await answer()
    finally:
        gateway.reset_call_context(token)
    assert observer.send_count == len(replies[branch])
    assert len({item["invocation_id"] for item in observer.observations}) == observer.send_count
    assert gateway.current_call_context() is None


@pytest.mark.asyncio
async def test_archive_worker_summary_uses_claim_owner_and_committed_snapshot(tmp_path, monkeypatch):
    db, thread = _completed_thread(tmp_path, owner_id="archive-owner")
    db, _, _ = _configured_control_plane(tmp_path, monkeypatch, database=db, owner_id="archive-owner")
    summary = {
        "synopsis": [{"text": "用户提出了一项要求。", "source_message_ids": ["fault-user"]}],
        "topics": [], "decisions": [], "outcomes": [], "open_loops": [], "sensitivity": "normal",
    }
    observer = CommittedSnapshotTransport(db, "archive-owner", "CS-MA-01", _answer(json.dumps(summary, ensure_ascii=False)))
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
    archiver = ConversationArchiver(db, MemoryStore(db, tmp_path / "memory"), LiveEpisodeSummarizer(gateway), keep_messages=0)
    job_id = archiver.enqueue(thread.id, source_turn_id="fault-turn")
    result = await archiver.process(archiver.claim(job_id))

    assert result is not None
    assert len(observer.observations) == 1
    assert observer.observations[0]["owner_id"] == "archive-owner"
    assert observer.observations[0]["snapshot_id"]
    assert gateway.current_call_context() is None


@pytest.mark.asyncio
async def test_reference_resolution_freezes_the_final_candidate_list(tmp_path, monkeypatch):
    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, owner_id="memory-owner")
    reply = '{"动作":"补全后检索","目标实体":["SVC03"],"依据消息编号":["m2"]}'
    observer = CommittedSnapshotTransport(db, "memory-owner", "CS-MR-01", _answer(reply))
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
    history = [
        {"id": "m1", "role": "user", "content": "SVC02 每周维护。"},
        {"id": "m2", "role": "user", "content": "还有 SVC03。"},
    ]
    context = ModelCallContext(role="coordinator", purpose="resolve_memory_reference", owner_id="memory-owner",
                               runtime_bundle_id=bundle.id)

    result = await resolve("后者呢？", history, mode="hybrid", gateway=gateway, context=context)

    assert result.action == "resolved"
    assert result.entities == ("SVC03",)
    assert len(observer.observations) == 1
    assert observer.observations[0]["owner_id"] == "memory-owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
async def test_t17_reference_interruption_does_not_leak_to_next_owner(tmp_path, monkeypatch, interruption):
    import asyncio
    from app.model_input_snapshot_store import ModelInputSnapshotStore

    db, bundle_a, _ = _configured_control_plane(tmp_path, monkeypatch, owner_id="owner-a")
    _, bundle_b, _ = _configured_control_plane(tmp_path, monkeypatch, database=db, owner_id="owner-b")
    history = [{"id": "m1", "role": "user", "content": "SVC02。"},
               {"id": "m2", "role": "user", "content": "还有 SVC03。"}]
    first = CommittedSnapshotTransport(db, "owner-a", "CS-MR-01", _answer("unused"))
    second = CommittedSnapshotTransport(db, "owner-b", "CS-MR-01", _answer(
        '{"动作":"补全后检索","目标实体":["SVC03"],"依据消息编号":["m2"]}'
    ))
    entered = asyncio.Event()

    async def execute(profile, request, **kwargs):
        if not entered.is_set():
            await first(profile, request, **kwargs)
            entered.set()
            await asyncio.Event().wait()
        return await second(profile, request, **kwargs)

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    task = asyncio.create_task(resolve("后者呢？", history, mode="hybrid", gateway=gateway,
        context=ModelCallContext("coordinator", "resolve_memory_reference", owner_id="owner-a", runtime_bundle_id=bundle_a.id),
        timeout=.2 if interruption == "timeout" else 5))
    await asyncio.wait_for(entered.wait(), 5)
    if interruption == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).action == "clarify"
    frozen = ModelInputSnapshotStore(db).load("owner-a", first.observations[0]["snapshot_id"])
    assert gateway.current_call_context() is None
    result = await resolve("后者呢？", history, mode="hybrid", gateway=gateway,
        context=ModelCallContext("coordinator", "resolve_memory_reference", owner_id="owner-b", runtime_bundle_id=bundle_b.id))
    assert result.entities == ("SVC03",)
    assert first.send_count == second.send_count == 1
    assert gateway.current_call_context() is None
    assert ModelInputSnapshotStore(db).load("owner-a", frozen.id).content_json == frozen.content_json
    assert first.observations[0]["invocation_id"] != second.observations[0]["invocation_id"]


@pytest.mark.asyncio
async def test_t16_archive_commit_failure_retries_the_same_selected_messages(tmp_path, monkeypatch):
    from app.model_input_snapshot_store import ModelInputSnapshotStore
    from test_memory_archive import valid_summary

    db, thread = _completed_thread(tmp_path, owner_id="archive-owner")
    _configured_control_plane(tmp_path, monkeypatch, database=db, owner_id="archive-owner")
    observer = CommittedSnapshotTransport(db, "archive-owner", "CS-MA-01", None)
    store = MemoryStore(db, tmp_path / "memory")
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
    archiver = ConversationArchiver(db, store, LiveEpisodeSummarizer(gateway), keep_messages=0, max_attempts=1)
    job_id = archiver.enqueue(thread.id, source_turn_id="fault-turn")
    claim = archiver.claim(job_id)
    transcript = archiver.transcripts.build(thread.id, expected_owner_id="archive-owner",
        after_sequence=claim.start_sequence - 1, through_sequence=claim.end_sequence)
    payload = archiver._input_payload(transcript)
    observer.response = _answer(json.dumps(await valid_summary(payload)))
    original = store.save_episode

    def fail_after_save(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("commit failed")

    monkeypatch.setattr(store, "save_episode", fail_after_save)
    assert await archiver.process(claim) is None
    assert gateway.current_call_context() is None
    first = ModelInputSnapshotStore(db).load("archive-owner", observer.observations[0]["snapshot_id"])
    assert json.loads(first.to_request().messages[-1]["content"]) == payload
    state = archiver.status(thread.id, "archive-owner")
    assert state["archived_through_seq"] == 0 and state["jobs"][0]["status"] == "DEAD_LETTER"
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 0
    job = state["jobs"][0]
    with pytest.raises((PermissionError, ValueError, KeyError)):
        archiver.retry(thread.id, "other-owner", job_id, job["updated_at"])
    archiver.retry(thread.id, "archive-owner", job_id, job["updated_at"])
    monkeypatch.setattr(store, "save_episode", original)
    assert await archiver.process(archiver.claim(job_id)) is not None
    assert archiver.claim(job_id) is None
    assert observer.send_count == 2
    second = ModelInputSnapshotStore(db).load("archive-owner", observer.observations[1]["snapshot_id"])
    assert second.id != first.id and second.to_request().messages == first.to_request().messages
    assert ModelInputSnapshotStore(db).load("archive-owner", first.id).content_json == first.content_json
    with db.connection() as connection:
        rows = connection.execute("SELECT owner_id,thread_id FROM model_invocations").fetchall()
    assert len(rows) == 2 and all(row["owner_id"] == "archive-owner" and row["thread_id"] == thread.id for row in rows)
