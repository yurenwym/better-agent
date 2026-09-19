from __future__ import annotations

import json

import pytest

from app.db import Database
from app.memory_v2 import MemoryConflict, MemoryContextRequest, MemoryContextProvider, MemoryStore


def _thread(db, thread_id: str, owner_id: str, project_id: str | None = None) -> None:
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id,title,owner_id,project_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (thread_id, thread_id, owner_id, project_id, "now", "now"),
        )


def test_context_excludes_unrelated_non_pinned_memory(tmp_path):
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    _thread(db, "m1", "u")
    relevant = store.remember("u", "fact", "user", "", "PostgreSQL indexing", "relevant")
    unrelated = store.remember("u", "fact", "user", "", "Gardening calendar", "unrelated")
    pinned = store.remember("u", "preference", "user", "", "Answer concisely", "pinned", pinned=True)
    provider = MemoryContextProvider(db)
    bundle = provider.select(MemoryContextRequest("u", "m1", None, "PostgreSQL"))
    assert set(bundle.revision_ids) == {relevant.revision_id, pinned.revision_id}
    assert unrelated.revision_id not in bundle.revision_ids
    assert bundle.trace["irrelevant_entry_count"] == 1
    assert provider.select(MemoryContextRequest("u", "m1", None, "")).revision_ids == (pinned.revision_id,)


def test_context_renderer_change_rejects_old_pin(tmp_path):
    db = Database(tmp_path / "agent.db")
    _thread(db, "m1", "u")
    request = MemoryContextRequest("u", "m1", None, "topic", model_invocation_id="old-pin")
    old = MemoryContextProvider(db)
    old.renderer_version = "memory-v4"
    old.select(request)
    with pytest.raises(MemoryConflict, match="different context"):
        MemoryContextProvider(db).select(request)


def test_context_chinese_terms_do_not_require_exact_query_phrase(tmp_path):
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    _thread(db, "m1", "u")
    relevant = store.remember("u", "constraint", "user", "", "每周负荷增幅不超过10%", "relevant")
    store.remember("u", "fact", "user", "", "每周阅读两本小说", "unrelated")
    bundle = MemoryContextProvider(db).select(MemoryContextRequest("u", "m1", None, "训练负荷"))
    assert bundle.revision_ids == (relevant.revision_id,)


def test_episode_context_preserves_decisions_open_loops_and_provenance(tmp_path):
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    _thread(db, "m1", "u")
    episode = store.save_episode(owner_id="u", thread_id="m1", project_id=None,
        start_message_seq=1, end_message_seq=2, source_hash="sha256:source-test", summary="用户讨论数据库学习。",
        synopsis=[{"text":"用户讨论数据库学习。", "source_message_ids":["synopsis-source"]}],
        decisions=[{"text":"用户确认每天60分钟。", "source_message_ids":["source-message"]}],
        open_loops=[{"text":"助手建议比较Hash，尚待用户确认。", "source_message_ids":["source-message"]}])
    provider = MemoryContextProvider(db)
    bundle = provider.select(MemoryContextRequest("u", "m1", None, "学习"))
    assert "用户确认每天60分钟" in bundle.rendered
    assert "尚待用户确认" in bundle.rendered
    assert episode.id in bundle.rendered and "source-message" in bundle.rendered
    assert "synopsis-source" in bundle.rendered
    assert provider.renderer_version == "memory-v5"


def test_rendered_context_budget_includes_headings_and_separators(tmp_path):
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    _thread(db, "budget", "u")
    store.remember("u", "fact", "user", "", "short fact", "budget", pinned=True)
    store.save_episode(owner_id="u", thread_id="budget", project_id=None,
        start_message_seq=1, end_message_seq=2, source_hash="sha256:budget", summary="short summary")
    provider = MemoryContextProvider(db)
    for semantic, episodic in ((24, 0), (0, 160), (75, 210), (1000, 1000)):
        bundle = provider.select(MemoryContextRequest("u", "budget", None, "fact",
            semantic_token_budget=semantic, episode_token_budget=episodic))
        assert bundle.token_count <= semantic + episodic
        assert bool(bundle.episode_ids) == ("short summary" in bundle.rendered)


