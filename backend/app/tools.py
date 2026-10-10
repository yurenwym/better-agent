"""Builtin tools and compatibility facade for existing registry callers.

Execution callers use .executor. Forwarders preserve direct tool integrations
and existing registry consumers; remove them when those callers migrate.
"""
from __future__ import annotations
import ast
import os
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Any
from .db import Database
from .domain import ApprovalService
from .trusted_connectors import ConnectorSecurityError, TrustedConnectorService
from .tool_contracts import (ToolSpec, ToolCall, ToolResult, ToolRisk, ToolExecutionContext,
    ToolRejected, ToolArgumentError, ToolReconciliationRequired)
from .capability_registry import CapabilityRegistry
from .tool_executor import ToolExecutor


class ToolRegistry(CapabilityRegistry):
    def __init__(self, workspace: str | Path, db: Database | None = None,
                 approval_service: ApprovalService | None = None):
        super().__init__()
        self.executor = ToolExecutor(self, workspace, db, approval_service)

    @property
    def workspace(self):
        return self.executor.workspace

    @property
    def db(self):
        return self.executor.db

    @property
    def approval_service(self):
        return self.executor.approval_service

    def risk_of(self, name, params):
        return self.executor.risk_of(name, params)

    def authorize(self, call, *, run_id, skill_tools, authorization=None):
        return self.executor.authorize(call, run_id=run_id, skill_tools=skill_tools, authorization=authorization)

    def execute(self, call, *, run_id, skill_tools, authorization=None):
        return self.executor.execute(call, run_id=run_id, skill_tools=skill_tools, authorization=authorization)

    async def execute_async(self, call, *, context, skill_tools, authorization=None):
        return await self.executor.execute_async(call, context=context, skill_tools=skill_tools, authorization=authorization)

    def safe_path(self, value):
        return self.executor.safe_path(value)


def create_default_registry(
    workspace: str | Path,
    *,
    db: Database | None = None,
    approval_service: ApprovalService | None = None,
    connectors: TrustedConnectorService | None = None,
) -> ToolRegistry:
    registry = ToolRegistry(workspace, db, approval_service)
    registry.register(
        ToolSpec(
            "local_time",
            "返回本地当前时间。",
            {"type": "object", "properties": {}, "additionalProperties": False},
            ToolRisk.PURE,
            lambda params: ToolResult(
                True,
                "本地时间",
                {"iso": datetime.now().astimezone().isoformat(), "timezone": datetime.now().astimezone().tzname()},
            ),
        )
    )
    registry.register(
        ToolSpec(
            "calculator",
            "计算一个简单算术表达式。",
            {
                "type": "object",
                "required": ["expression"],
                "properties": {"expression": {"type": "string"}},
                "additionalProperties": False,
            },
            ToolRisk.PURE,
            lambda params: ToolResult(True, "计算完成", {"value": _calculate(params["expression"])}),
        )
    )
    registry.register(
        ToolSpec(
            "read_note",
            "读取 Agent 工作区中的 Markdown 笔记。",
            {
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
            ToolRisk.READ,
            lambda params: _read_note(registry, params),
            path_fields=("path",),
        )
    )
    registry.register(
        ToolSpec(
            "write_note",
            "在用户明确批准后写入 Markdown 笔记。",
            {
                "type": "object",
                "required": ["path", "content"],
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "additionalProperties": False,
            },
            ToolRisk.WRITE,
            lambda params: _write_note(registry, params),
            path_fields=("path",),
        )
    )
    if connectors is not None:
        registry.register(
            ToolSpec(
                "trusted_connector",
                "通过项目注册并验证的可信 HTTPS 连接器访问外部服务。",
                {
                    "type": "object",
                    "required": ["connector_version_id", "method", "path"],
                    "properties": {
                        "connector_version_id": {"type": "string"},
                        "method": {"type": "string"},
                        "path": {"type": "string"},
                        "query": {"type": "object"},
                        "body": {"type": "object"},
                    },
                    "additionalProperties": False,
                },
                ToolRisk.WRITE,
                lambda params: _trusted_connector(connectors, params),
            )
        )
    return registry


def _trusted_connector(connectors: TrustedConnectorService, params: dict[str, Any]) -> ToolResult:
    try:
        result = connectors.execute(
            params["connector_version_id"], params["method"], params["path"],
            query=params.get("query"), body=params.get("body"),
        )
    except ConnectorSecurityError as exc:
        return ToolResult(False, "可信连接器请求被拒绝", error="connector_rejected", meta={"reason": str(exc)})
    return ToolResult(True, "可信连接器请求完成", result, meta={"untrusted_external_data": True})


def _read_note(registry: ToolRegistry, params: dict[str, Any]) -> ToolResult:
    path = registry.safe_path(params["path"])
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ToolResult(False, "未找到笔记", error="not_found", meta={"path": params["path"]})
    return ToolResult(True, "笔记读取完成", {"path": params["path"], "content": content})


def _write_note(registry: ToolRegistry, params: dict[str, Any]) -> ToolResult:
    path = registry.safe_path(params["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=".agent-", suffix=".tmp", delete=False
    ) as handle:
        temp_name = handle.name
        handle.write(params["content"])
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_name, path)
    return ToolResult(True, "笔记写入完成", {"path": params["path"], "length": len(params["content"])})


def _calculate(expression: str) -> int | float:
    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval_node(tree.body)
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        raise ToolRejected("calculator expression is not allowed") from exc
    if abs(value) > 10**12:
        raise ToolRejected("calculator result is too large")
    return value


def _eval_node(node: ast.AST) -> int | float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod)):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ToolRejected("exponent is too large")
        return {
            ast.Add: lambda: left + right,
            ast.Sub: lambda: left - right,
            ast.Mult: lambda: left * right,
            ast.Div: lambda: left / right,
            ast.Pow: lambda: left**right,
            ast.Mod: lambda: left % right,
        }[type(node.op)]()
    raise ToolRejected("calculator expression is not allowed")
