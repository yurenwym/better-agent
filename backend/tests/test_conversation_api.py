import asyncio
import json

import pytest

from fastapi.testclient import TestClient

from test_runtime import make_runtime


def _headers(app):
    return {
        "host": "127.0.0.1:8000",
        "origin": "http://127.0.0.1:8000",
        "content-type": "application/json",
        "x-csrf-token": app.state.csrf_token,
    }


class BlockingConversationModel:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def route_and_respond(self, **kwargs):
        self.started.set()
        await self.release.wait()
        return None


def _count(runtime, table: str) -> int:
    with runtime.db.connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class PendingAskModel:
    async def route_and_respond(self, **kwargs):
        from app.ask import AskQuestion, AskRequest

        return AskRequest(
            "call-ask-api",
            (
                AskQuestion(
                    "training_level",
                    "训练水平",
                    "你目前的训练水平是什么？",
                    (
                        {"label": "新手", "description": "刚开始训练"},
                        {"label": "有基础", "description": "已有训练习惯"},
                    ),
                    False,
                    True,
                ),
            ),
        )


def _pending_ask(tmp_path):
    from app.main import create_app

    runtime = make_runtime(tmp_path, PendingAskModel())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    thread = client.post("/api/threads", headers=_headers(app), json={}).json()
    accepted = client.post(
        f"/api/threads/{thread['id']}/turns",
        headers=_headers(app),
        json={"client_turn_id": "client-ask", "content": "制定训练计划", "skill_names": []},
    ).json()
    asyncio.run(runtime.turn_worker.run_once())
    return runtime, app, client, runtime.conversation.turn(accepted["turn_id"])


def test_turn_submission_is_durable_and_idempotent_before_model_finishes(tmp_path) -> None:
    from app.main import create_app

    model = BlockingConversationModel()
    runtime = make_runtime(tmp_path, model)
    app = create_app(runtime=runtime)
    client = TestClient(app)

    thread_response = client.post("/api/threads", headers=_headers(app), json={"title": "Chat"})
    assert thread_response.status_code == 201
    thread_id = thread_response.json()["id"]
    payload = {
        "client_turn_id": "client-1",
        "content": "创建桂林 7 天攻略",
        "skill_names": [],
    }

    first = client.post(f"/api/threads/{thread_id}/turns", headers=_headers(app), json=payload)
    second = client.post(f"/api/threads/{thread_id}/turns", headers=_headers(app), json=payload)

    assert first.status_code == second.status_code == 202
    assert first.json()["turn_id"] == second.json()["turn_id"]
    assert first.json()["status"] == "ACCEPTED"
    assert _count(runtime, "thread_messages") == 1
    assert _count(runtime, "turn_jobs") == 1
    assert _count(runtime, "goals") == 0
    assert _count(runtime, "runs") == 0


def test_thread_list_preserves_multiple_conversations_and_owner_scope(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    first = client.post("/api/threads", headers=_headers(app), json={"title": "骑行计划"}).json()
    second = client.post("/api/threads", headers=_headers(app), json={"title": "广西旅行"}).json()
    with runtime.db.transaction() as connection:
        connection.execute("INSERT INTO threads(id,title,owner_id,version,next_event_seq,created_at,updated_at) VALUES ('other-thread','private','other-user',0,1,datetime('now'),datetime('now'))")

    response = client.get("/api/threads", headers={"host": "127.0.0.1:8000"})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["threads"]] == [second["id"], first["id"]]
    assert all(item["id"] != "other-thread" for item in response.json()["threads"])


