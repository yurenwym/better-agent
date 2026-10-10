from types import SimpleNamespace

import pytest

from app.conversation import ManagedTurnWorker, RouteProtocolError


def worker():
    instance = object.__new__(ManagedTurnWorker)
    instance.conversation = SimpleNamespace(agent_runtime=SimpleNamespace())
    return instance


@pytest.mark.parametrize("kind", ["research", "expert"])
def test_handoff_rejects_incomplete_context_before_services(kind):
    instance = worker()
    with pytest.raises(Exception, match="incomplete history"):
        if kind == "research":
            instance.handoff_start_research(None, "topic", "web", context_incomplete=True)
        else:
            instance.handoff_start_expert(None, "topic", ("critic",), content="topic", history=[], plan_context=None, context_incomplete=True)


@pytest.mark.parametrize("kind", ["research", "expert"])
def test_handoff_rejects_visible_body_and_missing_service(kind):
    instance = worker()
    turn = SimpleNamespace(goal_action_id=None)
    def execute(**kwargs):
        if kind == "research":
            return instance.handoff_start_research(turn, "topic", "web", **kwargs)
        return instance.handoff_start_expert(turn, "topic", ("critic",), content="topic", history=[], plan_context=None, **kwargs)
    with pytest.raises(RouteProtocolError, match="visible body"):
        execute(pending="already streamed")
    with pytest.raises(RuntimeError, match="not configured"):
        execute()


def test_action_help_cannot_dispatch_experts():
    with pytest.raises(RouteProtocolError, match="action help"):
        worker().handoff_start_expert(SimpleNamespace(goal_action_id="action"), "topic", ("critic",), content="topic", history=[], plan_context=None)
