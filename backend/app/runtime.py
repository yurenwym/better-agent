from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol

from .context import ContextAssembler, MemoryForContext
from .db import Database
from .domain import (
    AgentState,
    Approval,
    ApprovalRequired,
    ApprovalService,
    Checkpoint,
    CheckpointStore,
    PlanVersion,
    PlanVersionService,
    StateMachine,
)
from .events import EventStore
from .memory import MemoryService
from .stats import StatsProjector
from .tools import ToolCall, ToolRegistry, ToolRejected, ToolResult


@dataclass
class RuntimeConfig:
    max_react_iterations_per_step: int = 5
    max_step_wall_time_seconds: float = 300
    max_consecutive_tool_errors: int = 2
    max_identical_actions: int = 2


@dataclass(frozen=True)
class ModelDecision:
    action: str
    summary: str = ""
    observation: str = ""
    tool_call: ToolCall | None = None

    @classmethod
    def complete(cls, summary: str = "completed") -> "ModelDecision":
        return cls("complete_step", summary=summary)

    @classmethod
    def continue_(cls, observation: str = "continue") -> "ModelDecision":
        return cls("continue", observation=observation)

    @classmethod
    def tool(cls, tool_call: ToolCall, summary: str = "tool proposed") -> "ModelDecision":
        return cls("tool_call", summary=summary, tool_call=tool_call)

    @classmethod
    def await_outcome(cls, observation: str = "awaiting outcome") -> "ModelDecision":
        return cls("await_outcome", observation=observation)

    @classmethod
    def blocked(cls, summary: str = "blocked") -> "ModelDecision":
        return cls("blocked", summary=summary)


@dataclass(frozen=True)
class PlanDraft:
    steps: list[dict[str, Any]]
    summary: str = ""


class RuntimeModel(Protocol):
    async def needs_clarification(self, goal: dict[str, Any], interactions: list[str]) -> bool: ...

    async def plan(self, goal: dict[str, Any], interactions: list[str]) -> PlanDraft: ...

    async def decide(self, step: dict[str, Any], observation: str, iteration: int) -> ModelDecision: ...

    async def reflect(self, goal: dict[str, Any], plan: PlanVersion, run_id: str) -> list[dict[str, Any]]: ...


class MockModelGateway:
    def __init__(
        self,
        *,
        clarification_required: bool = False,
        plan_steps: list[dict[str, Any]] | None = None,
        decisions: Iterable[ModelDecision] | None = None,
        reflection_candidates: list[dict[str, Any]] | None = None,
    ) -> None:
        self.clarification_required = clarification_required
        self.plan_draft = PlanDraft(plan_steps or [{"id": "step-1", "title": "Make progress"}])
        self.decisions = deque(decisions or [])
        self.reflection_candidates = reflection_candidates or []

    async def needs_clarification(self, goal: dict[str, Any], interactions: list[str]) -> bool:
        return self.clarification_required and len(interactions) == 1

    async def plan(self, goal: dict[str, Any], interactions: list[str]) -> PlanDraft:
        return self.plan_draft

    async def decide(self, step: dict[str, Any], observation: str, iteration: int) -> ModelDecision:
        return self.decisions.popleft() if self.decisions else ModelDecision.complete()

    async def reflect(self, goal: dict[str, Any], plan: PlanVersion, run_id: str) -> list[dict[str, Any]]:
        return self.reflection_candidates


@dataclass(frozen=True)
class RunSnapshot:
    id: str
    goal_id: str
    session_id: str
    state: AgentState
    resume_state: AgentState | None
    current_plan_version_id: str | None
    current_step_id: str | None
    version: int
    budget: dict[str, Any]
    project_id: str | None = None


