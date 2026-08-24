import httpx
import pytest

from app.agents import LiveExpertModel
from app.model_gateway import ModelGateway, ModelProfile


@pytest.mark.asyncio
async def test_live_expert_model_uses_no_tools_and_validates_structured_result(monkeypatch):
    monkeypatch.setenv("EXPERT_KEY", "x")
    requests = []
    def handler(request):
        payload = __import__("json").loads(request.content); requests.append(payload)
        data = '{"summary":"可行","findings":[{"text":"证据","confidence":0.8,"source_refs":[]}],"risks":[],"open_questions":[]}'
        return httpx.Response(200, content=(f'data: '+__import__("json").dumps({"choices":[{"delta":{"content":data},"finish_reason":"stop"}]})+'\n\ndata: [DONE]\n\n').encode())
    gateway = ModelGateway(ModelProfile("https://provider.test/v1","demo","EXPERT_KEY",max_attempts=1,network_retries=0), transport=httpx.MockTransport(handler))
    result = await LiveExpertModel(gateway).execute("critic", "检查计划", {"user":"data"}, [])
    assert result["summary"] == "可行" and result["findings"][0]["confidence"] == .8
    assert requests[0].get("tools") == []


@pytest.mark.asyncio
async def test_live_expert_model_applies_the_pinned_bundle_prompt(monkeypatch):
    monkeypatch.setenv("EXPERT_KEY", "x")
    requests = []
    def handler(request):
        requests.append(__import__("json").loads(request.content))
        data = '{"summary":"ok","findings":[],"risks":[],"open_questions":[]}'
        return httpx.Response(200, content=(f'data: '+__import__("json").dumps({"choices":[{"delta":{"content":data},"finish_reason":"stop"}]})+'\n\ndata: [DONE]\n\n').encode())
    gateway = ModelGateway(ModelProfile("https://provider.test/v1","demo","EXPERT_KEY",max_attempts=1,network_retries=0), transport=httpx.MockTransport(handler))
    await LiveExpertModel(gateway).execute_bundle("planner", "goal", {}, [], {"prompts":"candidate-v2"})
    assert "candidate-v2" in requests[0]["messages"][0]["content"]
