import json
import asyncio
from types import SimpleNamespace

import httpx
import pytest


def test_conversation_clock_is_available_in_preflight_and_send(monkeypatch):
    from datetime import datetime, timezone
    from app import live_model

    class FrozenClock:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 20, 23, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(live_model, "datetime", FrozenClock)
    model = live_model.LiveConversationModel(object())
    kwargs = dict(content="明天开始，Asia/Shanghai", history=[], human_mode=False, branch_state=None)
    preflight = model.prepare_mandatory_request(**kwargs)
    send = model._conversation_messages(**kwargs)
    assert preflight[0] == send[0]
    assert "2026-09-20T23:30:00+00:00" in send[0]["content"]
    assert "先换算到用户明确指定的时区" in send[0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["payment", "authentication", "configuration", "budget"])
@pytest.mark.parametrize("classifier", ["research", "plan", "remember"])
async def test_permanent_classifier_error_is_not_a_negative_intent(kind, classifier):
    from app.live_model import LiveConversationModel
    from app.model_gateway import GatewayError
    class Gateway:
        supports_intent_classification = True
        async def complete(self, *args, **kwargs):
            raise GatewayError("stopped", kind)
    model = LiveConversationModel(Gateway())
    with pytest.raises(GatewayError) as caught:
        if classifier == "research":
            await model._classify_explicit_research_request("question", None)
        elif classifier == "plan":
            await model._classify_explicit_plan_document_request("question", [], None)
        else:
            await model._classify_explicit_remember("请记住我的偏好", None)
    assert caught.value.kind == kind


@pytest.mark.asyncio
@pytest.mark.parametrize("indent", [None, 2])
async def test_expert_header_at_eof_is_forwarded_without_fallback(indent):
    from app.live_model import LiveConversationModel
    from app.conversation import ControlHeadDecoder

    header = json.dumps({"v": 4, "policy": "start_expert", "content_shape": "expert",
                         "reason_code": "explicit_collaboration",
                         "expert": {"objective": "review", "roles": ["critic"]}}, indent=indent)
    class Gateway:
        async def complete(self, request, **kwargs):
            kwargs["on_text_delta"](header)
            return SimpleNamespace(message=header, tool_calls=[])

    chunks = []
    result = await LiveConversationModel(Gateway()).route_and_respond(
        content="协作审阅", history=[], skill_names=[], on_text_delta=chunks.append,
        on_text_reset=chunks.clear, cancel_event=asyncio.Event(),
    )
    decoder = ControlHeadDecoder()
    decoder.feed("".join(chunks))
    assert decoder.finish().policy == "start_expert"
    assert json.loads(result.message) == json.loads(header)
    assert result.message.endswith("\n")


@pytest.mark.asyncio
async def test_explicit_expert_command_routes_deterministically_without_model_call():
    from app.live_model import LiveConversationModel
    from app.conversation import ControlHeadDecoder

    class Gateway:
        async def complete(self, *_args, **_kwargs):
            raise AssertionError("explicit expert routing must not call the model")

    chunks = []
    result = await LiveConversationModel(Gateway()).route_and_respond(
        content="请调用 researcher、planner、critic 三个专家协作检查方案。",
        history=[], skill_names=[], on_text_delta=chunks.append,
        on_text_reset=chunks.clear, cancel_event=asyncio.Event(),
    )

    decoder = ControlHeadDecoder()
    decoder.feed("".join(chunks))
    header = decoder.finish()
    assert header.policy == "start_expert"
    assert header.expert_roles == ("researcher", "planner", "critic")
    assert header.expert_objective == "请调用 researcher、planner、critic 三个专家协作检查方案。"
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_stream_buffer_is_used_when_final_response_message_differs():
    from app.live_model import LiveConversationModel
    header = '{"v":4,"policy":"start_expert","content_shape":"expert","reason_code":"ok","expert":{"objective":"review","roles":["critic"]}}'
    class Gateway:
        async def complete(self, request, **kwargs):
            kwargs["on_text_delta"](header)
            return SimpleNamespace(message="", tool_calls=[])
    result = await LiveConversationModel(Gateway()).route_and_respond(
        content="协作", history=[], skill_names=[], on_text_delta=lambda _: None,
        on_text_reset=lambda: None, cancel_event=asyncio.Event(),
    )
    assert result.message.endswith("\n") and '"policy":"start_expert"' in result.message


