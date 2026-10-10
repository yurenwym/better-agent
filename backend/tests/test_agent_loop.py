from dataclasses import FrozenInstanceError
import asyncio
import copy

import pytest

from app.agent_loop import AgentProfile, CapabilityKind, CapabilityOutcome, CapabilitySet, Final, LoopGuards
from app.agent_loop import AgentLoop, Cancelled, Exhausted, LoopCallbacks, LoopInput, Suspended
from app.execution_context import create_root_context
from app.model_gateway import ModelResponse, Timing, UsageBuckets


def response(text="", calls=()):
    return ModelResponse(text, list(calls), "stop", UsageBuckets(), Timing(0, None, 0), 1)


def call(name="read", arguments='{}', identity="call-1"):
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": arguments}}


class FakeModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def complete(self, messages, tools, context, callbacks):
        self.requests.append((copy.deepcopy(messages), tools, context))
        return next(self.responses)


class FakeExecutor:
    def __init__(self, outcomes=()):
        self.outcomes = iter(outcomes)
        self.calls = []

    async def execute(self, call, *, bound_text=None):
        self.calls.append((call, bound_text))
        return next(self.outcomes, CapabilityOutcome(result={"ok": True}))


def profile(**kwargs):
    return AgentProfile("test", "conversation", "route", lambda _: [],
                        lambda _: CapabilitySet(({"type": "function", "function": {"name": "read"}},)), **kwargs)


def inputs(cancel_event=None):
    return LoopInput([{"role": "user", "content": "test"}], create_root_context(owner_id="owner"), cancel_event)


@pytest.mark.asyncio
async def test_al_t01_direct_answer():
    model, executor = FakeModel([response("answer")]), FakeExecutor()
    assert await AgentLoop(model, executor).run(profile(), inputs()) == Final("answer")
    assert len(model.requests) == 1 and not executor.calls


@pytest.mark.asyncio
async def test_al_t02_order_and_new_invocations():
    model = FakeModel([response(calls=[call(), call("second", identity="call-2")]), response("done")])
    executor = FakeExecutor()
    assert await AgentLoop(model, executor).run(profile(), inputs()) == Final("done")
    assert [c.name for c, _ in executor.calls] == ["read", "second"]
    messages = model.requests[1][0]
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == ["call-1", "call-2"]
    first, second = [r[2] for r in model.requests]
    assert first.invocation_id != second.invocation_id and first.span_id != second.span_id
    assert first.trace_id == second.trace_id and first.root_budget_id == second.root_budget_id
    assert [first.purpose, second.purpose] == ["route", "route_tool_1"]


