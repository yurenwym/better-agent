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

