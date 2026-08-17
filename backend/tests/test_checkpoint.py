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
    from app.runtime import MockModelGateway, ModelDecision

    runtime = make_runtime(tmp_path, MockModelGateway(
        plan_steps=[{"id": "step-1", "title": "Observe"}],
        decisions=[ModelDecision.await_outcome("wait")],
    ))
    candidate = runtime.memory.create_candidate("memory", "memory", "preference", "Use concise plans", "global", 0.9, [])
    runtime.memory.confirm(candidate.id)
    run = await runtime.create_goal("Memory", "Use memory")
    await runtime.handle_message(run.id, "Use memory")
    await runtime.approve_plan(run.id, 1)

    checkpoint = runtime.checkpoints.latest(run.id)

    assert checkpoint is not None
    assert checkpoint.applied_memory_versions
    assert any(event.type == "memory.applied" for event in runtime.events.list(run.id))
