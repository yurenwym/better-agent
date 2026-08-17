from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable

from .db import Database


class AgentState(StrEnum):
    RECEIVED = "RECEIVED"
    CLARIFYING = "CLARIFYING"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    AWAITING_OUTCOME = "AWAITING_OUTCOME"
    REFLECTING = "REFLECTING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class InvalidTransition(ValueError):
    pass


@dataclass(frozen=True)
class StateTransition:
    state: AgentState
    resume_state: AgentState | None = None


class StateMachine:
    _allowed = {
        AgentState.RECEIVED: {AgentState.CLARIFYING, AgentState.PLANNING},
        AgentState.CLARIFYING: {AgentState.PLANNING, AgentState.BLOCKED},
        AgentState.PLANNING: {AgentState.AWAITING_APPROVAL, AgentState.BLOCKED},
        AgentState.AWAITING_APPROVAL: {AgentState.PLANNING, AgentState.EXECUTING},
        AgentState.EXECUTING: {
            AgentState.AWAITING_OUTCOME,
            AgentState.REFLECTING,
            AgentState.BLOCKED,
        },
        AgentState.AWAITING_OUTCOME: {
            AgentState.EXECUTING,
            AgentState.REFLECTING,
            AgentState.BLOCKED,
        },
        AgentState.REFLECTING: {AgentState.COMPLETED, AgentState.BLOCKED},
        AgentState.BLOCKED: {
            AgentState.CLARIFYING,
            AgentState.PLANNING,
            AgentState.EXECUTING,
            AgentState.AWAITING_OUTCOME,
            AgentState.REFLECTING,
        },
    }
    _terminal = {AgentState.COMPLETED, AgentState.FAILED, AgentState.CANCELLED}

    def transition(
        self,
        current: AgentState,
        target: AgentState,
        resume_state: AgentState | None = None,
    ) -> StateTransition:
        if current in self._terminal:
            raise InvalidTransition(f"terminal state {current} cannot transition")
        if target in {AgentState.FAILED, AgentState.CANCELLED}:
            return StateTransition(target)
        if current == AgentState.BLOCKED:
            if resume_state is None or target != resume_state:
                raise InvalidTransition("blocked run may only resume its saved state")
            return StateTransition(target)
        if target not in self._allowed.get(current, set()):
            raise InvalidTransition(f"{current} -> {target} is not allowed")
        return StateTransition(target, current if target == AgentState.BLOCKED else None)


class PlanConflict(ValueError):
    pass


@dataclass(frozen=True)
class PlanStep:
    id: str
    title: str
    description: str = ""
    status: str = "pending"
    position: int = 0


@dataclass(frozen=True)
class PlanVersion:
    id: str
    run_id: str
    goal_id: str
    version: int
    status: str
    steps: tuple[PlanStep, ...]
    summary: str = ""


