import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from test_runtime import make_runtime


def _headers(app):
    return {
        "host": "127.0.0.1:8000",
        "origin": "http://127.0.0.1:8000",
        "content-type": "application/json",
        "x-csrf-token": app.state.csrf_token,
    }


class ResponseModel:
    def __init__(self):
        self.last_response = None

    async def needs_clarification(self, goal, interactions):
        self.last_response = SimpleNamespace(
            message='{"needs_clarification": true}',
            attempts=1,
            timing=SimpleNamespace(ttft_seconds=0.01, decode_seconds=0.02),
            usage=SimpleNamespace(
                uncached_input_tokens=12,
                cache_read_tokens=0,
                cache_write_tokens=0,
                output_tokens=5,
                reasoning_tokens=0,
            ),
        )
        return True

    async def plan(self, goal, interactions):
        raise AssertionError("planning should not run while clarifying")

    async def decide(self, step, observation, iteration):
        raise AssertionError("react should not run while clarifying")

    async def reflect(self, goal, plan, run_id):
        raise AssertionError("reflection should not run while clarifying")


def test_model_response_is_persisted_and_exposed_by_messages_api(tmp_path) -> None:
    from app.main import create_app

    runtime = make_runtime(tmp_path, ResponseModel())
    run = asyncio.run(runtime.create_goal("Clarify", "Need a response"))
    app = create_app(runtime=runtime)
    client = TestClient(app)

    response = client.post(
        f"/api/goals/{run.goal_id}/messages",
        json={"content": "Please help me continue"},
        headers=_headers(app),
    )

    assert response.status_code == 200
    messages = client.get(f"/api/runs/{run.id}/messages", headers={"host": "127.0.0.1:8000"})
    assert messages.status_code == 200
    assert [item["role"] for item in messages.json()["messages"]] == ["user", "assistant"]
    assert messages.json()["messages"][-1]["content"] == '{"needs_clarification": true}'
    assert any(event.type == "model.response" for event in runtime.events.list(run.id))
