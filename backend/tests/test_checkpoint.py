def test_checkpoint_persists_runtime_state_and_deduplicates_completed_side_effects(tmp_path) -> None:
    from app.db import Database
    from app.domain import Checkpoint, CheckpointStore

    store = CheckpointStore(Database(tmp_path / "agent.db"))
    checkpoint = store.save(
        Checkpoint(
            run_id="run-1",
            state="EXECUTING",
            plan_version_id="pv-1",
            step_id="step-1",
            completed_steps=["step-0"],
            react_iteration=2,
            remaining_budget={"react_iterations": 3},
            observation="observed",
            artifact_refs=["artifact-1"],
            pending_approvals=["approval-1"],
            applied_memory_versions=["memory-1"],
            last_event_seq=12,
        )
    )
    store.record_completed_tool("run-1", "call-1", {"ok": True})

    loaded = store.latest("run-1")

    assert loaded.id == checkpoint.id
    assert loaded.remaining_budget["react_iterations"] == 3
    assert loaded.last_event_seq == 12
    assert store.completed_tool_result("run-1", "call-1") == {"ok": True}
    assert store.completed_tool_result("run-1", "call-2") is None
import pytest

from test_runtime import make_runtime


@pytest.mark.asyncio
async def test_runtime_checkpoint_records_applied_memory_versions(tmp_path) -> None:
    from app.memory_v2 import MemoryContextProvider, MemoryStore
    from app.runtime import MockModelGateway, ModelDecision

    class ContextModel(MockModelGateway):
        def __init__(self):
            super().__init__(
                plan_steps=[{"id": "step-1", "title": "Observe"}],
                decisions=[ModelDecision.await_outcome("wait")],
            )
            self.contexts = []

        def set_context_snapshot(self, snapshot_hash, text):
            self.contexts.append(text)

    model = ContextModel()
    runtime = make_runtime(tmp_path, model)
    store = MemoryStore(runtime.db, tmp_path / "memory-v2")
    entry = store.remember("local-user", "preference", "user", "", "Use concise plans", "remember-1")
    runtime.memory_store = store
    runtime.memory_context = MemoryContextProvider(runtime.db)
    thread = runtime.conversation.create_thread("Memory", owner_id="local-user")
    source = runtime.conversation.accept_turn(thread.id, "source", "Use memory", owner_id="local-user")
    run = await runtime.create_goal("Memory", "Use concise plans")
    runtime._set_run_fields(run.id, source_turn_id=source.turn_id)
    run = runtime.get_run(run.id)
    await runtime.handle_message(run.id, "Use concise plans")
    await runtime.approve_plan(run.id, 1)

    checkpoint = runtime.checkpoints.latest(run.id)

    assert checkpoint is not None
    assert any("Use concise plans" in context for context in model.contexts)
    assert entry.revision_id in checkpoint.applied_memory_versions
    applied = [event for event in runtime.events.list(run.id) if event.type == "memory.context_applied"]
    assert applied
    assert entry.revision_id in applied[-1].data["revision_ids"]
