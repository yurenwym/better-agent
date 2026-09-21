"""Plain-chat access to the goal business tools (D2).

The eight goal tools already run inside the Agent Runtime with a durable
approval, execution-claim and idempotency path. Plain chat needs the same
tools, so this module adapts that machinery instead of writing a second
persistence layer:

* ``ChatToolCallStore`` owns only the *chat-scoped* part - which tool call is
  pending on a turn, which continuation turn resumed it and what the replayed
  result is. The approval row itself stays in ``approvals`` and execution
  claims stay in ``tool_execution_claims``/``tool_calls``.
* ``ChatToolRunner`` validates and executes calls through the shared
  :class:`~app.tools.ToolRegistry` with a trusted :class:`ToolExecutionContext`
  built from the durable turn/thread binding, never from model parameters.

A READ call executes inline and returns its result. A WRITE call requests an
approval and reports ``pending`` so the turn can pause; the user's decision
creates a continuation turn that resumes exactly this call.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .domain import ApprovalRequired, ApprovalService, normalized_params_hash
from .tools import (
    ToolCall,
    ToolExecutionContext,
    ToolReconciliationRequired,
    ToolRegistry,
    ToolRejected,
    ToolResult,
    ToolRisk,
)

CHAT_RUN_PREFIX = "chat-turn:"

# Kept as a literal to avoid importing the MCP modules from the chat path:
# a deployment with no MCP server configured must not load the SDK at all.
MCP_SOURCE = "mcp"

# The eight goal business tools, in the same order the registration uses.
GOAL_TOOL_NAMES: frozenset[str] = frozenset({
    "create_plan_draft",
    "modify_plan_document",
    "activate_goal_plan",
    "query_goals",
    "get_today_tasks",
    "get_action_context",
    "get_plan",
    "record_action_feedback",
    "defer_action",
})

STATUS_PENDING = "PENDING_APPROVAL"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_EXECUTED = "EXECUTED"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"

# Synthetic user-role input for the continuation turn: the tool exchange itself
# is replayed from ``turn_tool_calls`` through the transcript.
TOOL_CONTINUATION_INSTRUCTION = (
    "（系统）待确认的工具操作已经有了结果。请依据工具结果继续完成用户最初的请求："
    "成功时用工具返回的真实内容回答；失败或被拒绝时如实说明，不得声称操作已完成。"
)

REJECTION_OBSERVATION = "用户拒绝执行该操作。不得声称该操作已完成或已生效。"


def chat_run_id(turn_id: str) -> str:
    return f"{CHAT_RUN_PREFIX}{turn_id}"


def decide_approval(
    connection,
    *,
    approval_id: str,
    run_id: str,
    tool_call_id: str,
    params: dict[str, Any],
    binding: dict[str, Any] | None,
    approved: bool,
) -> None:
    """Grant or reject one approval inside the caller's transaction.

    Mirrors :meth:`ApprovalService._act` validation so granting and recording
    the continuation commit atomically; the granted row is still what the
    ToolRegistry checks before executing the WRITE call.
    """
    params_hash = normalized_params_hash(params)
    binding_json = json.dumps(binding or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    binding_digest = hashlib.sha256(binding_json.encode("utf-8")).hexdigest()
    row = connection.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    if (
        row is None
        or row["run_id"] != run_id
        or row["tool_call_id"] != tool_call_id
        or row["params_hash"] != params_hash
        or row["binding_digest"] != binding_digest
        or row["status"] != "pending"
    ):
        raise ApprovalRequired("approval binding is invalid")
    connection.execute(
        "UPDATE approvals SET status=?, acted_at=? WHERE id=?",
        ("granted" if approved else "rejected", _now(), approval_id),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ChatToolCallSnapshot:
    id: str
    turn_id: str
    thread_id: str
    tool_name: str
    params: dict[str, Any]
    params_hash: str
    risk: str
    status: str
    approval_id: str | None
    binding: dict[str, Any]
    result: dict[str, Any] | None
    error_code: str | None
    continuation_turn_id: str | None
    decision_idempotency_key: str | None
    created_at: str
    acted_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "turn_id": self.turn_id,
            "tool_name": self.tool_name,
            "params": self.params,
            "risk": self.risk,
            "status": self.status,
            "approval_id": self.approval_id,
            "binding": self.binding,
            "error_code": self.error_code,
            "continuation_turn_id": self.continuation_turn_id,
            "created_at": self.created_at,
            "acted_at": self.acted_at,
        }


@dataclass(frozen=True)
class ChatToolOutcome:
    call_id: str
    result: ToolResult | None = None
    pending: bool = False
    approval_id: str | None = None
    binding: dict[str, Any] | None = None


def _row_to_snapshot(row: Any) -> ChatToolCallSnapshot:
    return ChatToolCallSnapshot(
        id=row["id"],
        turn_id=row["turn_id"],
        thread_id=row["thread_id"],
        tool_name=row["tool_name"],
        params=json.loads(row["params_json"]),
        params_hash=row["params_hash"],
        risk=row["risk"],
        status=row["status"],
        approval_id=row["approval_id"],
        binding=json.loads(row["binding_json"] or "{}"),
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error_code=row["error_code"],
        continuation_turn_id=row["continuation_turn_id"],
        decision_idempotency_key=row["decision_idempotency_key"],
        created_at=row["created_at"],
        acted_at=row["acted_at"],
    )


class ChatToolCallStore:
    """Chat-scoped durable state for one pending or completed tool call."""

    def __init__(self, db) -> None:
        self.db = db

    def create(
        self,
        *,
        turn_id: str,
        thread_id: str,
        tool_name: str,
        params: dict[str, Any],
        risk: str,
        call_id: str | None = None,
        status: str = STATUS_PENDING,
        approval_id: str | None = None,
        binding: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> ChatToolCallSnapshot:
        call_id = call_id or f"chat-tool-{uuid.uuid4().hex}"
        binding = binding or {}
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO turn_tool_calls(id,turn_id,thread_id,tool_name,params_json,params_hash,risk,status,"
                "approval_id,binding_json,result_json,error_code,created_at,acted_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    call_id,
                    turn_id,
                    thread_id,
                    tool_name,
                    json.dumps(params, ensure_ascii=False, sort_keys=True),
                    normalized_params_hash(params),
                    risk,
                    status,
                    approval_id,
                    json.dumps(binding, ensure_ascii=False, sort_keys=True),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error_code,
                    _now(),
                    _now() if status != STATUS_PENDING else None,
                ),
            )
        return self.get(call_id)

    def get(self, call_id: str) -> ChatToolCallSnapshot:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM turn_tool_calls WHERE id=?", (call_id,)
            ).fetchone()
        if row is None:
            raise KeyError(call_id)
        return _row_to_snapshot(row)

    def pending_for_turn(self, turn_id: str) -> ChatToolCallSnapshot | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM turn_tool_calls WHERE turn_id=? AND status=? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (turn_id, STATUS_PENDING),
            ).fetchone()
        return _row_to_snapshot(row) if row is not None else None

    def by_decision_key(self, idempotency_key: str) -> ChatToolCallSnapshot | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM turn_tool_calls WHERE decision_idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return _row_to_snapshot(row) if row is not None else None

    def by_continuation(self, continuation_turn_id: str) -> ChatToolCallSnapshot | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM turn_tool_calls WHERE continuation_turn_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (continuation_turn_id,),
            ).fetchone()
        return _row_to_snapshot(row) if row is not None else None

    def for_turn(self, turn_id: str) -> list[ChatToolCallSnapshot]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM turn_tool_calls WHERE turn_id=? ORDER BY created_at, id",
                (turn_id,),
            ).fetchall()
        return [_row_to_snapshot(row) for row in rows]

    def transcript_rows(self, thread_id: str, turn_ids: list[str]) -> dict[str, list[ChatToolCallSnapshot]]:
        if not turn_ids:
            return {}
        placeholders = ",".join("?" for _ in turn_ids)
        with self.db.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM turn_tool_calls WHERE thread_id=? AND turn_id IN ({placeholders}) "
                "AND status IN (?,?,?) ORDER BY created_at, id",
                (thread_id, *turn_ids, STATUS_EXECUTED, STATUS_FAILED, STATUS_REJECTED),
            ).fetchall()
        grouped: dict[str, list[ChatToolCallSnapshot]] = {}
        for row in rows:
            grouped.setdefault(row["turn_id"], []).append(_row_to_snapshot(row))
        return grouped

    def record_result(
        self,
        call_id: str,
        *,
        result: dict[str, Any] | None,
        error_code: str | None,
        status: str,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE turn_tool_calls SET status=?, result_json=?, error_code=?, acted_at=? WHERE id=?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error_code,
                    _now(),
                    call_id,
                ),
            )

    def cascade_pending(self, turn_id: str) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE turn_tool_calls SET status=?, error_code=?, acted_at=? "
                "WHERE turn_id=? AND status=?",
                (STATUS_CANCELLED, "TURN_CANCELLED", _now(), turn_id, STATUS_PENDING),
            )


class ChatToolRunner:
    """Per-turn runner bound to one turn's trusted scope."""

    def __init__(
        self,
        registry: ToolRegistry,
        approvals: ApprovalService,
        store: ChatToolCallStore,
        *,
        turn_id: str,
        thread_id: str,
        owner_id: str,
        project_id: str | None,
        skill_names: tuple[str, ...] | list[str],
        root_budget_id: str | None = None,
        runtime_bundle_id: str | None = None,
        goal_programs=None,
        plan_documents=None,
        tool_allowance=None,
        mcp_sync=None,
    ) -> None:
        self.registry = registry
        self.approvals = approvals
        self.store = store
        self.turn_id = turn_id
        self.thread_id = thread_id
        self.owner_id = owner_id
        self.project_id = project_id
        self.skill_names = tuple(skill_names or ())
        self.root_budget_id = root_budget_id
        self.runtime_bundle_id = runtime_bundle_id
        self.goal_programs = goal_programs
        self.plan_documents = plan_documents
        self._allowance = tool_allowance
        self._mcp_sync = mcp_sync

    @property
    def run_id(self) -> str:
        return chat_run_id(self.turn_id)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.allowed_names())

    def allowed_names(self) -> set[str]:
        """Tools this turn may call.

        The native business tools are the fixed chat set. MCP tools are the ones
        currently registered for a *configured and enabled* server, and they
        only stay allowed when the resolved allowance names them - so a server
        that was disabled, or a skill intersection that removed it, fails
        closed rather than leaving the tool reachable.
        """
        registered = {
            schema["function"]["name"] for schema in self.registry.describe()
        }
        allowed = set(GOAL_TOOL_NAMES & registered)
        if self._allowance is None:
            return allowed
        allowed &= set(self._allowance)
        allowed |= {
            spec.name for spec in self.registry.specs()
            if spec.source == MCP_SOURCE and spec.server_id and spec.name in self._allowance
        }
        return allowed

    def schemas(self) -> list[dict[str, Any]]:
        allowed = self.allowed_names()
        return [
            schema for schema in self.registry.describe()
            if schema["function"]["name"] in allowed
        ]

    def context(self, tool_call_id: str) -> ToolExecutionContext:
        return ToolExecutionContext(
            owner_id=self.owner_id or "",
            run_id=self.run_id,
            tool_call_id=tool_call_id,
            thread_id=self.thread_id,
            project_id=self.project_id,
            root_budget_id=self.root_budget_id,
            runtime_bundle_id=self.runtime_bundle_id,
        )

    def authorization(self, tool_name: str, params: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
        if self._mcp_sync is not None:
            # A remote write is approved against one server configuration
            # version and one definition digest, so a rotated credential or a
            # changed remote definition invalidates the consent.
            mcp_binding = self._mcp_sync.approval_binding_for(tool_name)
            if mcp_binding:
                return mcp_binding
        if (
            tool_name == "activate_goal_plan"
            and params.get("mode") == "activate"
            and self.goal_programs is not None
            and self.plan_documents is not None
        ):
            from .goal_tools import activation_approval_binding

            return activation_approval_binding(
                self.goal_programs, self.plan_documents, params, context,
            )
        return {}

    @staticmethod
    def observation(result: ToolResult) -> dict[str, Any]:
        return result.as_dict()

    def _bound_call(self, tool_name: str, params: dict[str, Any], call_id: str) -> ToolCall:
        return ToolCall(id=call_id, name=tool_name, params=params)

    async def execute(
        self,
        *,
        tool_name: str,
        params: dict[str, Any],
        provider_call_id: str | None = None,
    ) -> ChatToolOutcome:
        """Run one model tool call, pausing for a WRITE approval when needed."""
        allowed = self.allowed_names()
        if tool_name not in allowed:
            return ChatToolOutcome(
                call_id=provider_call_id or "",
                result=ToolResult(False, "该工具当前不可用", error="TOOL_NOT_ALLOWED"),
            )
        call_id = f"chat-tool-{uuid.uuid4().hex}"
        call = self._bound_call(tool_name, params, call_id)
        try:
            spec = self.registry.spec(tool_name)
        except ToolRejected as exc:
            return ChatToolOutcome(call_id=call_id, result=ToolResult(False, str(exc), error="TOOL_NOT_ALLOWED"))
        risk = self.registry.risk_of(tool_name, params)
        try:
            self.registry.authorize(call, run_id=self.run_id, skill_tools=allowed)
        except ApprovalRequired:
            # Parameters passed validation; a WRITE call needs the user.
            return await self._request_approval(
                call_id=call_id, tool_name=tool_name, params=params, risk=risk,
            )
        except ToolRejected as exc:
            return ChatToolOutcome(
                call_id=call_id,
                result=ToolResult(False, f"工具参数无效：{exc}", error="INVALID_ARGUMENT"),
            )
        context = self.context(call_id)
        try:
            result = await self.registry.execute_async(
                call, context=context, skill_tools=allowed, authorization=None,
            )
        except ToolReconciliationRequired:
            # The effect may already have landed remotely. Reporting success or
            # a plain failure would both be a claim the Harness cannot support.
            result = ToolResult(
                False,
                "工具执行状态待核对，暂不能继续。",
                error="TOOL_RECONCILIATION_REQUIRED",
            )
            self.store.create(
                turn_id=self.turn_id,
                thread_id=self.thread_id,
                tool_name=tool_name,
                params=params,
                risk=str(risk),
                call_id=call_id,
                status=STATUS_FAILED,
                result=result.as_dict(),
                error_code=result.error,
            )
            return ChatToolOutcome(call_id=call_id, result=result)
        self.store.create(
            turn_id=self.turn_id,
            thread_id=self.thread_id,
            tool_name=tool_name,
            params=params,
            risk=str(risk),
            call_id=call_id,
            status=STATUS_EXECUTED if result.ok else STATUS_FAILED,
            result=result.as_dict(),
            error_code=result.error,
        )
        return ChatToolOutcome(call_id=call_id, result=result)

    async def _request_approval(
        self,
        *,
        call_id: str,
        tool_name: str,
        params: dict[str, Any],
        risk: ToolRisk,
    ) -> ChatToolOutcome:
        context = self.context(call_id)
        try:
            binding = self.authorization(tool_name, params, context)
        except (ToolRejected, KeyError, ValueError) as exc:
            return ChatToolOutcome(
                call_id=call_id,
                result=ToolResult(False, f"当前无法发起该操作：{exc}", error="ACTION_NOT_ELIGIBLE"),
            )
        # A retried turn regenerates its model output, so an earlier pending
        # approval for the same turn is stale: reject it before recording the
        # current call, keeping at most one pending approval per turn.
        stale = self.store.pending_for_turn(self.turn_id)
        while stale is not None:
            if stale.approval_id:
                try:
                    self.approvals.reject(
                        stale.approval_id, self.run_id, stale.id, stale.params, stale.binding,
                    )
                except ApprovalRequired:
                    pass
            self.store.record_result(
                stale.id, result=None, error_code="SUPERSEDED", status=STATUS_CANCELLED,
            )
            stale = self.store.pending_for_turn(self.turn_id)
        approval = self.approvals.request(
            self.run_id, call_id, tool_name, params, binding=binding,
        )
        self.store.create(
            turn_id=self.turn_id,
            thread_id=self.thread_id,
            tool_name=tool_name,
            params=params,
            risk=str(risk),
            call_id=call_id,
            status=STATUS_PENDING,
            approval_id=approval.id,
            binding=binding,
        )
        return ChatToolOutcome(
            call_id=call_id,
            pending=True,
            approval_id=approval.id,
            binding=binding,
        )

    async def resume(self, call: ChatToolCallSnapshot) -> ToolResult:
        """Execute an approved call after the decision created a continuation."""
        if call.result is not None:
            return ToolResult(**call.result)
        if call.status == STATUS_REJECTED:
            return ToolResult(False, REJECTION_OBSERVATION, error="USER_REJECTED")
        if call.status != STATUS_APPROVED:
            return ToolResult(False, "该工具调用当前不可执行", error="ACTION_NOT_ELIGIBLE")
        allowed = self.allowed_names()
        if call.tool_name not in allowed:
            self.store.record_result(
                call.id, result=ToolResult(False, "该工具当前不可用", error="TOOL_NOT_ALLOWED").as_dict(),
                error_code="TOOL_NOT_ALLOWED", status=STATUS_FAILED,
            )
            return ToolResult(False, "该工具当前不可用", error="TOOL_NOT_ALLOWED")
        tool_call = self._bound_call(call.tool_name, call.params, call.id)
        # The approval and execution claims were created under the *original*
        # turn's chat run scope, so resuming must reuse it rather than the
        # continuation turn's id.
        context = ToolExecutionContext(
            owner_id=self.owner_id or "",
            run_id=chat_run_id(call.turn_id),
            tool_call_id=call.id,
            thread_id=call.thread_id,
            project_id=self.project_id,
            root_budget_id=self.root_budget_id,
            runtime_bundle_id=self.runtime_bundle_id,
        )
        try:
            result = await self.registry.execute_async(
                tool_call,
                context=context,
                skill_tools=allowed,
                authorization=call.binding or None,
            )
        except ToolReconciliationRequired:
            return ToolResult(
                False,
                "工具执行状态待核对，暂不能继续。",
                error="TOOL_RECONCILIATION_REQUIRED",
            )
        except (ToolRejected, ApprovalRequired) as exc:
            result = ToolResult(False, f"工具执行被拒绝：{exc}", error="TOOL_AUTHORIZATION_DENIED")
        self.store.record_result(
            call.id,
            result=result.as_dict(),
            error_code=result.error,
            status=STATUS_EXECUTED if result.ok else STATUS_FAILED,
        )
        return result