@pytest.mark.parametrize("query", ["art", "the and please", "", "请问一下"])
def test_lexical_retrieval_rejects_substrings_and_stopwords(tmp_path, query):
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    _thread(db, "lexical", "u")
    store.remember("u", "fact", "user", "", "The cartography workshop", "unrelated")
    assert not MemoryContextProvider(db).select(MemoryContextRequest("u", "lexical", None, query)).revision_ids


def _message(db, message_id: str, thread_id: str, owner_id: str, *, role: str = "user", turn_id: str | None = None, project_id: str | None = None, content: str = "evidence") -> dict[str, str]:
    turn_id = turn_id or f"turn-{message_id}"
    with db.transaction() as connection:
        if connection.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone() is None:
            connection.execute(
                "INSERT INTO threads(id,title,owner_id,project_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (thread_id, thread_id, owner_id, project_id, "now", "now"),
            )
        if connection.execute("SELECT 1 FROM turns WHERE id=?", (turn_id,)).fetchone() is None:
            connection.execute(
                "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,'COMPLETED','now','now')",
                (turn_id, thread_id, turn_id),
            )
        seq = connection.execute("SELECT COALESCE(MAX(message_seq),0)+1 FROM thread_messages WHERE thread_id=?", (thread_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,message_seq,created_at) VALUES (?,?,?,?,?,'ready',?,'now')",
            (message_id, thread_id, turn_id, role, content, seq),
        )
    return {"source_type": "thread_message", "source_id": message_id}


