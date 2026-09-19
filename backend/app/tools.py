from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePath
from typing import Any, Callable

from .db import Database
from .domain import ApprovalRequired, ApprovalService, normalized_params_hash
from .trusted_connectors import ConnectorReconciliationRequired, ConnectorSecurityError, TrustedConnectorService


class ToolRisk(StrEnum):
    PURE = "PURE"
    READ = "READ"
    WRITE = "WRITE"


class ToolRejected(PermissionError):
    pass


class ToolReconciliationRequired(ToolRejected):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    params: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    artifact_ref: str | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "data": self.data,
            "artifact_ref": self.artifact_ref,
            "error": self.error,
            "meta": self.meta,
        }


Handler = Callable[[dict[str, Any]], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    risk: ToolRisk
    handler: Handler
    timeout_seconds: float = 30
    path_fields: tuple[str, ...] = ()


class ToolRegistry:
    def __init__(
        self,
        workspace: str | Path,
        db: Database | None = None,
        approval_service: ApprovalService | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db = db
        self.approval_service = approval_service
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.schema,
                },
            }
            for spec in self._tools.values()
        ]

    def execute(
        self,
        call: ToolCall,
        *,
        run_id: str,
        skill_tools: set[str] | None,
        authorization: dict[str, Any] | None = None,
    ) -> ToolResult:
        try:
            spec = self.authorize(call, run_id=run_id, skill_tools=skill_tools, authorization=authorization)
        except ToolRejected as exc:
            self._run_event(run_id, "tool.authorization.denied", {
                "tool_call_id": call.id, "tool_name": call.name, "reason": str(exc),
            })
            raise
        risk = self._risk(spec, call)
        params_hash = normalized_params_hash(call.params)
        existing = self._existing_call(call.id)
        if existing:
            if existing["run_id"] != run_id or existing["params_hash"] != params_hash:
                raise ToolRejected("tool_call_id binding changed")
            if existing["status"] == "completed":
                return ToolResult(**json.loads(existing["result_json"]))
        if risk == ToolRisk.WRITE:
            if self.approval_service is None:
                raise ApprovalRequired("WRITE tool requires approval")
            self.approval_service.require_granted(run_id, call.id, call.params, authorization)
        claimed = self._claim_execution(call, run_id, params_hash, risk)
        if isinstance(claimed, ToolResult):
            return claimed
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="better-agent-tool")
        future = executor.submit(spec.handler, call.params)
        try:
            result = future.result(timeout=spec.timeout_seconds)
        except ConnectorReconciliationRequired as exc:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            self._mark_reconciliation(call, run_id, params_hash, str(exc))
            raise ToolReconciliationRequired("tool execution requires reconciliation") from exc
        except FutureTimeout as exc:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            # A running thread cannot be cancelled; WRITE effects may still land.
            if risk == ToolRisk.WRITE:
                self._mark_reconciliation(call, run_id, params_hash, "timeout")
                raise ToolReconciliationRequired("tool execution requires reconciliation") from exc
            timeout_result = ToolResult(False, "tool timed out", error="timeout", meta={"timeout_seconds": spec.timeout_seconds})
            self._record_call(call, run_id, params_hash, risk, timeout_result)
            return timeout_result
        else:
            executor.shutdown(wait=True)
        if not isinstance(result, ToolResult):
            raise TypeError("tool handler must return ToolResult")
        self._record_call(call, run_id, params_hash, risk, result)
        return result

    def authorize(
        self,
        call: ToolCall,
        *,
        run_id: str,
        skill_tools: set[str] | None,
        authorization: dict[str, Any] | None = None,
    ) -> ToolSpec:
        spec = self._tools.get(call.name)
        if spec is None:
            raise ToolRejected("unknown tool")
        if skill_tools is not None and call.name not in skill_tools:
            raise ToolRejected("skill does not allow tool")
        _validate_schema(spec.schema, call.params)
        for field_name in spec.path_fields:
            self.safe_path(call.params[field_name])
        if self._risk(spec, call) == ToolRisk.WRITE:
            if self.approval_service is None:
                raise ApprovalRequired("WRITE tool requires approval")
            self.approval_service.require_granted(run_id, call.id, call.params, authorization)
        return spec

    @staticmethod
    def _risk(spec: ToolSpec, call: ToolCall) -> ToolRisk:
        if spec.name == "trusted_connector":
            return ToolRisk.WRITE if str(call.params.get("method", "")).upper() in {"POST", "PUT", "PATCH", "DELETE"} else ToolRisk.READ
        return spec.risk

    def safe_path(self, value: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ToolRejected("path must be a non-empty string")
        candidate = Path(value)
        if candidate.is_absolute() or os.path.splitdrive(value)[0] or ".." in PurePath(value).parts:
            raise ToolRejected("path must stay inside the workspace")
        resolved = (self.workspace / candidate).resolve(strict=False)
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ToolRejected("path escapes the workspace") from exc
        return resolved

    def _existing_call(self, call_id: str):
        if self.db is None:
            return None
        with self.db.connection() as connection:
            return connection.execute("SELECT * FROM tool_calls WHERE id = ?", (call_id,)).fetchone()

    def _claim_execution(self, call: ToolCall, run_id: str, params_hash: str, risk: ToolRisk) -> ToolResult | None:
        if self.db is None:
            return None
        now = datetime.now().astimezone().isoformat()
        logical_key = f"{run_id}:{call.id}"
        reconciliation_required = False
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM tool_execution_claims WHERE logical_action_key=?", (logical_key,)).fetchone()
            if row:
                if row["run_id"] != run_id or row["tool_name"] != call.name or row["params_hash"] != params_hash:
                    raise ToolRejected("tool execution binding changed")
                if row["status"] == "COMPLETED" and row["result_json"]:
                    return ToolResult(**json.loads(row["result_json"]))
                if row["status"] == "RECONCILIATION_REQUIRED":
                    reconciliation_required = True
                elif row["status"] == "RUNNING" and risk == ToolRisk.WRITE:
                    connection.execute("UPDATE tool_execution_claims SET status='RECONCILIATION_REQUIRED',updated_at=? WHERE logical_action_key=?", (now, logical_key))
                    self._run_event(run_id, "tool.reconciliation_required", {
                        "tool_call_id": call.id, "tool_name": call.name,
                    }, connection=connection)
                    reconciliation_required = True
                else:
                    raise ToolRejected("tool execution already running")
            else:
                connection.execute(
                    "INSERT INTO tool_execution_claims(logical_action_key,run_id,tool_call_id,tool_name,params_hash,status,created_at,updated_at) VALUES (?,?,?,?,?,'RUNNING',?,?)",
                    (logical_key, run_id, call.id, call.name, params_hash, now, now),
                )
        if reconciliation_required:
            raise ToolReconciliationRequired("tool execution requires reconciliation")
        return None

    def _mark_reconciliation(self, call: ToolCall, run_id: str, params_hash: str, error: str) -> None:
        if self.db is None:
            return
        now = datetime.now().astimezone().isoformat()
        with self.db.transaction() as connection:
            changed = connection.execute(
                "UPDATE tool_execution_claims SET status='RECONCILIATION_REQUIRED',error_code=?,updated_at=? "
                "WHERE logical_action_key=? AND run_id=? AND params_hash=? AND status<>'RECONCILIATION_REQUIRED'",
                (error, now, f"{run_id}:{call.id}", run_id, params_hash),
            ).rowcount
            if changed:
                self._run_event(run_id, "tool.reconciliation_required", {
                    "tool_call_id": call.id, "tool_name": call.name,
                }, connection=connection)

    def _run_event(self, run_id: str, event_type: str, data: dict[str, Any], *, connection=None) -> None:
        if self.db is None:
            return
        def append(active) -> None:
            row = active.execute("SELECT goal_id FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is not None:
                from .events import EventStore
                EventStore(self.db).append(run_id, row["goal_id"], event_type, "runtime", data, connection=active)
        if connection is not None:
            append(connection)
        else:
            with self.db.transaction() as active:
                append(active)

    def _record_call(
        self,
        call: ToolCall,
        run_id: str,
        params_hash: str,
        risk: ToolRisk,
        result: ToolResult,
    ) -> None:
        if self.db is None:
            return
        now = datetime.now().astimezone().isoformat()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO tool_calls(id, run_id, tool_name, params_hash, risk, status, result_json, "
                "created_at, completed_at) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id,tool_name=excluded.tool_name,"
                "params_hash=excluded.params_hash,risk=excluded.risk,status=excluded.status,"
                "result_json=excluded.result_json,created_at=excluded.created_at,completed_at=excluded.completed_at",
                (call.id, run_id, call.name, params_hash, risk.value, json.dumps(result.as_dict()), now, now),
            )
            connection.execute(
                "UPDATE tool_execution_claims SET status=?,result_json=?,error_code=?,updated_at=?,completed_at=? WHERE logical_action_key=?",
                ("COMPLETED" if result.ok else "FAILED", json.dumps(result.as_dict()), result.error, now, now, f"{run_id}:{call.id}"),
            )


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


def _validate_schema(schema: dict[str, Any], params: dict[str, Any]) -> None:
    if schema.get("type") == "object" and not isinstance(params, dict):
        raise ToolRejected("tool parameters must be an object")
    for required in schema.get("required", []):
        if required not in params:
            raise ToolRejected(f"missing parameter: {required}")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unknown = set(params) - set(properties)
        if unknown:
            raise ToolRejected("unknown tool parameter")
    for name, value in params.items():
        expected = properties.get(name, {}).get("type")
        if expected == "string" and not isinstance(value, str):
            raise ToolRejected(f"parameter {name} must be a string")