class PlanVersionService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        run_id: str,
        goal_id: str,
        steps: Iterable[dict[str, Any]],
        summary: str = "",
    ) -> PlanVersion:
        with self.db.transaction() as connection:
            current = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM plan_versions WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            version = current + 1
            plan_id = f"pv_{uuid.uuid4().hex}"
            self._insert(connection, plan_id, run_id, goal_id, version, summary, None, steps)
        return self.get(plan_id)

    def revise(
        self,
        run_id: str,
        goal_id: str,
        expected_version: int,
        steps: Iterable[dict[str, Any]],
        summary: str = "",
    ) -> PlanVersion:
        with self.db.transaction() as connection:
            current_row = connection.execute(
                "SELECT * FROM plan_versions WHERE run_id = ? ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            current_version = current_row["version"] if current_row else 0
            if current_version != expected_version:
                raise PlanConflict(f"expected plan version {expected_version}, current is {current_version}")
            completed = {
                row["id"]: row["status"]
                for row in connection.execute(
                    "SELECT id, status FROM plan_steps WHERE plan_version_id = ?",
                    (current_row["id"],),
                )
                if row["status"] == "completed"
            } if current_row else {}
            version = current_version + 1
            plan_id = f"pv_{uuid.uuid4().hex}"
            normalized_steps = [
                {**step, "status": completed.get(step.get("id"), step.get("status", "pending"))}
                for step in steps
            ]
            self._insert(
                connection,
                plan_id,
                run_id,
                goal_id,
                version,
                summary,
                expected_version,
                normalized_steps,
            )
        return self.get(plan_id)

    def approve(self, plan_id: str) -> PlanVersion:
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM plan_versions WHERE id = ?", (plan_id,)).fetchone()
            if row is None:
                raise PlanConflict("unknown plan version")
            latest = connection.execute(
                "SELECT MAX(version) FROM plan_versions WHERE run_id = ?", (row["run_id"],)
            ).fetchone()[0]
            if row["version"] != latest:
                raise PlanConflict("only the current plan version may be approved")
            connection.execute(
                "UPDATE plan_versions SET status = 'approved', approved_at = ? WHERE id = ?",
                (_now(), plan_id),
            )
        return self.get(plan_id)

    def mark_step_completed(self, plan_id: str, step_id: str) -> None:
        with self.db.transaction() as connection:
            updated = connection.execute(
                "UPDATE plan_steps SET status = 'completed', completed_at = ? "
                "WHERE plan_version_id = ? AND id = ?",
                (_now(), plan_id, step_id),
            ).rowcount
            if updated != 1:
                raise PlanConflict("unknown plan step")

    def mark_step_cancelled(self, plan_id: str, step_id: str) -> None:
        with self.db.transaction() as connection:
            updated = connection.execute(
                "UPDATE plan_steps SET status = 'cancelled', canceled_at = ? "
                "WHERE plan_version_id = ? AND id = ? AND status NOT IN ('completed', 'cancelled')",
                (_now(), plan_id, step_id),
            ).rowcount
            if updated != 1:
                raise PlanConflict("unknown or finished plan step")

    def current(self, run_id: str) -> PlanVersion:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT id FROM plan_versions WHERE run_id = ? ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            raise PlanConflict("run has no plan")
        return self.get(row["id"])

    def get(self, plan_id: str) -> PlanVersion:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM plan_versions WHERE id = ?", (plan_id,)).fetchone()
            if row is None:
                raise PlanConflict("unknown plan version")
            steps = connection.execute(
                "SELECT * FROM plan_steps WHERE plan_version_id = ? ORDER BY position",
                (plan_id,),
            ).fetchall()
        return PlanVersion(
            id=row["id"],
            run_id=row["run_id"],
            goal_id=row["goal_id"],
            version=row["version"],
            status=row["status"],
            summary=row["summary"],
            steps=tuple(
                PlanStep(
                    id=step["id"],
                    title=step["title"],
                    description=step["description"],
                    status=step["status"],
                    position=step["position"],
                )
                for step in steps
            ),
        )

    @staticmethod
    def _insert(
        connection: Any,
        plan_id: str,
        run_id: str,
        goal_id: str,
        version: int,
        summary: str,
        base_version: int | None,
        steps: Iterable[dict[str, Any]],
    ) -> None:
        now = _now()
        connection.execute(
            "INSERT INTO plan_versions(id, run_id, goal_id, version, status, summary, base_version, created_at) "
            "VALUES (?, ?, ?, ?, 'draft', ?, ?, ?)",
            (plan_id, run_id, goal_id, version, summary, base_version, now),
        )
        for position, step in enumerate(steps):
            connection.execute(
                "INSERT INTO plan_steps(id, plan_version_id, position, title, description, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    step.get("id") or f"step_{uuid.uuid4().hex}",
                    plan_id,
                    position,
                    step["title"],
                    step.get("description", ""),
                    step.get("status", "pending"),
                ),
            )


class ApprovalRequired(PermissionError):
    pass


@dataclass(frozen=True)
class Approval:
    id: str
    run_id: str
    tool_call_id: str
    params_hash: str
    status: str


