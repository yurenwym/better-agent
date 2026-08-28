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
    assert "准确贴合用户目标" in seen[0]["messages"][0]["content"]
    assert "交付物" in seen[0]["messages"][0]["content"]
    assert "不要为了补充假设" in seen[1]["messages"][0]["content"]
    assert "用户可见结果" in seen[1]["messages"][0]["content"]


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
    assert "基于明确假设先给出第一版" in seen[0]["messages"][0]["content"]
    assert "不要先询问个人信息" in seen[0]["messages"][0]["content"]


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
async def test_live_runtime_model_forwards_cancel_event_to_gateway() -> None:
    from app.live_model import LiveRuntimeModel
    from app.model_gateway import GatewayError

    cancel_event = asyncio.Event()

    class CancelAwareGateway:
        async def complete(self, request, **kwargs):
            assert kwargs["cancel_event"] is cancel_event
            raise GatewayError("model request cancelled", "cancelled")

    model = LiveRuntimeModel(CancelAwareGateway())
    cancel_event.set()
    token = model.set_cancel_event(cancel_event)
    try:
        with pytest.raises(GatewayError, match="cancelled"):
            await model._json("Return JSON", {"label": "cancel"})
    finally:
        model.reset_cancel_event(token)


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


@pytest.mark.asyncio
async def test_live_conversation_model_returns_valid_ask_request_from_tool_call() -> None:
    from app.ask import ASK_TOOL_SCHEMA
    from app.live_model import LiveConversationModel

    class AskGateway:
        def __init__(self) -> None:
            self.requests = []

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            return SimpleNamespace(
                message="",
                tool_calls=[{
                    "id": "call-ask-1",
                    "function": {
                        "name": "ask_user",
                        "arguments": json.dumps({
                            "questions": [{
                                "id": "training_level",
                                "header": "训练水平",
                                "question": "你目前的训练水平是什么？",
                                "options": [
                                    {"label": "新手", "description": "刚开始训练"},
                                    {"label": "有基础", "description": "已有训练习惯"},
                                ],
                                "multi_select": False,
                                "allow_free_text": True,
                            }]
                        }, ensure_ascii=False),
                    },
                }],
            )

    gateway = AskGateway()
    result = await LiveConversationModel(gateway).route_and_respond(
        content="travel plan",
        history=[],
        skill_names=[],
        on_text_delta=lambda _: None,
        on_text_reset=lambda: None,
        cancel_event=asyncio.Event(),
    )

    assert result.call_id == "call-ask-1"
    assert result.questions[0].id == "training_level"
    assert gateway.requests[0].tools == [ASK_TOOL_SCHEMA]


@pytest.mark.asyncio
async def test_routed_conversation_regenerates_ask_with_the_dedicated_role() -> None:
    from app.ask import ASK_TOOL_SCHEMA
    from app.live_model import LiveConversationModel

    def call(call_id: str, question: str):
        return {"id": call_id, "function": {"name": "ask_user", "arguments": json.dumps({"questions": [{
            "id": "context", "header": "关键信息", "question": question, "options": [],
            "multi_select": False, "allow_free_text": True,
        }]}, ensure_ascii=False)}}

    class RoutedAskGateway:
        supports_role_routing = True
        def __init__(self): self.requests = []
        async def complete(self, request, **_kwargs):
            self.requests.append(request)
            return SimpleNamespace(message="", tool_calls=[call(
                "ask-final" if request.role == "ask" else "ask-draft",
                "你希望优先解决哪一项？" if request.role == "ask" else "draft",
            )])

    gateway = RoutedAskGateway()
    result = await LiveConversationModel(gateway).route_and_respond(
        content="帮我制定长期成长计划", history=[], skill_names=[],
        on_text_delta=lambda _: None, on_text_reset=lambda: None, cancel_event=asyncio.Event(),
    )

    assert result.call_id == "ask-final"
    assert result.questions[0].question == "你希望优先解决哪一项？"
    assert [request.role for request in gateway.requests[:2]] == ["conversation", "ask"]
    assert sum(request.role == "ask" for request in gateway.requests) == 1
    assert gateway.requests[1].tools == [ASK_TOOL_SCHEMA]


