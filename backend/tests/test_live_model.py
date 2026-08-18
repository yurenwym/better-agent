import json
import asyncio
from types import SimpleNamespace

import httpx
import pytest


class ConcurrentGateway:
    async def complete(self, request, **kwargs):
        payload = json.loads(request.messages[1]["content"])
        label = payload["label"]
        await asyncio.sleep(0)
        kwargs["on_text_delta"](label)
        return SimpleNamespace(message=json.dumps({"label": label}), tool_calls=[])


class RepairGateway:
    def __init__(self):
        self.calls = 0

    async def complete(self, request, **kwargs):
        self.calls += 1
        if self.calls == 1:
            kwargs["on_text_delta"]("{\"summary\":\"old")
            return SimpleNamespace(message="not valid JSON", tool_calls=[])
        kwargs["on_text_delta"]("{\"summary\":\"new\"}")
        return SimpleNamespace(message='{"summary":"new"}', tool_calls=[])


def _response(content: str) -> bytes:
    return (f"data: {json.dumps({'choices': [{'delta': {'content': content}, 'finish_reason': 'stop'}]})}\n\n"
            "data: [DONE]\n\n").encode()


def _tool_response(name: str = "calculator", arguments: str = '{"expression":"2 + 2"}') -> bytes:
    chunks = [
        {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call-1",
                        "function": {"name": name, "arguments": arguments},
                    }],
                },
                "finish_reason": None,
            }]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks).encode() + b"data: [DONE]\n\n"


def _tool_batch_response() -> bytes:
    chunk = {
        "choices": [{
            "delta": {
                "tool_calls": [
                    {"index": 0, "id": "call-1", "function": {"name": "read_note", "arguments": '{"path":"user.md"}'}},
                    {"index": 1, "id": "call-2", "function": {"name": "local_time", "arguments": "{}"}},
                ],
            },
            "finish_reason": "tool_calls",
        }]
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()


@pytest.mark.asyncio
async def test_live_runtime_model_parses_structured_plan_and_decision(monkeypatch) -> None:
    from app.live_model import LiveRuntimeModel
    from app.model_gateway import ModelGateway, ModelProfile

    monkeypatch.setenv("LIVE_MODEL_KEY", "configured")
    seen: list[dict] = []
    responses = iter([
        _response('{"summary":"Ship","steps":[{"id":"step-1","title":"Draft","description":"Write it"}]}'),
        _response('{"action":"complete_step","summary":"done"}'),
    ])

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=next(responses))

    model = LiveRuntimeModel(ModelGateway(ModelProfile("https://provider.test", "demo", "LIVE_MODEL_KEY"), transport=httpx.MockTransport(handler)))

    plan = await model.plan({"title": "Ship", "description": ""}, ["Ship"])
    decision = await model.decide({"id": "step-1", "title": "Draft"}, "", 1)

    assert plan.steps[0]["title"] == "Draft"
    assert decision.action == "complete_step"
    assert "tailored" in seen[0]["messages"][0]["content"].lower()
    assert "deliverable" in seen[0]["messages"][0]["content"].lower()
    assert "do not call tools just to fill assumptions" in seen[1]["messages"][0]["content"].lower()
    assert "visible answer" in seen[1]["messages"][0]["content"].lower()


@pytest.mark.asyncio
async def test_live_runtime_model_includes_context_snapshot_in_request(monkeypatch) -> None:
    from app.live_model import LiveRuntimeModel
    from app.model_gateway import ModelGateway, ModelProfile

    monkeypatch.setenv("LIVE_MODEL_KEY", "configured")
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=_response('{"needs_clarification":false}'))

    model = LiveRuntimeModel(ModelGateway(ModelProfile("https://provider.test", "demo", "LIVE_MODEL_KEY"), transport=httpx.MockTransport(handler)))
    model.set_context_snapshot("snapshot-hash", "[security]\nconfirmed memory")

    await model.needs_clarification({"title": "Ship"}, ["Ship"])

    assert seen[0]["messages"][1]["content"]
    assert "snapshot-hash" in seen[0]["messages"][1]["content"]
    assert "confirmed memory" in seen[0]["messages"][1]["content"]
    assert "assumption-based first version" in seen[0]["messages"][0]["content"].lower()
    assert "do not ask for personal details" in seen[0]["messages"][0]["content"].lower()