def test_direct_remember_is_idempotent_versioned_and_project_isolated(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")

    first = store.remember(
        owner_id="alice", kind="constraint", scope_type="project", scope_id="cycling",
        content="膝盖旧伤，训练需避免突然增加负荷", idempotency_key="message-1",
        source_refs=[],
    )
    repeated = store.remember(
        owner_id="alice", kind="constraint", scope_type="project", scope_id="cycling",
        content="膝盖旧伤，训练需避免突然增加负荷", idempotency_key="message-1",
        source_refs=[],
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
    evidence = _message(db, "message-a", "thread-a", "local-user")
    proposal = store.propose(
        owner_id="local-user", operation="ADD", kind="preference", scope_type="user", scope_id="",
        content="喜欢简洁明确的回答", confidence=.88, evidence_refs=[evidence],
        idempotency_key="proposal-a",
    )
    accepted = store.decide_proposal(proposal.id, "local-user", True, "decision-a", expected_version=proposal.version)
    repeated = store.decide_proposal(proposal.id, "local-user", True, "decision-a", expected_version=proposal.version)
    assert accepted.status == "ACCEPTED"
    assert repeated.accepted_revision_id == accepted.accepted_revision_id

    with pytest.raises(ValueError, match="already decided"):
        store.decide_proposal(proposal.id, "local-user", False, "decision-b", expected_version=proposal.version)


def test_memory_idempotency_keys_are_scoped_to_owner(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")

    alice_entry = store.remember("alice", "fact", "user", "", "Alice fact", "same-key")
    bob_entry = store.remember("bob", "fact", "user", "", "Bob fact", "same-key")
    assert alice_entry.id != bob_entry.id
    assert store.remember("alice", "fact", "user", "", "Alice fact", "same-key").id == alice_entry.id

    alice_evidence = _message(db, "alice-evidence", "alice-thread", "alice")
    bob_evidence = _message(db, "bob-evidence", "bob-thread", "bob")
    alice_proposal = store.propose(
        owner_id="alice", operation="ADD", kind="preference", scope_type="user", scope_id="",
        content="Alice prefers concise answers", confidence=.8, evidence_refs=[alice_evidence], idempotency_key="proposal-key",
    )
    bob_proposal = store.propose(
        owner_id="bob", operation="ADD", kind="preference", scope_type="user", scope_id="",
        content="Bob prefers detailed answers", confidence=.8, evidence_refs=[bob_evidence], idempotency_key="proposal-key",
    )
    assert alice_proposal.id != bob_proposal.id
    with pytest.raises(MemoryConflict, match="different request"):
        store.propose(
            owner_id="alice", operation="ADD", kind="preference", scope_type="user", scope_id="",
            content="different payload is rejected", confidence=.8, evidence_refs=[alice_evidence], idempotency_key="proposal-key",
        )

    alice_accepted = store.decide_proposal(alice_proposal.id, "alice", True, "decision-key", expected_version=alice_proposal.version)
    bob_accepted = store.decide_proposal(bob_proposal.id, "bob", True, "decision-key", expected_version=bob_proposal.version)
    assert alice_accepted.accepted_revision_id != bob_accepted.accepted_revision_id


def test_idempotency_key_cannot_be_reused_across_memory_operations(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    evidence = _message(db, "cross-operation", "thread", "u")
    store.remember("u", "fact", "user", "", "direct", "shared")
    with pytest.raises(MemoryConflict) as error:
        store.propose(owner_id="u", operation="ADD", kind="preference", scope_type="user", scope_id="", content="candidate", confidence=.8, evidence_refs=[evidence], idempotency_key="shared")
    assert error.value.reason_code == "IDEMPOTENCY_KEY_REUSED"


def test_duplicate_add_proposal_is_reported_as_memory_conflict(tmp_path) -> None:
    from app.memory_v2 import MemoryConflict

    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    store.remember("u", "fact", "user", "", "same fact", "direct")
    evidence = _message(db, "same-evidence", "thread", "u")
    with pytest.raises(MemoryConflict, match="same content"):
        store.propose(
            owner_id="u", operation="ADD", kind="fact", scope_type="user", scope_id="",
            content="same fact", confidence=.8, evidence_refs=[evidence], idempotency_key="proposal",
        )


def test_evidence_is_owner_thread_and_actor_scoped(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    alice = _message(db, "alice-message", "alice-thread", "alice")
    bob = _message(db, "bob-message", "bob-thread", "bob")
    assistant = _message(db, "assistant-message", "alice-thread", "alice", role="assistant")

    with pytest.raises(ValueError, match="owner scope"):
        store.propose(owner_id="alice", operation="ADD", kind="fact", scope_type="user", scope_id="", content="x", confidence=.8, evidence_refs=[bob], idempotency_key="cross-owner")
    with pytest.raises(ValueError, match="source_thread_id"):
        store.propose(owner_id="alice", operation="ADD", kind="fact", scope_type="user", scope_id="", content="x", confidence=.8, evidence_refs=[alice], idempotency_key="cross-thread", source_thread_id="other")
    with pytest.raises(ValueError, match="originate from a user"):
        store.propose(owner_id="alice", operation="ADD", kind="fact", scope_type="user", scope_id="", content="x", confidence=.8, evidence_refs=[assistant], idempotency_key="assistant")
    with pytest.raises(ValueError, match="project scope"):
        store.propose(owner_id="alice", operation="ADD", kind="fact", scope_type="project", scope_id="other-project", content="x", confidence=.8, evidence_refs=[alice], idempotency_key="cross-project")
    with pytest.raises(ValueError, match="does not exist"):
        store.propose(owner_id="alice", operation="ADD", kind="fact", scope_type="user", scope_id="", content="x", confidence=.8, evidence_refs=[{"source_type":"thread_message","source_id":"missing"}], idempotency_key="missing")


def test_pending_proposal_merges_new_evidence_and_acceptance_copies_links(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    first_ref = _message(db, "m1", "thread", "u", turn_id="turn-1")
    second_ref = _message(db, "m2", "thread", "u", turn_id="turn-2")
    first = store.propose(owner_id="u", operation="ADD", kind="preference", scope_type="user", scope_id="", content="concise", confidence=.8, evidence_refs=[first_ref], idempotency_key="p1")
    merged = store.propose(owner_id="u", operation="ADD", kind="preference", scope_type="user", scope_id="", content="concise", confidence=.9, evidence_refs=[second_ref], idempotency_key="p2")
    assert merged.id == first.id and merged.version == first.version + 1
    assert {item.source_id for item in merged.evidence} == {"m1", "m2"}
    accepted = store.decide_proposal(first.id, "u", True, "decision", accepted_content="Be concise", expected_version=merged.version)
    entry = next(item for item in store.list_entries("u") if item.revision_id == accepted.accepted_revision_id)
    assert entry.content == "Be concise"
    assert {item.source_id for item in entry.evidence} == {"m1", "m2"}
    assert accepted.original_content == "concise" and accepted.accepted_content == "Be concise"


def test_rejected_proposal_requires_a_new_independent_user_turn(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    first_ref = _message(db, "reject-m1", "thread", "u", turn_id="reject-turn-1")
    same_turn_ref = _message(db, "reject-m2", "thread", "u", turn_id="reject-turn-1")
    next_turn_ref = _message(db, "reject-m3", "thread", "u", turn_id="reject-turn-2")
    proposal = store.propose(owner_id="u", operation="ADD", kind="preference", scope_type="user", scope_id="", content="candidate", confidence=.8, evidence_refs=[first_ref], idempotency_key="reject-p1")
    store.decide_proposal(proposal.id, "u", False, "reject-decision", expected_version=proposal.version)
    with pytest.raises(MemoryConflict) as error:
        store.propose(owner_id="u", operation="ADD", kind="preference", scope_type="user", scope_id="", content="candidate", confidence=.8, evidence_refs=[same_turn_ref], idempotency_key="reject-p2")
    assert error.value.reason_code == "REJECTED_WITHOUT_NEW_EVIDENCE"
    retried = store.propose(owner_id="u", operation="ADD", kind="preference", scope_type="user", scope_id="", content="candidate", confidence=.8, evidence_refs=[next_turn_ref], idempotency_key="reject-p3")
    assert retried.status == "PENDING" and retried.id != proposal.id


def test_rejected_proposal_cannot_reuse_evidence_from_any_earlier_rejection(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    first_ref = _message(db, "history-m1", "thread", "u", turn_id="history-turn-1")
    second_ref = _message(db, "history-m2", "thread", "u", turn_id="history-turn-2")
    first = store.propose(owner_id="u", operation="ADD", kind="preference", scope_type="user", scope_id="", content="candidate", confidence=.8, evidence_refs=[first_ref], idempotency_key="history-p1")
    store.decide_proposal(first.id, "u", False, "history-d1", expected_version=first.version)
    second = store.propose(owner_id="u", operation="ADD", kind="preference", scope_type="user", scope_id="", content="candidate", confidence=.8, evidence_refs=[second_ref], idempotency_key="history-p2")
    store.decide_proposal(second.id, "u", False, "history-d2", expected_version=second.version)

    with pytest.raises(MemoryConflict) as error:
        store.propose(owner_id="u", operation="ADD", kind="preference", scope_type="user", scope_id="", content="candidate", confidence=.8, evidence_refs=[first_ref], idempotency_key="history-p3")
    assert error.value.reason_code == "REJECTED_WITHOUT_NEW_EVIDENCE"


def test_decision_idempotency_rejects_changed_payload(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    evidence = _message(db, "decision-m", "thread", "u")
    proposal = store.propose(owner_id="u", operation="ADD", kind="preference", scope_type="user", scope_id="", content="candidate", confidence=.8, evidence_refs=[evidence], idempotency_key="decision-p")
    store.decide_proposal(proposal.id, "u", True, "same-decision", accepted_content="accepted", expected_version=proposal.version)
    with pytest.raises(MemoryConflict) as error:
        store.decide_proposal(proposal.id, "u", True, "same-decision", accepted_content="changed", expected_version=proposal.version)
    assert error.value.reason_code == "IDEMPOTENCY_KEY_REUSED"


def test_archive_restore_rebuilds_search_index_and_checks_duplicate(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    entry = store.remember("u", "fact", "user", "", "restore me", "create")
    store.set_status(entry.id, "u", "ARCHIVED", idempotency_key="archive")
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_fts WHERE entry_id=?", (entry.id,)).fetchone()[0] == 0
    restored = store.restore(entry.id, "u", "restore")
    assert restored.status == "ACTIVE"
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_fts WHERE entry_id=?", (entry.id,)).fetchone()[0] == 1
    store.set_status(entry.id, "u", "ARCHIVED", idempotency_key="archive-again")
    store.remember("u", "fact", "user", "", "restore me", "duplicate")
    with pytest.raises(MemoryConflict) as error:
        store.restore(entry.id, "u", "restore-conflict")
    assert error.value.reason_code == "ACTIVE_DUPLICATE"


def test_edit_and_accepted_update_reject_duplicate_active_content(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    first = store.remember("u", "preference", "user", "", "alpha", "first")
    second = store.remember("u", "preference", "user", "", "beta", "second")
    with pytest.raises(MemoryConflict) as edit_error:
        store.edit(second.id, "u", "alpha", second.revision_id, idempotency_key="duplicate-edit")
    assert edit_error.value.reason_code == "ACTIVE_DUPLICATE"

    evidence = _message(db, "update-duplicate", "thread", "u")
    proposal = store.propose(
        owner_id="u", operation="UPDATE", kind="preference", scope_type="user", scope_id="",
        content="alpha", confidence=.8, evidence_refs=[evidence], idempotency_key="duplicate-proposal",
        target_entry_id=second.id, base_revision_id=second.revision_id,
    )
    with pytest.raises(MemoryConflict) as proposal_error:
        store.decide_proposal(proposal.id, "u", True, "duplicate-decision", expected_version=proposal.version)
    assert proposal_error.value.reason_code == "ACTIVE_DUPLICATE"
    assert store.get(first.id, "u").content == "alpha"
    assert store.get(second.id, "u").content == "beta"


def test_repeated_purge_does_not_invalidate_unrelated_context_pin(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    _thread(db, "purge-thread", "u")
    old = store.remember("u", "fact", "user", "", "old unique memory", "old")
    provider = MemoryContextProvider(db)
    provider.select(MemoryContextRequest("u", "purge-thread", None, "old unique memory", model_invocation_id="old-pin"))
    store.purge(old.id, "u", idempotency_key="purge-first")
    fresh = store.remember("u", "fact", "user", "", "fresh unique memory", "fresh")
    bundle = provider.select(MemoryContextRequest("u", "purge-thread", None, "fresh unique memory", model_invocation_id="fresh-pin"))
    assert fresh.revision_id in bundle.revision_ids

    store.purge(old.id, "u", idempotency_key="purge-second")
    with db.connection() as connection:
        pin = connection.execute("SELECT invalidated_at FROM memory_context_pins WHERE model_invocation_id='fresh-pin'").fetchone()
        payload = connection.execute("SELECT 1 FROM memory_context_pin_payloads WHERE pin_invocation_id='fresh-pin'").fetchone()
    assert pin["invalidated_at"] is None
    assert payload is not None


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


def test_expired_active_memory_is_not_injected_into_context(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    expired = store.remember("u", "fact", "user", "", "expired fact", "expired")
    current = store.remember("u", "fact", "user", "", "current fact", "current")
    with db.transaction() as connection:
        connection.execute("UPDATE memory_entries SET valid_until='2000-01-01T00:00:00+00:00' WHERE id=?", (expired.id,))
    _thread(db, "expiry-thread", "u")
    bundle = MemoryContextProvider(db).select(MemoryContextRequest("u", "expiry-thread", None, "fact"))
    assert expired.revision_id not in bundle.revision_ids
    assert current.revision_id in bundle.revision_ids


def test_expired_active_memory_does_not_block_remembering_same_content(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    expired = store.remember("u", "fact", "user", "", "renewable fact", "expired-create")
    with db.transaction() as connection:
        connection.execute("UPDATE memory_entries SET valid_until='2000-01-01T00:00:00+00:00' WHERE id=?", (expired.id,))

    renewed = store.remember("u", "fact", "user", "", "renewable fact", "renew")
    assert renewed.id != expired.id
    assert renewed.status == "ACTIVE"
    assert store.get(expired.id, "u").status == "ARCHIVED"


def test_expired_memory_is_removed_from_index_projection_and_cannot_restore(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    entry = store.remember("u", "preference", "user", "", "expired projection", "create-expired")
    with db.transaction() as connection:
        connection.execute("UPDATE memory_entries SET valid_until='2000-01-01T00:00:00+00:00' WHERE id=?", (entry.id,))
    assert store.list_entries("u", "ACTIVE") == []
    assert store.get(entry.id, "u").status == "ARCHIVED"
    with db.connection() as connection:
        assert connection.execute("SELECT 1 FROM memory_fts WHERE entry_id=?", (entry.id,)).fetchone() is None
    projection = tmp_path / "memory" / "users" / __import__("hashlib").sha256(b"u").hexdigest()[:24] / "USER.md"
    assert "expired projection" not in projection.read_text(encoding="utf-8")
    with pytest.raises(MemoryConflict) as error:
        store.restore(entry.id, "u", "restore-expired")
    assert error.value.reason_code == "EXPIRED_MEMORY"


def test_expired_active_memory_cannot_be_restored_before_listing(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    entry = store.remember("u", "preference", "user", "", "expired direct restore", "create")
    with db.transaction() as connection:
        connection.execute("UPDATE memory_entries SET valid_until='2000-01-01T00:00:00+00:00' WHERE id=?", (entry.id,))

    with pytest.raises(MemoryConflict) as error:
        store.restore(entry.id, "u", "restore-expired-directly")

    assert error.value.reason_code == "EXPIRED_MEMORY"
    assert store.get(entry.id, "u").status == "ARCHIVED"
    with db.connection() as connection:
        assert connection.execute("SELECT 1 FROM memory_fts WHERE entry_id=?", (entry.id,)).fetchone() is None


def test_expired_restore_cannot_change_purged_memory_or_reused_key_state(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    purged = store.remember("u", "fact", "user", "", "purged body", "create-purged")
    with db.transaction() as connection:
        connection.execute("UPDATE memory_entries SET valid_until='2000-01-01T00:00:00+00:00' WHERE id=?", (purged.id,))
    store.purge(purged.id, "u", idempotency_key="purge")
    with pytest.raises(MemoryConflict) as purged_error:
        store.restore(purged.id, "u", "restore-purged")
    assert purged_error.value.reason_code == "PURGED_MEMORY"
    assert store.get(purged.id, "u").status == "PURGED"

    expired = store.remember("u", "fact", "user", "", "expired body", "create-expired-key")
    with db.transaction() as connection:
        connection.execute("UPDATE memory_entries SET valid_until='2000-01-01T00:00:00+00:00' WHERE id=?", (expired.id,))
    with pytest.raises(MemoryConflict) as key_error:
        store.restore(expired.id, "u", "create-purged")
    assert key_error.value.reason_code == "IDEMPOTENCY_KEY_REUSED"
    assert store.get(expired.id, "u").status == "ACTIVE"


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


def test_episode_edit_is_versioned_idempotent_and_invalidates_pins(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    _thread(db, "episode-edit-thread", "u")
    episode = store.save_episode(
        owner_id="u", thread_id="episode-edit-thread", project_id=None,
        start_message_seq=1, end_message_seq=2, source_hash="sha256:edit",
        summary="old summary", synopsis=[{"text":"old","source_message_ids":[]}],
    )
    provider = MemoryContextProvider(db)
    provider.select(MemoryContextRequest("u", "episode-edit-thread", None, "summary", model_invocation_id="episode-pin"))
    edited = store.edit_episode(episode.id, "u", "new summary", "thread", episode.version, "edit-key")
    replay = store.edit_episode(episode.id, "u", "new summary", "thread", episode.version, "edit-key")
    assert edited == replay and edited.version == episode.version + 1
    with db.connection() as connection:
        row = connection.execute("SELECT synopsis_json,topics_json,decisions_json,outcomes_json,open_loops_json FROM memory_episodes WHERE id=?", (episode.id,)).fetchone()
        pin = connection.execute("SELECT invalidated_at,invalidation_reason FROM memory_context_pins WHERE model_invocation_id='episode-pin'").fetchone()
        payload = connection.execute("SELECT 1 FROM memory_context_pin_payloads WHERE pin_invocation_id='episode-pin'").fetchone()
    assert set(row) == {"[]"}
    assert pin["invalidated_at"] is not None and pin["invalidation_reason"] == "episode_edited"
    assert payload is None
    with pytest.raises(MemoryConflict) as error:
        store.edit_episode(episode.id, "u", "stale", "thread", episode.version, "stale-key")
    assert error.value.reason_code == "STALE_EPISODE_VERSION"


def test_episode_edit_replay_fails_after_its_result_is_superseded(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    _thread(db, "episode-replay-thread", "u")
    episode = store.save_episode(
        owner_id="u", thread_id="episode-replay-thread", project_id=None,
        start_message_seq=1, end_message_seq=2, source_hash="sha256:replay",
        summary="original", synopsis=[{"text":"original","source_message_ids":[]}],
    )
    first = store.edit_episode(episode.id, "u", "first", "thread", episode.version, "edit-first")
    second = store.edit_episode(episode.id, "u", "second", "thread", first.version, "edit-second")

    with pytest.raises(MemoryConflict) as error:
        store.edit_episode(episode.id, "u", "first", "thread", episode.version, "edit-first")

    assert error.value.reason_code == "IDEMPOTENT_RESULT_SUPERSEDED"
    assert store.get_episode(episode.id, "u") == second


def test_episode_delete_with_new_key_requires_current_version(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    _thread(db, "episode-delete-thread", "u")
    episode = store.save_episode(
        owner_id="u", thread_id="episode-delete-thread", project_id=None,
        start_message_seq=1, end_message_seq=2, source_hash="sha256:delete",
        summary="summary", synopsis=[{"text":"summary","source_message_ids":[]}],
    )
    store.delete_episode(episode.id, "u", episode.version, "delete-first")

    with pytest.raises(MemoryConflict) as error:
        store.delete_episode(episode.id, "u", 999, "delete-wrong-version")

    assert error.value.reason_code == "STALE_EPISODE_VERSION"


def test_episode_delete_removes_summary_from_audit_metadata(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    store = MemoryStore(db, tmp_path / "memory")
    _thread(db, "episode-private-thread", "u")
    episode = store.save_episode(
        owner_id="u", thread_id="episode-private-thread", project_id=None,
        start_message_seq=1, end_message_seq=2, source_hash="sha256:private",
        summary="initial", synopsis=[{"text":"initial","source_message_ids":[]}],
    )
    edited = store.edit_episode(
        episode.id, "u", "PRIVATE SUMMARY MUST DISAPPEAR", "thread", episode.version, "edit-private",
    )
    store.delete_episode(episode.id, "u", edited.version, "delete-private")

    with db.connection() as connection:
        rows = connection.execute(
            "SELECT metadata_json FROM memory_audit_events WHERE aggregate_type='episode' AND aggregate_id=?",
            (episode.id,),
        ).fetchall()
    assert all("PRIVATE SUMMARY MUST DISAPPEAR" not in row["metadata_json"] for row in rows)


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
    store.delete_episode(episode.id, "alice", episode.version, "delete-episode")
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
