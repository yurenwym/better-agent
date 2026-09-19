import asyncio
import json
from types import SimpleNamespace
import pytest
from app.live_model import LiveConversationModel
from app.memory_archive import ArchiveUnavailable


@pytest.mark.asyncio
@pytest.mark.parametrize("independent", [True, False])
async def test_action_fallback_uses_only_snapshot_and_question(independent):
    requests = []
    class Gateway:
        async def complete(self, request, **kwargs):
            requests.append(request)
            assert request.tools == []
            assert "OLD PRIVATE HISTORY" not in str(request.messages)
            if request.purpose == "classify_context_dependency":
                return SimpleNamespace(message=json.dumps({"independent": independent}))
            return SimpleNamespace(message="Use csv.reader.", tool_calls=[])
    model = LiveConversationModel(Gateway())
    chunks = []
    call = model.answer_without_history(content="How do I read this CSV?", action_context="Current CSV action, 15 minutes left",
         history=[{"role":"user","content":"OLD PRIVATE HISTORY"}], on_text_delta=chunks.append, cancel_event=asyncio.Event())
    if independent:
        await call
        assert "当前行动记录" in "".join(chunks)
        assert len(requests) == 2
        assert "15 minutes left" in requests[-1].messages[-1]["content"]
    else:
        with pytest.raises(ArchiveUnavailable):
            await call
        assert len(requests) == 1 and not chunks


@pytest.mark.asyncio
async def test_worker_passes_action_snapshot_when_archive_fails(tmp_path, monkeypatch):
    from test_runtime import make_runtime
    seen = []
    class Model:
        async def route_and_respond(self, **kwargs):
            raise AssertionError("must not use incomplete history")
        async def answer_without_history(self, *, action_context, history, memory_context_content, on_text_delta, **kwargs):
            assert action_context == "CURRENT ACTION ONLY"
            assert history == [] and memory_context_content is None
            seen.append(True)
            on_text_delta('{"v":1,"policy":"answer","content_shape":"text","reason_code":"limited"}\nCurrent action help')
    runtime = make_runtime(tmp_path, Model())
    monkeypatch.setattr(runtime.conversation.goal_context, "load_for_turn", lambda *args: SimpleNamespace(context_text="CURRENT ACTION ONLY"))
    async def unavailable(turn, **kwargs):
        raise ArchiveUnavailable("dead letter")
    monkeypatch.setattr(runtime.turn_worker.primary, "_archive_history_before_generation", unavailable)
    thread = runtime.conversation.create_thread("fallback")
    submission = runtime.conversation.accept_turn(thread.id, "fallback-1", "Read CSV", [])
    await runtime.turn_worker.run_once()
    assert seen == [True]
    assert runtime.conversation.turn(submission.turn_id).status == "COMPLETED"