class ConcurrentGateway:
    async def complete(self, request, **kwargs):
        payload = json.loads(request.messages[1]["content"])
        label = payload["label"]
        await asyncio.sleep(0)
        kwargs["on_text_delta"](label)
        return SimpleNamespace(message=json.dumps({"label": label}), tool_calls=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("independent", [True, False, 1, 1.0, "true", None, [], {}])
async def test_incomplete_context_uses_read_only_independent_path(independent):
    from app.live_model import LiveConversationModel
    from app.memory_archive import ArchiveUnavailable
    class Gateway:
        def __init__(self): self.calls=[]
        async def complete(self, request, **kwargs):
            self.calls.append(request)
            assert request.tools == []
            assert not any("PRIVATE HISTORY" in message["content"] for message in request.messages)
            if request.purpose == "classify_context_dependency":
                return SimpleNamespace(message=json.dumps({"independent":independent}))
            return SimpleNamespace(message="A hash table maps keys to values.",tool_calls=[])
    gateway=Gateway(); model=LiveConversationModel(gateway); chunks=[]
    call=model.answer_without_history(content="What is a hash table?",history=[{"role":"user","content":"PRIVATE HISTORY"}],on_text_delta=chunks.append,cancel_event=asyncio.Event())
    if independent is True:
        await call
        assert "历史上下文不完整" in "".join(chunks)
        assert len(gateway.calls) == 2
    else:
        with pytest.raises(ArchiveUnavailable): await call
        assert not chunks
        assert len(gateway.calls) == 1


@pytest.mark.parametrize("prefix", [
    "v=1\npolicy=answer\n\n",
    "v = 2\r\npolicy = answer\r\ncontent_shape = text\r\nreason_code = content_only\r\n\r\n",
])
def test_plain_answer_strips_key_value_control_head(prefix):
    from app.live_model import _wrap_plain_answer

    body = "# Answer\n\nUse a vector index."
    response = _wrap_plain_answer(SimpleNamespace(message=prefix + body, tool_calls=[]))
    assert response.message.split("\n", 1)[1] == body


@pytest.mark.parametrize("body", [
    "v=1\nThis is a variable example.",
    "policy=answer\nThis is a setting.",
    "v=9\npolicy=answer\nUnknown version example.",
    "v=1\npolicy=custom\nCustom setting example.",
    "Example:\nv=1\npolicy=answer",
    "```python\nv=1\npolicy=answer\n```",
])
def test_plain_answer_preserves_non_header_content(body):
    from app.live_model import _wrap_plain_answer

    response = _wrap_plain_answer(SimpleNamespace(message=body, tool_calls=[]))
    assert response.message.split("\n", 1)[1] == body


def test_plain_answer_rejects_key_value_header_without_body():
    from app.live_model import _wrap_plain_answer
    from app.model_gateway import GatewayError

    with pytest.raises(GatewayError, match="no usable answer"):
        _wrap_plain_answer(SimpleNamespace(message="v=1\npolicy=answer", tool_calls=[]))


def test_confirmed_plan_repair_normalizes_markdown_without_provider_declaration():
    from app.conversation import ControlHeadDecoder
    from app.live_model import _wrap_confirmed_plan_markdown

    repaired = _wrap_confirmed_plan_markdown(SimpleNamespace(
        message="这里是按确认信息生成的计划：\n\n# 14 天训练计划\n\n- 每周 3 天\n",
        tool_calls=[],
    ))
    decoder = ControlHeadDecoder()
    visible = decoder.feed(repaired.message)
    decision = decoder.finish()

    assert decision.artifact is not None
    assert decision.artifact.title == "14 天训练计划"
    assert visible.startswith("# 14 天训练计划")


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

    sent = json.loads(seen[0]["messages"][1]["content"])
    assert sent == {"context_snapshot": {
        "hash": "snapshot-hash", "text": "[security]\nconfirmed memory",
    }}
    assert "goal" not in sent
    assert "interactions" not in sent
    assert "基于明确假设先给出第一版" in seen[0]["messages"][0]["content"]
    assert "不要先询问个人信息" in seen[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_live_runtime_model_enforces_gateway_budget_before_call() -> None:
    from app.live_model import LiveRuntimeModel
    from app.model_gateway import GatewayError

    class TinyGateway:
        calls = 0

        def input_limit(self):
            return 100

        async def complete(self, request, **kwargs):
            self.calls += 1
            raise AssertionError("over-budget request must not reach the gateway")

    gateway = TinyGateway()
    model = LiveRuntimeModel(gateway)
    model.set_context_snapshot("hash", "x" * 1000)

    with pytest.raises(GatewayError) as caught:
        await model.needs_clarification({"title": "Ship"}, ["Ship"])

    assert caught.value.kind == "context_overflow"
    assert gateway.calls == 0


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
                {"kind": "preference", "content": "Prefer concise plans", "scope": "global", "confidence": 0.8, "evidence_event_ids": ["message-1"]},
                {"kind": "preference", "content": "Nutrition topic", "scope": "nutrition", "confidence": 0.8, "evidence_event_ids": ["message-1"]},
            ]
        })))

    model = LiveRuntimeModel(
        ModelGateway(
            ModelProfile("https://provider.test", "demo", "LIVE_MODEL_KEY"),
            transport=httpx.MockTransport(handler),
        )
    )

    candidates = await model.reflect({}, object(), "run-1", [{
        "ref": "message-1",
        "source_type": "thread_message",
        "excerpt": "Please keep plans concise",
        "turn_id": "turn-1",
    }])

    assert candidates == [{
        "kind": "preference",
        "content": "Prefer concise plans",
        "scope": "global",
        "confidence": 0.8,
        "evidence_event_ids": ["message-1"],
    }]