@pytest.mark.asyncio
async def test_live_conversation_model_keeps_streaming_a_direct_answer() -> None:
    from app.live_model import LiveConversationModel

    message = '{"v":1,"policy":"answer","content_shape":"guide","reason_code":"content_only"}\n# Answer'

    class DirectAnswerGateway:
        async def complete(self, request, **kwargs):
            kwargs["on_text_delta"](message)
            return SimpleNamespace(message=message, tool_calls=[])

    deltas: list[str] = []
    resets: list[bool] = []
    response = await LiveConversationModel(DirectAnswerGateway()).route_and_respond(
        content="给我一份广西攻略",
        history=[],
        skill_names=[],
        on_text_delta=deltas.append,
        on_text_reset=lambda: resets.append(True),
        cancel_event=asyncio.Event(),
    )

    assert response.message == message
    assert deltas == [message]
    assert resets == []


@pytest.mark.asyncio
async def test_live_conversation_model_lets_llm_choose_ask_questions_for_personalized_plan() -> None:
    from app.ask import ASK_TOOL_SCHEMA
    from app.live_model import LiveConversationModel

    class AskGateway:
        def __init__(self) -> None:
            self.requests = []

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            return SimpleNamespace(
                message="",
                tool_calls=[{
                    "id": "model-ask-1",
                    "function": {
                        "name": "ask_user",
                        "arguments": json.dumps({
                            "questions": [{
                                "id": "riding_experience",
                                "header": "骑行经历",
                                "question": "你过去通常能连续骑行多长时间？",
                                "options": [
                                    {"label": "没有稳定经验", "description": "还没有形成固定骑行习惯"},
                                    {"label": "可以完成短途", "description": "能够完成一小时左右的骑行"},
                                ],
                                "multi_select": False,
                                "allow_free_text": True,
                            }, {
                                "id": "weekly_availability",
                                "header": "每周时间",
                                "question": "你每周大约可以安排几天训练？",
                                "options": [
                                    {"label": "1–2 天", "description": "优先建立稳定习惯"},
                                    {"label": "3–4 天", "description": "可以进行规律训练"},
                                ],
                                "multi_select": False,
                                "allow_free_text": True,
                            }]
                        }, ensure_ascii=False),
                    },
                }],
            )

    gateway = AskGateway()
    result = await LiveConversationModel(gateway).route_and_respond(
        content="我想制作一个长期的训练计划，学习骑行",
        history=[],
        skill_names=[],
        on_text_delta=lambda _: None,
        on_text_reset=lambda: None,
        cancel_event=asyncio.Event(),
    )

    assert result.call_id == "model-ask-1"
    assert [question.id for question in result.questions] == [
        "riding_experience",
        "weekly_availability",
    ]
    assert gateway.requests[0].tools == [ASK_TOOL_SCHEMA]
    assert gateway.requests[0].temperature == 0
    assert gateway.requests[0].messages[-1]["content"] == "我想制作一个长期的训练计划，学习骑行"
    prompt = gateway.requests[0].messages[0]["content"].lower()
    assert "ask_user" in prompt
    assert "训练教程、方案、日程或习惯计划" in prompt
    assert "用户准备亲自遵循" in prompt
    assert "通用解释还是个人计划" in prompt
    assert "必须先调用 ask_user" in prompt
    assert "已经提供相关背景" in prompt


@pytest.mark.asyncio
async def test_explicit_sourced_deep_research_routes_without_waiting_for_classifier() -> None:
    from app.conversation import ControlHeadDecoder
    from app.live_model import LiveConversationModel
    class NeverCalled:
        async def complete(self,*args,**kwargs):raise AssertionError("explicit research must not call the model classifier")
    chunks=[];model=LiveConversationModel(NeverCalled());model.memory_store=object()
    response=await model.route_and_respond(
        content="请深度研究 SQLite WAL 是否适合作为本地 Agent 的存储方案，给出带引用来源的完整报告",
        history=[],skill_names=[],on_text_delta=chunks.append,on_text_reset=None,cancel_event=None,
    )
    decoder=ControlHeadDecoder();decoder.feed("".join(chunks));header=decoder.finish()
    assert header.policy=="start_research" and header.research_scope=="web"
    assert "SQLite WAL" in (header.research_topic or "")


def test_research_shortcut_distinguishes_a_new_command_from_prior_research_context() -> None:
    from app.live_model import _is_explicit_research_command

    assert _is_explicit_research_command("请深度研究 SQLite WAL，并给出带来源的报告")
    assert not _is_explicit_research_command("基于刚才的深度研究，生成计划并保存到计划中")
