"""One execution pipeline for approval, claims, recovery and handler calls."""
from __future__ import annotations
import asyncio
import inspect
import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any
from contextvars import copy_context
from .send_authority import assert_send_authority
from .db import Database
from .domain import ApprovalRequired, ApprovalService, normalized_params_hash
from .execution_outcome import Effect
from .policy_engine import PolicyAction, PolicyInput, PolicyReason, decide
from .trusted_connectors import ConnectorReconciliationRequired
from .tool_contracts import (ToolCall, ToolSpec, ToolResult, ToolRisk, ToolExecutionContext,
    ToolRejected, ToolArgumentError, ToolReconciliationRequired, IDENTITY_PARAMETER_NAMES)
from .capability_registry import CapabilityRegistry


class ToolExecutor:
    def __init__(self, registry: CapabilityRegistry, workspace: str | Path,
                 db: Database | None = None, approval_service: ApprovalService | None = None):
        self.registry = registry
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db = db
        self.approval_service = approval_service

    def risk_of(self, name: str, params: dict[str, Any]) -> ToolRisk:
        return self._risk(self.registry.spec(name), ToolCall(id="", name=name, params=params))

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
        assert_send_authority()
        claimed = self._claim_execution(call, run_id, params_hash, risk)
        if isinstance(claimed, ToolResult):
            return claimed
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="better-agent-tool")
        def invoke():
            assert_send_authority()
            return spec.handler(call.params)

        future = executor.submit(copy_context().run, invoke)
        try:
            result = future.result(timeout=spec.timeout_seconds)
        except ConnectorReconciliationRequired as exc:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            self._mark_reconciliation(call, run_id, params_hash, str(exc))
            raise ToolReconciliationRequired(operation_ref=call.id) from exc
        except FutureTimeout as exc:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            # A running thread cannot be cancelled; WRITE effects may still land.
            if risk == ToolRisk.WRITE:
                self._mark_reconciliation(call, run_id, params_hash, "timeout")
                raise ToolReconciliationRequired(operation_ref=call.id) from exc
            timeout_result = ToolResult(False, "tool timed out", error="timeout", meta={"timeout_seconds": spec.timeout_seconds})
            self._record_call(call, run_id, params_hash, risk, timeout_result)
            return timeout_result
        except Exception as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            if risk == ToolRisk.WRITE:
                self._mark_reconciliation(call, run_id, params_hash, "handler_failure")
                raise ToolReconciliationRequired(operation_ref=call.id) from exc
            raise
        else:
            executor.shutdown(wait=True)
        if not isinstance(result, ToolResult):
            if risk == ToolRisk.WRITE:
                self._mark_reconciliation(call, run_id, params_hash, "invalid_handler_result")
                raise ToolReconciliationRequired(operation_ref=call.id)
            raise TypeError("tool handler must return ToolResult")
        if risk == ToolRisk.WRITE and not result.ok and result.effect != Effect.NOT_STARTED:
            self._mark_reconciliation(call, run_id, params_hash, "unconfirmed_write_result")
            raise ToolReconciliationRequired(operation_ref=call.id)
        self._record_call(call, run_id, params_hash, risk, result)
        return result

    async def execute_async(
        self,
        call: ToolCall,
        *,
        context: ToolExecutionContext,
        skill_tools: set[str] | None,
        authorization: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Awaitable execution path for context-aware (possibly async) tools.

        Shares authorization, approval, execution claims, persistence and
        timeout/reconciliation with the synchronous :meth:`execute`. Sync
        handlers keep running in a worker thread; async context handlers are
        awaited directly, so a model-backed compile can use the existing
        gateway lifecycle instead of a nested ``asyncio.run``.
        """
        context = replace(context, authorization=authorization)
        run_id = context.run_id
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
        assert_send_authority()
        try:
            claimed = self._claim_execution(call, run_id, params_hash, risk)
        except ToolReconciliationRequired:
            if spec.recover_result is None:
                raise
            if inspect.iscoroutinefunction(spec.recover_result):
                # An MCP receipt lookup is a real remote read, so it is awaited
                # under the same deadline as the original call.
                recovered = await asyncio.wait_for(
                    spec.recover_result(call.params, context), timeout=spec.timeout_seconds,
                )
            else:
                recovered = await asyncio.to_thread(spec.recover_result, call.params, context)
            if recovered is None or not recovered.ok:
                raise
            self._record_call(call, run_id, params_hash, risk, recovered)
            return recovered
        if isinstance(claimed, ToolResult):
            return claimed
        async def invoke():
            if spec.context_handler is not None and inspect.iscoroutinefunction(spec.context_handler):
                assert_send_authority()
                return await spec.context_handler(call.params, context)

            def invoke_sync():
                assert_send_authority()
                return spec.context_handler(call.params, context) if spec.context_handler else spec.handler(call.params)

            return await asyncio.to_thread(invoke_sync)

        try:
            result = await asyncio.wait_for(invoke(), timeout=spec.timeout_seconds)
        except (ConnectorReconciliationRequired, ToolReconciliationRequired) as exc:
            # A handler may already know its effect is unverifiable - an MCP
            # write whose connection dropped, for example. Persist the claim as
            # RECONCILIATION_REQUIRED instead of leaving it RUNNING.
            self._mark_reconciliation(call, run_id, params_hash, str(exc))
            raise ToolReconciliationRequired(operation_ref=call.id) from exc
        except asyncio.TimeoutError as exc:
            if risk == ToolRisk.WRITE:
                self._mark_reconciliation(call, run_id, params_hash, "timeout")
                raise ToolReconciliationRequired(operation_ref=call.id) from exc
            timeout_result = ToolResult(False, "tool timed out", error="timeout", meta={"timeout_seconds": spec.timeout_seconds})
            self._record_call(call, run_id, params_hash, risk, timeout_result)
            return timeout_result
        except asyncio.CancelledError:
            if risk == ToolRisk.WRITE:
                self._mark_reconciliation(call, run_id, params_hash, "cancelled")
            raise
        except Exception as exc:
            if risk == ToolRisk.WRITE:
                self._mark_reconciliation(call, run_id, params_hash, "handler_failure")
                raise ToolReconciliationRequired(operation_ref=call.id) from exc
            raise
        if not isinstance(result, ToolResult):
            if risk == ToolRisk.WRITE:
                self._mark_reconciliation(call, run_id, params_hash, "invalid_handler_result")
                raise ToolReconciliationRequired(operation_ref=call.id)
            raise TypeError("tool handler must return ToolResult")
        if risk == ToolRisk.WRITE and not result.ok and result.effect != Effect.NOT_STARTED:
            self._mark_reconciliation(call, run_id, params_hash, "unconfirmed_write_result")
            raise ToolReconciliationRequired(operation_ref=call.id)
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
        spec = self.registry.find(call.name)
        supplied = IDENTITY_PARAMETER_NAMES & set(call.params) if spec and spec.reject_identity_params else set()
        facts = PolicyInput(registered=spec is not None,
            allowed=skill_tools is None or call.name in skill_tools, identity_valid=not supplied)
        decision = decide(facts)
        if decision.action == PolicyAction.DENY:
            messages = {
                PolicyReason.UNKNOWN_CAPABILITY: "unknown tool",
                PolicyReason.CAPABILITY_NOT_ALLOWED: "skill does not allow tool",
                PolicyReason.IDENTITY_INVALID: "identity parameters are injected by the harness: " + ",".join(sorted(supplied)),
            }
            raise ToolRejected(messages[decision.reason])
        _validate_schema(spec.schema, call.params)
        if spec.validator is not None:
            spec.validator(call.params)
        for field_name in spec.path_fields:
            self.safe_path(call.params[field_name])
        approval_required = self._risk(spec, call) == ToolRisk.WRITE
        granted = False
        if approval_required and self.approval_service is not None:
            try:
                self.approval_service.require_granted(run_id, call.id, call.params, authorization)
                granted = True
            except ApprovalRequired:
                pass
        decision = decide(replace(facts, approval_required=approval_required, approval_granted=granted))
        if decision.action == PolicyAction.REQUIRE_APPROVAL:
            raise ApprovalRequired("valid approval required" if self.approval_service else "WRITE tool requires approval")
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
            raise ToolReconciliationRequired(operation_ref=call.id)
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

def _validate_schema(schema: dict[str, Any], params: dict[str, Any]) -> None:
    if schema.get("type") == "object" and not isinstance(params, dict):
        raise ToolArgumentError("tool parameters must be an object")
    for required in schema.get("required", []):
        if required not in params:
            raise ToolArgumentError(f"missing parameter: {required}")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unknown = set(params) - set(properties)
        if unknown:
            raise ToolArgumentError("unknown tool parameter")
    for name, value in params.items():
        expected = properties.get(name, {}).get("type")
        if expected == "string" and not isinstance(value, str):
            raise ToolArgumentError(f"parameter {name} must be a string")
