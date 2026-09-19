import httpx
import pytest

from app.agents import LiveExpertModel
from app.model_gateway import ModelGateway, ModelProfile


@pytest.mark.asyncio
async def test_user_creation_uses_delivery_contract_not_evidence_only_audit():
    from types import SimpleNamespace
    class Gateway:
        async def complete(self, request):
            system = request.messages[0]["content"]
            assert "产出可执行的计划草案" in system
            assert "上述角色合同是输出范围的最高优先级" not in system
            assert "仅核算" not in system
            return SimpleNamespace(message='{"summary":"7天计划","findings":[],"risks":[],"open_questions":[]}',finish_reason="stop")
    result = await LiveExpertModel(Gateway()).execute("planner", "制定一个7天的骑车计划", {"task_mode":"user_task"}, [])
    assert result["summary"] == "7天计划"


@pytest.mark.asyncio
async def test_user_synthesis_requires_deliverable_without_invented_refs():
    from types import SimpleNamespace
    class Gateway:
        async def complete(self, request):
            assert "完整、清晰、可用" in request.messages[0]["content"]
            assert "不要求用户提供内部source_ref" in request.messages[0]["content"]
            return SimpleNamespace(message="## 7天计划\n第1天：轻松骑行", finish_reason="stop")
    result = await LiveExpertModel(Gateway()).synthesize_user_task("制定计划", [], [])
    assert "第1天" in result


def test_role_specific_system_prompts_are_distinct():
    from app.agents import expert_system_prompt

    prompts = {role: expert_system_prompt({}, role) for role in ("researcher", "planner", "critic")}
    assert len(set(prompts.values())) == 3
    assert "逐项核对来源" in prompts["researcher"]
    assert "核算时间预算" in prompts["planner"]
    assert "寻找反例" in prompts["critic"]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["researcher", "planner", "critic"])
async def test_expert_request_carries_role_contract_and_original_constraints(role):
    import json
    from types import SimpleNamespace

    class Gateway:
        async def complete(self, request):
            system = request.messages[0]["content"]
            expected = {
                "researcher": ("原始主张", "证据核验"),
                "planner": ("两小时=120分钟", "计算过程", "前置依赖"),
                "critic": ("单位换算", "失败模式", "错误执行承诺"),
            }
            assert all(contract in system for contract in expected[role])
            assert "上述角色合同是输出范围的最高优先级" in system
            assert "不编造来源" in system
            assert "角色总合同" not in system
            data = json.loads(request.messages[1]["content"])
            assert data["role"] == role
            assert data["context"]["request"] == "仅使用给定资料，预算两小时"
            assert role in request.messages[-1]["content"]
            assert request.messages[-1]["role"] == "user"
            assert request.tools == []
            assert request.purpose == f"expert_{role}"
            assert request.response_format == {"type": "json_object"}
            return SimpleNamespace(
                message='{"summary":"ok","findings":[],"risks":[],"open_questions":[]}',
                finish_reason="stop",
            )

    await LiveExpertModel(Gateway()).execute(
        role, "概括目标", {"request": "仅使用给定资料，预算两小时"}, [],
    )


@pytest.mark.asyncio
async def test_live_expert_model_uses_no_tools_and_validates_structured_result(monkeypatch):
    monkeypatch.setenv("EXPERT_KEY", "x")
    requests = []
    def handler(request):
        payload = __import__("json").loads(request.content); requests.append(payload)
        data = '{"summary":"可行","findings":[{"text":"证据","confidence":0.8,"source_refs":[]}],"risks":[],"open_questions":[]}'
        return httpx.Response(200, content=(f'data: '+__import__("json").dumps({"choices":[{"delta":{"content":data},"finish_reason":"stop"}]})+'\n\ndata: [DONE]\n\n').encode())
    gateway = ModelGateway(ModelProfile("https://provider.test/v1","demo","EXPERT_KEY",provider_name="deepseek",max_attempts=1,network_retries=0), transport=httpx.MockTransport(handler))
    result = await LiveExpertModel(gateway, thinking=False).execute("critic", "检查计划", {"user":"data"}, [])
    assert result["summary"] == "可行" and result["findings"][0]["confidence"] == .8
    assert requests[0].get("tools") == []
    assert requests[0]["max_tokens"] == 4096
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_coordinator_requires_claim_local_source_refs():
    from types import SimpleNamespace

    class Gateway:
        async def complete(self, request):
            system = request.messages[0]["content"]
            final = request.messages[-1]["content"]
            assert "每个关键事实、数字、依赖和冲突" in system
            assert "不得只在文末笼统罗列来源" in system
            assert "每一项旁明确写出对应 source_ref" in final
            assert request.messages[-1]["role"] == "user"
            assert request.tools == []
            assert request.purpose == "synthesize_experts"
            return SimpleNamespace(message="S1：结论。", finish_reason="stop")

    result = await LiveExpertModel(Gateway()).synthesize("目标", [], [])
    assert result == "S1：结论。"


@pytest.mark.asyncio
async def test_expert_rejects_truncated_output():
    from types import SimpleNamespace
    from app.model_gateway import GatewayError
    class Gateway:
        async def complete(self, request):
            return SimpleNamespace(message='{"summary":"partial"}', finish_reason="length")
    with pytest.raises(GatewayError, match="truncated"):
        await LiveExpertModel(Gateway()).execute("critic", "Review", {}, [])


@pytest.mark.asyncio
async def test_safety_judge_budget_and_strict_label():
    from types import SimpleNamespace
    from app.evolution import LiveSafetyJudge
    class Gateway:
        async def complete(self, request):
            assert request.max_tokens == 1024
            return SimpleNamespace(message="safe")
    assert await LiveSafetyJudge(Gateway()).judge({"summary":"Read-only advice"}) is True


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
