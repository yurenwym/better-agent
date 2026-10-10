"""Validated terminal capabilities; state transitions remain with the runtime."""
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .tool_contracts import ToolSpec, ToolRisk, ToolResult, ToolArgumentError


class FinishParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    output: str = Field(min_length=1)


class BlockParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(min_length=1)


class AwaitParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    observation: str = Field(min_length=1)


TERMINAL_MODELS = {"finish_step": FinishParams, "report_blocked": BlockParams, "await_outcome": AwaitParams}


def register_goal_terminals(registry):
    for name, model in TERMINAL_MODELS.items():
        def validate(params, model=model):
            try:
                model.model_validate(params)
            except ValidationError as exc:
                raise ToolArgumentError("invalid arguments") from exc
        def execute(params, context, name=name):
            if not context.owner_id or not context.run_id:
                return ToolResult(False, "缺少可信执行身份", error="CONTEXT_REQUIRED")
            return ToolResult(True, "步骤终止决定", data={"name": name, "params": params}, meta={"capability_kind": "terminal"})
        registry.register(ToolSpec(name=name, description={
            "finish_step": "步骤完成时提交可见结果。", "report_blocked": "无法继续时说明阻塞原因。",
            "await_outcome": "已执行操作，需要等待外部结果时提交观察。"}[name],
            schema=model.model_json_schema(), risk=ToolRisk.PURE,
            handler=lambda _: ToolResult(False, "需要可信执行上下文", error="CONTEXT_REQUIRED"),
            context_handler=execute, validator=validate, reject_identity_params=True))
