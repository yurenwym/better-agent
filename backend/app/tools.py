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


class ToolRisk(StrEnum):
    PURE = "PURE"
    READ = "READ"
    WRITE = "WRITE"


class ToolRejected(PermissionError):
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
    ) -> ToolResult:
        spec = self.authorize(call, run_id=run_id, skill_tools=skill_tools)
        params_hash = normalized_params_hash(call.params)
        existing = self._existing_call(call.id)
        if existing:
            if existing["run_id"] != run_id or existing["params_hash"] != params_hash:
                raise ToolRejected("tool_call_id binding changed")
            if existing["status"] == "completed":
                return ToolResult(**json.loads(existing["result_json"]))
        if spec.risk == ToolRisk.WRITE:
            if self.approval_service is None:
                raise ApprovalRequired("WRITE tool requires approval")
            self.approval_service.require_granted(run_id, call.id, call.params)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="better-agent-tool")
        future = executor.submit(spec.handler, call.params)
        try:
            result = future.result(timeout=spec.timeout_seconds)
        except FutureTimeout:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            timeout_result = ToolResult(False, "tool timed out", error="timeout", meta={"timeout_seconds": spec.timeout_seconds})
            self._record_call(call, run_id, params_hash, spec.risk, timeout_result)
            return timeout_result
        else:
            executor.shutdown(wait=True)
        if not isinstance(result, ToolResult):
            raise TypeError("tool handler must return ToolResult")
        self._record_call(call, run_id, params_hash, spec.risk, result)
        return result

    def authorize(
        self,
        call: ToolCall,
        *,
        run_id: str,
        skill_tools: set[str] | None,
    ) -> ToolSpec:
        spec = self._tools.get(call.name)
        if spec is None:
            raise ToolRejected("unknown tool")
        if skill_tools is not None and call.name not in skill_tools:
            raise ToolRejected("skill does not allow tool")
        _validate_schema(spec.schema, call.params)
        for field_name in spec.path_fields:
            self.safe_path(call.params[field_name])
        if spec.risk == ToolRisk.WRITE:
            if self.approval_service is None:
                raise ApprovalRequired("WRITE tool requires approval")
            self.approval_service.require_granted(run_id, call.id, call.params)
        return spec

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
                "INSERT OR REPLACE INTO tool_calls(id, run_id, tool_name, params_hash, risk, status, result_json, "
                "created_at, completed_at) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)",
                (call.id, run_id, call.name, params_hash, risk.value, json.dumps(result.as_dict()), now, now),
            )


def create_default_registry(
    workspace: str | Path,
    *,
    db: Database | None = None,
    approval_service: ApprovalService | None = None,
) -> ToolRegistry:
    registry = ToolRegistry(workspace, db, approval_service)
    registry.register(
        ToolSpec(
            "local_time",
            "Return the local wall-clock time.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            ToolRisk.PURE,
            lambda params: ToolResult(
                True,
                "local time",
                {"iso": datetime.now().astimezone().isoformat(), "timezone": datetime.now().astimezone().tzname()},
            ),
        )
    )
    registry.register(
        ToolSpec(
            "calculator",
            "Evaluate a small arithmetic expression.",
            {
                "type": "object",
                "required": ["expression"],
                "properties": {"expression": {"type": "string"}},
                "additionalProperties": False,
            },
            ToolRisk.PURE,
            lambda params: ToolResult(True, "calculation complete", {"value": _calculate(params["expression"])}),
        )
    )
    registry.register(
        ToolSpec(
            "read_note",
            "Read a Markdown note in the Agent workspace.",
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
            "Write a Markdown note after explicit user approval.",
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
    return registry


def _read_note(registry: ToolRegistry, params: dict[str, Any]) -> ToolResult:
    path = registry.safe_path(params["path"])
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ToolResult(False, "note not found", error="not_found", meta={"path": params["path"]})
    return ToolResult(True, "note read", {"path": params["path"], "content": content})


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
    return ToolResult(True, "note written", {"path": params["path"], "length": len(params["content"])})


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