class AgentRuntime:
    def __init__(
        self,
        *,
        db: Database,
        events: EventStore,
        plans: PlanVersionService,
        approvals: ApprovalService,
        checkpoints: CheckpointStore,
        memory: MemoryService,
        tools: ToolRegistry,
        model: RuntimeModel,
        config: RuntimeConfig | None = None,
    ) -> None:
        self.db = db
        self.events = events
        self.plans = plans
        self.approvals = approvals
        self.checkpoints = checkpoints
        self.memory = memory
        self.tools = tools
        self.model = model
        self.config = config or RuntimeConfig()
        self.stats = StatsProjector(db, events)
        self.events.projector = self.stats
        self.context_assembler = ContextAssembler()
        self.state_machine = StateMachine()
        self._locks: dict[str, asyncio.Lock] = {}

    async def create_goal(self, title: str, description: str, project_id: str | None = None) -> RunSnapshot:
        goal_id = f"goal_{uuid.uuid4().hex}"
        session_id = f"session_{uuid.uuid4().hex}"
        run_id = f"run_{uuid.uuid4().hex}"
        now = _now()
        budget = {
            "react_iterations_remaining": self.config.max_react_iterations_per_step,
            "react_iteration": 0,
            "consecutive_tool_errors": 0,
            "identical_actions": {},
            "applied_memory_versions": [],
        }
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO goals(id, title, description, project_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (goal_id, title, description, project_id, now, now),
            )
            connection.execute(
                "INSERT INTO sessions(id, goal_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, goal_id, now, now),
            )
            connection.execute(
                "INSERT INTO runs(id, goal_id, session_id, state, budget_json, created_at, updated_at) "
                "VALUES (?, ?, ?, 'RECEIVED', ?, ?, ?)",
                (run_id, goal_id, session_id, json.dumps(budget), now, now),
            )
        self.events.append(run_id, goal_id, "run.created", "runtime", {})
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> RunSnapshot:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT runs.*, goals.project_id FROM runs JOIN goals ON goals.id = runs.goal_id WHERE runs.id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunSnapshot(
            id=row["id"],
            goal_id=row["goal_id"],
            session_id=row["session_id"],
            state=AgentState(row["state"]),
            resume_state=AgentState(row["resume_state"]) if row["resume_state"] else None,
            current_plan_version_id=row["current_plan_version_id"],
            current_step_id=row["current_step_id"],
            version=row["version"],
            budget=json.loads(row["budget_json"]),
            project_id=row["project_id"],
        )

    def recoverable_runs(self) -> list[RunSnapshot]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM runs WHERE state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED') ORDER BY updated_at, id"
            ).fetchall()
        return [self.get_run(row["id"]) for row in rows]

    async def handle_message(self, run_id: str, content: str) -> RunSnapshot:
        lock = self._lock(run_id)
        async with lock:
            run = self.get_run(run_id)
            interaction_id = f"interaction_{uuid.uuid4().hex}"
            now = _now()
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO interactions(id, run_id, role, content, created_at) VALUES (?, ?, 'user', ?, ?)",
                    (interaction_id, run_id, content, now),
                )
                connection.execute(
                    "INSERT INTO messages(id, run_id, interaction_id, role, content, created_at) VALUES (?, ?, ?, 'user', ?, ?)",
                    (f"message_{uuid.uuid4().hex}", run_id, interaction_id, content, now),
                )
            self.events.append(run_id, run.goal_id, "interaction.started", "user", {"interaction_id": interaction_id})
            interactions = self._interaction_text(run_id)
            goal = self._goal(run.goal_id)
            if run.state == AgentState.RECEIVED:
                needs_clarification = await self._model_call(
                    run,
                    "clarification",
                    self.model.needs_clarification,
                    goal,
                    interactions,
                )
                if needs_clarification is None:
                    return self.get_run(run_id)
                if needs_clarification:
                    self._transition(run, AgentState.CLARIFYING, {"reason": "more information required"})
                    return self.get_run(run_id)
                self._transition(run, AgentState.PLANNING, {})
                run = self.get_run(run_id)
            elif run.state == AgentState.CLARIFYING:
                self._transition(run, AgentState.PLANNING, {"reason": "clarification received"})
                run = self.get_run(run_id)
            if run.state == AgentState.PLANNING:
                draft = await self._model_call(run, "planning", self.model.plan, goal, interactions)
                if draft is None:
                    return self.get_run(run_id)
                plan = self.plans.create(run_id, run.goal_id, draft.steps, draft.summary)
                self._set_run_fields(run_id, current_plan_version_id=plan.id)
                self.events.append(
                    run_id,
                    run.goal_id,
                    "plan.version_created",
                    "runtime",
                    {"plan_version_id": plan.id, "version": plan.version},
                )
                self._transition(self.get_run(run_id), AgentState.AWAITING_APPROVAL, {"plan_version_id": plan.id})
            self.events.append(run_id, run.goal_id, "interaction.ended", "runtime", {"interaction_id": interaction_id})
            return self.get_run(run_id)

    async def approve_plan(self, run_id: str, version: int) -> RunSnapshot:
        lock = self._lock(run_id)
        async with lock:
            run = self.get_run(run_id)
            plan = self.plans.current(run_id)
            if plan.version != version:
                raise ValueError("plan version conflict")
            approved = self.plans.approve(plan.id)
            self.events.append(
                run_id,
                run.goal_id,
                "plan.approved",
                "user",
                {"plan_version_id": approved.id, "version": approved.version},
            )
            self._transition(self.get_run(run_id), AgentState.EXECUTING, {"plan_version_id": approved.id})
            return await self._execute_locked(run_id)

    async def revise_plan(
        self,
        run_id: str,
        expected_version: int,
        steps: list[dict[str, Any]],
        summary: str = "",
    ) -> PlanVersion:
        lock = self._lock(run_id)
        async with lock:
            run = self.get_run(run_id)
            plan = self.plans.revise(run_id, run.goal_id, expected_version, steps, summary)
            self._set_run_fields(run_id, current_plan_version_id=plan.id)
            self.events.append(
                run_id,
                run.goal_id,
                "plan.version_created",
                "user",
                {"plan_version_id": plan.id, "version": plan.version, "base_version": expected_version},
            )
            return plan

    def pending_approvals(self, run_id: str) -> list[Approval]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND status = 'pending' ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [
            Approval(row["id"], row["run_id"], row["tool_call_id"], row["params_hash"], row["status"])
            for row in rows
        ]

    async def grant_approval(self, approval_id: str) -> RunSnapshot:
        lock_run_id = self._approval_run_id(approval_id)
        async with self._lock(lock_run_id):
            approval = self._approval_row(approval_id)
            params = json.loads(approval["params_json"])
            self.approvals.grant(approval_id, approval["run_id"], approval["tool_call_id"], params)
            run = self.get_run(approval["run_id"])
            self.events.append(
                run.id,
                run.goal_id,
                "approval.granted",
                "user",
                {"approval_id": approval_id, "tool_call_id": approval["tool_call_id"]},
            )
            return await self._execute_locked(run.id)

    async def reject_approval(self, approval_id: str) -> RunSnapshot:
        run_id = self._approval_run_id(approval_id)
        async with self._lock(run_id):
            approval = self._approval_row(approval_id)
            params = json.loads(approval["params_json"])
            self.approvals.reject(approval_id, approval["run_id"], approval["tool_call_id"], params)
            run = self.get_run(run_id)
            self.events.append(run_id, run.goal_id, "approval.rejected", "user", {"approval_id": approval_id})
            return await self._execute_locked(run_id)

    async def add_budget(self, run_id: str, amount: int) -> RunSnapshot:
        if amount <= 0:
            raise ValueError("budget addition must be positive")
        lock = self._lock(run_id)
        async with lock:
            run = self.get_run(run_id)
            budget = dict(run.budget)
            budget["react_iterations_remaining"] = int(budget.get("react_iterations_remaining", 0)) + amount
            self._set_run_fields(run_id, budget=budget)
            self.events.append(run_id, run.goal_id, "budget.warning", "user", {"added": amount})
            return self.get_run(run_id)

    async def cancel(self, run_id: str) -> RunSnapshot:
        lock = self._lock(run_id)
        async with lock:
            run = self.get_run(run_id)
            if run.state not in {AgentState.COMPLETED, AgentState.FAILED, AgentState.CANCELLED}:
                self._transition(run, AgentState.CANCELLED, {"reason": "user cancelled"})
                self.events.append(run_id, run.goal_id, "run.cancelled", "user", {})
            return self.get_run(run_id)

    async def cancel_step(self, run_id: str, step_id: str) -> RunSnapshot:
        lock = self._lock(run_id)
        async with lock:
            run = self.get_run(run_id)
            plan = self.plans.get(run.current_plan_version_id) if run.current_plan_version_id else self.plans.current(run_id)
            self.plans.mark_step_cancelled(plan.id, step_id)
            self._set_run_fields(run_id, current_step_id=None)
            self.events.append(
                run_id,
                run.goal_id,
                "plan.step_cancelled",
                "user",
                {"plan_version_id": plan.id, "plan_step_id": step_id},
            )
            self._save_checkpoint(run_id, "step cancelled")
            return self.get_run(run_id)

    async def resume(self, run_id: str) -> RunSnapshot:
        lock = self._lock(run_id)
        async with lock:
            run = self.get_run(run_id)
            if run.state == AgentState.AWAITING_OUTCOME:
                return run
            if run.state != AgentState.BLOCKED:
                return await self._execute_locked(run_id) if run.state == AgentState.EXECUTING else run
            if run.resume_state is None:
                raise ValueError("blocked run has no resume state")
            self._transition(run, run.resume_state, {"resume": True}, resume_state=run.resume_state)
            self.events.append(run_id, run.goal_id, "run.resumed", "user", {})
            return await self._execute_locked(run_id)

    async def continue_outcome(self, run_id: str, finished: bool) -> RunSnapshot:
        lock = self._lock(run_id)
        async with lock:
            run = self.get_run(run_id)
            if run.state != AgentState.AWAITING_OUTCOME:
                return run
            if finished:
                self._transition(run, AgentState.REFLECTING, {"outcome": "finished"})
                return await self._reflect_locked(run_id)
            self._transition(run, AgentState.EXECUTING, {"outcome": "continue"})
            return await self._execute_locked(run_id)

    async def _execute_locked(self, run_id: str) -> RunSnapshot:
        run = self.get_run(run_id)
        if run.state != AgentState.EXECUTING:
            return run
        while True:
            run = self.get_run(run_id)
            plan = self.plans.get(run.current_plan_version_id) if run.current_plan_version_id else self.plans.current(run_id)
            step = next(
                (
                    item
                    for item in plan.steps
                    if item.id == run.current_step_id and item.status not in {"completed", "cancelled"}
                ),
                None,
            )
            if step is None:
                step = next((item for item in plan.steps if item.status == "pending"), None)
            if step is None:
                if self.get_run(run_id).state == AgentState.EXECUTING:
                    self._transition(self.get_run(run_id), AgentState.REFLECTING, {"plan_complete": True})
                return await self._reflect_locked(run_id)
            self._set_run_fields(run_id, current_step_id=step.id)
            self.events.append(
                run_id,
                run.goal_id,
                "plan.step_started",
                "runtime",
                {"plan_step_id": step.id, "plan_version_id": plan.id},
            )
            applied = self.memory.apply_confirmed(run_id, run.goal_id, run.project_id, None)
            if applied:
                budget = dict(self.get_run(run_id).budget)
                versions = list(budget.get("applied_memory_versions", []))
                for record in applied:
                    reference = f"{record.id}:{record.version or 0}"
                    if reference not in versions:
                        versions.append(reference)
                budget["applied_memory_versions"] = versions
                self._set_run_fields(run_id, budget=budget)
            outcome = await self._execute_step(run_id, plan, step.id)
            if outcome in {"blocked", "awaiting_outcome", "approval"}:
                return self.get_run(run_id)

    async def _execute_step(self, run_id: str, plan: PlanVersion, step_id: str) -> str:
        started = time.monotonic()
        observation = ""
        step = next(item for item in plan.steps if item.id == step_id)
        pending = self._pending_action(run_id)
        while True:
            resumed_pending_action = pending is not None
            run = self.get_run(run_id)
            budget = dict(run.budget)
            iteration = int(budget.get("react_iteration", 0))
            if time.monotonic() - started > self.config.max_step_wall_time_seconds:
                return self._block(run_id, "step wall time exhausted", budget, "budget.exhausted")
            if not pending and int(budget.get("react_iterations_remaining", 0)) <= 0:
                return self._block(run_id, "react iteration budget exhausted", budget, "budget.exhausted")
            if pending:
                decision = ModelDecision.tool(
                    ToolCall(pending["id"], pending["name"], pending["params"]),
                    "resume pending tool",
                )
                pending = None
            else:
                budget["react_iterations_remaining"] = int(budget.get("react_iterations_remaining", 0)) - 1
                budget["react_iteration"] = iteration + 1
                self._set_run_fields(run_id, budget=budget)
                decision = await self._model_call(
                    run,
                    "react",
                    self.model.decide,
                    {
                        "id": step.id,
                        "title": step.title,
                        "description": step.description,
                        "position": step.position,
                        "status": step.status,
                    },
                    observation,
                    iteration + 1,
                )
                if decision is None:
                    return "blocked"
            iteration = int(self.get_run(run_id).budget.get("react_iteration", iteration))
            correlation = {"plan_version_id": plan.id, "plan_step_id": step_id, "react_iteration": iteration}
            self.events.append(run_id, run.goal_id, "react.iteration_started", "runtime", correlation)
            if decision.action == "complete_step":
                self.plans.mark_step_completed(plan.id, step_id)
                self.events.append(
                    run_id,
                    run.goal_id,
                    "plan.step_finished",
                    "runtime",
                    {**correlation, "summary": decision.summary},
                )
                self.events.append(run_id, run.goal_id, "react.iteration_finished", "runtime", correlation)
                self._set_run_fields(
                    run_id,
                    current_step_id=None,
                    budget=self._reset_step_budget(
                        self.get_run(run_id).budget,
                        self.config.max_react_iterations_per_step,
                    ),
                )
                return "completed"
            if decision.action == "await_outcome":
                self.events.append(run_id, run.goal_id, "react.iteration_finished", "runtime", correlation)
                self._transition(self.get_run(run_id), AgentState.AWAITING_OUTCOME, {"observation": decision.observation})
                self._save_checkpoint(run_id, decision.observation, pending_actions=[])
                return "awaiting_outcome"
            if decision.action == "blocked":
                self.events.append(run_id, run.goal_id, "react.iteration_finished", "runtime", correlation)
                return self._block(run_id, decision.summary or "model requested block", self.get_run(run_id).budget, "run.blocked")
            if decision.action == "continue":
                observation = decision.observation
                self.events.append(run_id, run.goal_id, "react.iteration_finished", "runtime", correlation)
                continue
            if decision.action != "tool_call" or decision.tool_call is None:
                return self._block(run_id, "invalid model action", self.get_run(run_id).budget, "run.blocked")
            action_key = json.dumps(
                {"name": decision.tool_call.name, "params": decision.tool_call.params},
                ensure_ascii=False,
                sort_keys=True,
            )
            if not resumed_pending_action:
                actions = dict(self.get_run(run_id).budget.get("identical_actions", {}))
                actions[action_key] = int(actions.get(action_key, 0)) + 1
                budget = dict(self.get_run(run_id).budget)
                budget["identical_actions"] = actions
                self._set_run_fields(run_id, budget=budget)
                if actions[action_key] > self.config.max_identical_actions:
                    return self._block(run_id, "identical action budget exhausted", budget, "budget.exhausted")
            call = decision.tool_call
            self.events.append(run_id, run.goal_id, "tool.proposed", "model", {**correlation, "tool_call_id": call.id, "name": call.name})
            try:
                approval_before = self._pending_approval_for(call.id, run_id)
                if approval_before:
                    self._save_checkpoint(run_id, "approval pending", [
                        {"id": call.id, "name": call.name, "params": call.params}
                    ])
                    return "approval"
                result = self.checkpoints.completed_tool_result(run_id, call.id)
                if result is None:
                    self.tools.authorize(call, run_id=run_id, skill_tools=self._skill_tools_for("react"))
                    self.events.append(
                        run_id,
                        run.goal_id,
                        "tool.execution.started",
                        "tool",
                        {**correlation, "tool_call_id": call.id, "name": call.name},
                    )
                    tool_result = self.tools.execute(call, run_id=run_id, skill_tools=self._skill_tools_for("react"))
                    self.events.append(
                        run_id,
                        run.goal_id,
                        "tool.execution.finished",
                        "tool",
                        {**correlation, "tool_call_id": call.id, "result": tool_result.as_dict()},
                    )
                else:
                    tool_result = ToolResult(**result)
            except ApprovalRequired:
                approval = self.approvals.request(run_id, call.id, call.name, call.params)
                self.events.append(
                    run_id,
                    run.goal_id,
                    "approval.requested",
                    "runtime",
                    {**correlation, "approval_id": approval.id, "tool_call_id": call.id},
                )
                self._save_checkpoint(
                    run_id,
                    "approval pending",
                    [{"id": call.id, "name": call.name, "params": call.params}],
                    [approval.id],
                )
                return "approval"
            except ToolRejected as exc:
                return self._block(run_id, f"tool rejected: {exc}", self.get_run(run_id).budget, "run.blocked")
            if not tool_result.ok:
                budget = dict(self.get_run(run_id).budget)
                budget["consecutive_tool_errors"] = int(budget.get("consecutive_tool_errors", 0)) + 1
                self._set_run_fields(run_id, budget=budget)
                if budget["consecutive_tool_errors"] >= self.config.max_consecutive_tool_errors:
                    return self._block(run_id, "consecutive tool errors exhausted", budget, "budget.exhausted")
            else:
                budget = dict(self.get_run(run_id).budget)
                budget["consecutive_tool_errors"] = 0
                self._set_run_fields(run_id, budget=budget)
            observation = tool_result.summary
            if tool_result.data:
                observation += "\n" + json.dumps(tool_result.data, ensure_ascii=False, sort_keys=True, default=str)
            if tool_result.artifact_ref:
                observation += f"\nartifact_ref={tool_result.artifact_ref}"
            self.events.append(run_id, run.goal_id, "react.iteration_finished", "runtime", correlation)

    async def _reflect_locked(self, run_id: str) -> RunSnapshot:
        run = self.get_run(run_id)
        plan = self.plans.get(run.current_plan_version_id) if run.current_plan_version_id else self.plans.current(run_id)
        candidates = await self._model_call(run, "reflection", self.model.reflect, self._goal(run.goal_id), plan, run_id)
        if candidates is None:
            return self.get_run(run_id)
        for candidate in candidates:
            try:
                self.memory.create_candidate(
                    run_id,
                    run.goal_id,
                    candidate["kind"],
                    candidate["content"],
                    candidate["scope"],
                    candidate.get("confidence", 0.5),
                    candidate.get("evidence_event_ids", []),
                    candidate.get("project_id"),
                    candidate.get("skill_name"),
                )
            except (KeyError, TypeError, ValueError):
                self.events.append(
                    run_id,
                    run.goal_id,
                    "memory.candidate_discarded",
                    "runtime",
                    {"reason": "invalid model candidate"},
                )
        if self.get_run(run_id).state == AgentState.REFLECTING:
            self._transition(self.get_run(run_id), AgentState.COMPLETED, {"candidates": len(candidates)})
        self.events.append(run_id, run.goal_id, "run.completed", "runtime", {"candidate_count": len(candidates)})
        return self.get_run(run_id)

    def _block(
        self,
        run_id: str,
        reason: str,
        budget: dict[str, Any],
        event_type: str,
    ) -> str:
        run = self.get_run(run_id)
        budget = dict(budget)
        budget["blocked_reason"] = reason
        self._set_run_fields(run_id, budget=budget)
        if run.state != AgentState.BLOCKED:
            self._transition(self.get_run(run_id), AgentState.BLOCKED, {"reason": reason})
        if event_type == "budget.exhausted":
            self.events.append(run_id, run.goal_id, "budget.exhausted", "runtime", {"reason": reason})
        self._save_checkpoint(run_id, reason)
        self.events.append(run_id, run.goal_id, "run.blocked", "runtime", {"reason": reason})
        return "blocked"

    def _save_checkpoint(
        self,
        run_id: str,
        observation: str,
        pending_actions: list[dict[str, Any]] | None = None,
        pending_approvals: list[str] | None = None,
    ) -> Checkpoint:
        run = self.get_run(run_id)
        plan = self.plans.get(run.current_plan_version_id) if run.current_plan_version_id else None
        checkpoint = self.checkpoints.save(
            Checkpoint(
                run_id=run_id,
                state=run.state.value,
                plan_version_id=run.current_plan_version_id,
                step_id=run.current_step_id,
                completed_steps=[step.id for step in plan.steps if step.status == "completed"] if plan else [],
                react_iteration=int(run.budget.get("react_iteration", 0)),
                remaining_budget=run.budget,
                observation=observation,
                artifact_refs=[],
                pending_approvals=pending_approvals or [],
                applied_memory_versions=list(run.budget.get("applied_memory_versions", [])),
                pending_actions=pending_actions or [],
                last_event_seq=self.events.list(run_id)[-1].seq if self.events.list(run_id) else 0,
            )
        )
        self.events.append(run_id, run.goal_id, "checkpoint.saved", "runtime", {"checkpoint_id": checkpoint.id})
        return checkpoint

    def _pending_action(self, run_id: str) -> dict[str, Any] | None:
        checkpoint = self.checkpoints.latest(run_id)
        if not checkpoint or not checkpoint.pending_actions:
            return None
        approval = self._approval_for(checkpoint.pending_actions[0]["id"], run_id)
        completed = self.checkpoints.completed_tool_result(run_id, checkpoint.pending_actions[0]["id"])
        return checkpoint.pending_actions[0] if (approval and approval["status"] in {"pending", "granted"}) or completed is not None else None

    def _pending_approval_for(self, tool_call_id: str, run_id: str) -> dict[str, Any] | None:
        approval = self._approval_for(tool_call_id, run_id)
        return approval if approval and approval["status"] == "pending" else None

    def _approval_for(self, tool_call_id: str, run_id: str) -> dict[str, Any] | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND tool_call_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (run_id, tool_call_id),
            ).fetchone()
        return dict(row) if row else None

    def _approval_run_id(self, approval_id: str) -> str:
        return self._approval_row(approval_id)["run_id"]

    def _approval_row(self, approval_id: str):
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(approval_id)
        return row

    def _transition(
        self,
        run: RunSnapshot,
        target: AgentState,
        data: dict[str, Any],
        resume_state: AgentState | None = None,
    ) -> None:
        transition = self.state_machine.transition(run.state, target, resume_state=resume_state or run.resume_state)
        self._set_run_fields(run.id, state=transition.state.value, resume_state=transition.resume_state.value if transition.resume_state else None)
        self.events.append(
            run.id,
            run.goal_id,
            "state.transitioned",
            "runtime",
            {"from": run.state.value, "to": transition.state.value, "resume_state": transition.resume_state.value if transition.resume_state else None, **data},
        )

    def _set_run_fields(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        values = dict(fields)
        if "budget" in values:
            values["budget_json"] = json.dumps(values.pop("budget"), ensure_ascii=False)
        values["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.db.transaction() as connection:
            connection.execute(
                f"UPDATE runs SET {assignments}, version = version + 1 WHERE id = ?",
                (*values.values(), run_id),
            )

    async def _model_call(self, run: RunSnapshot, kind: str, method, *args):
        invocation_id = f"invocation_{uuid.uuid4().hex}"
        attempt_id = f"{invocation_id}_attempt_1"
        self._prepare_model_context(run, kind, args)
        self.events.append(run.id, run.goal_id, "model.invocation_started", "runtime", {"model_invocation_id": invocation_id, "kind": kind})
        self.events.append(
            run.id,
            run.goal_id,
            "model.attempt_started",
            "runtime",
            {"model_invocation_id": invocation_id, "model_attempt_id": attempt_id, "attempt": 1},
        )
        try:
            result = await method(*args)
        except Exception as exc:
            from .model_gateway import GatewayError

            if not isinstance(exc, GatewayError):
                raise
            self.events.append(
                run.id,
                run.goal_id,
                "model.attempt_finished",
                "runtime",
                {"model_invocation_id": invocation_id, "model_attempt_id": attempt_id, "status": "failed", "error_kind": exc.kind},
            )
            self.events.append(
                run.id,
                run.goal_id,
                "model.invocation_finished",
                "runtime",
                {"model_invocation_id": invocation_id, "kind": kind, "status": "failed", "error_kind": exc.kind},
            )
            current = self.get_run(run.id)
            reason = f"model {exc.kind}"
            if current.state == AgentState.RECEIVED:
                self._transition(current, AgentState.FAILED, {"reason": reason})
                self.events.append(run.id, run.goal_id, "run.failed", "runtime", {"reason": reason})
                self._save_checkpoint(run.id, reason)
            elif current.state not in {AgentState.COMPLETED, AgentState.FAILED, AgentState.CANCELLED}:
                self._block(run.id, reason, current.budget, "run.blocked")
            return None
        else:
            response = getattr(self.model, "last_response", None)
            self._record_model_response(run, invocation_id, attempt_id, response)
            self._append_model_message(run, kind, invocation_id, response, result)
            self.events.append(run.id, run.goal_id, "model.invocation_finished", "runtime", {"model_invocation_id": invocation_id, "kind": kind, "status": "success"})
            return result

    def _prepare_model_context(self, run: RunSnapshot, kind: str, args: tuple[Any, ...]) -> None:
        setter = getattr(self.model, "set_context_snapshot", None)
        if setter is None:
            return
        goal = self._goal(run.goal_id)
        interactions = self._interaction_text(run.id)
        memories = [
            MemoryForContext(
                id=record.id,
                content=record.content,
                scope=record.scope,
                status=record.status,
                project_id=record.project_id,
                skill_name=record.skill_name,
            )
            for record in self.memory.all_records()
        ]
        plan = args[1] if kind == "reflection" and len(args) > 1 else ""
        step = args[0] if kind == "react" and args else ""
        observation = args[1] if kind == "react" and len(args) > 1 else ""
        snapshot = self.context_assembler.assemble(
            user_instruction=interactions[-1] if interactions else "",
            goal=json.dumps(goal, ensure_ascii=False, sort_keys=True),
            plan=json.dumps(plan, ensure_ascii=False, default=str) if plan else "",
            step=json.dumps(step, ensure_ascii=False, default=str),
            skill="goal-planning" if kind in {"clarification", "planning", "react"} else "reflection",
            history=interactions + ([observation] if observation else []),
            tool_results=[],
            memories=memories,
            project_id=run.project_id,
            skill_name="goal-planning" if kind in {"clarification", "planning", "react"} else "reflection",
        )
        setter(snapshot.snapshot_hash, snapshot.text)
        self.events.append(
            run.id,
            run.goal_id,
            "context.snapshot_created",
            "runtime",
            {"snapshot_hash": snapshot.snapshot_hash, "cropped": snapshot.cropped, "crop_count": snapshot.crop_count},
        )

    def _record_model_response(self, run: RunSnapshot, invocation_id: str, attempt_id: str, response: Any) -> None:
        if response is None:
            self.events.append(
                run.id,
                run.goal_id,
                "model.attempt_finished",
                "runtime",
                {"model_invocation_id": invocation_id, "model_attempt_id": attempt_id, "status": "success"},
            )
            return
        attempts = max(int(getattr(response, "attempts", 1)), 1)
        for number in range(2, attempts + 1):
            retry_id = f"{invocation_id}_attempt_{number}"
            self.events.append(run.id, run.goal_id, "model.attempt_started", "runtime", {"model_invocation_id": invocation_id, "model_attempt_id": retry_id, "attempt": number, "status": "retry"})
            self.events.append(run.id, run.goal_id, "model.attempt_finished", "runtime", {"model_invocation_id": invocation_id, "model_attempt_id": retry_id, "attempt": number, "status": "retry"})
        timing = response.timing
        if timing.ttft_seconds is not None:
            self.events.append(run.id, run.goal_id, "model.first_token", "runtime", {"model_invocation_id": invocation_id, "model_attempt_id": attempt_id, "ttft_seconds": timing.ttft_seconds})
        usage = response.usage
        self.events.append(
            run.id,
            run.goal_id,
            "model.usage_updated",
            "runtime",
            {
                "model_invocation_id": invocation_id,
                "model_attempt_id": attempt_id,
                "usage": {
                    "uncached_input_tokens": usage.uncached_input_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_write_tokens": usage.cache_write_tokens,
                    "output_tokens": usage.output_tokens,
                    "reasoning_tokens": usage.reasoning_tokens,
                },
            },
        )
        self.events.append(
            run.id,
            run.goal_id,
            "model.attempt_finished",
            "runtime",
            {"model_invocation_id": invocation_id, "model_attempt_id": attempt_id, "decode_seconds": timing.decode_seconds, "status": "success"},
        )

    def _append_model_message(self, run: RunSnapshot, kind: str, invocation_id: str, response: Any, result: Any) -> None:
        content = str(getattr(response, "message", "") or "") if response is not None else ""
        if not content:
            content = json.dumps(_model_result_payload(kind, result), ensure_ascii=False, default=str)
        message_id = f"message_{uuid.uuid4().hex}"
        interaction_id = self._latest_interaction_id(run.id)
        now = _now()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO messages(id, run_id, interaction_id, role, content, created_at) VALUES (?, ?, ?, 'assistant', ?, ?)",
                (message_id, run.id, interaction_id, content, now),
            )
        self.events.append(
            run.id,
            run.goal_id,
            "model.response",
            "model",
            {
                "message_id": message_id,
                "interaction_id": interaction_id,
                "model_invocation_id": invocation_id,
                "kind": kind,
                "content": content,
            },
        )

    def _interaction_text(self, run_id: str) -> list[str]:
        with self.db.connection() as connection:
            return [
                row["content"]
                for row in connection.execute(
                    "SELECT content FROM interactions WHERE run_id = ? ORDER BY created_at, id", (run_id,)
                )
            ]

    def _latest_interaction_id(self, run_id: str) -> str | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT id FROM interactions WHERE run_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return row["id"] if row else None

    def _goal(self, goal_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        return dict(row)

    def _step_title(self, plan: PlanVersion, step_id: str) -> str:
        return next(step.title for step in plan.steps if step.id == step_id)

    @staticmethod
    def _reset_step_budget(budget: dict[str, Any], max_react_iterations: int = 5) -> dict[str, Any]:
        result = dict(budget)
        result["react_iterations_remaining"] = max_react_iterations
        result["react_iteration"] = 0
        result["consecutive_tool_errors"] = 0
        result["identical_actions"] = {}
        return result

    def _lock(self, run_id: str) -> asyncio.Lock:
        return self._locks.setdefault(run_id, asyncio.Lock())

    @staticmethod
    def _skill_tools_for(skill_name: str) -> set[str]:
        if skill_name in {"goal-planning", "planning", "reflection"}:
            return {"local_time", "calculator", "read_note"}
        if skill_name == "react":
            return {"local_time", "calculator", "read_note", "write_note"}
        return set()


def _model_result_payload(kind: str, result: Any) -> dict[str, Any]:
    if kind == "clarification":
        return {"needs_clarification": bool(result)}
    if kind == "planning" and isinstance(result, PlanDraft):
        return {"summary": result.summary, "steps": result.steps}
    if kind == "react" and isinstance(result, ModelDecision):
        payload: dict[str, Any] = {"action": result.action}
        if result.summary:
            payload["summary"] = result.summary
        if result.observation:
            payload["observation"] = result.observation
        if result.tool_call:
            payload["tool_call"] = {
                "id": result.tool_call.id,
                "name": result.tool_call.name,
                "params": result.tool_call.params,
            }
        return payload
    if kind == "reflection" and isinstance(result, list):
        return {"candidates": result}
    return {"result": result}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
