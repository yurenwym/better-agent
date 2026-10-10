"""Store boundary: trusted identities and semantic projections for domain events."""
from __future__ import annotations

from dataclasses import replace
import json

from .event_envelope import EventMetadata
from .execution_context import deserialize_context, create_child_context
from .execution_outcome import ExecutionError, ExecutionOutcome, Phase, Scope, Status


def _context(connection, event):
    from .harness_context_store import HarnessContextStore
    # _read validates digest AND row associations using this same transaction.
    store = HarnessContextStore(None)
    # A user decision is authored by the turn, not by the pending tool. A
    # corrupt tool context must still be rejectable and auditable; execution
    # separately verifies that tool context before dispatch.
    decision = event.type in {"chat_tool.approved", "chat_tool.rejected", "chat_tool.approval_requested"}
    for key, table in (("model_invocation_id", "model_invocations"),
                       ("call_id", "turn_tool_calls"), ("tool_call_id", "turn_tool_calls")):
        if decision and table == "turn_tool_calls":
            continue
        ref = event.data.get(key)
        if ref:
            row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (ref,)).fetchone()
            if row is not None and (row["execution_context_json"] or row["execution_context_digest"]):
                if hasattr(event, "turn_id") and row["turn_id"] != event.turn_id:
                    # A continuation-turn accepted event references the old
                    # tool but belongs to the new turn, not the old context.
                    continue
                return store._read(connection, table, row)
    if hasattr(event, "turn_id"):
        row = connection.execute("SELECT * FROM turns WHERE id=?", (event.turn_id,)).fetchone()
        if row is not None and (row["execution_context_json"] or row["execution_context_digest"]):
            return store._read(connection, "turns", row)
    else:
        row = connection.execute("SELECT budget_json,source_turn_id,runtime_bundle_id,root_budget_id FROM runs WHERE id=?", (event.run_id,)).fetchone()
        saved = json.loads(row["budget_json"]).get("agent_loop_context") if row else None
        if saved:
            context = deserialize_context(saved)
            if context.run_id != event.run_id or context.runtime_bundle_id != row["runtime_bundle_id"]:
                raise ValueError("run context binding changed")
            if context.turn_id != row["source_turn_id"]:
                raise ValueError("run source identity changed")
            if context.root_budget_id != row["root_budget_id"]:
                raise ValueError("run budget binding changed")
            if row["source_turn_id"]:
                owner = connection.execute("SELECT th.owner_id,t.thread_id FROM turns t JOIN threads th ON th.id=t.thread_id WHERE t.id=?", (row["source_turn_id"],)).fetchone()
                if owner is None or context.owner_id != owner["owner_id"] or context.thread_id != owner["thread_id"]:
                    raise ValueError("run source identity changed")
            return context
    return None


