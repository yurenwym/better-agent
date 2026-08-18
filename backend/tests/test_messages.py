import asyncio
import json
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


class StreamingResponseModel(ResponseModel):
    def __init__(self):
        super().__init__()
        self._on_text_delta = None

    def set_text_delta_callback(self, callback):
        self._on_text_delta = callback

    async def needs_clarification(self, goal, interactions):
        assert self._on_text_delta is not None
        self._on_text_delta('{"needs_clarification":')
        return await super().needs_clarification(goal, interactions)


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


def test_streamed_model_deltas_use_one_precreated_message_and_final_replacement(tmp_path) -> None:
    runtime = make_runtime(tmp_path, StreamingResponseModel())
    run = asyncio.run(runtime.create_goal("Clarify", "Need a response"))

    asyncio.run(runtime.handle_message(run.id, "Please help me continue"))

    events = runtime.events.list(run.id)
    deltas = [event for event in events if event.type == "model.response.delta"]
    responses = [event for event in events if event.type == "model.response"]
    assert len(deltas) == 1
    assert len(responses) == 1
    assert deltas[0].seq < responses[0].seq
    assert deltas[0].data["message_id"] == responses[0].data["message_id"]
    assert deltas[0].data["delta"] == '{"needs_clarification":'
    assert deltas[0].data["content_length"] == len('{"needs_clarification":')

    with runtime.db.connection() as connection:
        rows = connection.execute(
            "SELECT role, content FROM messages WHERE run_id = ? ORDER BY created_at, id",
            (run.id,),
        ).fetchall()
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[-1]["content"] == '{"needs_clarification": true}'


def test_default_mock_model_also_creates_visible_assistant_messages(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(
        tmp_path,
        MockModelGateway(plan_steps=[{"id": "step-1", "title": "准备饮食计划"}]),
    )
    run = asyncio.run(runtime.create_goal("Diet", "Make a seven-day diet plan"))
    app = create_app(runtime=runtime)
    client = TestClient(app)

    response = client.post(
        f"/api/goals/{run.goal_id}/messages",
        json={"content": "制定一个7天的减脂饮食计划"},
        headers=_headers(app),
    )

    assert response.status_code == 200
    messages = client.get(f"/api/runs/{run.id}/messages", headers={"host": "127.0.0.1:8000"})
    payloads = [json.loads(item["content"]) for item in messages.json()["messages"] if item["role"] == "assistant"]
    assert [item["role"] for item in messages.json()["messages"]] == ["user", "assistant", "assistant"]
    assert any(item.get("needs_clarification") is False for item in payloads)
    assert any(item.get("steps") == [{"id": "step-1", "title": "准备饮食计划"}] for item in payloads)
    assert len([event for event in runtime.events.list(run.id) if event.type == "model.response"]) == 2


def test_empty_provider_message_uses_safe_decision_payload(tmp_path) -> None:
    from app.runtime import ModelDecision
    from app.tools import ToolCall

    runtime = make_runtime(tmp_path, ResponseModel())
    run = asyncio.run(runtime.create_goal("Tool", "Use a tool"))
    decision = ModelDecision.tool(ToolCall("call-1", "calculator", {"expression": "2 + 2"}), "计算结果")

    runtime._append_model_message(
        run,
        "react",
        "invocation-1",
        SimpleNamespace(message=""),
        decision,
    )

    with runtime.db.connection() as connection:
        row = connection.execute(
            "SELECT content FROM messages WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
            (run.id,),
        ).fetchone()
    payload = json.loads(row["content"])
    assert payload["summary"] == "计算结果"
    assert payload["tool_call"]["name"] == "calculator"


def test_messages_api_marks_precreated_empty_assistant_as_streaming(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    run = asyncio.run(runtime.create_goal("Stream", "Show progress"))
    with runtime.db.transaction() as connection:
        connection.execute(
            "INSERT INTO messages(id, run_id, role, content, created_at) VALUES (?, ?, 'assistant', '', ?)",
            ("message-streaming", run.id, "2026-08-18T00:00:00Z"),
        )

    response = TestClient(create_app(runtime=runtime)).get(
        f"/api/runs/{run.id}/messages",
        headers={"host": "127.0.0.1:8000"},
    )

    assert response.status_code == 200
    assert response.json()["messages"][-1]["streaming"] is True
