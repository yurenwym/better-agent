from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from test_runtime import make_runtime


class MemoryConversationGateway:
    supports_intent_classification = True

    def __init__(self) -> None:
        self.conversation_requests = []

    async def complete(self, request, **kwargs):
        if request.purpose == "classify_memory_request":
            return SimpleNamespace(message=json.dumps({
                "remember": True,
                "kind": "preference",
                "scope_type": "user",
                "scope_id": "",
                "content": "回答时先给结论",
            }, ensure_ascii=False), tool_calls=[])
        if request.purpose == "classify_research_request":
            return SimpleNamespace(
                message=json.dumps({"start_research": False, "topic": ""}),
                tool_calls=[],
            )
        if request.purpose == "route_and_respond":
            self.conversation_requests.append(request)
            response = (
                '{"v":1,"policy":"answer","content_shape":"text",'
                '"reason_code":"content_only"}\n结论：可以，下面说明原因。'
            )
            kwargs["on_text_delta"](response)
            return SimpleNamespace(message=response, tool_calls=[], finish_reason="stop")
        raise AssertionError(f"unexpected model purpose: {request.purpose}")


@pytest.mark.asyncio
async def test_explicit_memory_is_used_and_audited_in_a_later_isolated_conversation(tmp_path) -> None:
    from app.live_model import LiveConversationModel
    from app.memory_v2 import MemoryContextProvider, MemoryStore

    gateway = MemoryConversationGateway()
    model = LiveConversationModel(gateway)
    runtime = make_runtime(tmp_path, model)
    runtime.memory_store = MemoryStore(runtime.db, tmp_path / "memory-v2")
    runtime.memory_context = MemoryContextProvider(runtime.db)
    model.memory_store = runtime.memory_store

    alice_thread = runtime.conversation.create_thread("Alice", owner_id="alice")
    first = runtime.conversation.accept_turn(
        alice_thread.id,
        "alice-remember",
        "请记住，以后回答先给结论",
        owner_id="alice",
    )
    assert await runtime.turn_worker.run_once() is True
    assert runtime.conversation.turn(first.turn_id, "alice").status == "COMPLETED"

    entries = runtime.memory_store.list_entries("alice", "ACTIVE")
    assert len(entries) == 1
    assert entries[0].content == "回答时先给结论"
    assert entries[0].evidence_state == "VERIFIED"
    assert len(entries[0].evidence) == 1
    assert entries[0].evidence[0].source_type == "thread_message"
    assert entries[0].evidence[0].excerpt == "请记住，以后回答先给结论"

    second = runtime.conversation.accept_turn(
        alice_thread.id,
        "alice-follow-up",
        "请先给结论，再解释上下文压缩",
        owner_id="alice",
    )
    assert await runtime.turn_worker.run_once() is True
    assert runtime.conversation.turn(second.turn_id, "alice").status == "COMPLETED"
    alice_request = gateway.conversation_requests[-1]
    assert any(
        "回答时先给结论" in message["content"]
        for message in alice_request.messages
        if message["role"] == "system"
    )
    assert runtime.conversation.messages(alice_thread.id, "alice")[-1].content.startswith("结论：")
    applied = [
        event for event in runtime.conversation.events.list(alice_thread.id)
        if event.type == "memory.context_applied" and event.turn_id == second.turn_id
    ]
    assert len(applied) == 1
    assert applied[0].data["revision_ids"] == [entries[0].revision_id]

    bob_thread = runtime.conversation.create_thread("Bob", owner_id="bob")
    bob_turn = runtime.conversation.accept_turn(
        bob_thread.id,
        "bob-follow-up",
        "解释一下为什么需要上下文压缩",
        owner_id="bob",
    )
    assert await runtime.turn_worker.run_once() is True
    assert runtime.conversation.turn(bob_turn.turn_id, "bob").status == "COMPLETED"
    bob_request = gateway.conversation_requests[-1]
    assert all("回答时先给结论" not in message["content"] for message in bob_request.messages)
    assert runtime.memory_store.list_entries("bob") == []


@pytest.mark.asyncio
async def test_reflection_proposal_approval_and_later_conversation_form_one_audited_flow(tmp_path) -> None:
    from app.memory_v2 import MemoryContextProvider, MemoryStore
    from app.runtime import MockModelGateway, ModelDecision

    class ReflectionModel(MockModelGateway):
        async def reflect(self, goal, plan, run_id, evidence_catalog=None):
            assert evidence_catalog
            return [{
                "kind": "preference",
                "scope": "global",
                "content": "回答时先给结论",
                "confidence": 0.9,
                "evidence_event_ids": [evidence_catalog[0]["ref"]],
            }]

    class ConversationModel:
        def __init__(self) -> None:
            self.requests = []

        async def route_and_respond(self, *, history, on_text_delta, on_memory_context_applied=None, **kwargs):
            self.requests.append(history)
            if on_memory_context_applied is not None:
                on_memory_context_applied()
            on_text_delta('{"v":1,"policy":"answer","content_shape":"text","reason_code":"content_only"}\n结论：已使用偏好。')
            return None

    runtime = make_runtime(
        tmp_path,
        ReflectionModel(
            plan_steps=[{"id": "step-1", "title": "完成任务"}],
            decisions=[ModelDecision.complete("完成")],
        ),
    )
    runtime.memory_store = MemoryStore(runtime.db, tmp_path / "memory-v2")
    runtime.memory_context = MemoryContextProvider(runtime.db)
    conversation_model = ConversationModel()
    runtime.conversation.route_model = conversation_model

    evidence_thread = runtime.conversation.create_thread("偏好依据", owner_id="alice")
    source = runtime.conversation.accept_turn(
        evidence_thread.id, "preference-source", "以后回答时先给结论", owner_id="alice",
    )
    assert await runtime.turn_worker.run_once() is True

    run = await runtime.create_goal("完成示例任务", "产生可复用的回答偏好")
    runtime._set_run_fields(run.id, source_turn_id=source.turn_id)
    await runtime.handle_message(run.id, "完成任务")
    await runtime.approve_plan(run.id, 1)

    proposals = runtime.memory_store.list_proposals("alice", "PENDING")
    assert len(proposals) == 1
    source_message = next(
        message for message in runtime.conversation.messages(evidence_thread.id, "alice")
        if message.turn_id == source.turn_id and message.role == "user"
    )
    assert proposals[0].evidence[0].source_id == source_message.id
    accepted = runtime.memory_store.decide_proposal(
        proposals[0].id, "alice", True, "approve-preference",
        expected_version=proposals[0].version,
    )
    assert accepted.status == "ACCEPTED"

    later_thread = runtime.conversation.create_thread("后续对话", owner_id="alice")
    later = runtime.conversation.accept_turn(
        later_thread.id, "later-question", "请按记住的回答方式先给结论，解释上下文压缩", owner_id="alice",
    )
    assert await runtime.turn_worker.run_once() is True

    assert any(
        "回答时先给结论" in message["content"]
        for message in conversation_model.requests[-1]
        if message["role"] == "system"
    )
    applied = [
        event for event in runtime.conversation.events.list(later_thread.id)
        if event.type == "memory.context_applied" and event.turn_id == later.turn_id
    ]
    assert len(applied) == 1
    assert applied[0].data["revision_ids"] == [accepted.accepted_revision_id]