@pytest.mark.asyncio
async def test_live_runtime_model_requires_catalog_evidence_and_rejects_unknown_refs() -> None:
    from app.live_model import LiveRuntimeModel

    class Gateway:
        calls = 0

        async def complete(self, request, **kwargs):
            self.calls += 1
            return SimpleNamespace(message=json.dumps({"candidates": [{
                "kind": "preference",
                "content": "Prefer concise plans",
                "scope": "global",
                "confidence": .9,
                "evidence_event_ids": ["made-up-message"],
            }]}), tool_calls=[])

    gateway = Gateway()
    model = LiveRuntimeModel(gateway)
    assert await model.reflect({}, object(), "run-1") == []
    assert gateway.calls == 0
    assert await model.reflect({}, object(), "run-1", [{
        "ref": "message-1", "source_type": "thread_message",
        "excerpt": "Please keep plans concise", "turn_id": "turn-1",
    }]) == []
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_live_runtime_model_normalizes_a_low_risk_habit() -> None:
    from app.live_model import LiveRuntimeModel

    class Gateway:
        async def complete(self, request, **kwargs):
            return SimpleNamespace(message=json.dumps({"candidates": [{
                "kind": "habit",
                "content": "制定计划时不要安排超过四十五分钟的单项任务",
                "scope": "global",
                "confidence": .8,
                "evidence_event_ids": ["message-1"],
            }]}), tool_calls=[])

    candidates = await LiveRuntimeModel(Gateway()).reflect({}, object(), "run-1", [{
        "ref": "message-1", "source_type": "thread_message",
        "excerpt": "以后单项任务不要超过四十五分钟", "turn_id": "turn-1",
    }])
    assert candidates[0]["kind"] == "constraint"


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
    from app.ask import ASK_TOOL_SCHEMA, REVIEW_TOOL_SCHEMA
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
    # 对话暴露澄清（ask_user）与复盘（review_check_in）两个工具。
    assert gateway.requests[0].tools == [ASK_TOOL_SCHEMA, REVIEW_TOOL_SCHEMA]


