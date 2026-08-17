def test_memory_candidate_confirmation_writes_versioned_markdown_and_applies_event(tmp_path) -> None:
    from app.db import Database
    from app.events import EventStore
    from app.memory import MemoryService

    root = tmp_path / "memory"
    db = Database(tmp_path / "agent.db")
    service = MemoryService(db, EventStore(db), root)

    candidate = service.create_candidate(
        run_id="run-1",
        goal_id="goal-1",
        kind="preference",
        content="Prefer concise plans",
        scope="global",
        confidence=0.8,
        evidence_event_ids=["evt-1"],
    )
    assert service.context_memories(project_id=None, skill_name=None) == []

    confirmed = service.confirm(candidate.id)
    assert confirmed.status == "confirmed"
    assert (root / "preferences.md").read_text(encoding="utf-8") == "Prefer concise plans\n"

    applied = service.apply_confirmed("run-2", "goal-1", project_id=None, skill_name=None)
    assert [memory.id for memory in applied] == [candidate.id]
    assert [event.type for event in service.events.list("run-2")] == ["memory.applied"]


def test_memory_reject_disable_edit_and_rollback_preserve_file_versions(tmp_path) -> None:
    from app.db import Database
    from app.events import EventStore
    from app.memory import MemoryService

    db = Database(tmp_path / "agent.db")
    events = EventStore(db)
    root = tmp_path / "memory"
    service = MemoryService(db, events, root)
    first = service.create_candidate("run-1", "goal-1", "habit", "Morning review", "global", 0.7, [])
    rejected = service.reject(first.id)
    assert rejected.status == "rejected"

    second = service.create_candidate("run-1", "goal-1", "preference", "Use bullets", "global", 0.9, [])
    service.confirm(second.id)
    edited = service.edit(second.id, "Use short bullets")
    assert edited.content == "Use short bullets"
    assert len(service.versions(second.id)) == 2
    service.rollback(second.id, 1)
    assert (root / "preferences.md").read_text(encoding="utf-8") == "Use bullets\n"
    assert service.disable(second.id).status == "disabled"
    assert service.context_memories(None, None) == []
