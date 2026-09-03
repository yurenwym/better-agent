from __future__ import annotations

import json

import pytest

from app.db import Database
from app.memory_v2 import MemoryContextRequest, MemoryContextProvider, MemoryStore


def _thread(db, thread_id: str, owner_id: str, project_id: str | None = None) -> None:
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id,title,owner_id,project_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (thread_id, thread_id, owner_id, project_id, "now", "now"),
        )


def test_direct_remember_is_idempotent_versioned_and_project_isolated(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")

    first = store.remember(
        owner_id="alice", kind="constraint", scope_type="project", scope_id="cycling",
        content="膝盖旧伤，训练需避免突然增加负荷", idempotency_key="message-1",
        source_refs=["message-1"],
    )
    repeated = store.remember(
        owner_id="alice", kind="constraint", scope_type="project", scope_id="cycling",
        content="膝盖旧伤，训练需避免突然增加负荷", idempotency_key="message-1",
        source_refs=["message-1"],
    )
    assert repeated.id == first.id
    assert first.revision_no == 1

    edited = store.edit(first.id, "alice", "膝盖旧伤，每周负荷增幅不超过 10%", first.revision_id)
    assert edited.revision_no == 2
    assert [item.revision_no for item in store.revisions(first.id, "alice")] == [1, 2]

    provider = MemoryContextProvider(db)
    _thread(db, "thread-a", "alice", "cycling")
    _thread(db, "thread-b", "alice", "travel")
    _thread(db, "thread-c", "bob", "cycling")
    same_project = provider.select(MemoryContextRequest("alice", "thread-a", "cycling", "训练负荷"))
    other_project = provider.select(MemoryContextRequest("alice", "thread-b", "travel", "训练负荷"))
    other_owner = provider.select(MemoryContextRequest("bob", "thread-c", "cycling", "训练负荷"))
    assert edited.revision_id in same_project.revision_ids
    assert edited.revision_id not in other_project.revision_ids
    assert edited.revision_id not in other_owner.revision_ids


def test_proposal_accept_uses_compare_and_swap_and_is_retry_safe(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    proposal = store.propose(
        owner_id="local-user", operation="ADD", kind="preference", scope_type="user", scope_id="",
        content="喜欢简洁明确的回答", confidence=.88, evidence_refs=["message-a"],
        idempotency_key="proposal-a",
    )
    accepted = store.decide_proposal(proposal.id, "local-user", True, "decision-a")
    repeated = store.decide_proposal(proposal.id, "local-user", True, "decision-a")
    assert accepted.status == "ACCEPTED"
    assert repeated.accepted_revision_id == accepted.accepted_revision_id

    with pytest.raises(ValueError, match="already decided"):
        store.decide_proposal(proposal.id, "local-user", False, "decision-b")


def test_memory_idempotency_keys_are_scoped_to_owner(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")

    alice_entry = store.remember("alice", "fact", "user", "", "Alice fact", "same-key")
    bob_entry = store.remember("bob", "fact", "user", "", "Bob fact", "same-key")
    assert alice_entry.id != bob_entry.id
    assert store.remember("alice", "fact", "user", "", "Alice fact", "same-key").id == alice_entry.id

    alice_proposal = store.propose(
        owner_id="alice", operation="ADD", kind="preference", scope_type="user", scope_id="",
        content="Alice prefers concise answers", confidence=.8, evidence_refs=[], idempotency_key="proposal-key",
    )
    bob_proposal = store.propose(
        owner_id="bob", operation="ADD", kind="preference", scope_type="user", scope_id="",
        content="Bob prefers detailed answers", confidence=.8, evidence_refs=[], idempotency_key="proposal-key",
    )
    assert alice_proposal.id != bob_proposal.id
    assert store.propose(
        owner_id="alice", operation="ADD", kind="preference", scope_type="user", scope_id="",
        content="different payload is ignored on an idempotent retry", confidence=.8, evidence_refs=[], idempotency_key="proposal-key",
    ).id == alice_proposal.id

    alice_accepted = store.decide_proposal(alice_proposal.id, "alice", True, "decision-key")
    bob_accepted = store.decide_proposal(bob_proposal.id, "bob", True, "decision-key")
    assert alice_accepted.accepted_revision_id != bob_accepted.accepted_revision_id


def test_duplicate_add_proposal_is_reported_as_memory_conflict(tmp_path) -> None:
    from app.memory_v2 import MemoryConflict

    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    store.remember("u", "fact", "user", "", "same fact", "direct")
    with pytest.raises(MemoryConflict, match="same content"):
        store.propose(
            owner_id="u", operation="ADD", kind="fact", scope_type="user", scope_id="",
            content="same fact", confidence=.8, evidence_refs=[], idempotency_key="proposal",
        )


def test_remember_rejects_invalid_sensitivity_and_importance(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    with pytest.raises(ValueError, match="sensitivity"):
        store.remember("u", "fact", "user", "", "x", "s1", sensitivity="unknown")
    with pytest.raises(ValueError, match="importance"):
        store.remember("u", "fact", "user", "", "y", "s2", importance=2)


def test_markdown_projection_isolated_per_owner_and_removes_archived_entries(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    alice = store.remember("alice", "preference", "user", "", "Alice private preference", "a")
    bob = store.remember("bob", "preference", "user", "", "Bob private preference", "b")
    alice_path = tmp_path / "memory" / "users" / __import__("hashlib").sha256(b"alice").hexdigest()[:24] / "USER.md"
    bob_path = tmp_path / "memory" / "users" / __import__("hashlib").sha256(b"bob").hexdigest()[:24] / "USER.md"
    assert "Alice private preference" in alice_path.read_text(encoding="utf-8")
    assert "Bob private preference" not in alice_path.read_text(encoding="utf-8")
    assert "Bob private preference" in bob_path.read_text(encoding="utf-8")
    store.set_status(alice.id, "alice", "ARCHIVED")
    assert "Alice private preference" not in alice_path.read_text(encoding="utf-8")


def test_context_budget_never_slices_a_memory_entry_and_pin_is_reused(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    first = store.remember("u", "fact", "user", "", "A" * 80, "one")
    second = store.remember("u", "fact", "user", "", "B" * 80, "two")
    provider = MemoryContextProvider(db)
    _thread(db, "t", "u")
    request = MemoryContextRequest("u", "t", None, "A", semantic_token_budget=25, model_invocation_id="invoke-1")
    bundle = provider.select(request)
    repeated = provider.select(request)
    assert bundle == repeated
    assert set(bundle.revision_ids).issubset({first.revision_id, second.revision_id})
    assert "A" * 80 in bundle.rendered or "B" * 80 in bundle.rendered or bundle.rendered == ""
    assert bundle.rendered.count("A") in {0, 80}
    assert bundle.rendered.count("B") in {0, 80}


def test_episode_scope_and_secret_redaction(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id,title,owner_id,project_id,created_at,updated_at) VALUES ('t1','one','u',NULL,'n','n')"
        )
        connection.execute(
            "INSERT INTO threads(id,title,owner_id,project_id,created_at,updated_at) VALUES ('t2','two','u',NULL,'n','n')"
        )
    episode = store.save_episode(
        owner_id="u", thread_id="t1", project_id=None, start_message_seq=1, end_message_seq=2,
        source_hash="sha256:x", summary="用户 token=secret-value，正在制定计划", sensitivity="sensitive",
    )
    assert "secret-value" not in episode.summary
    provider = MemoryContextProvider(db)
    assert episode.id in provider.select(MemoryContextRequest("u", "t1", None, "计划")).episode_ids
    assert episode.id not in provider.select(MemoryContextRequest("u", "t2", None, "计划")).episode_ids


def test_save_episode_rejects_invalid_ranges_sensitivity_and_source_ids(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    _thread(db, "t", "u")
    invalid = dict(
        owner_id="u", thread_id="t", project_id=None, source_hash="sha256:x", summary="summary",
    )
    with pytest.raises(ValueError, match="sensitivity"):
        store.save_episode(**invalid, start_message_seq=0, end_message_seq=0, sensitivity="unknown")
    with pytest.raises(ValueError, match="end sequence"):
        store.save_episode(**invalid, start_message_seq=3, end_message_seq=2)
    with pytest.raises(ValueError, match="source messages"):
        store.save_episode(**invalid, start_message_seq=1, end_message_seq=2, source_message_ids=["missing"])


def test_context_pin_rejects_binding_changes_and_is_invalidated_on_episode_delete(tmp_path) -> None:
    from app.memory_v2 import MemoryConflict

    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    _thread(db, "t", "alice")
    episode = store.save_episode(
        owner_id="alice", thread_id="t", project_id=None, start_message_seq=1, end_message_seq=2,
        source_hash="sha256:episode", summary="conversation summary",
    )
    provider = MemoryContextProvider(db)
    request = MemoryContextRequest("alice", "t", None, "summary", model_invocation_id="invoke")
    assert provider.select(request).episode_ids == (episode.id,)
    with pytest.raises(MemoryConflict, match="different context"):
        provider.select(MemoryContextRequest("alice", "t", None, "different", model_invocation_id="invoke"))
    store.delete_episode(episode.id, "alice")
    with pytest.raises(MemoryConflict, match="CONTEXT_INVALIDATED"):
        provider.select(request)
    with db.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_context_pin_payloads WHERE pin_invocation_id='invoke'"
        ).fetchone()[0] == 0


def test_context_pin_expiry_and_cross_owner_reuse_are_rejected(tmp_path) -> None:
    from app.memory_v2 import MemoryConflict

    db = Database(tmp_path / "agent.db")
    _thread(db, "alice-thread", "alice")
    _thread(db, "bob-thread", "bob")
    provider = MemoryContextProvider(db, pin_ttl_seconds=-1)
    request = MemoryContextRequest("alice", "alice-thread", None, "query", model_invocation_id="shared-invocation")
    provider.select(request)
    with pytest.raises(MemoryConflict, match="CONTEXT_PIN_EXPIRED"):
        provider.select(request)
    with pytest.raises(MemoryConflict, match="different context"):
        provider.select(MemoryContextRequest("bob", "bob-thread", None, "query", model_invocation_id="shared-invocation"))


def test_thread_delete_scrubs_episode_and_pin_payload(tmp_path) -> None:
    from app.conversation import ConversationService

    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    conversation = ConversationService(db)
    thread = conversation.create_thread(owner_id="alice")
    episode = store.save_episode(
        owner_id="alice", thread_id=thread.id, project_id=None, start_message_seq=1, end_message_seq=2,
        source_hash="sha256:delete", summary="must disappear",
    )
    request = MemoryContextRequest("alice", thread.id, None, "disappear", model_invocation_id="delete-pin")
    MemoryContextProvider(db).select(request)
    conversation.delete_thread(thread.id, "alice")
    with db.connection() as connection:
        row = connection.execute("SELECT status,summary FROM memory_episodes WHERE id=?", (episode.id,)).fetchone()
        assert row["status"] == "DELETED" and row["summary"] == ""
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_context_pin_payloads WHERE pin_invocation_id='delete-pin'"
        ).fetchone()[0] == 0


def test_schema_constraints_and_migration_checksum(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    with db.connection() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"memory_entries", "memory_episodes", "memory_context_pins", "research_jobs", "research_schedules"} <= tables
        migration = connection.execute("SELECT version,checksum FROM schema_migrations").fetchone()
        assert migration[0] == 1 and len(migration[1]) == 64
        with pytest.raises(Exception):
            connection.execute(
                "INSERT INTO memory_entries(id,owner_id,kind,scope_type,scope_id,status,canonical_fingerprint,created_at,updated_at) "
                "VALUES ('bad','u','habit','user','','ACTIVE','x','n','n')"
            )