@pytest.mark.asyncio
async def test_routed_conversation_regenerates_ask_with_the_dedicated_role() -> None:
    from app.ask import ASK_TOOL_SCHEMA, REVIEW_TOOL_SCHEMA
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
    assert gateway.requests[1].tools == [ASK_TOOL_SCHEMA, REVIEW_TOOL_SCHEMA]


@pytest.mark.asyncio
async def test_routed_conversation_recovers_an_empty_draft_with_the_dedicated_role() -> None:
    from app.ask import AskRequest
    from app.live_model import LiveConversationModel

    class EmptyDraftGateway:
        supports_role_routing = True

        def __init__(self):
            self.requests = []

        async def complete(self, request, **_kwargs):
            self.requests.append(request)
            if request.role == "ask":
                arguments = json.dumps({"questions": [{
                    "id": "daily_time",
                    "header": "每日投入",
                    "question": "你每天可以投入多长时间？",
                    "options": [],
                    "multi_select": False,
                    "allow_free_text": True,
                }]}, ensure_ascii=False)
                return SimpleNamespace(message="", tool_calls=[{
                    "id": "ask-final",
                    "function": {"name": "ask_user", "arguments": arguments},
                }])
            return SimpleNamespace(message="", tool_calls=[{
                "id": "ask-empty",
                "function": {"name": "ask_user", "arguments": '{"questions":[]}'},
            }])

    gateway = EmptyDraftGateway()
    result = await LiveConversationModel(gateway).route_and_respond(
        content="帮我制定数据库学习安排", history=[], skill_names=[],
        on_text_delta=lambda _: None, on_text_reset=lambda: None,
        cancel_event=asyncio.Event(),
    )

    assert isinstance(result, AskRequest)
    assert result.call_id == "ask-final"
    assert result.questions[0].id == "daily_time"
    assert [request.role for request in gateway.requests[:2]] == ["conversation", "ask"]


@pytest.mark.asyncio
async def test_explicit_plan_document_keeps_a_valid_requested_ask() -> None:
    from app.ask import AskRequest
    from app.live_model import LiveConversationModel

    class Gateway:
        supports_role_routing = True

        def __init__(self):
            self.requests = []

        async def complete(self, request, **_kwargs):
            self.requests.append(request)
            arguments = json.dumps({"questions": [{
                "id": "days",
                "header": "训练频率",
                "question": "每周可训练几天？",
                "options": [],
                "multi_select": False,
                "allow_free_text": True,
            }]}, ensure_ascii=False)
            return SimpleNamespace(message="", tool_calls=[{
                "id": "ask-final" if request.role == "ask" else "ask-draft",
                "function": {"name": "ask_user", "arguments": arguments},
            }])

    gateway = Gateway()
    result = await LiveConversationModel(gateway).route_and_respond(
        content="请创建计划文档，但先询问我每周可训练几天。",
        history=[], skill_names=[], on_text_delta=lambda _: None,
        on_text_reset=lambda: None, cancel_event=asyncio.Event(),
    )

    assert isinstance(result, AskRequest)
    assert result.questions[0].id == "days"
    assert [request.role for request in gateway.requests] == ["conversation", "ask"]
    assert all(request.thinking is False for request in gateway.requests)