def _outcome(connection, event):
    data, kind = event.data, event.type
    if kind == "research.partial" or (kind == "expert.run.completed" and data.get("incomplete")):
        return ExecutionOutcome(Scope.TASK, Status.FAILED,
            error=ExecutionError("TASK_INCOMPLETE", Phase.TASK, False, "任务仅产出部分结果，请查看缺失项。"))
    if kind in {"turn.completed", "run.completed", "research.completed", "expert.run.completed"}:
        return ExecutionOutcome(Scope.TASK, Status.COMPLETED)
    if kind in {"research.cancelled", "expert.run.cancelled"}:
        return ExecutionOutcome(Scope.TASK, Status.CANCELLED, reason="CANCELLED")
    if kind in {"turn.cancelled", "run.cancelled"}:
        run_id = "chat-turn:" + event.turn_id if kind == "turn.cancelled" else event.run_id
        row = connection.execute("SELECT tool_call_id FROM tool_execution_claims WHERE run_id=? AND status='RECONCILIATION_REQUIRED' ORDER BY updated_at DESC LIMIT 1", (run_id,)).fetchone()
        if row:
            from .execution_outcome import Effect
            return ExecutionOutcome(Scope.TASK, Status.CANCELLED, reason="CANCELLED",
                effect=Effect.UNKNOWN, reconciliation_ref=row["tool_call_id"])
        return ExecutionOutcome(Scope.TASK, Status.CANCELLED, reason="CANCELLED")
    if kind == "turn.awaiting_input":
        return ExecutionOutcome(Scope.TASK, Status.AWAITING_INPUT, continuation_ref=data["ask_id"])
    if kind == "turn.awaiting_tool_approval":
        ref = data.get("call_id")
        if not ref:
            row = connection.execute("SELECT id FROM turn_tool_calls WHERE turn_id=? AND status='PENDING_APPROVAL' ORDER BY created_at DESC LIMIT 1", (event.turn_id,)).fetchone()
            ref = row["id"] if row else None
        return ExecutionOutcome(Scope.TASK, Status.AWAITING_APPROVAL, continuation_ref=ref)
    if kind == "research.queued":
        return ExecutionOutcome(Scope.LOOP, Status.HANDOFF, target_ref=data["job_id"])
    if kind == "expert.requested" and data.get("run_id"):
        return ExecutionOutcome(Scope.LOOP, Status.HANDOFF, target_ref=data["run_id"])
    if kind == "research.retry_scheduled":
        return ExecutionOutcome(Scope.LOOP, Status.FAILED,
            error=ExecutionError("RESEARCH_ATTEMPT_FAILED", Phase.TASK, True, "研究本次尝试未完成，已安排重试。"))
    if kind in {"turn.failed", "run.failed", "research.failed", "expert.run.failed"}:
        if data.get("outcome"):
            outcome = ExecutionOutcome.from_dict(data["outcome"])
            if outcome.scope != Scope.TASK or outcome.status not in {Status.FAILED, Status.RECONCILIATION_REQUIRED, Status.EXHAUSTED}:
                raise ValueError("invalid failed task outcome")
            return outcome
        return ExecutionOutcome(Scope.TASK, Status.FAILED,
            error=ExecutionError("INTERNAL_ERROR", Phase.TASK, False, "任务未完成。"))
    if kind == "tool.reconciliation_required":
        from .outcome_adapters import exception_outcome
        from .tools import ToolReconciliationRequired
        return exception_outcome(ToolReconciliationRequired(), scope=Scope.TOOL,
                                 reconciliation_ref=data["tool_call_id"])
    if kind == "tool.execution.finished" and data.get("result"):
        from .outcome_adapters import tool_result_outcome
        from .tools import ToolResult
        return tool_result_outcome(ToolResult(**data["result"]), write=data.get("risk") == "WRITE",
                                   operation_ref=data["tool_call_id"])
    if kind == "tool.execution.finished" and data.get("outcome"):
        return ExecutionOutcome.from_dict(data["outcome"])
    if kind in {"model.attempt.finished", "model.attempt_finished"}:
        if data["status"] in {"succeeded", "success"}:
            return ExecutionOutcome(Scope.MODEL, Status.COMPLETED)
        from .outcome_adapters import exception_outcome
        from .model_gateway import GatewayError
        return exception_outcome(GatewayError("", "cancelled" if data["status"] == "cancelled" else data.get("error_kind") or "unknown"), scope=Scope.MODEL)
    if kind == "loop.finished":
        return ExecutionOutcome.from_dict(data["outcome"])
    return None


