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


def _mutation_headers(app, owner: str, key: str) -> dict[str, str]:
    return {**_headers(app, owner), "idempotency-key": key}


def _evidence(runtime, owner: str, message_id: str, thread_id: str = "evidence-thread") -> dict[str, str]:
    from test_memory_v2 import _message
    return _message(runtime.db, message_id, f"{owner}-{thread_id}", owner)


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
        headers=_mutation_headers(app, "alice", "remember-1"),
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
        headers=_mutation_headers(app, "bob", "foreign-edit"),
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
    evidence = _evidence(runtime, "alice", "evt-1")
    proposal = runtime.memory_store.propose(
        owner_id="alice",
        operation="ADD",
        kind="fact",
        scope_type="user",
        scope_id="",
        content="Alice uses Python",
        confidence=0.8,
        evidence_refs=[evidence],
        idempotency_key="proposal-1",
        reason="observed in conversation",
    )
    client = TestClient(app)

    response = client.post(
        f"/api/memory/proposals/{proposal.id}/decision",
        json={"accept": True, "expected_version": proposal.version},
        headers=_mutation_headers(app, "bob", "decision-1"),
    )
    assert response.status_code == 404
    assert runtime.memory_store.get_proposal(proposal.id, "alice").status == "PENDING"


def test_memory_api_requires_idempotency_and_rejects_changed_replay(tmp_path) -> None:
    _, app = _runtime_with_v2(tmp_path)
    client = TestClient(app)
    payload = {"kind": "fact", "scope_type": "user", "content": "one"}
    assert client.post("/api/memory/entries", json=payload, headers=_headers(app, "alice")).status_code == 422
    headers = _mutation_headers(app, "alice", "same-key")
    first = client.post("/api/memory/entries", json=payload, headers=headers)
    replay = client.post("/api/memory/entries", json=payload, headers=headers)
    conflict = client.post("/api/memory/entries", json={**payload, "content": "two"}, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["reason_code"] == "IDEMPOTENCY_KEY_REUSED"


def test_memory_api_decision_requires_strict_boolean_and_supports_edited_acceptance(tmp_path) -> None:
    runtime, app = _runtime_with_v2(tmp_path)
    evidence = _evidence(runtime, "alice", "decision-evidence")
    proposal = runtime.memory_store.propose(
        owner_id="alice", operation="ADD", kind="preference", scope_type="user", scope_id="",
        content="original", confidence=.8, evidence_refs=[evidence], idempotency_key="proposal",
    )
    client = TestClient(app)
    url = f"/api/memory/proposals/{proposal.id}/decision"
    for invalid in (1, "true", None):
        response = client.post(url, json={"accept": invalid}, headers=_mutation_headers(app, "alice", f"invalid-{invalid}"))
        assert response.status_code == 422
    missing_version = client.post(url, json={"accept": True}, headers=_mutation_headers(app, "alice", "missing-version"))
    assert missing_version.status_code == 422
    assert missing_version.json()["detail"]["reason_code"] == "INVALID_APPROVAL_DECISION"
    accepted = client.post(
        url,
        json={"accept": True, "accepted_content": "edited", "expected_version": proposal.version},
        headers=_mutation_headers(app, "alice", "accept"),
    )
    assert accepted.status_code == 200
    assert accepted.json()["original_content"] == "original"
    assert accepted.json()["accepted_content"] == "edited"
    assert accepted.json()["model_confidence"] == .8


def test_memory_api_rejects_invalid_field_types_with_422(tmp_path) -> None:
    _, app = _runtime_with_v2(tmp_path)
    client = TestClient(app)
    base = {"kind":"preference", "scope_type":"user", "scope_id":"", "content":"valid"}
    invalid_payloads = [
        {**base, "content": 123},
        {**base, "pinned": 2},
        {**base, "scope_id": []},
        {**base, "source_refs": {}},
        {**base, "source_thread_id": []},
        {**base, "source_run_id": []},
    ]
    for index, payload in enumerate(invalid_payloads):
        response = client.post(
            "/api/memory/entries", json=payload,
            headers=_mutation_headers(app, "alice", f"invalid-fields-{index}"),
        )
        assert response.status_code == 422
        assert response.json()["detail"]["reason_code"] == "INVALID_MEMORY_REQUEST"


def test_memory_api_archive_and_restore_require_keys(tmp_path) -> None:
    _, app = _runtime_with_v2(tmp_path)
    client = TestClient(app)
    created = client.post(
        "/api/memory/entries", json={"kind":"fact","scope_type":"user","content":"restore"},
        headers=_mutation_headers(app, "alice", "create"),
    ).json()
    archive_url = f"/api/memory/entries/{created['id']}/archive"
    restore_url = f"/api/memory/entries/{created['id']}/restore"
    assert client.post(archive_url, headers=_headers(app, "alice")).status_code == 422
    assert client.post(archive_url, headers=_mutation_headers(app, "alice", "archive")).json()["status"] == "ARCHIVED"
    assert client.post(restore_url, headers=_mutation_headers(app, "alice", "restore")).json()["status"] == "ACTIVE"


def test_memory_api_edit_and_purge_are_idempotent(tmp_path) -> None:
    _, app = _runtime_with_v2(tmp_path)
    client = TestClient(app)
    headers = _mutation_headers(app, "alice", "create-editable")
    entry = client.post(
        "/api/memory/entries",
        json={"kind":"preference","scope_type":"user","content":"short answers"},
        headers=headers,
    ).json()
    edit_url = f"/api/memory/entries/{entry['id']}"
    edit_body = {"content":"lead with the answer","base_revision_id":entry["revision_id"]}
    edit_headers = _mutation_headers(app, "alice", "edit-once")
    first = client.patch(edit_url, json=edit_body, headers=edit_headers)
    replay = client.patch(edit_url, json=edit_body, headers=edit_headers)
    changed = client.patch(
        edit_url,
        json={**edit_body, "content":"different content"},
        headers=edit_headers,
    )
    assert first.status_code == replay.status_code == 200
    assert first.json()["revision_id"] == replay.json()["revision_id"]
    assert changed.status_code == 409
    assert changed.json()["detail"]["reason_code"] == "IDEMPOTENCY_KEY_REUSED"

    purge_headers = _mutation_headers(app, "alice", "purge-once")
    assert client.delete(edit_url, headers=purge_headers).status_code == 204
    assert client.delete(edit_url, headers=purge_headers).status_code == 204


def test_episode_api_requires_version_and_idempotency_and_rejects_invalid_summary(tmp_path) -> None:
    runtime, app = _runtime_with_v2(tmp_path)
    from test_memory_v2 import _thread

    _thread(runtime.db, "episode-api-thread", "alice")
    episode = runtime.memory_store.save_episode(
        owner_id="alice", thread_id="episode-api-thread", project_id=None,
        start_message_seq=1, end_message_seq=2, source_hash="sha256:api", summary="old",
    )
    client = TestClient(app)
    url = f"/api/memory/episodes/{episode.id}"
    assert client.patch(url, json={"summary":"new"}, headers=_headers(app,"alice")).status_code == 422
    invalid = client.patch(url, json={"summary":123,"expected_version":episode.version}, headers=_mutation_headers(app,"alice","invalid-summary"))
    assert invalid.status_code == 422
    headers = _mutation_headers(app,"alice","edit-episode")
    body = {"summary":"new","retrieval_policy":"thread","expected_version":episode.version}
    first = client.patch(url,json=body,headers=headers)
    replay = client.patch(url,json=body,headers=headers)
    assert first.status_code == replay.status_code == 200
    assert first.json()["version"] == episode.version + 1
    stale = client.patch(url,json={**body,"summary":"stale"},headers=_mutation_headers(app,"alice","stale-episode"))
    assert stale.status_code == 409
    delete_headers = _mutation_headers(app,"alice","delete-episode")
    current_version = first.json()["version"]
    assert client.request("DELETE",url,json={"expected_version":current_version},headers=delete_headers).status_code == 204
    assert client.request("DELETE",url,json={"expected_version":current_version},headers=delete_headers).status_code == 204