@pytest.mark.asyncio
async def test_explicit_plan_document_boundaries_skip_intent_classifiers() -> None:
    from app.live_model import LiveConversationModel

    prior_plan = "# 14 天训练计划\n\n- 每周 3 天\n- 每次 30 分钟\n"

    class Gateway:
        supports_intent_classification = True

        def __init__(self):
            self.requests = []

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            if "不要修改" in request.messages[-1]["content"]:
                message = (
                    '{"v":1,"policy":"answer","content_shape":"plan",'
                    '"reason_code":"constraint_only"}\n已确认每周 2 天。'
                )
            else:
                message = (
                    '{"v":2,"policy":"answer","content_shape":"plan_document",'
                    '"reason_code":"explicit_plan_save","artifact":{"kind":"plan_document",'
                    '"operation":"upsert","title":"14 天训练计划"}}\n'
                    '# 14 天训练计划\n\n- 每周 2 天\n- 每次 30 分钟\n'
                )
            callback = kwargs.get("on_text_delta")
            if callback is not None:
                callback(message)
            return SimpleNamespace(message=message, tool_calls=[], finish_reason="stop")

    gateway = Gateway()
    model = LiveConversationModel(gateway)
    history = [{"role": "assistant", "content": prior_plan}]
    await model.route_and_respond(
        content="不要修改、创建或保存计划文档，只确认每周2天。",
        history=history, skill_names=[], on_text_delta=lambda _: None,
        on_text_reset=lambda: None, cancel_event=asyncio.Event(),
    )
    await model.route_and_respond(
        content="修改并保存现有计划文档：改为每周2天。不要提问。",
        history=history, skill_names=[], on_text_delta=lambda _: None,
        on_text_reset=lambda: None, cancel_event=asyncio.Event(),
    )

    assert [request.purpose for request in gateway.requests] == [
        "route_and_respond", "route_and_respond",
    ]


@pytest.mark.asyncio
async def test_answered_ask_parent_authorizes_returned_plan_artifact_without_reclassification() -> None:
    from app.live_model import LiveConversationModel

    message = (
        '{"v":2,"policy":"answer","content_shape":"plan_document",'
        '"reason_code":"explicit_plan_save","artifact":{"kind":"plan_document",'
        '"operation":"upsert","title":"14 天训练计划"}}\n'
        '# 14 天训练计划\n\n- 每周 3 天\n- 每次 30 分钟\n'
    )

    class Gateway:
        supports_intent_classification = True

        def __init__(self):
            self.requests = []

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            assert request.purpose == "route_and_respond"
            kwargs["on_text_delta"](message)
            return SimpleNamespace(message=message, tool_calls=[], finish_reason="stop")

    gateway = Gateway()
    response = await LiveConversationModel(gateway).route_and_respond(
        content="训练水平：新手",
        ask_parent_request="请创建并保存一份14天训练计划文档。开始前先询问训练频率。",
        history=[], skill_names=[], on_text_delta=lambda _: None,
        on_text_reset=lambda: None, cancel_event=asyncio.Event(),
    )

    assert response.message == message
    assert [request.purpose for request in gateway.requests] == ["route_and_respond"]


@pytest.mark.asyncio
async def test_routed_conversation_uses_a_relevant_fallback_when_both_asks_are_invalid() -> None:
    from app.ask import AskRequest
    from app.live_model import LiveConversationModel

    class InvalidAskGateway:
        supports_role_routing = True

        async def complete(self, request, **_kwargs):
            arguments = '{"questions":[]}' if request.role == "conversation" else '{"questions":'
            return SimpleNamespace(message="", tool_calls=[{
                "id": "ask-invalid",
                "function": {"name": "ask_user", "arguments": arguments},
            }])

    result = await LiveConversationModel(InvalidAskGateway()).route_and_respond(
        content="帮我制定 PostgreSQL 和 pgvector 学习安排", history=[], skill_names=[],
        on_text_delta=lambda _: None, on_text_reset=lambda: None,
        cancel_event=asyncio.Event(),
    )

    assert isinstance(result, AskRequest)
    assert result.call_id.startswith("ask-fallback-")
    assert len(result.questions) == 1
    assert "PostgreSQL 和 pgvector" in result.questions[0].question
    assert result.questions[0].allow_free_text is True


