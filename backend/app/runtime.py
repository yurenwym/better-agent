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
from .public_text import public_message, reason_message
from .skill_registry import SkillCatalog
from .skill_platform import SkillPlatform
from .stats import StatsProjector
from .tools import ToolCall, ToolRegistry, ToolRejected, ToolReconciliationRequired, ToolResult


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
    def complete(cls, summary: str = "当前步骤已完成") -> "ModelDecision":
        return cls("complete_step", summary=summary)

    @classmethod
    def continue_(cls, observation: str = "继续执行当前步骤") -> "ModelDecision":
        return cls("continue", observation=observation)

    @classmethod
    def tool(cls, tool_call: ToolCall, summary: str = "模型请求调用工具") -> "ModelDecision":
        return cls("tool_call", summary=summary, tool_call=tool_call)

    @classmethod
    def await_outcome(cls, observation: str = "正在等待外部结果") -> "ModelDecision":
        return cls("await_outcome", observation=observation)

    @classmethod
    def blocked(cls, summary: str = "当前任务需要暂停处理") -> "ModelDecision":
        return cls("blocked", summary=summary)


@dataclass(frozen=True)
class PlanDraft:
    steps: list[dict[str, Any]]
    summary: str = ""


class RuntimeModel(Protocol):
    async def needs_clarification(self, goal: dict[str, Any], interactions: list[str]) -> bool: ...

    async def plan(self, goal: dict[str, Any], interactions: list[str]) -> PlanDraft: ...

    async def decide(self, step: dict[str, Any], observation: str, iteration: int) -> ModelDecision: ...

    async def reflect(
        self, goal: dict[str, Any], plan: PlanVersion, run_id: str,
        evidence_catalog: list[dict[str, str | None]] | None = None,
    ) -> list[dict[str, Any]]: ...


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

    async def reflect(
        self, goal: dict[str, Any], plan: PlanVersion, run_id: str,
        evidence_catalog: list[dict[str, str | None]] | None = None,
    ) -> list[dict[str, Any]]:
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
    skill_names: tuple[str, ...] = ()
    source_plan_document_id: str | None = None
    source_plan_document_version_id: str | None = None
    source_plan_content_hash: str | None = None
    runtime_bundle_id: str | None = None
    source_turn_id: str | None = None
    root_budget_id: str | None = None


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
        conversation_model: Any | None = None,
        config: RuntimeConfig | None = None,
        skill_catalog: SkillCatalog | None = None,
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
        self.skill_platform = SkillPlatform(db, db.path.parent / "skills")
        from .trusted_connectors import TrustedConnectorService
        self.connectors = TrustedConnectorService(db)
        self.skills = skill_catalog or SkillCatalog(platform=self.skill_platform)
        from .conversation import ConversationService, ManagedTurnWorkerPool

        self.conversation = ConversationService(
            db,
            agent_runtime=self,
            route_model=conversation_model or model,
        )
        self.plan_documents = self.conversation.plan_documents
        from .goal_program_compiler import FixedGoalProgramCompiler, GoalProgramCompiler
        from .goal_programs import GoalProgramService

        goal_compiler = GoalProgramCompiler(model.gateway) if hasattr(model, "gateway") else FixedGoalProgramCompiler()
        self.goal_programs = GoalProgramService(
            db, goal_compiler, plan_documents=self.plan_documents, conversation=self.conversation
        )
        from .goal_adjustments import GoalAdjustmentService
        self.goal_adjustments = GoalAdjustmentService(self.goal_programs, goal_compiler, self.plan_documents)
        from .goal_reviews import GoalReviewService, ManagedGoalReviewWorker
        self.goal_reviews = GoalReviewService(self.goal_programs, goal_compiler, self.goal_adjustments)
        self.goal_programs.reviews = self.goal_reviews
        self.goal_review_worker = ManagedGoalReviewWorker(self.goal_reviews)
        self.turn_worker = ManagedTurnWorkerPool(self.conversation)
        self.research = None
        self.research_worker = None
        self.scheduler = None
        self.archiver = None
        self.safety_judge = None
        self.stats = StatsProjector(db, events)
        self.events.projector = self.stats
        self.context_assembler = ContextAssembler()
        self.state_machine = StateMachine()
        self._locks: dict[str, asyncio.Lock] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._active_model_tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancel_guard = asyncio.Lock()

    async def create_goal(self, title: str, description: str, project_id: str | None = None) -> RunSnapshot:
        goal_id = f"goal_{uuid.uuid4().hex}"
        session_id = f"session_{uuid.uuid4().hex}"
        run_id = f"run_{uuid.uuid4().hex}"
        now = _now()
        budget = self.initial_budget()
        with self.db.transaction() as connection:
            bundle_id = None
            evolution = getattr(self, "evolution", None)
            if evolution is not None:
                bundle_id, _ = evolution.assign_run(run_id, project_id or goal_id, connection=connection)
            root_budget_id = None
            costs = getattr(self, "costs", None)
            if self.db.backend == "postgresql" and costs is not None:
                root_budget_id = costs.create_default_root_budget(
                    "local-user", "run", run_id, connection=connection,
                )["id"]
            connection.execute(
                "INSERT INTO goals(id, title, description, project_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (goal_id, title, description, project_id, now, now),
            )
            connection.execute(
                "INSERT INTO sessions(id, goal_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, goal_id, now, now),
            )
            connection.execute(
                "INSERT INTO runs(id, goal_id, session_id, state, budget_json, runtime_bundle_id, root_budget_id, created_at, updated_at) "
                "VALUES (?, ?, ?, 'RECEIVED', ?, ?, ?, ?, ?)",
                (run_id, goal_id, session_id, json.dumps(budget), bundle_id, root_budget_id, now, now),
            )
        self.events.append(run_id, goal_id, "run.created", "runtime", {})
        return self.get_run(run_id)

    def initial_budget(self) -> dict[str, Any]:
        return {
            "react_iterations_remaining": self.config.max_react_iterations_per_step,
            "react_iteration": 0,
            "consecutive_tool_errors": 0,
            "identical_actions": {},
            "applied_memory_versions": [],
        }

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
            skill_names=tuple(json.loads(row["skill_names_json"] or "[]")),
            source_plan_document_id=row["source_plan_document_id"],
            source_plan_document_version_id=row["source_plan_document_version_id"],
            source_plan_content_hash=row["source_plan_content_hash"],
            runtime_bundle_id=row["runtime_bundle_id"],
            source_turn_id=row["source_turn_id"],
            root_budget_id=row["root_budget_id"] if "root_budget_id" in row.keys() else None,
        )

    def recoverable_runs(self) -> list[RunSnapshot]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM runs WHERE state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED') ORDER BY updated_at, id"
            ).fetchall()
        return [self.get_run(row["id"]) for row in rows]

    async def handle_message(
        self,
        run_id: str,
        content: str,
        skill_names: list[str] | tuple[str, ...] | None = None,
    ) -> RunSnapshot:
        lock = self._lock(run_id)
        async with lock:
            run = self.get_run(run_id)
            if skill_names is not None:
                selected_skills = self.skills.validate(skill_names)
                if selected_skills != run.skill_names:
                    self.skill_platform.bind(
                        "RUN", run_id, self.skills.version_ids(selected_skills),
                        idempotency_key=f"run-skill-binding:{run_id}:{','.join(selected_skills)}",
                    )
                    self._set_run_fields(run_id, skill_names_json=json.dumps(selected_skills, ensure_ascii=False))
                    self.events.append(
                        run_id,
                        run.goal_id,
                        "skills.selected",
                        "user",
                        {"skill_names": list(selected_skills)},
                    )
                    binding = self.skill_platform.binding("RUN", run_id)
                    self.events.append(
                        run_id, run.goal_id, "skill.snapshot_applied", "runtime",
                        {"binding_snapshot_digest": binding["snapshot_digest"], "skill_version_ids": binding["version_ids"]},
                    )
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
            binding = json.loads(approval["binding_json"] or "{}")
            self.approvals.grant(approval_id, approval["run_id"], approval["tool_call_id"], params, binding=binding)
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
            binding = json.loads(approval["binding_json"] or "{}")
            self.approvals.reject(approval_id, approval["run_id"], approval["tool_call_id"], params, binding=binding)
            run = self.get_run(run_id)
            self.events.append(run_id, run.goal_id, "approval.rejected", "user", {"approval_id": approval_id})
            return await self._execute_locked(run_id)

    async def add_budget(self, run_id: str, amount: int) -> RunSnapshot:
        if amount <= 0:
            raise ValueError("追加的执行轮次必须大于零")
        lock = self._lock(run_id)
        async with lock:
            run = self.get_run(run_id)
            reason_code = run.budget.get("blocked_reason_code") or run.budget.get("blocked_reason")
            if run.state != AgentState.BLOCKED or reason_code not in {
                "REACT_ITERATION_BUDGET_EXHAUSTED",
                "react iteration budget exhausted",
            }:
                raise ValueError("只有执行轮次用完后才能追加轮次并恢复")
            budget = dict(run.budget)
            budget["react_iterations_remaining"] = int(budget.get("react_iterations_remaining", 0)) + amount
            self._set_run_fields(run_id, budget=budget)
            self.events.append(run_id, run.goal_id, "budget.warning", "user", {"added": amount})
            return self.get_run(run_id)

    async def cancel(self, run_id: str) -> RunSnapshot:
        self._cancel_event(run_id).set()
        async with self._cancel_guard:
            run = self.get_run(run_id)
            if run.state not in {AgentState.COMPLETED, AgentState.FAILED, AgentState.CANCELLED}:
                self._transition(run, AgentState.CANCELLED, {"reason": "user cancelled"})
                self.events.append(run_id, run.goal_id, "run.cancelled", "user", {})
                self._save_checkpoint(run_id, "user cancelled")
                await self._finish_exposure(run_id, success=False)
            active_task = self._active_model_tasks.get(run_id)
            current_task = asyncio.current_task()
            if active_task is not None and active_task is not current_task:
                active_task.cancel("run cancelled")
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
        if run.state != AgentState.EXECUTING or self._is_cancelled(run_id):
            return run
        while True:
            run = self.get_run(run_id)
            if self._is_cancelled(run_id):
                return run
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
            outcome = await self._execute_step(run_id, plan, step.id)
            if outcome in {"blocked", "awaiting_outcome", "approval", "cancelled"}:
                return self.get_run(run_id)

    async def _execute_step(self, run_id: str, plan: PlanVersion, step_id: str) -> str:
        started = time.monotonic()
        observation = ""
        step = next(item for item in plan.steps if item.id == step_id)
        pending = self._pending_action(run_id)
        while True:
            if self._is_cancelled(run_id):
                return "cancelled"
            resumed_pending_action = pending is not None
            run = self.get_run(run_id)
            budget = dict(run.budget)
            iteration = int(budget.get("react_iteration", 0))
            if time.monotonic() - started > self.config.max_step_wall_time_seconds:
                return self._block(
                    run_id,
                    "STEP_WALL_TIME_EXHAUSTED",
                    reason_message("STEP_WALL_TIME_EXHAUSTED"),
                    budget,
                    "budget.exhausted",
                )
            if not pending and int(budget.get("react_iterations_remaining", 0)) <= 0:
                return self._block(
                    run_id,
                    "REACT_ITERATION_BUDGET_EXHAUSTED",
                    reason_message("REACT_ITERATION_BUDGET_EXHAUSTED"),
                    budget,
                    "budget.exhausted",
                )
            if pending:
                decision = ModelDecision.tool(
                    ToolCall(pending["id"], pending["name"], pending["params"]),
                    "正在恢复尚未完成的工具调用",
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
                    return "cancelled" if self._is_cancelled(run_id) else "blocked"
            if self._is_cancelled(run_id):
                return "cancelled"
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
                return self._block(
                    run_id,
                    "MODEL_REQUESTED_BLOCK",
                    decision.summary or "模型请求暂停当前任务",
                    self.get_run(run_id).budget,
                    "run.blocked",
                )
            if decision.action == "continue":
                observation = decision.observation
                self.events.append(run_id, run.goal_id, "react.iteration_finished", "runtime", correlation)
                continue
            if decision.action != "tool_call" or decision.tool_call is None:
                return self._block(
                    run_id,
                    "INVALID_MODEL_ACTION",
                    reason_message("INVALID_MODEL_ACTION"),
                    self.get_run(run_id).budget,
                    "run.blocked",
                )
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
                    return self._block(
                        run_id,
                        "IDENTICAL_ACTION_BUDGET_EXHAUSTED",
                        reason_message("IDENTICAL_ACTION_BUDGET_EXHAUSTED"),
                        budget,
                        "budget.exhausted",
                    )
            call = decision.tool_call
            self.events.append(run_id, run.goal_id, "tool.proposed", "model", {**correlation, "tool_call_id": call.id, "name": call.name})
            try:
                approval_before = self._pending_approval_for(call.id, run_id)
                if approval_before:
                    self._save_checkpoint(run_id, "工具调用正在等待你的批准", [
                        {"id": call.id, "name": call.name, "params": call.params}
                    ])
                    return "approval"
                result = self.checkpoints.completed_tool_result(run_id, call.id)
                if result is None:
                    skill_tools = self._skill_tools_for_run(run, "react")
                    authorization = self._tool_authorization(run, "react", call)
                    try:
                        self.tools.authorize(call, run_id=run_id, skill_tools=skill_tools, authorization=authorization)
                    except ToolRejected as exc:
                        self.events.append(run_id, run.goal_id, "tool.authorization.denied", "runtime", {
                            "tool_call_id": call.id, "tool_name": call.name, "reason": str(exc),
                        })
                        raise
                    self.events.append(
                        run_id,
                        run.goal_id,
                        "tool.execution.started",
                        "tool",
                        {**correlation, "tool_call_id": call.id, "name": call.name},
                    )
                    tool_result = self.tools.execute(
                        call, run_id=run_id, skill_tools=skill_tools, authorization=authorization,
                    )
                    self.events.append(
                        run_id,
                        run.goal_id,
                        "tool.execution.finished",
                        "tool",
                        {**correlation, "tool_call_id": call.id, "result": tool_result.as_dict()},
                    )
                    if self._is_cancelled(run_id):
                        return "cancelled"
                else:
                    tool_result = ToolResult(**result)
            except ApprovalRequired:
                approval = self.approvals.request(
                    run_id, call.id, call.name, call.params,
                    binding=self._tool_authorization(run, "react", call),
                )
                self.events.append(
                    run_id,
                    run.goal_id,
                    "approval.requested",
                    "runtime",
                    {**correlation, "approval_id": approval.id, "tool_call_id": call.id},
                )
                self._save_checkpoint(
                    run_id,
                    "工具调用正在等待你的批准",
                    [{"id": call.id, "name": call.name, "params": call.params}],
                    [approval.id],
                )
                return "approval"
            except ToolReconciliationRequired:
                reason = "TOOL_RECONCILIATION_REQUIRED"
                outcome = self._block(
                    run_id, reason, reason_message(reason),
                    self.get_run(run_id).budget, "run.blocked",
                )
                if outcome != "cancelled":
                    self._save_checkpoint(run_id, reason_message(reason), [
                        {"id": call.id, "name": call.name, "params": call.params}
                    ])
                return outcome
            except ToolRejected as exc:
                return self._block(
                    run_id,
                    "TOOL_AUTHORIZATION_DENIED",
                    reason_message("TOOL_AUTHORIZATION_DENIED"),
                    self.get_run(run_id).budget,
                    "run.blocked",
                )
            if not tool_result.ok:
                budget = dict(self.get_run(run_id).budget)
                budget["consecutive_tool_errors"] = int(budget.get("consecutive_tool_errors", 0)) + 1
                self._set_run_fields(run_id, budget=budget)
                if budget["consecutive_tool_errors"] >= self.config.max_consecutive_tool_errors:
                    return self._block(
                        run_id,
                        "CONSECUTIVE_TOOL_ERRORS_EXHAUSTED",
                        reason_message("CONSECUTIVE_TOOL_ERRORS_EXHAUSTED"),
                        budget,
                        "budget.exhausted",
                    )
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
        if self._is_cancelled(run_id):
            return run
        plan = self.plans.get(run.current_plan_version_id) if run.current_plan_version_id else self.plans.current(run_id)
        candidates = await self._model_call(
            run, "reflection", self.model.reflect, self._goal(run.goal_id), plan, run_id,
            self._reflection_evidence_catalog(run),
        )
        if candidates is None or self._is_cancelled(run_id):
            return self.get_run(run_id)
        v2_store = getattr(self, "memory_store", None)
        owner_id = None
        source_thread_id = None
        authoritative_project_id = None
        source_scope_missing = run.source_turn_id is None
        if run.source_turn_id:
            with self.db.connection() as connection:
                scope = connection.execute(
                    "SELECT h.owner_id,h.id thread_id,h.project_id FROM turns t JOIN threads h ON h.id=t.thread_id "
                    "WHERE t.id=? AND h.deleted_at IS NULL", (run.source_turn_id,),
                ).fetchone()
            if scope:
                owner_id = scope["owner_id"]
                source_thread_id = scope["thread_id"]
                authoritative_project_id = scope["project_id"]
            else:
                # A materialized run must never fall back to the legacy
                # default owner when its source turn was deleted or malformed.
                source_scope_missing = True
        for index, candidate in enumerate(candidates):
            try:
                if source_scope_missing:
                    raise ValueError("reflection source scope is unavailable")
                if v2_store is not None:
                    source_scope = candidate["scope"]
                    if source_scope not in {"global", "project"}:
                        raise ValueError("reflection memory scope is unsupported")
                    if candidate["kind"] not in {"preference", "constraint"}:
                        raise ValueError("reflection memory kind is unsupported")
                    scope_type = "project" if source_scope == "project" else "user"
                    scope_id = str(authoritative_project_id or "") if scope_type == "project" else ""
                    if scope_type == "project" and not scope_id:
                        raise ValueError("project reflection memory requires project_id")
                    v2_store.propose(
                        owner_id=str(owner_id), operation="ADD", kind=candidate["kind"],
                        scope_type=scope_type, scope_id=scope_id,
                        content=candidate["content"], confidence=candidate.get("confidence", 0.5),
                        evidence_refs=candidate.get("evidence_event_ids", []),
                        idempotency_key=f"reflection:{run_id}:{index}",
                        source_thread_id=source_thread_id, source_run_id=run.id,
                        reason="Agent 任务完成后的反思候选",
                    )
                else:
                    self.memory.create_candidate(
                        run_id, run.goal_id, candidate["kind"], candidate["content"],
                        candidate["scope"], candidate.get("confidence", 0.5),
                        candidate.get("evidence_event_ids", []), candidate.get("project_id"),
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
        await self._finish_exposure(run_id, success=True)
        return self.get_run(run_id)

    async def _finish_exposure(self, run_id: str, *, success: bool) -> None:
        evolution = getattr(self, "evolution", None)
        if evolution is None:
            return
        exposure_id = run_id
        with self.db.connection() as connection:
            if connection.execute("SELECT 1 FROM canary_exposures WHERE run_id=?", (run_id,)).fetchone() is None:
                source = connection.execute("SELECT source_turn_id FROM runs WHERE id=?", (run_id,)).fetchone()
                if source and source["source_turn_id"]:
                    exposure_id = source["source_turn_id"]
        safety_pass = None
        judge = getattr(self, "safety_judge", None)
        if judge is not None:
            with self.db.connection() as connection:
                messages = [row["content"] for row in connection.execute(
                    "SELECT content FROM messages WHERE run_id=? AND role='assistant' AND content<>'' ORDER BY created_at,id",
                    (run_id,),
                )]
            gateway = getattr(judge, "gateway", None)
            token = None
            run = self.get_run(run_id)
            if getattr(gateway, "control_store", None) is not None:
                from .model_control import ModelCallContext
                token = gateway.set_call_context(ModelCallContext(
                    role="judge_safety", purpose="judge_run_output", run_id=run.id,
                    goal_id=run.goal_id, runtime_bundle_id=run.runtime_bundle_id,
                    root_budget_id=run.root_budget_id,
                ))
            try:
                safety_pass = await judge.judge({"run_id": run_id, "output": "\n\n".join(messages[-8:])})
            except Exception:
                safety_pass = None
            finally:
                if token is not None:
                    gateway.reset_call_context(token)
        evolution.finish_run_exposure(exposure_id, success=success, safety_pass=safety_pass)

    def _block(
        self,
        run_id: str,
        reason_code: str,
        message: str,
        budget: dict[str, Any],
        event_type: str,
    ) -> str:
        if self._is_cancelled(run_id):
            return "cancelled"
        run = self.get_run(run_id)
        budget = dict(budget)
        budget["blocked_reason_code"] = reason_code
        budget["blocked_message"] = message
        budget.pop("blocked_reason", None)
        self._set_run_fields(run_id, budget=budget)
        if run.state != AgentState.BLOCKED:
            self._transition(
                self.get_run(run_id),
                AgentState.BLOCKED,
                {"reason_code": reason_code, "message": message},
            )
        message = public_message(message, reason_message(reason_code))
        event_data = {"reason_code": reason_code, "message": message}
        if event_type == "budget.exhausted":
            self.events.append(run_id, run.goal_id, "budget.exhausted", "runtime", event_data)
        self._save_checkpoint(run_id, message)
        self.events.append(run_id, run.goal_id, "run.blocked", "runtime", event_data)
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
        if self._is_cancelled(run.id):
            return None
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_model_tasks[run.id] = current_task
        invocation_id = f"invocation_{uuid.uuid4().hex}"
        attempt_id = f"{invocation_id}_attempt_1"
        gateway = getattr(self.model, "gateway", None)
        persistent_calls = getattr(gateway, "control_store", None) is not None
        call_context_token = None
        if persistent_calls:
            from .model_control import ModelCallContext

            role = {"clarification": "ask", "planning": "planner", "react": "executor", "reflection": "reflector"}.get(kind, kind)
            thread_id = None
            if run.source_turn_id:
                with self.db.connection() as connection:
                    source_turn = connection.execute("SELECT thread_id FROM turns WHERE id=?", (run.source_turn_id,)).fetchone()
                thread_id = source_turn["thread_id"] if source_turn else None
            call_context_token = gateway.set_call_context(ModelCallContext(
                role=role,
                purpose=kind,
                invocation_id=invocation_id,
                run_id=run.id,
                goal_id=run.goal_id,
                thread_id=thread_id,
                turn_id=run.source_turn_id,
                runtime_bundle_id=run.runtime_bundle_id,
                root_budget_id=run.root_budget_id,
            ))
        snapshot = self._prepare_model_context(run, kind, args, invocation_id)
        message_id = self._create_model_message(run)
        if not persistent_calls:
            self.events.append(run.id, run.goal_id, "model.invocation_started", "runtime", {"model_invocation_id": invocation_id, "kind": kind})
            self.events.append(
                run.id,
                run.goal_id,
                "model.attempt_started",
                "runtime",
                {"model_invocation_id": invocation_id, "model_attempt_id": attempt_id, "attempt": 1},
            )
        setter = getattr(self.model, "set_text_delta_callback", None)
        resetter = getattr(self.model, "set_text_reset_callback", None)
        reset_delta = getattr(self.model, "reset_text_delta_callback", None)
        reset_stream = getattr(self.model, "reset_text_reset_callback", None)
        cancel_setter = getattr(self.model, "set_cancel_event", None)
        cancel_resetter = getattr(self.model, "reset_cancel_event", None)
        streamed = False

        def on_delta(delta: str) -> None:
            nonlocal streamed
            streamed = True
            self._append_model_delta(run, kind, invocation_id, message_id, delta)

        def on_reset() -> None:
            nonlocal streamed
            if streamed:
                self._reset_model_message(run, kind, invocation_id, message_id)
                streamed = False

        delta_token = setter(on_delta) if setter is not None else None
        reset_token = resetter(on_reset) if resetter is not None else None
        cancel_token = cancel_setter(self._cancel_event(run.id)) if cancel_setter is not None else None

        def clear_callbacks() -> None:
            if cancel_resetter is not None and cancel_token is not None:
                cancel_resetter(cancel_token)
            if reset_stream is not None and reset_token is not None:
                reset_stream(reset_token)
            elif resetter is not None:
                resetter(None)
            if reset_delta is not None and delta_token is not None:
                reset_delta(delta_token)
            elif setter is not None:
                setter(None)
        try:
            result = await method(*args)
        except asyncio.CancelledError:
            clear_callbacks()
            if not persistent_calls:
                self._record_cancelled_model_call(run, invocation_id, attempt_id, kind)
            return None
        except Exception as exc:
            clear_callbacks()
            from .model_gateway import GatewayError

            if not isinstance(exc, GatewayError):
                raise
            if exc.kind == "cancelled" or self._is_cancelled(run.id):
                if not persistent_calls:
                    self._record_cancelled_model_call(run, invocation_id, attempt_id, kind)
                return None
            if not persistent_calls:
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
            reason_code = f"MODEL_{exc.kind.upper()}"
            message = reason_message(reason_code, "模型请求未完成")
            if current.state == AgentState.RECEIVED:
                self._transition(current, AgentState.FAILED, {"reason_code": reason_code, "message": message})
                self.events.append(
                    run.id,
                    run.goal_id,
                    "run.failed",
                    "runtime",
                    {"reason_code": reason_code, "message": message},
                )
                self._save_checkpoint(run.id, message)
                await self._finish_exposure(run.id, success=False)
            elif current.state not in {AgentState.COMPLETED, AgentState.FAILED, AgentState.CANCELLED}:
                self._block(
                    run.id,
                    reason_code,
                    message,
                    current.budget,
                    "run.blocked",
                )
            return None
        else:
            clear_callbacks()
            if self._is_cancelled(run.id):
                if not persistent_calls:
                    self._record_cancelled_model_call(run, invocation_id, attempt_id, kind)
                return None
            response = getattr(self.model, "last_response", None)
            if not persistent_calls:
                self._record_model_response(run, invocation_id, attempt_id, response)
            self._append_model_message(run, kind, invocation_id, response, result, message_id=message_id)
            if not persistent_calls:
                self.events.append(run.id, run.goal_id, "model.invocation_finished", "runtime", {"model_invocation_id": invocation_id, "kind": kind, "status": "success"})
            if snapshot is not None:
                self._record_applied_context(run, kind, invocation_id, snapshot)
            return result
        finally:
            if persistent_calls and call_context_token is not None:
                gateway.reset_call_context(call_context_token)
            if current_task is not None and self._active_model_tasks.get(run.id) is current_task:
                self._active_model_tasks.pop(run.id, None)

    def _record_cancelled_model_call(self, run: RunSnapshot, invocation_id: str, attempt_id: str, kind: str) -> None:
        self.events.append(
            run.id,
            run.goal_id,
            "model.attempt_finished",
            "runtime",
            {"model_invocation_id": invocation_id, "model_attempt_id": attempt_id, "kind": kind, "status": "cancelled"},
        )
        self.events.append(
            run.id,
            run.goal_id,
            "model.invocation_finished",
            "runtime",
            {"model_invocation_id": invocation_id, "kind": kind, "status": "cancelled"},
        )

    def _prepare_model_context(
        self, run: RunSnapshot, kind: str, args: tuple[Any, ...], invocation_id: str,
    ):
        setter = getattr(self.model, "set_context_snapshot", None)
        if setter is None:
            return None
        goal = self._goal(run.goal_id)
        interactions = self._interaction_text(run.id)
        # Reflection may compare against existing memory elsewhere, but existing
        # memory and lossy episodes are never source evidence for new proposals.
        provider = None if kind == "reflection" else getattr(self, "memory_context", None)
        if provider is not None:
            from .memory_v2 import MemoryContextRequest
            if run.source_turn_id:
                with self.db.connection() as connection:
                    scope = connection.execute(
                        "SELECT th.id thread_id,th.owner_id,th.project_id FROM turns t "
                        "JOIN threads th ON th.id=t.thread_id WHERE t.id=? AND th.deleted_at IS NULL",
                        (run.source_turn_id,),
                    ).fetchone()
            else:
                scope = None
            if scope is None:
                memories = []
            else:
                selected = provider.select(MemoryContextRequest(
                    scope["owner_id"], scope["thread_id"], scope["project_id"],
                    interactions[-1] if interactions else "", purpose=kind,
                    model_invocation_id=invocation_id, parent_type="run", parent_id=run.id,
                ))
                memories = []
                if selected.revision_ids:
                    placeholders = ",".join("?" for _ in selected.revision_ids)
                    with self.db.connection() as connection:
                        rows = connection.execute(
                            "SELECT r.id,r.content,e.kind,e.scope_type,e.scope_id FROM memory_revisions r "
                            "JOIN memory_entries e ON e.id=r.entry_id AND e.current_revision_id=r.id "
                            f"WHERE r.id IN ({placeholders}) AND e.owner_id=?",
                            (*selected.revision_ids, scope["owner_id"]),
                        ).fetchall()
                    by_id = {row["id"]: row for row in rows}
                    for revision_id in selected.revision_ids:
                        row = by_id.get(revision_id)
                        if row is None:
                            continue
                        memories.append(MemoryForContext(
                            id=revision_id,
                            content=f"[{row['kind']}/{row['scope_type']}] {row['content']}",
                            scope="project" if row["scope_type"] == "project" else "global",
                            status="confirmed",
                                 project_id=row["scope_id"] if row["scope_type"] == "project" else None,
                                 skill_name=None,
                                 source_type="revision",
                             ))
                if selected.episode_ids:
                    placeholders = ",".join("?" for _ in selected.episode_ids)
                    with self.db.connection() as connection:
                        rows = connection.execute(
                            "SELECT id,summary,project_id FROM memory_episodes "
                            f"WHERE owner_id=? AND status='ACTIVE' AND id IN ({placeholders})",
                            (scope["owner_id"], *selected.episode_ids),
                        ).fetchall()
                    by_id = {row["id"]: row for row in rows}
                    for episode_id in selected.episode_ids:
                        row = by_id.get(episode_id)
                        if row is not None:
                            memories.append(MemoryForContext(
                                id=episode_id,
                                content=row["summary"],
                                scope="global",
                                status="confirmed",
                                 project_id=row["project_id"],
                                 skill_name=None,
                                 source_type="episode",
                             ))
        else:
            memories = [] if kind == "reflection" else [
                MemoryForContext(
                    id=record.id, content=record.content, scope=record.scope,
                    status=record.status, project_id=record.project_id,
                    skill_name=record.skill_name,
                )
                for record in self.memory.all_records()
            ]
        plan = args[1] if kind == "reflection" and len(args) > 1 else ""
        if kind == "reflection" and len(args) > 3:
            step = {"evidence_catalog": args[3]}
        else:
            step = args[0] if kind == "react" and args else ""
        observation = args[1] if kind == "react" and len(args) > 1 else ""
        phase_skill_name = "goal-planning" if kind in {"clarification", "planning", "react"} else "reflection"
        memory_skill_names = tuple(dict.fromkeys((phase_skill_name, *run.skill_names)))
        snapshot = self.context_assembler.assemble(
            user_instruction=interactions[-1] if interactions else "",
            goal=json.dumps(goal, ensure_ascii=False, sort_keys=True),
            plan=json.dumps(plan, ensure_ascii=False, default=str) if plan else "",
            step=json.dumps(step, ensure_ascii=False, default=str),
            skill=(self.skill_platform.context_text("RUN", run.id, "*")
                   if run.skill_names else self.skills.context_text((), phase_skill_name)),
            # The latest interaction is already the user_instruction block.
            history=interactions[:-1],
            # A tool observation is useful only as a whole. ContextAssembler
            # drops this block atomically when it cannot fit.
            tool_results=[{"summary": observation}] if observation else [],
            memories=memories,
            project_id=run.project_id,
            skill_name=phase_skill_name,
            skill_names=memory_skill_names,
        )
        setter(snapshot.snapshot_hash, snapshot.text)
        self.events.append(
            run.id, run.goal_id, "context.snapshot_created", "runtime",
            {
                "model_invocation_id": invocation_id, "kind": kind,
                "snapshot_hash": snapshot.snapshot_hash,
                "cropped": snapshot.cropped, "crop_count": snapshot.crop_count,
            },
        )
        return snapshot

    def _record_applied_context(self, run: RunSnapshot, kind: str, invocation_id: str, snapshot) -> None:
        revision_ids = [item.id for item in snapshot.memories if item.source_type == "revision"]
        episode_ids = [item.id for item in snapshot.memories if item.source_type == "episode"]
        with self.db.transaction() as connection:
            applied = connection.execute(
                "SELECT data_json FROM events WHERE run_id=? AND type='memory.context_applied'",
                (run.id,),
            ).fetchall()
            if any(
                json.loads(item["data_json"]).get("model_invocation_id") == invocation_id
                for item in applied
            ):
                return
            self.events.append(
                run.id, run.goal_id, "memory.context_applied", "runtime",
                {
                    "model_invocation_id": invocation_id, "kind": kind,
                    "snapshot_hash": snapshot.snapshot_hash,
                    "revision_ids": revision_ids[:64], "episode_ids": episode_ids[:64],
                }, connection=connection,
            )
            row = connection.execute("SELECT budget_json FROM runs WHERE id=?", (run.id,)).fetchone()
            budget = json.loads(row["budget_json"]) if row is not None else dict(run.budget)
            versions = list(budget.get("applied_memory_versions", []))
            for revision_id in revision_ids:
                if revision_id not in versions:
                    versions.append(revision_id)
            budget["applied_memory_versions"] = versions[-256:]
            connection.execute(
                "UPDATE runs SET budget_json=?,updated_at=?,version=version+1 WHERE id=?",
                (json.dumps(budget, ensure_ascii=False), _now(), run.id),
            )
        checkpoint = self.checkpoints.latest(run.id)
        if checkpoint is not None:
            self._save_checkpoint(
                run.id, checkpoint.observation,
                list(checkpoint.pending_actions), list(checkpoint.pending_approvals),
            )

    def _reflection_evidence_catalog(self, run: RunSnapshot) -> list[dict[str, str | None]]:
        if not run.source_turn_id:
            return []
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT id,content FROM thread_messages WHERE turn_id=? AND role='user' "
                "ORDER BY created_at,id LIMIT 1", (run.source_turn_id,),
            ).fetchall()
        return [
            {
                "ref": row["id"], "source_type": "thread_message",
                "excerpt": row["content"][:1000], "turn_id": run.source_turn_id,
            }
            for row in rows
        ]

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

    def _create_model_message(self, run: RunSnapshot) -> str:
        message_id = f"message_{uuid.uuid4().hex}"
        interaction_id = self._latest_interaction_id(run.id)
        now = _now()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO messages(id, run_id, interaction_id, role, content, created_at) VALUES (?, ?, ?, 'assistant', '', ?)",
                (message_id, run.id, interaction_id, now),
            )
        return message_id

    def _append_model_delta(
        self,
        run: RunSnapshot,
        kind: str,
        invocation_id: str,
        message_id: str,
        delta: str,
    ) -> None:
        if not delta:
            return
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE messages SET content = content || ? WHERE id = ? AND run_id = ?",
                (delta, message_id, run.id),
            )
            content_length = connection.execute(
                "SELECT length(content) FROM messages WHERE id = ? AND run_id = ?",
                (message_id, run.id),
            ).fetchone()[0]
        self.events.append(
            run.id,
            run.goal_id,
            "model.response.delta",
            "model",
            {
                "message_id": message_id,
                "interaction_id": self._latest_interaction_id(run.id),
                "model_invocation_id": invocation_id,
                "kind": kind,
                "delta": delta,
                "content_length": content_length,
            },
        )

    def _reset_model_message(
        self,
        run: RunSnapshot,
        kind: str,
        invocation_id: str,
        message_id: str,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE messages SET content = '' WHERE id = ? AND run_id = ?",
                (message_id, run.id),
            )
        self.events.append(
            run.id,
            run.goal_id,
            "model.response.reset",
            "model",
            {
                "message_id": message_id,
                "interaction_id": self._latest_interaction_id(run.id),
                "model_invocation_id": invocation_id,
                "kind": kind,
            },
        )

    def _append_model_message(
        self,
        run: RunSnapshot,
        kind: str,
        invocation_id: str,
        response: Any,
        result: Any,
        message_id: str | None = None,
    ) -> None:
        content = str(getattr(response, "message", "") or "") if response is not None else ""
        if not content:
            content = json.dumps(_model_result_payload(kind, result), ensure_ascii=False, default=str)
        message_id = message_id or f"message_{uuid.uuid4().hex}"
        interaction_id = self._latest_interaction_id(run.id)
        now = _now()
        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM messages WHERE id = ? AND run_id = ?",
                (message_id, run.id),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE messages SET interaction_id = ?, content = ? WHERE id = ? AND run_id = ?",
                    (interaction_id, content, message_id, run.id),
                )
            else:
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

    def _cancel_event(self, run_id: str) -> asyncio.Event:
        return self._cancel_events.setdefault(run_id, asyncio.Event())

    def _is_cancelled(self, run_id: str) -> bool:
        event = self._cancel_events.get(run_id)
        return bool(event and event.is_set()) or self.get_run(run_id).state == AgentState.CANCELLED

    @staticmethod
    def _skill_tools_for(skill_name: str) -> set[str]:
        if skill_name in {"goal-planning", "planning", "reflection"}:
            return {"local_time", "calculator", "read_note"}
        if skill_name == "react":
            return {"local_time", "calculator", "read_note", "write_note", "trusted_connector"}
        return set()

    def _skill_tools_for_run(self, run: RunSnapshot, phase_name: str) -> set[str]:
        allowed = self._skill_tools_for(phase_name)
        if run.skill_names:
            try: binding = self.skill_platform.binding("RUN", run.id)
            except KeyError: return set()
            global_tools = {item["function"]["name"] for item in self.tools.describe()}
            selected_tools: set[str] = set()
            tool_bound = False
            for version_id in binding["version_ids"]:
                if self.skill_platform.version(version_id)["kind"] == "instruction_only":
                    continue
                tool_bound = True
                selected_tools |= self.skill_platform.effective_tools(
                    version_id, global_tools=global_tools, role_tools=allowed, phase_tools=allowed,
                    phase_name="executor" if phase_name == "react" else phase_name,
                    grant_snapshot=binding["grant_snapshots"].get(version_id),
                )
            return allowed & selected_tools if tool_bound else allowed
        return allowed

    def _tool_authorization(self, run: RunSnapshot, phase_name: str, call: ToolCall) -> dict[str, str] | None:
        if not run.skill_names:
            return None
        allowed = self._skill_tools_for(phase_name)
        global_tools = {item["function"]["name"] for item in self.tools.describe()}
        connector_version_id = call.params.get("connector_version_id") if call.name == "trusted_connector" else None
        return self.skill_platform.tool_authorization(
            "RUN", run.id, call.name, connector_version_id=connector_version_id,
            global_tools=global_tools, role_tools=allowed, phase_tools=allowed,
            phase_name="executor" if phase_name == "react" else phase_name,
            routing_policy_digest=str(run.budget.get("routing_policy_digest", "direct")),
        )


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
