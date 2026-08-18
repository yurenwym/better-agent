import json

import httpx
import pytest


def _response(content: str) -> bytes:
    return (f"data: {json.dumps({'choices': [{'delta': {'content': content}, 'finish_reason': 'stop'}]})}\n\n"
            "data: [DONE]\n\n").encode()


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
