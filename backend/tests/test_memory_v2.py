from __future__ import annotations

import json

import pytest

from app.db import Database
from app.memory_v2 import MemoryContextRequest, MemoryContextProvider, MemoryStore


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
    same_project = provider.select(MemoryContextRequest("alice", "thread-a", "cycling", "训练负荷"))
    other_project = provider.select(MemoryContextRequest("alice", "thread-b", "travel", "训练负荷"))
    other_owner = provider.select(MemoryContextRequest("bob", "thread-a", "cycling", "训练负荷"))
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


def test_context_budget_never_slices_a_memory_entry_and_pin_is_reused(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    first = store.remember("u", "fact", "user", "", "A" * 80, "one")
    second = store.remember("u", "fact", "user", "", "B" * 80, "two")
    provider = MemoryContextProvider(db)
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
    episode = store.save_episode(
        owner_id="u", thread_id="t1", project_id=None, start_message_seq=1, end_message_seq=2,
        source_hash="sha256:x", summary="用户 token=secret-value，正在制定计划", sensitivity="sensitive",
    )
    assert "secret-value" not in episode.summary
    provider = MemoryContextProvider(db)
    assert episode.id in provider.select(MemoryContextRequest("u", "t1", None, "计划")).episode_ids
    assert episode.id not in provider.select(MemoryContextRequest("u", "t2", None, "计划")).episode_ids


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
