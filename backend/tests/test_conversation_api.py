import asyncio

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


def test_direction_route_materializes_after_confirmation(tmp_path) -> None:
    from app.main import create_app
    from test_materializer import MaterializerModel

    runtime = make_runtime(tmp_path, MaterializerModel())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    thread = client.post("/api/threads", headers=_headers(app), json={}).json()
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