def bind_event_metadata(connection, event):
    thread = hasattr(event, "thread_id")
    table, field, stream_id = ("thread_events", "thread_id", event.thread_id) if thread else ("events", "run_id", event.run_id)
    trusted = _context(connection, event)
    if event.type.startswith("expert.run.") and trusted is not None:
        run = connection.execute(
            "SELECT r.owner_id,r.thread_id,r.runtime_bundle_id,s.content_json FROM agent_runs r "
            "JOIN agent_context_snapshots s ON s.id=r.context_snapshot_id WHERE r.id=?",
            (event.data.get("agent_run_id"),),
        ).fetchone()
        if run is None or any(run[key] != getattr(trusted, key)
                              for key in ("owner_id", "thread_id", "runtime_bundle_id")):
            raise ValueError("expert event source identity conflict")
        if json.loads(run["content_json"]).get("source_turn_id") != event.turn_id:
            raise ValueError("expert event source turn conflict")
    if event.envelope_json:
        metadata = EventMetadata.from_dict(json.loads(event.envelope_json))
        context = metadata.context
        if trusted is None:
            raise ValueError("event has no durable trusted context")
        # Only execution nesting may differ. All business bindings must remain
        # identical, including budget, bundle, project and task ancestry.
        if replace(context, span_id=trusted.span_id, parent_span_id=trusted.parent_span_id) != trusted:
            raise ValueError("event execution identity conflict")
    elif trusted is None:
        if event.type == "loop.finished":
            raise ValueError("native loop completion requires a durable execution context")
        # Once a stream operation has a trusted identity it cannot silently
        # downgrade to legacy because both context columns were removed.
        identity_field = "turn_id" if thread else "run_id"
        identity = event.turn_id if thread else event.run_id
        prior = connection.execute(
            f"SELECT 1 FROM {table} WHERE {identity_field}=? AND envelope_json IS NOT NULL LIMIT 1",
            (identity,),
        ).fetchone()
        if prior is not None:
            raise ValueError("event lost its durable execution context")
        return event  # Explicit historical/domain-only record, no fabricated trace.
    else:
        context = trusted
        operation = str(event.data.get("model_invocation_id") or event.data.get("tool_call_id") or
                        event.data.get("call_id") or event.data.get("job_id") or event.data.get("agent_run_id") or
                        (event.turn_id if thread else event.run_id))
        attempt = event.data.get("attempt", 1)
        if type(attempt) is not int:
            attempt = 1
        # Find causation only within this operation; unrelated events in the
        # same stream do not become parents merely by arriving first.
        # Iterate newest first and stop at the first match. Keep the cursor
        # lazy: long-running research streams must not be copied on every event.
        rows = connection.execute(
            f"SELECT event_id,type,envelope_json FROM {table} WHERE {field}=? AND envelope_json IS NOT NULL ORDER BY seq DESC",
            (stream_id,),
        )
        previous = None
        attempt_context = None
        for row in rows:
            body = json.loads(row["envelope_json"])
            if body.get("schema_version") == 1 and body.get("operation_id") == operation:
                candidate = EventMetadata.from_dict(body)
                if previous is None:
                    previous = (row["event_id"], candidate, row["type"])
                if event.type.startswith(("model.attempt.", "model.attempt_")):
                    if candidate.attempt == attempt and row["type"] in {"model.attempt.started", "model.attempt_started"}:
                        attempt_context = candidate.context
                        break
                else:
                    break
        if event.type == "chat_tool.context_resumed":
            context = create_child_context(trusted)
            attempt = previous[1].attempt + 1 if previous else 2
        elif event.type.startswith("research.") and event.data.get("job_id"):
            if event.type == "research.started":
                context = create_child_context(trusted)
            elif previous:
                context, attempt = previous[1].context, previous[1].attempt
            else:
                context = create_child_context(trusted)
        elif event.type.startswith("expert.run."):
            context = previous[1].context if previous else create_child_context(trusted)
        elif event.type == "tool.execution.finished" and previous and previous[2] == "chat_tool.context_resumed":
            context, attempt = previous[1].context, previous[1].attempt
        elif event.type.startswith(("model.attempt.", "model.attempt_")):
            context = attempt_context or create_child_context(trusted)
        source = "model_control" if event.type.startswith(("model.", "cost.")) else (
            "research" if event.type.startswith("research.") else "conversation" if thread else "runtime")
        metadata = EventMetadata(context, source, operation, attempt,
            causation_event_id=previous[0] if previous else None, outcome=_outcome(connection, event))
        if not thread and event.type == "run.created" and trusted.turn_id:
            parent = connection.execute(
                "SELECT event_id,data_json FROM thread_events WHERE thread_id=? AND turn_id=? "
                "AND type='execution.materialized' ORDER BY seq DESC",
                (trusted.thread_id, trusted.turn_id),
            )
            for row in parent:
                if json.loads(row["data_json"]).get("run_id") == event.run_id:
                    metadata = replace(metadata, causation_event_id=row["event_id"])
                    break
        invocation = event.data.get("model_invocation_id")
        if invocation:
            row = connection.execute("SELECT context_snapshot_id FROM model_invocations WHERE id=?", (invocation,)).fetchone()
            if row and row["context_snapshot_id"]:
                metadata = replace(metadata, snapshot_refs=(row["context_snapshot_id"],))
    if thread and (context.thread_id != event.thread_id or context.turn_id != event.turn_id):
        raise ValueError("thread event context mismatch")
    if not thread and context.run_id != event.run_id:
        raise ValueError("run event context mismatch")
    return replace(event, envelope_json=json.dumps(metadata.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
