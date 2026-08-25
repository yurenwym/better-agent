from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from test_runtime import make_runtime


def _headers(app, key: str):
    return {
        "host": "127.0.0.1:8000",
        "origin": "http://127.0.0.1:8000",
        "content-type": "application/json",
        "x-csrf-token": app.state.csrf_token,
        "idempotency-key": key,
    }


def test_golden_goal_journey_runs_through_review_adjustment_and_memory(tmp_path) -> None:
    from app.main import create_app
    from app.memory_v2 import MemoryStore
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    runtime.memory_store = MemoryStore(runtime.db, tmp_path / "memory")
    runtime.goal_reviews.queue_delay_seconds = 0
    app = create_app(runtime=runtime)
    client = TestClient(app)
    thread = client.post("/api/threads", json={"title": "Golden journey"}, headers=_headers(app, "thread")).json()
    version = runtime.plan_documents.save_model_revision(
        thread_id=thread["id"], title="Two day plan", markdown_content="# Two day plan\n\nDo one action each day.",
        source_turn_id=None, source_message_id=None, actor="user",
    )
    start = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    end = start + timedelta(days=1)
    body = {"start_date": start.isoformat(), "requested_end_date": end.isoformat(), "timezone": "Asia/Shanghai", "daily_minutes": 60}

    preview = client.post(f"/api/plans/{version.plan_document_id}/program-preview", json=body, headers=_headers(app, "preview")).json()
    active = client.post(f"/api/programs/{preview['id']}/activate", json={"expected_version": preview["version"]}, headers=_headers(app, "activate")).json()
    first = next(action for action in active["actions"] if action["scheduled_date"] == start.isoformat())
    client.post(
        f"/api/actions/{first['id']}/defer",
        json={"expected_version": first["version"], "scheduled_date": end.isoformat()},
        headers=_headers(app, "defer"),
    ).raise_for_status()

    assert __import__("asyncio").run(runtime.goal_review_worker.run_once()) is True
    today_response = client.get(f"/api/today?date={start.isoformat()}", headers={"host": "127.0.0.1:8000"})
    assert today_response.status_code == 200, today_response.text
    review = today_response.json()["programs"][0]["review"]
    assert review["status"] == "COMPLETED"
    accepted = client.post(
        f"/api/adjustments/{review['proposal']['id']}/accept",
        json={"expected_version": review["proposal"]["version"]}, headers=_headers(app, "accept-adjustment"),
    ).json()["program"]
    for action in accepted["actions"]:
        if action["status"] == "SCHEDULED":
            client.post(
                f"/api/actions/{action['id']}/complete",
                json={"expected_version": action["version"]}, headers=_headers(app, f"complete-{action['id']}"),
            ).raise_for_status()

    ready = client.get(f"/api/programs/{accepted['id']}", headers={"host": "127.0.0.1:8000"}).json()
    completed = client.post(
        f"/api/programs/{accepted['id']}/complete",
        json={"expected_version": ready["version"]}, headers=_headers(app, "complete-program"),
    ).json()
    assert completed["status"] == "COMPLETED"
    assert completed["completion_episode_id"].startswith("episode_")
    memories = client.get("/api/memories", headers={"host": "127.0.0.1:8000"}).json()
    assert any(item["id"] == completed["completion_episode_id"] for item in memories["episodes"])