@pytest.mark.asyncio
async def test_live_runtime_model_uses_output_as_visible_step_result(monkeypatch) -> None:
    from app.live_model import LiveRuntimeModel
    from app.model_gateway import ModelGateway, ModelProfile

    monkeypatch.setenv("LIVE_MODEL_KEY", "configured")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_response(
            '{"action":"complete_step","step_id":"step-1",'
            '"output":"先按基础代谢估算热量，再给出一周菜单。"}'
        ))

    model = LiveRuntimeModel(
        ModelGateway(
            ModelProfile("https://provider.test", "demo", "LIVE_MODEL_KEY"),
            transport=httpx.MockTransport(handler),
        )
    )

    decision = await model.decide(
        {"id": "step-1", "title": "生成菜单"},
        "",
        1,
    )

    assert decision.action == "complete_step"
    assert decision.summary == "先按基础代谢估算热量，再给出一周菜单。"


@pytest.mark.asyncio
async def test_live_runtime_model_adapts_one_streamed_tool_call(monkeypatch) -> None:
    from app.live_model import LiveRuntimeModel
    from app.model_gateway import ModelGateway, ModelProfile

    monkeypatch.setenv("LIVE_MODEL_KEY", "configured")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_tool_response())

    model = LiveRuntimeModel(
        ModelGateway(
            ModelProfile("https://provider.test", "demo", "LIVE_MODEL_KEY"),
            transport=httpx.MockTransport(handler),
        ),
        tool_schemas=[{"type": "function", "function": {"name": "calculator"}}],
    )

    decision = await model.decide({"id": "step-1", "title": "计算"}, "", 1)

    assert decision.action == "tool_call"
    assert decision.tool_call is not None
    assert decision.tool_call.name == "calculator"
    assert decision.tool_call.params == {"expression": "2 + 2"}


@pytest.mark.asyncio
async def test_live_runtime_model_serializes_provider_tool_batch(monkeypatch) -> None:
    from app.live_model import LiveRuntimeModel
    from app.model_gateway import ModelGateway, ModelProfile

    monkeypatch.setenv("LIVE_MODEL_KEY", "configured")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_tool_batch_response())

    model = LiveRuntimeModel(
        ModelGateway(
            ModelProfile("https://provider.test", "demo", "LIVE_MODEL_KEY"),
            transport=httpx.MockTransport(handler),
        ),
        tool_schemas=[{"type": "function", "function": {"name": "read_note"}}],
    )

    decision = await model.decide({"id": "step-1", "title": "读取"}, "", 1)

    assert decision.action == "tool_call"
    assert decision.tool_call is not None
    assert decision.tool_call.id == "call-1"
    assert decision.tool_call.name == "read_note"


@pytest.mark.asyncio
async def test_live_runtime_model_discards_invalid_reflection_scopes(monkeypatch) -> None:
    from app.live_model import LiveRuntimeModel
    from app.model_gateway import ModelGateway, ModelProfile

    monkeypatch.setenv("LIVE_MODEL_KEY", "configured")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_response(json.dumps({
            "candidates": [
                {"kind": "preference", "content": "Prefer concise plans", "scope": "global", "confidence": 0.8, "evidence_event_ids": []},
                {"kind": "preference", "content": "Nutrition topic", "scope": "nutrition", "confidence": 0.8, "evidence_event_ids": []},
            ]
        })))

    model = LiveRuntimeModel(
        ModelGateway(
            ModelProfile("https://provider.test", "demo", "LIVE_MODEL_KEY"),
            transport=httpx.MockTransport(handler),
        )
    )

    candidates = await model.reflect({}, object(), "run-1")

    assert candidates == [{
        "kind": "preference",
        "content": "Prefer concise plans",
        "scope": "global",
        "confidence": 0.8,
        "evidence_event_ids": [],
    }]


@pytest.mark.asyncio
async def test_live_runtime_model_keeps_stream_callbacks_isolated_per_task() -> None:
    from app.live_model import LiveRuntimeModel

    model = LiveRuntimeModel(ConcurrentGateway())
    seen = {"A": [], "B": []}

    async def call(label: str) -> None:
        token = model.set_text_delta_callback(lambda delta: seen[label].append(delta))
        try:
            payload = await model._json("Return JSON", {"label": label})
            assert payload["label"] == label
        finally:
            model.reset_text_delta_callback(token)

    await asyncio.gather(call("A"), call("B"))

    assert seen == {"A": ["A"], "B": ["B"]}


@pytest.mark.asyncio
async def test_live_runtime_model_resets_partial_output_before_json_repair() -> None:
    from app.live_model import LiveRuntimeModel

    model = LiveRuntimeModel(RepairGateway())
    deltas: list[str] = []
    resets: list[str] = []
    delta_token = model.set_text_delta_callback(deltas.append)
    reset_token = model.set_text_reset_callback(lambda: resets.append("reset"))
    try:
        payload = await model._json("Return JSON", {"label": "repair"})
    finally:
        model.reset_text_reset_callback(reset_token)
        model.reset_text_delta_callback(delta_token)

    assert payload == {"summary": "new"}
    assert deltas == ['{"summary":"old', '{"summary":"new"}']
    assert resets == ["reset"]