@pytest.mark.asyncio
async def test_routed_conversation_keeps_valid_draft_when_dedicated_ask_is_invalid() -> None:
    from app.live_model import LiveConversationModel

    draft_call = {
        "id": "ask-draft",
        "function": {
            "name": "ask_user",
            "arguments": json.dumps({"questions": [{
                "id": "focus",
                "header": "学习重点",
                "question": "你想优先学习具身智能的哪个方向？",
                "options": [],
                "multi_select": False,
                "allow_free_text": True,
            }]}, ensure_ascii=False),
        },
    }

    class InvalidDedicatedAskGateway:
        supports_role_routing = True

        async def complete(self, request, **_kwargs):
            if request.role == "ask":
                return SimpleNamespace(message="", tool_calls=[{
                    "id": "ask-invalid",
                    "function": {"name": "ask_user", "arguments": '{"questions":'},
                }])
            return SimpleNamespace(message="", tool_calls=[draft_call])

    result = await LiveConversationModel(InvalidDedicatedAskGateway()).route_and_respond(
        content="继续", history=[], skill_names=[],
        on_text_delta=lambda _: None, on_text_reset=lambda: None,
        cancel_event=asyncio.Event(),
    )

    assert result.call_id == "ask-draft"
    assert result.questions[0].question == "你想优先学习具身智能的哪个方向？"


@pytest.mark.asyncio
async def test_concurrent_conversations_survive_one_invalid_dedicated_ask() -> None:
    from app.ask import AskRequest
    from app.live_model import LiveConversationModel

    class ConcurrentMixedGateway:
        supports_role_routing = True

        async def complete(self, request, **kwargs):
            await asyncio.sleep(0)
            if request.role == "ask":
                return SimpleNamespace(message="", tool_calls=[{
                    "id": "ask-invalid",
                    "function": {"name": "ask_user", "arguments": '{"questions":'},
                }])
            content = request.messages[-1]["content"]
            if content == "什么是 Java":
                message = '{"v":1,"policy":"answer","content_shape":"text","reason_code":"done"}\nJava answer'
                kwargs["on_text_delta"](message)
                return SimpleNamespace(message=message, tool_calls=[])
            return SimpleNamespace(message="", tool_calls=[{
                "id": "ask-draft",
                "function": {
                    "name": "ask_user",
                    "arguments": json.dumps({"questions": [{
                        "id": "focus", "header": "学习重点",
                        "question": "你想优先学习具身智能的哪个方向？",
                        "options": [], "multi_select": False, "allow_free_text": True,
                    }]}, ensure_ascii=False),
                },
            }])

    model = LiveConversationModel(ConcurrentMixedGateway())

    async def respond(content: str):
        return await model.route_and_respond(
            content=content, history=[], skill_names=[],
            on_text_delta=lambda _: None, on_text_reset=lambda: None,
            cancel_event=asyncio.Event(),
        )

    direct, clarification = await asyncio.gather(
        respond("什么是 Java"), respond("继续"),
    )

    assert "Java answer" in direct.message
    assert isinstance(clarification, AskRequest)
    assert clarification.call_id == "ask-draft"


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
async def test_live_conversation_accepts_strict_content_encoded_ask_tool_call() -> None:
    from app.ask import AskRequest
    from app.live_model import LiveConversationModel

    message = json.dumps({
        "v": 1,
        "policy": "clarify",
        "content_shape": "text",
        "reason_code": "need_personalization",
        "ask_user": {"questions": [{
            "id": "time",
            "header": "每日时间",
            "question": "你每天可以投入多久？",
            "options": [
                {"label": "1 小时", "description": "每天稳定投入一小时"},
                {"label": "2 小时", "description": "每天稳定投入两小时"},
            ],
            "multi_select": False,
            "allow_free_text": True,
        }]},
    }, ensure_ascii=False)

    class ContentAskGateway:
        async def complete(self, request, **kwargs):
            kwargs["on_text_delta"](message)
            return SimpleNamespace(message=message, tool_calls=[])

    resets: list[bool] = []
    result = await LiveConversationModel(ContentAskGateway()).route_and_respond(
        content="帮我制定学习计划", history=[], skill_names=[],
        on_text_delta=lambda _: None, on_text_reset=lambda: resets.append(True),
        cancel_event=asyncio.Event(),
    )

    assert isinstance(result, AskRequest)
    assert result.questions[0].id == "time"
    assert resets == [True]