def test_deleting_thread_removes_it_from_history_and_blocks_direct_access(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    thread = client.post("/api/threads", headers=_headers(app), json={"title": "待删除会话"}).json()
    accepted = client.post(
        f"/api/threads/{thread['id']}/turns",
        headers=_headers(app),
        json={"client_turn_id": "client-delete", "content": "保留审计内容", "skill_names": []},
    ).json()
    client.post(f"/api/turns/{accepted['turn_id']}/cancel", headers=_headers(app), json={})

    deleted = client.delete(f"/api/threads/{thread['id']}", headers=_headers(app))

    assert deleted.status_code == 204
    assert client.get(f"/api/threads/{thread['id']}", headers={"host": "127.0.0.1:8000"}).status_code == 404
    assert client.post(
        f"/api/threads/{thread['id']}/turns",
        headers=_headers(app),
        json={"client_turn_id": "after-delete", "content": "不应写入", "skill_names": []},
    ).status_code == 404
    assert all(item["id"] != thread["id"] for item in client.get("/api/threads", headers={"host": "127.0.0.1:8000"}).json()["threads"])
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT deleted_at FROM threads WHERE id=?", (thread["id"],)).fetchone()["deleted_at"]
        assert connection.execute("SELECT COUNT(*) FROM thread_messages WHERE thread_id=?", (thread["id"],)).fetchone()[0] == 1


def test_deleting_thread_is_owner_scoped(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    with runtime.db.transaction() as connection:
        connection.execute("INSERT INTO threads(id,title,owner_id,version,next_event_seq,created_at,updated_at) VALUES ('other-thread-delete','private','other-user',0,1,datetime('now'),datetime('now'))")

    response = client.delete("/api/threads/other-thread-delete", headers=_headers(app))

    assert response.status_code == 404
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT deleted_at FROM threads WHERE id='other-thread-delete'").fetchone()["deleted_at"] is None


def test_deleting_thread_requires_active_work_to_stop_first(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    thread = client.post("/api/threads", headers=_headers(app), json={"title": "回复中"}).json()
    client.post(
        f"/api/threads/{thread['id']}/turns",
        headers=_headers(app),
        json={"client_turn_id": "client-active", "content": "继续回复", "skill_names": []},
    )

    response = client.delete(f"/api/threads/{thread['id']}", headers=_headers(app))

    assert response.status_code == 409
    assert client.get(f"/api/threads/{thread['id']}", headers={"host": "127.0.0.1:8000"}).status_code == 200


def test_turn_validation_rejects_empty_content_and_bad_skills(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    thread = client.post("/api/threads", headers=_headers(app), json={}).json()

    empty = client.post(
        f"/api/threads/{thread['id']}/turns",
        headers=_headers(app),
        json={"client_turn_id": "client-1", "content": "   "},
    )
    bad_skills = client.post(
        f"/api/threads/{thread['id']}/turns",
        headers=_headers(app),
        json={"client_turn_id": "client-2", "content": "hello", "skill_names": [1]},
    )

    assert empty.status_code == 422
    assert bad_skills.status_code == 422


def test_json_request_body_has_a_finite_size_limit(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)

    response = client.post(
        "/api/threads",
        headers=_headers(app),
        json={"title": "x" * (2 * 1024 * 1024)},
    )

    assert response.status_code == 413


def test_json_request_body_limit_also_applies_without_content_length(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    payload = b'{"title":"' + (b"x" * (2 * 1024 * 1024)) + b'"}'

    response = client.post(
        "/api/threads",
        headers=_headers(app),
        content=iter([payload]),
    )

    assert response.status_code == 413


def test_direction_route_materializes_after_confirmation(tmp_path) -> None:
    from app.main import create_app
    from test_materializer import MaterializerModel

    runtime = make_runtime(tmp_path, MaterializerModel())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    thread = client.post("/api/threads", headers=_headers(app), json={}).json()
    runtime.plan_documents.save_model_revision(
        thread_id=thread["id"],
        title="Execution plan",
        markdown_content="# Execution plan\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    accepted = client.post(
        f"/api/threads/{thread['id']}/turns",
        headers=_headers(app),
        json={"client_turn_id": "client-1", "content": "执行清单", "skill_names": []},
    ).json()
    asyncio.run(runtime.turn_worker.run_once())
    turn = runtime.conversation.turn(accepted["turn_id"])

    response = client.post(
        f"/api/turns/{turn.id}/direction",
        headers=_headers(app),
        json={
            "action": "continue_execution",
            "expected_version": turn.version,
            "idempotency_key": "action-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["turn"]["materialized_run_id"]
    assert _count(runtime, "goals") == 1


def test_thread_messages_events_and_cancel_routes_are_durable(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    thread = client.post("/api/threads", headers=_headers(app), json={}).json()
    accepted = client.post(
        f"/api/threads/{thread['id']}/turns",
        headers=_headers(app),
        json={"client_turn_id": "client-1", "content": "hello", "skill_names": []},
    ).json()

    messages = client.get(f"/api/threads/{thread['id']}/messages", headers={"host": "127.0.0.1:8000"})
    events = client.get(f"/api/threads/{thread['id']}/events", headers={"host": "127.0.0.1:8000"})
    cancelled = client.post(
        f"/api/turns/{accepted['turn_id']}/cancel",
        headers=_headers(app),
        json={},
    )
    resumed = client.get(
        f"/api/threads/{thread['id']}/events?after_seq=1",
        headers={"host": "127.0.0.1:8000"},
    )

    assert messages.status_code == 200
    assert messages.json()["messages"][0]["content"] == "hello"
    assert events.status_code == 200
    assert [event["seq"] for event in events.json()["events"]] == [1]
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert resumed.status_code == 200
    assert [event["seq"] for event in resumed.json()["events"]] == [2, 3]
    assert [event["type"] for event in resumed.json()["events"]] == [
        "turn.cancel_requested", "turn.cancelled"
    ]


def test_thread_sse_uses_seq_and_last_event_id_for_reconnect(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    thread = client.post("/api/threads", headers=_headers(app), json={}).json()
    accepted = client.post(
        f"/api/threads/{thread['id']}/turns",
        headers=_headers(app),
        json={"client_turn_id": "client-1", "content": "hello", "skill_names": []},
    ).json()
    client.post(f"/api/turns/{accepted['turn_id']}/cancel", headers=_headers(app), json={})

    initial = client.get(
        f"/api/threads/{thread['id']}/events/stream?after_seq=0&follow=0",
        headers={"host": "127.0.0.1:8000"},
    )
    resumed = client.get(
        f"/api/threads/{thread['id']}/events/stream?follow=0",
        headers={"host": "127.0.0.1:8000", "last-event-id": "1"},
    )

    assert initial.status_code == 200
    assert "id: 1\nevent: conversation" in initial.text
    assert "id: 2\nevent: conversation" in initial.text
    assert "id: 3\nevent: conversation" in initial.text
    assert "id: 1\nevent: conversation" not in resumed.text
    assert "id: 2\nevent: conversation" in resumed.text
    assert "id: 3\nevent: conversation" in resumed.text


def test_answering_pending_ask_creates_one_child_turn_and_is_idempotent(tmp_path) -> None:
    runtime, app, client, pending = _pending_ask(tmp_path)
    payload = {
        "expected_version": pending.version,
        "idempotency_key": "answer-1",
        "answers": [{"question_id": "training_level", "selected_options": ["新手"], "free_text": ""}],
    }

    first = client.post(f"/api/turns/{pending.id}/ask/answer", headers=_headers(app), json=payload)
    second = client.post(f"/api/turns/{pending.id}/ask/answer", headers=_headers(app), json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json()["turn"]["id"] == second.json()["turn"]["id"]
    assert first.json()["turn"]["parent_turn_id"] == pending.id
    assert first.json()["turn"]["status"] == "ACCEPTED"
    assert _count(runtime, "turns") == 2
    with runtime.db.connection() as connection:
        ask = connection.execute("SELECT status, answer_json, continuation_turn_id FROM turn_asks").fetchone()
    assert ask["status"] == "ANSWERED"
    assert json.loads(ask["answer_json"])[0]["selected_options"] == ["新手"]
    assert ask["continuation_turn_id"] == first.json()["turn"]["id"]
    completed_event = next(
        event for event in runtime.conversation.events.list(pending.thread_id)
        if event.type == "turn.completed" and event.turn_id == pending.id
    )
    assert completed_event.data["continuation_turn_id"] == first.json()["turn"]["id"]


def test_answering_pending_ask_rejects_version_conflict_and_invalid_choice(tmp_path) -> None:
    runtime, app, client, pending = _pending_ask(tmp_path)

    conflict = client.post(
        f"/api/turns/{pending.id}/ask/answer",
        headers=_headers(app),
        json={
            "expected_version": pending.version + 1,
            "idempotency_key": "answer-conflict",
            "answers": [{"question_id": "training_level", "selected_options": ["未知"], "free_text": ""}],
        },
    )
    invalid = client.post(
        f"/api/turns/{pending.id}/ask/answer",
        headers=_headers(app),
        json={
            "expected_version": pending.version,
            "idempotency_key": "answer-invalid",
            "answers": [{"question_id": "training_level", "selected_options": ["未知"], "free_text": ""}],
        },
    )

    assert conflict.status_code == 409
    assert invalid.status_code == 422
    assert _count(runtime, "turns") == 1


@pytest.mark.parametrize("version_kind", ["bool", "float", "string"])
def test_answering_pending_ask_rejects_non_integer_versions(tmp_path, version_kind) -> None:
    runtime, app, client, pending = _pending_ask(tmp_path)
    expected_version = {
        "bool": True,
        "float": float(pending.version),
        "string": str(pending.version),
    }[version_kind]

    response = client.post(
        f"/api/turns/{pending.id}/ask/answer",
        headers=_headers(app),
        json={
            "expected_version": expected_version,
            "idempotency_key": f"invalid-version-{version_kind}",
            "answers": [{"question_id": "training_level", "selected_options": [], "free_text": "test"}],
        },
    )

    assert response.status_code == 422
    assert _count(runtime, "turns") == 1


def test_pending_ask_can_be_loaded_and_cancelled_without_child_turn(tmp_path) -> None:
    runtime, app, client, pending = _pending_ask(tmp_path)

    loaded = client.get(f"/api/turns/{pending.id}/ask", headers={"host": "127.0.0.1:8000"})
    cancelled = client.post(f"/api/turns/{pending.id}/cancel", headers=_headers(app), json={})

    assert loaded.status_code == 200
    assert loaded.json()["questions"][0]["id"] == "training_level"
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert _count(runtime, "turns") == 1
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT status FROM turn_asks").fetchone()["status"] == "CANCELLED"
