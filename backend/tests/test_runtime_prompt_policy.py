from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.goal_program_compiler import GoalProgramCompiler
from app.live_model import LiveConversationModel, LiveRuntimeModel
from app.research.live import LiveResearchModel
from app.research.models import ResearchLimits


class Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def complete(self, request, **_kwargs):
        self.requests.append(request)
        return SimpleNamespace(message=self.responses.pop(0), tool_calls=[], attempts=1)


def test_approved_runtime_prompt_is_injected_into_all_main_models():
    policy = {"improvement": "keep responses bounded"}

    conversation_gateway = Gateway(['{"v":1,"policy":"answer","content_shape":"text","reason_code":"ok"}\nDone'])
    conversation = LiveConversationModel(conversation_gateway)
    conversation.runtime_prompt_policy = lambda: policy
    asyncio.run(conversation.route_and_respond(
        content="hello", history=[], skill_names=[], on_text_delta=None, on_text_reset=None,
        cancel_event=asyncio.Event(),
    ))
    assert "keep responses bounded" in conversation_gateway.requests[-1].messages[0]["content"]

    runtime_gateway = Gateway([json.dumps({"needs_clarification": False})])
    runtime = LiveRuntimeModel(runtime_gateway)
    runtime.runtime_prompt_policy = lambda: policy
    asyncio.run(runtime.needs_clarification({"title": "goal"}, []))
    assert "keep responses bounded" in runtime_gateway.requests[0].messages[0]["content"]

    research_gateway = Gateway([json.dumps({"title": "T", "sections": ["A", "B"], "queries": ["A", "B"]})])
    research = LiveResearchModel(research_gateway)
    research.runtime_prompt_policy = lambda: policy
    asyncio.run(research.plan("topic", ResearchLimits()))
    assert "keep responses bounded" in research_gateway.requests[0].messages[0]["content"]

    compiler_gateway = Gateway([json.dumps({
        "summary": "good", "encouragement": "continue", "needs_adjustment": False, "adjustment_reason": "",
    })])
    compiler = GoalProgramCompiler(compiler_gateway)
    compiler.runtime_prompt_policy = lambda: policy
    asyncio.run(compiler.review({"signals": []}))
    assert "keep responses bounded" in compiler_gateway.requests[0].messages[0]["content"]
