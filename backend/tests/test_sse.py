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
    runtime._set_run_fields(run.id, state="COMPLETED")
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


def test_event_stream_waits_for_events_created_after_connection(tmp_path) -> None:
    from app.api import _event_stream
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    import asyncio

    run = asyncio.run(runtime.create_goal("Stream", "events"))
    runtime.events.append(run.id, run.goal_id, "interaction.started", "user", {"n": 1})
    class RequestStub:
        async def is_disconnected(self):
            return False

    async def collect_until_second_event():
        chunks = []
        async for chunk in _event_stream(runtime, run.id, RequestStub(), 1, True):
            chunks.append(chunk)
            if '"seq": 2' in chunk:
                return chunks
        return chunks

    async def exercise():
        task = asyncio.create_task(collect_until_second_event())
        await asyncio.sleep(0)
        runtime.events.append(run.id, run.goal_id, "state.transitioned", "runtime", {"n": 2})
        chunks = await asyncio.wait_for(task, 1)
        assert any('"seq": 2' in chunk for chunk in chunks)

    asyncio.run(exercise())

