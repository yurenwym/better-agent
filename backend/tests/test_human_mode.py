from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.conversation import ControlHeadDecoder, RouteProtocolError
from app.live_model import LiveConversationModel
from app.model_gateway import ModelResponse, Timing, UsageBuckets


class Gateway:
    def __init__(self): self.requests = []
    async def complete(self, request, **kwargs):
        self.requests.append(request)
        if "start_research" in request.messages[0]["content"]:
            return ModelResponse('{"start_research":false,"topic":""}', [], "stop", UsageBuckets(), Timing(0,0,1), 1)
        message = '{"v":1,"policy":"answer","content_shape":"text","reason_code":"direct"}\n你好[[next]]继续聊'
        callback = kwargs.get("on_text_delta")
        if callback: callback(message)
        return ModelResponse(message, [], "stop", UsageBuckets(), Timing(0, 0, 1), 1)


@pytest.mark.asyncio
async def test_human_mode_prompt_is_visible_body_only() -> None:
    gateway = Gateway()
    settings = SimpleNamespace(get=lambda: SimpleNamespace(human_mode=True))
    model = LiveConversationModel(gateway, settings)
    await model.route_and_respond(content="你好", history=[], skill_names=[], on_text_delta=None, on_text_reset=None, cancel_event=None)
    system = "\n".join(message["content"] for message in gateway.requests[-1].messages if message["role"] == "system")
    assert "user-facing text after" in system
    assert "Never apply it to control JSON" in system


def test_control_v3_accepts_only_valid_start_research() -> None:
    decoder = ControlHeadDecoder()
    assert decoder.feed('{"v":3,"policy":"start_research","content_shape":"research","reason_code":"explicit_deep_research","research":{"topic":"SQLite","scope":"web"}}\n') == ""
    assert decoder.finish().research_topic == "SQLite"
    with pytest.raises(RouteProtocolError):
        ControlHeadDecoder().feed('{"v":3,"policy":"answer","content_shape":"text","reason_code":"x","research":{"topic":"x","scope":"web"}}\n')
