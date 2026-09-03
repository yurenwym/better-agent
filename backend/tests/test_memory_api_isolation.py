from __future__ import annotations

from fastapi.testclient import TestClient

from test_runtime import make_runtime


def _headers(app, owner: str) -> dict[str, str]:
    return {
        "host": "127.0.0.1:8000",
        "origin": "http://127.0.0.1:8000",
        "content-type": "application/json",
        "x-csrf-token": app.state.csrf_token,
        "x-owner-id": owner,
    }


def _runtime_with_v2(tmp_path):
    from app.main import create_app
    from app.memory_v2 import MemoryStore
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    runtime.memory_store = MemoryStore(runtime.db, tmp_path / "memory-v2")
    return runtime, create_app(runtime=runtime)


def test_memory_entries_are_isolated_by_owner_and_legacy_writes_are_rejected(tmp_path) -> None:
    runtime, app = _runtime_with_v2(tmp_path)
    client = TestClient(app)

    created = client.post(
        "/api/memory/entries",
        json={
            "kind": "preference",
            "scope_type": "user",
            "content": "Alice prefers concise answers",
            "idempotency_key": "remember-1",
        },
        headers=_headers(app, "alice"),
    )
    assert created.status_code == 201
    entry_id = created.json()["id"]

    alice = client.get("/api/memories", headers={"host": "127.0.0.1:8000", "x-owner-id": "alice"})
    bob = client.get("/api/memories", headers={"host": "127.0.0.1:8000", "x-owner-id": "bob"})
    assert [item["id"] for item in alice.json()["entries"]] == [entry_id]
    assert bob.json()["entries"] == []

    foreign_edit = client.patch(
        f"/api/memory/entries/{entry_id}",
        json={"content": "Bob must not edit this", "base_revision_id": created.json()["revision_id"]},
        headers=_headers(app, "bob"),
    )
    assert foreign_edit.status_code == 404

    legacy = client.post(
        "/api/memories",
        json={"kind": "preference", "content": "legacy write"},
        headers=_headers(app, "alice"),
    )
    assert legacy.status_code == 410


def test_v2_versions_do_not_fall_back_to_ownerless_legacy_memory(tmp_path) -> None:
    runtime, app = _runtime_with_v2(tmp_path)
    client = TestClient(app)

    response = client.get(
        "/api/memories/legacy-id/versions",
        headers={"host": "127.0.0.1:8000", "x-owner-id": "local-user"},
    )
    assert response.status_code == 404


def test_proposal_decision_is_owner_scoped(tmp_path) -> None:
    runtime, app = _runtime_with_v2(tmp_path)
    proposal = runtime.memory_store.propose(
        owner_id="alice",
        operation="ADD",
        kind="fact",
        scope_type="user",
        scope_id="",
        content="Alice uses Python",
        confidence=0.8,
        evidence_refs=["evt-1"],
        idempotency_key="proposal-1",
        reason="observed in conversation",
    )
    client = TestClient(app)

    response = client.post(
        f"/api/memory/proposals/{proposal.id}/decision",
        json={"accept": True, "idempotency_key": "decision-1"},
        headers=_headers(app, "bob"),
    )
    assert response.status_code == 404
    assert runtime.memory_store.get_proposal(proposal.id, "alice").status == "PENDING"