class ApprovalService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def request(
        self,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        params: dict[str, Any],
        expires_at: str | None = None,
    ) -> Approval:
        approval_id = f"approval_{uuid.uuid4().hex}"
        params_hash = normalized_params_hash(params)
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO approvals(id, run_id, tool_call_id, params_hash, params_json, status, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (approval_id, run_id, tool_call_id, params_hash, json.dumps(params, ensure_ascii=False, sort_keys=True), _now(), expires_at),
            )
        return Approval(approval_id, run_id, tool_call_id, params_hash, "pending")

    def grant(self, approval_id: str, run_id: str, tool_call_id: str, params: dict[str, Any]) -> Approval:
        return self._act(approval_id, run_id, tool_call_id, params, "granted")

    def reject(self, approval_id: str, run_id: str, tool_call_id: str, params: dict[str, Any]) -> Approval:
        return self._act(approval_id, run_id, tool_call_id, params, "rejected")

    def require_granted(self, run_id: str, tool_call_id: str, params: dict[str, Any]) -> None:
        params_hash = normalized_params_hash(params)
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND tool_call_id = ? AND params_hash = ? "
                "AND status = 'granted' ORDER BY created_at DESC LIMIT 1",
                (run_id, tool_call_id, params_hash),
            ).fetchone()
        if row is None or _expired(row["expires_at"]):
            raise ApprovalRequired("valid approval required")

    def _act(
        self,
        approval_id: str,
        run_id: str,
        tool_call_id: str,
        params: dict[str, Any],
        status: str,
    ) -> Approval:
        params_hash = normalized_params_hash(params)
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if (
                row is None
                or row["run_id"] != run_id
                or row["tool_call_id"] != tool_call_id
                or row["params_hash"] != params_hash
                or row["status"] != "pending"
                or _expired(row["expires_at"])
            ):
                raise ApprovalRequired("approval binding is invalid")
            connection.execute(
                "UPDATE approvals SET status = ?, acted_at = ? WHERE id = ?",
                (status, _now(), approval_id),
            )
        return Approval(approval_id, run_id, tool_call_id, params_hash, status)


@dataclass(frozen=True)
class Checkpoint:
    run_id: str
    state: str
    plan_version_id: str | None
    step_id: str | None
    completed_steps: list[str] = field(default_factory=list)
    react_iteration: int = 0
    remaining_budget: dict[str, Any] = field(default_factory=dict)
    observation: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    pending_approvals: list[str] = field(default_factory=list)
    applied_memory_versions: list[str] = field(default_factory=list)
    pending_actions: list[dict[str, Any]] = field(default_factory=list)
    last_event_seq: int = 0
    id: str = ""


class CheckpointStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, checkpoint: Checkpoint) -> Checkpoint:
        checkpoint_id = checkpoint.id or f"checkpoint_{uuid.uuid4().hex}"
        payload = {
            "completed_steps": checkpoint.completed_steps,
            "react_iteration": checkpoint.react_iteration,
            "remaining_budget": checkpoint.remaining_budget,
            "observation": checkpoint.observation,
            "artifact_refs": checkpoint.artifact_refs,
            "pending_approvals": checkpoint.pending_approvals,
            "applied_memory_versions": checkpoint.applied_memory_versions,
            "pending_actions": checkpoint.pending_actions,
        }
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO checkpoints(id, run_id, state, plan_version_id, step_id, payload_json, "
                "last_event_seq, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    checkpoint_id,
                    checkpoint.run_id,
                    checkpoint.state,
                    checkpoint.plan_version_id,
                    checkpoint.step_id,
                    json.dumps(payload, ensure_ascii=False),
                    checkpoint.last_event_seq,
                    _now(),
                ),
            )
        return Checkpoint(**{**checkpoint.__dict__, "id": checkpoint_id})

    def latest(self, run_id: str) -> Checkpoint | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        return Checkpoint(
            id=row["id"],
            run_id=row["run_id"],
            state=row["state"],
            plan_version_id=row["plan_version_id"],
            step_id=row["step_id"],
            last_event_seq=row["last_event_seq"],
            **payload,
        )

    def record_completed_tool(self, run_id: str, tool_call_id: str, result: dict[str, Any]) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO tool_calls(id, run_id, tool_name, params_hash, risk, status, result_json, "
                "created_at, completed_at) VALUES (?, ?, '', '', 'WRITE', 'completed', ?, ?, ?)",
                (
                    tool_call_id,
                    run_id,
                    json.dumps(result, ensure_ascii=False),
                    _now(),
                    _now(),
                ),
            )

    def completed_tool_result(self, run_id: str, tool_call_id: str) -> dict[str, Any] | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT result_json FROM tool_calls WHERE run_id = ? AND id = ? AND status = 'completed'",
                (run_id, tool_call_id),
            ).fetchone()
        return json.loads(row["result_json"]) if row else None


def normalized_params_hash(params: dict[str, Any]) -> str:
    encoded = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    value = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= datetime.now(timezone.utc)