@pytest.mark.asyncio
async def test_al_t03_pending_stops_remaining_calls():
    model = FakeModel([response(calls=[call("first"), call("second"), call("third")])])
    executor = FakeExecutor([CapabilityOutcome(result="ok"), CapabilityOutcome(kind="pending", continuation="checkpoint")])
    assert await AgentLoop(model, executor).run(profile(), inputs()) == Suspended("approval", "checkpoint")
    assert [c.name for c, _ in executor.calls] == ["first", "second"]
    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_al_t04_invalid_arguments_repeat_without_execution():
    model = FakeModel([response(calls=[call(arguments="[")]), response(calls=[call(arguments="[")]), response("failed safely")])
    executor = FakeExecutor()
    assert await AgentLoop(model, executor).run(profile(), inputs()) == Final("failed safely")
    assert not executor.calls and model.requests[-1][1] == []
    assert "INVALID_ARGUMENT" in model.requests[1][0][-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("guards,responses,outcomes,reason,count", [
    (LoopGuards(max_iterations=1), [response(calls=[call()])], [], "REACT_ITERATION_BUDGET_EXHAUSTED", 1),
    (LoopGuards(max_identical_actions=1), [response(calls=[call()]), response(calls=[call()])], [], "IDENTICAL_ACTION_BUDGET_EXHAUSTED", 2),
    (LoopGuards(), [response(calls=[call("one"), call("two")])], [CapabilityOutcome(ok=False), CapabilityOutcome(ok=False)], "CONSECUTIVE_TOOL_ERRORS_EXHAUSTED", 1),
])
async def test_al_t05_guards(guards, responses, outcomes, reason, count):
    model = FakeModel(responses)
    assert await AgentLoop(model, FakeExecutor(outcomes)).run(profile(guards=guards), inputs()) == Exhausted(reason)
    assert len(model.requests) == count


@pytest.mark.asyncio
async def test_al_t05_wall_time_cancels_inflight_model():
    stopped = asyncio.Event()

    class SlowModel:
        async def complete(self, *args):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    executor = FakeExecutor()
    assert await AgentLoop(SlowModel(), executor).run(profile(guards=LoopGuards(max_wall_time_seconds=.02)), inputs()) == Exhausted("STEP_WALL_TIME_EXHAUSTED")
    assert stopped.is_set() and not executor.calls


@pytest.mark.asyncio
async def test_al_t06_cancel_inflight():
    event, started, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowModel:
        async def complete(self, *args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    executor = FakeExecutor()
    task = asyncio.create_task(AgentLoop(SlowModel(), executor).run(profile(), inputs(event)))
    await started.wait()
    event.set()
    assert await task == Cancelled()
    assert stopped.is_set() and not executor.calls


@pytest.mark.asyncio
async def test_al_t07_reset_and_binding_exception():
    resets = []
    callbacks = LoopCallbacks(on_text_reset=lambda: resets.append(True))
    model = FakeModel([response("discard", [call()]), response("final")])
    executor = FakeExecutor()
    assert await AgentLoop(model, executor).run(profile(), inputs(), callbacks) == Final("final")
    assert resets == [True] and executor.calls[0][1] is None
    model = FakeModel([response("plan", [call("publish")])])
    executor = FakeExecutor([CapabilityOutcome(kind="bind_text", artifact="saved")])
    assert await AgentLoop(model, executor).run(profile(bind_text=frozenset({"publish"})), inputs(), callbacks) == Final("plan", "saved")
    assert resets == [True] and executor.calls[0][1] == "plan"


@pytest.mark.asyncio
async def test_mixed_binding_rejected_and_goal_text_cannot_finish():
    model = FakeModel([response("unsafe", [call("publish"), call()]), response("done")])
    executor = FakeExecutor()
    await AgentLoop(model, executor).run(profile(bind_text=frozenset({"publish"})), inputs())
    assert [c.name for c, _ in executor.calls] == ["read"]
    assert "NEED_BOUND_TEXT" in model.requests[1][0][2]["content"]
    model = FakeModel([response("not terminal")])
    assert await AgentLoop(model, FakeExecutor()).run(profile(allow_text_final=False, guards=LoopGuards(max_iterations=1)), inputs()) == Exhausted("REACT_ITERATION_BUDGET_EXHAUSTED")


def test_contracts_are_frozen_and_include_text_binding():
    profile = AgentProfile("test", "conversation", "route", lambda _: [], lambda _: CapabilitySet())
    assert profile.bind_text == frozenset()
    assert profile.guards.max_iterations == 8
    assert Final("answer", artifact={"id": "plan"}).artifact == {"id": "plan"}
    with pytest.raises(FrozenInstanceError):
        profile.name = "changed"


def test_invalid_capability_enum_and_guards_rejected():
    assert CapabilityOutcome(kind="pending").kind == CapabilityKind.PENDING
    with pytest.raises(ValueError):
        CapabilityOutcome(kind="unknown")
    with pytest.raises(ValueError):
        LoopGuards(max_iterations=0)


@pytest.mark.asyncio
async def test_error_guard_state_survives_early_stop():
    state = {}
    model = FakeModel([response(calls=[call()])])
    executor = FakeExecutor([CapabilityOutcome(ok=False, result="failed")])
    value = inputs()
    result = await AgentLoop(model, executor).run(profile(guards=LoopGuards(max_consecutive_tool_errors=1)),
        LoopInput(value.messages, value.harness, guard_state=state))
    assert result == Exhausted("CONSECUTIVE_TOOL_ERRORS_EXHAUSTED")
    assert state['consecutive_errors'] == 1 and state['failures'] == {'read:{}': 1}