@pytest.mark.asyncio
async def test_live_conversation_does_not_audit_memory_dropped_by_final_budget() -> None:
    from app.live_model import LiveConversationModel

    class BoundedGateway:
        def __init__(self):
            self.requests = []

        def input_limit(self, **kwargs):
            return 30_000

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            message = '{"v":1,"policy":"answer","content_shape":"text","reason_code":"done"}\ndone'
            if kwargs.get("on_text_delta"):
                kwargs["on_text_delta"](message)
            return SimpleNamespace(message=message, tool_calls=[])

    gateway = BoundedGateway()
    memory_content = "confirmed-memory:" + ("x" * 100_000)
    applied = []
    await LiveConversationModel(gateway).route_and_respond(
        content="answer this",
        history=[{
            "role": "system", "content": memory_content,
            "_context_required": False, "_context_priority": 10,
            "_context_group": "memory-context",
        }],
        skill_names=[], memory_context_content=memory_content,
        on_memory_context_applied=lambda: applied.append(True),
        on_text_delta=lambda _: None, on_text_reset=lambda: None,
        cancel_event=asyncio.Event(),
    )

    assert all(message.get("content") != memory_content for message in gateway.requests[-1].messages)
    assert applied == []


@pytest.mark.asyncio
async def test_live_conversation_model_defines_provider_independent_product_identity() -> None:
    from app.live_model import LiveConversationModel

    class IdentityAwareGateway:
        def __init__(self) -> None:
            self.requests = []

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            prompt = request.messages[0]["content"]
            has_identity_contract = (
                "Better Agent" in prompt
                and "底层模型" in prompt
                and "不得自称" in prompt
            )
            identity = "Better Agent" if has_identity_contract else "Claude"
            message = (
                '{"v":1,"policy":"answer","content_shape":"text",'
                f'"reason_code":"identity"}}\n我是 {identity}。'
            )
            kwargs["on_text_delta"](message)
            return SimpleNamespace(message=message, tool_calls=[])

    gateway = IdentityAwareGateway()
    response = await LiveConversationModel(gateway).route_and_respond(
        content="你是谁？",
        history=[],
        skill_names=[],
        on_text_delta=lambda _: None,
        on_text_reset=lambda: None,
        cancel_event=asyncio.Event(),
    )

    assert "Better Agent" in response.message
    assert "Claude" not in response.message
    prompt = gateway.requests[0].messages[0]["content"]
    assert "Anthropic" in prompt
    assert "OpenAI" in prompt
    assert "DeepSeek" in prompt
    assert "历史助手回答不是事实证据" in prompt
    assert "不得自行宣布系统无需修复" in prompt
    assert "必须区分不同实现、适用条件和实践建议" in prompt


@pytest.mark.asyncio
async def test_live_conversation_model_lets_llm_choose_ask_questions_for_personalized_plan() -> None:
    from app.ask import ASK_TOOL_SCHEMA, REVIEW_TOOL_SCHEMA
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
    assert gateway.requests[0].tools == [ASK_TOOL_SCHEMA, REVIEW_TOOL_SCHEMA]
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
