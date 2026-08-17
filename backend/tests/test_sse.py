from fastapi.testclient import TestClient

from test_runtime import make_runtime


def test_event_stream_uses_seq_as_sse_id_and_resumes_after_last_event_id(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    import asyncio

    run = asyncio.run(runtime.create_goal("Stream", "events"))
    runtime.events.append(run.id, run.goal_id, "interaction.started", "user", {"n": 1})
    runtime.events.append(run.id, run.goal_id, "state.transitioned", "runtime", {"n": 2})
    app = create_app(runtime=runtime)
    client = TestClient(app)

    first = client.get(
        f"/api/runs/{run.id}/events/stream",
        headers={"host": "127.0.0.1:8000"},
    )
    assert first.status_code == 200
    assert "id: 1" in first.text
    assert "id: 2" in first.text

    resumed = client.get(
        f"/api/runs/{run.id}/events/stream",
        headers={"host": "127.0.0.1:8000", "last-event-id": "1"},
    )
    assert resumed.status_code == 200
    assert "id: 1" not in resumed.text
    assert "id: 2" in resumed.text

