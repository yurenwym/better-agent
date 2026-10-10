import pytest

from app.goal_capabilities import TERMINAL_MODELS, register_goal_terminals
from app.tools import ToolCall, ToolExecutionContext, ToolRegistry, ToolRejected


@pytest.mark.asyncio
@pytest.mark.parametrize("name,param", [("finish_step", "output"), ("report_blocked", "reason"), ("await_outcome", "observation")])
async def test_goal_terminal_validation_and_mapping(tmp_path, name, param):
    registry = ToolRegistry(tmp_path)
    register_goal_terminals(registry)
    params = {param: "synthetic result"}
    assert set(s.name for s in registry.specs()) == set(TERMINAL_MODELS)
    context = ToolExecutionContext("owner", "run", "call")
    result = await registry.execute_async(ToolCall("call", name, params), context=context, skill_tools=None)
    assert result.ok and result.data == {"name": name, "params": params}
    assert result.meta["capability_kind"] == "terminal"
    for bad in ({**params, "owner_id": "foreign"}, {param: ""}):
        with pytest.raises(ToolRejected):
            registry.authorize(ToolCall("bad", name, bad), run_id="run", skill_tools=None)
