"""The V3 learning cycle: one Experience, one decision, one candidate, one gate.

    Experience -> JEV decision -> Learning LLM draft -> typed candidate
    -> rule eval -> blind judge -> promotion gate

The cycle is a **background, non-critical path** (V3 §51/§52): it never blocks a
user turn, and an exhausted learning budget stops the remote calls without
touching the runtime.

Two modes (V3 §41/§62):

* `SHADOW` — the whole chain runs, including the gate, but the gate is called
  with `shadow=True` so no production asset is modified. This is the mode the
  pipeline must live in until the acceptance gates are green.
* `ACTIVE` — the gate may apply. Behavior still needs approval, a release
  evaluation, a canary and clean exposures before `stable` moves.

Everything else is delegated, deliberately. Leases, job status, budget dispatch
and source revocation stay in `LearningService`; candidate creation stays in the
target adapters; the gate stays in `learning_promotion`. This module only owns
the *order* of the steps and the audit record of what happened.
"""
from __future__ import annotations

import time
import json
import uuid
from contextlib import ExitStack
from dataclasses import replace
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .learning_contract import TARGET_BEHAVIOR, TARGET_IGNORE, TARGET_MEMORY, TARGET_SKILL
from .learning_eval import evaluate_candidate
from .learning_promotion import (
    OUTCOME_PENDING_APPROVAL,
    OUTCOME_PROMOTED,
    OUTCOME_REJECTED,
    OUTCOME_SHADOW,
    LearningPromotionGate,
)

MODE_SHADOW = "SHADOW"
MODE_ACTIVE = "ACTIVE"
MODES: tuple[str, ...] = (MODE_SHADOW, MODE_ACTIVE)

#: The sources this pipeline can read. The rest are the legacy-only kinds whose
#: apply paths live in `learning_legacy`; `collect` stops enqueueing them once a
#: pipeline is attached, and an unexpected one is refused loudly here rather
#: than misread as a thread message.
SOURCE_KINDS: tuple[str, ...] = ("experience", "thread_message")

#: Job outcomes as recorded on `learning_jobs.status`.
STATUS_NO_CHANGE = "NO_CHANGE"
STATUS_APPLIED = "APPLIED"
STATUS_REJECTED = "REJECTED"
STATUS_UNKNOWN = "UNKNOWN"

DEFAULT_LEASE_SECONDS = 3600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CycleResult:
    """One completed learning cycle, in the shape V3 §53 asks to record."""

    job_id: str
    owner_id: str
    status: str
    outcome: str
    target: str = TARGET_IGNORE
    candidate_id: str = ""
    reason: str = ""
    decision: dict[str, Any] | None = None
    evaluation: dict[str, Any] | None = None
    promotion: dict[str, Any] | None = None
    observability: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "owner_id": self.owner_id, "status": self.status,
                "outcome": self.outcome, "target": self.target, "candidate_id": self.candidate_id,
                "reason": self.reason, "decision": self.decision, "evaluation": self.evaluation,
                "promotion": self.promotion, "observability": dict(self.observability)}

    @property
    def applied(self) -> bool:
        return self.status == STATUS_APPLIED


@dataclass
class _CycleState:
    """Mutable scratch for one cycle, so `run_job` can finish exactly once."""

    root_id: Any = None
    target: str = TARGET_IGNORE
    candidate_id: str = ""
    decision: dict[str, Any] | None = None
    evaluation: Any = None
    promotion: Any = None
    bundle_before: str = ""
    scope: Any = None
    candidate: Any = None


class LearningPipeline:
    """Runs the V3 cycle for one claimed learning job at a time."""

    def __init__(self, *, db: Any, learning: Any, decisions: Any, agent: Any,
                 adapters: Mapping[str, Any], gate: LearningPromotionGate | None = None,
                 judge: Any = None, replay: Callable[[str, Mapping[str, Any]], Any] | None = None,
                 bundles: Any = None, approval: Any = None, mode: str = MODE_SHADOW,
                 lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown learning pipeline mode: {mode!r}")
        self.db, self.learning, self.decisions, self.agent = db, learning, decisions, agent
        self.adapters = dict(adapters)
        self.gate = gate or LearningPromotionGate(self.adapters)
        self.judge = judge
        self.replay = replay
        self.bundles = bundles
        self.approval = approval
        self.mode = mode
        self.lease_seconds = lease_seconds

    # ------------------------------------------------------------------ mode

    @property
    def shadow(self) -> bool:
        return self.mode == MODE_SHADOW

    def set_mode(self, mode: str) -> str:
        if mode not in MODES:
            raise ValueError(f"unknown learning pipeline mode: {mode!r}")
        self.mode = mode
        return self.mode

    # ------------------------------------------------------------- scheduling

    def run_once(self, owner_id: str = "local-user") -> CycleResult | None:
        """Collect, claim and run one job. Returns None when there is no work."""
        self.learning.maintain(owner_id)
        self.advance_canaries(owner_id)
        self.learning.collect(owner_id)
        job = self.learning.claim(owner_id, lease_seconds=self.lease_seconds)
        if job is None:
            return None
        return self.run_job(job)

    def drain(self, owner_id: str = "local-user", *, limit: int = 20) -> list[CycleResult]:
        """Run until there is no more work, for shadow batch runs (V3 §41)."""
        results: list[CycleResult] = []
        for _ in range(limit):
            result = self.run_once(owner_id)
            if result is None:
                break
            results.append(result)
        return results

    # -------------------------------------------------------------- one cycle

    def run_job(self, job: Mapping[str, Any]) -> CycleResult:
        """Run the full chain for an already-claimed job, then close it once."""
        started = time.monotonic()
        state = _CycleState(bundle_before=self._active_bundle())
        state.scope = ExitStack()
        try:
            status, outcome, reason = self._run_chain(job, state)
        except Exception as exc:  # noqa: BLE001 — every failure becomes a recorded outcome
            # A request that was already dispatched but never committed is
            # UNKNOWN and must never be replayed automatically (V3 §48).
            from .skill_platform import SkillCandidateRejected
            status = STATUS_UNKNOWN if state.root_id is not None and not isinstance(exc, SkillCandidateRejected) else STATUS_REJECTED
            outcome, reason = status, f"{type(exc).__name__}: {exc}"[:400]
        finally:
            state.scope.close()
        return self._finish(job, status, outcome, reason=reason, started=started, state=state)

    def _run_chain(self, job: Mapping[str, Any], state: _CycleState) -> tuple[str, str, str]:
        owner_id, job_id = job["owner_id"], job["id"]
        experience, evidence_ids, source_refs = self._load_experience(job)

        # Budget and dispatch come before any network I/O (V3 §51): an exhausted
        # learning budget stops the cycle, it never blocks a user turn.
        state.root_id = self.learning.dispatch(job)
        if not self.learning.assert_learning_call_allowed(owner_id, state.root_id):
            return (STATUS_NO_CHANGE, "BUDGET_BLOCKED",
                    "learning call is not allowed under the current lease or budget")

        self._bind_model_context(state.scope, owner_id, state.root_id, state.bundle_before)

        decision = self.decisions.decide(
            owner_id=owner_id, job_id=job_id, experience=experience,
            task_result=experience.get("task_result") or {},
            failure_tags=experience.get("failure_tags") or [],
            tool_trace=experience.get("tool_trace") or [],
            user_feedback=experience.get("user_feedback"),
            assets_summary=self._assets_summary(owner_id),
        )
        state.decision = self.decisions.get(owner_id, job_id)
        state.target = decision.target
        if decision.ignored:
            return STATUS_NO_CHANGE, "IGNORE", ",".join(decision.reason_codes)
        allowed = self.learning.policy(owner_id)["config"].get("allowed_assets", [])
        asset = {TARGET_MEMORY: "memory", TARGET_SKILL: "skill", TARGET_BEHAVIOR: "prompt"}.get(decision.target)
        if asset not in allowed:
            return STATUS_REJECTED, "TARGET_DISABLED", "learning target is not enabled by owner policy"

        adapter = self.adapters.get(decision.target)
        if adapter is None:
            return STATUS_REJECTED, "NO_ADAPTER", f"no target adapter for {decision.target!r}"

        constraints = self._constraints(owner_id, experience)
        draft = self.agent.generate(decision=decision, experience=experience,
                                    evidence_ids=evidence_ids, source_refs=source_refs,
                                    asset_state=self._assets_summary(owner_id),
                                    constraints=constraints)
        self._assert_offered_scope(draft, constraints)
        if decision.target == TARGET_MEMORY and job["source_kind"] == "experience":
            # Only database-resolved user messages may become Memory evidence.
            sources = experience.get("user_sources", [])
            if not sources:
                return STATUS_REJECTED, "NO_USER_SOURCE", "Experience has no traceable user evidence"
            draft = replace(draft, evidence_refs=tuple(source["id"] for source in sources),
                            source_refs=tuple(source["id"] for source in sources))
        candidate = adapter.create_candidate(draft, owner_id=owner_id, job_id=job_id)
        candidate.update(owner_id=owner_id, job_id=job_id, root_budget_id=state.root_id)
        if decision.target == TARGET_MEMORY:
            candidate["user_sources"] = experience.get("user_sources", [])
        state.candidate = candidate
        state.candidate_id = candidate["candidate_id"]

        state.evaluation = self._evaluate(adapter, candidate, owner_id=owner_id)
        state.promotion = self.gate.promote(candidate, state.evaluation, owner_id=owner_id,
                                            approval=self.approval, job_id=job_id, shadow=self.shadow)
        status, outcome = self._status_for(state.promotion)
        return status, outcome, state.promotion.reason

    def _evaluate(self, adapter: Any, candidate: Mapping[str, Any], *, owner_id: str) -> Any:
        """Rule evaluation always; replay only where the target needs it."""
        cases = runner = None
        if candidate.get("target") in (TARGET_SKILL, TARGET_BEHAVIOR) and self.replay is not None:
            supplied = self.replay(candidate["target"], candidate)
            if supplied:
                cases, runner = supplied
        result = evaluate_candidate(adapter, candidate, owner_id=owner_id, cases=cases, runner=runner,
                                    judge=self.judge)
        if runner is not None and getattr(runner, "suite_digest", None):
            result.metrics["suite_digest"] = runner.suite_digest
            result.metrics["records"] = runner.records
        return result

    @staticmethod
    def _status_for(promotion: Any) -> tuple[str, str]:
        if promotion.outcome == OUTCOME_PROMOTED:
            return STATUS_APPLIED, OUTCOME_PROMOTED
        if promotion.outcome == OUTCOME_REJECTED:
            return STATUS_REJECTED, OUTCOME_REJECTED
        if promotion.outcome == OUTCOME_SHADOW:
            return STATUS_NO_CHANGE, OUTCOME_SHADOW
        if promotion.outcome == OUTCOME_PENDING_APPROVAL:
            return STATUS_NO_CHANGE, OUTCOME_PENDING_APPROVAL
        return STATUS_NO_CHANGE, promotion.outcome

    # ------------------------------------------------------------ canary loop

    def resume(self, job_id: str, *, owner_id: str, expected_version: int, approve: bool = False) -> CycleResult:
        """Explicitly continue a known candidate; never regenerate or replay UNKNOWN.

        Reuses the original budget without extending/resetting it. A spent or
        expired root remains blocked by the model gateway.
        """
        from .learning import LearningConflict
        from .learning_eval import EvaluationResult
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM learning_jobs WHERE id=? AND owner_id=?", (job_id, owner_id)).fetchone()
            if row is None:
                raise KeyError(job_id)
            checkpoint = json.loads(row["checkpoint_json"])
            if (row["status"] != STATUS_NO_CHANGE or row["version"] != expected_version
                    or checkpoint.get("outcome") not in {"NEEDS_REPLAY", "PENDING_APPROVAL", "PENDING_RELEASE_EVALUATION"}
                    or not checkpoint.get("candidate")):
                raise LearningConflict("job is not a resumable candidate")
            policy = self.learning.policy(owner_id, connection=connection)
            if policy["paused"] or policy["version"] != row["policy_version"]:
                raise LearningConflict("learning policy paused or changed")
            token = uuid.uuid4().hex
            changed = connection.execute("UPDATE learning_jobs SET status='RUNNING',lease_token=?,lease_until=?,version=version+1 "
                "WHERE id=? AND owner_id=? AND status='NO_CHANGE' AND version=?",
                (token, (datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat(), job_id, owner_id, expected_version)).rowcount
            if changed != 1:
                raise LearningConflict("candidate is already being resumed")
            job = dict(connection.execute("SELECT * FROM learning_jobs WHERE id=?", (job_id,)).fetchone())
        started = time.monotonic()
        candidate = checkpoint["candidate"]
        state = _CycleState(root_id=job["root_budget_id"], target=candidate["target"], candidate_id=candidate["candidate_id"],
                            candidate=candidate, bundle_before=self._active_bundle())
        effects_started = False
        try:
            self._load_experience(job)
            if candidate.get("base_bundle_id") and candidate["base_bundle_id"] != state.bundle_before:
                raise ValueError("candidate baseline is no longer stable")
            adapter = self.adapters[state.target]
            checks = adapter.validate_candidate(candidate, owner_id=owner_id)
            if not checks["pass"]:
                raise ValueError(checks["reason"])
            if checkpoint["outcome"] == "NEEDS_REPLAY":
                # Bind the Judge as well as the replay runner to the existing root.
                with ExitStack() as scope:
                    self._bind_model_context(scope, owner_id, state.root_id, state.bundle_before)
                    effects_started = True
                    state.evaluation = self._evaluate(adapter, candidate, owner_id=owner_id)
            else:
                state.evaluation = EvaluationResult(**checkpoint["evaluation"])
            effects_started = True
            state.promotion = self.gate.promote(candidate, state.evaluation, owner_id=owner_id, job_id=job_id,
                                                approval={"actor": "user"} if approve else None, shadow=self.shadow)
            status, outcome = self._status_for(state.promotion)
            reason = state.promotion.reason
        except Exception as exc:
            status = outcome = STATUS_UNKNOWN if effects_started else STATUS_REJECTED
            reason = f"{type(exc).__name__}: {exc}"[:400]
        return self._finish(job, status, outcome, reason=reason, started=started, state=state)

    def _bind_model_context(self, scope, owner_id, root_id, bundle_id):
        from .model_control import ModelCallContext
        gateways = {id(g): g for g in (getattr(self.agent, "gateway", None),
                    getattr(self.judge, "gateway", None), getattr(self.replay, "gateway", None)) if g is not None}
        for gateway in gateways.values():
            if hasattr(gateway, "set_call_context"):
                token = gateway.set_call_context(ModelCallContext("learning_generator", "generate_learning_candidate",
                    owner_id=owner_id, root_budget_id=root_id, runtime_bundle_id=bundle_id or None))
                scope.callback(gateway.reset_call_context, token)

    def advance_canaries(self, owner_id: str = "local-user") -> list[dict[str, Any]]:
        """Finish or abort every Behavior canary that has reached a decision.

        V3 §40: promote once the exposure gate is clean, stop immediately on a
        safety failure. The exposure counts come from the existing canary
        machinery, not from anything this pipeline computes itself.
        """
        if self.shadow:
            return []
        policy = self.learning.policy(owner_id)
        may_promote = not policy["paused"] and "prompt" in policy["config"].get("allowed_assets", [])
        advanced: list[dict[str, Any]] = []
        for candidate in self._canary_candidates(owner_id):
            status = self.gate.canary_status(candidate, owner_id=owner_id)
            if not status.get("checks"):
                continue
            if (status.get("stopped") or status["checks"].get("permission_violation") is False
                    or status["checks"].get("safety_failures") is False or status["checks"].get("success_failures") is False):
                decision = self.gate.rollback(candidate, owner_id=owner_id,
                                              reason="canary safety or success gate failed")
            elif status.get("ready") and may_promote:
                decision = self.gate.complete_canary(candidate, owner_id=owner_id)
            else:
                continue
            advanced.append(decision.to_dict())
        return advanced

    def _canary_candidates(self, owner_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT p.id,p.target,p.candidate_id FROM learning_promotions p "
                "WHERE p.owner_id=? AND p.outcome='CANARY_STARTED' "
                "AND NOT EXISTS (SELECT 1 FROM learning_promotions done WHERE done.owner_id=p.owner_id "
                "AND done.candidate_id=p.candidate_id AND done.outcome IN ('PROMOTED','ROLLED_BACK','REJECTED')) "
                "ORDER BY p.created_at,p.id",
                (owner_id,),
            ).fetchall()
        seen, candidates = set(), []
        for row in rows:
            if row["candidate_id"] in seen:
                continue
            seen.add(row["candidate_id"])
            candidates.append({"candidate_id": row["candidate_id"], "target": row["target"]})
        return candidates

    # --------------------------------------------------------------- helpers

    def _load_experience(self, job: Mapping[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
        """Read the job's source and refuse anything that changed or was revoked.

        For an Experience source the *recurrence* matters: JEV has to see that the
        same failure happened more than once, and a Behavior candidate needs at
        least three independent DISCOVERY experiences. So the sibling experiences
        in the same lineage are loaded too, and they become the evidence ids the
        Learning LLM is allowed to cite.
        """
        if job["source_kind"] not in SOURCE_KINDS:
            raise ValueError(f"the learning pipeline cannot read source_kind {job['source_kind']!r}; "
                             f"supported: {list(SOURCE_KINDS)}")
        if job["source_kind"] == "experience":
            with self.db.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM evolution_experiences WHERE id=? AND owner_id=?",
                    (job["source_id"], job["owner_id"]),
                ).fetchone()
                if row is None:
                    raise ValueError("learning source experience is missing")
                if row["source_state"] != "ACTIVE":
                    raise ValueError("learning source experience was revoked")
                siblings = connection.execute(
                    "SELECT id FROM evolution_experiences WHERE owner_id=? AND source_state='ACTIVE' "
                    "AND dataset_partition='DISCOVERY' AND (root_task_id=? OR task_type=?) "
                    "ORDER BY observed_at,id LIMIT 10",
                    (job["owner_id"], row["root_task_id"], row["task_type"]),
                ).fetchall()
            from .learning_decision import decision_from_experience_row

            # `sqlite3.Row` has no `.get`, and the projector reads optional keys.
            summary = decision_from_experience_row(dict(row))
            summary["task_result"] = {"outcome": row["outcome"], "signal_type": row["signal_type"]}
            evidence_ids = [item["id"] for item in siblings] or [row["id"]]
            if row["id"] not in evidence_ids:
                evidence_ids.insert(0, row["id"])
            summary["recurrence"] = {"count": len(evidence_ids), "task_type": row["task_type"]}
            from .learning_sources import user_sources
            sources = user_sources(self.db, row, job["owner_id"])
            summary["user_sources"] = sources
            projects = {source["project_id"] for source in sources}
            summary["project_id"] = next(iter(projects)) if len(projects) == 1 else ""
            return summary, evidence_ids, [source["id"] for source in sources]
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT m.*,t.owner_id,t.project_id,t.deleted_at FROM thread_messages m "
                "JOIN threads t ON t.id=m.thread_id WHERE m.id=?",
                (job["source_id"],),
            ).fetchone()
        if row is None or row["owner_id"] != job["owner_id"] or row["deleted_at"]:
            raise ValueError("learning source message is missing or was revoked")
        from .learning import digest as _digest

        if _digest(row["content"]) != job["source_hash"]:
            raise ValueError("learning source changed after it was enqueued")
        experience = {"id": row["id"], "task_type": "conversation", "outcome": "observed",
                      "signal_type": "user_message", "severity": "info", "source_kind": "thread_message",
                      "text": row["content"], "role": row["role"], "project_id": row["project_id"] or ""}
        return experience, [], [row["id"]]

    def _assets_summary(self, owner_id: str) -> dict[str, Any]:
        """What the agent already has, so JEV does not re-learn an existing asset."""
        with self.db.connection() as connection:
            memory = connection.execute(
                "SELECT COUNT(*) FROM memory_entries WHERE owner_id=? AND status='ACTIVE'", (owner_id,)).fetchone()[0]
            skills = connection.execute(
                "SELECT COUNT(*) FROM skill_versions WHERE status='ENABLED'").fetchone()[0]
        return {"memory_entries": int(memory), "enabled_skills": int(skills),
                "bundle": self._active_bundle()}

    def _constraints(self, owner_id: str, experience: Mapping[str, Any]) -> dict[str, Any]:
        """The scopes a draft is allowed to name.

        A memory draft that names a project the harness cannot resolve is a
        rejected draft (V3 fail-closed), so the generator is handed the scopes
        that actually exist instead of being left to guess one. The only project
        scope an Experience carries is the thread it came from — there is no
        separate project registry to consult.
        """
        project_id = str(experience.get("project_id") or "")
        constraints = {"memory_scopes": [project_id] if project_id else []}
        describe = getattr(self.replay, "generation_constraints", None)
        if callable(describe):
            constraints.update(describe(owner_id))
        return constraints

    @staticmethod
    def _assert_offered_scope(draft: Any, constraints: Mapping[str, Any]) -> None:
        """A draft may only name a scope the harness actually offered it.

        Telling the model which project scopes exist is not enough on its own —
        a model that names some other project would otherwise get a proposal
        written against a scope the harness never resolved. Fail closed instead.
        """
        from .learning_agent import DraftError

        if getattr(draft, "target", "") != TARGET_MEMORY:
            return
        scope_type, scope_id = draft.scope
        offered = [str(item) for item in (constraints.get("memory_scopes") or [])]
        if scope_type == "project" and scope_id not in offered:
            raise DraftError(f"memory scope {scope_id!r} was not offered to the generator")

    def _active_bundle(self) -> str:
        if self.bundles is None:
            return ""
        try:
            return self.bundles.active("stable").id
        except Exception:
            return ""

    def _finish(self, job: Mapping[str, Any], status: str, outcome: str, *, reason: str, started: float,
                state: _CycleState) -> CycleResult:
        """Close the job and hand the caller a fully populated cycle record."""
        observability = self._observability(job, state=state, outcome=outcome, started=started)
        changes = [{"target": state.target, "outcome": outcome, "candidate_id": state.candidate_id,
                    "shadow": self.shadow, "reason": reason}]
        self.learning.finish(job, status, changes=changes, checkpoint=observability, reason=reason[:400])
        return CycleResult(job_id=job["id"], owner_id=job["owner_id"], status=status, outcome=outcome,
                           target=state.target, candidate_id=state.candidate_id, reason=reason,
                           decision=state.decision,
                           evaluation=state.evaluation.to_dict() if state.evaluation else None,
                           promotion=state.promotion.to_dict() if state.promotion else None,
                           observability=observability)

    def _observability(self, job: Mapping[str, Any], *, state: _CycleState, outcome: str,
                       started: float) -> dict[str, Any]:
        """The per-cycle audit record V3 §53 asks for."""
        judge_identity = ""
        if state.evaluation is not None and isinstance(state.evaluation.judge, Mapping):
            judge_identity = str(state.evaluation.judge.get("model_identity", ""))
        return {
            "learning_job_id": job["id"],
            "experience_ids": [job["source_id"]] if job["source_kind"] == "experience" else [],
            "source_kind": job["source_kind"],
            "source_id": job["source_id"],
            "decision_id": (state.decision or {}).get("id", ""),
            "jev_model": (state.decision or {}).get("model_identity", ""),
            "target": (state.decision or {}).get("target", TARGET_IGNORE),
            "subtype": (state.decision or {}).get("subtype", ""),
            "candidate_id": state.candidate_id,
            "judge_model": judge_identity,
            "promotion_id": (state.promotion.promotion_id if state.promotion is not None else ""),
            "runtime_bundle_before": state.bundle_before,
            "runtime_bundle_after": self._active_bundle(),
            "cost_microusd": 0,
            "latency_seconds": round(time.monotonic() - started, 3),
            "mode": self.mode,
            "outcome": outcome,
            "candidate": state.candidate,
            "evaluation": state.evaluation.to_dict() if state.evaluation else None,
        }

    # ------------------------------------------------------------- dashboard

    def metrics(self, owner_id: str = "local-user") -> dict[str, Any]:
        """The V3 §54 dashboard counters, read from the audit tables."""
        with self.db.connection() as connection:
            decisions = connection.execute(
                "SELECT target,learn FROM learning_decisions WHERE owner_id=?", (owner_id,)).fetchall()
            jobs = connection.execute(
                "SELECT status FROM learning_jobs WHERE owner_id=?", (owner_id,)).fetchall()
            promotions = connection.execute(
                "SELECT outcome FROM learning_promotions WHERE owner_id=?", (owner_id,)).fetchall()
        total = len(decisions)
        learned = sum(1 for row in decisions if row["learn"])
        per_target = {target: sum(1 for row in decisions if row["target"] == target)
                      for target in (TARGET_MEMORY, TARGET_SKILL, TARGET_BEHAVIOR)}
        promoted = sum(1 for row in promotions if row["outcome"] == OUTCOME_PROMOTED)
        rejected = sum(1 for row in promotions if row["outcome"] == OUTCOME_REJECTED)
        decided = promoted + rejected
        return {
            "experience_count": sum(1 for row in jobs if row["status"] != "SUSPENDED"),
            "decision_count": total,
            "learn_rate": None if not total else round(learned / total, 4),
            "ignore_rate": None if not total else round((total - learned) / total, 4),
            "memory_candidate_rate": None if not total else round(per_target[TARGET_MEMORY] / total, 4),
            "skill_candidate_rate": None if not total else round(per_target[TARGET_SKILL] / total, 4),
            "behavior_candidate_rate": None if not total else round(per_target[TARGET_BEHAVIOR] / total, 4),
            "candidate_reject_rate": None if not decided else round(rejected / decided, 4),
            "candidate_promote_rate": None if not decided else round(promoted / decided, 4),
            "rollback_rate": self.gate.metrics(owner_id)["rollback_rate"],
            "promotion_regression_rate": self.gate.metrics(owner_id)["promotion_regression_rate"],
            "learning_cost_microusd": 0,
            "mode": self.mode,
        }


def build_pipeline(runtime: Any, *, mode: str = MODE_SHADOW, judge: Any = None,
                   replay: Callable[[str, Mapping[str, Any]], Any] | None = None,
                   approval: Any = None, decisions: Any = None, agent: Any = None) -> LearningPipeline:
    """Assemble the V3 pipeline from a built runtime.

    `decisions` and `agent` are injected rather than constructed here: the JEV
    client and the Learning LLM need credentials, and a shadow run must be able
    to proceed with neither (V3 §41).
    """
    from .learning_targets import build_adapters

    adapters = build_adapters(db=runtime.db, memory=runtime.memory_store, platform=runtime.skill_platform,
                              bundles=runtime.behavior, evolution=runtime.evolution)
    return LearningPipeline(db=runtime.db, learning=runtime.learning, decisions=decisions, agent=agent,
                            adapters=adapters, judge=judge, replay=replay, bundles=runtime.behavior,
                            approval=approval, mode=mode)


__all__ = [
    "CycleResult", "LearningPipeline", "MODES", "MODE_ACTIVE", "MODE_SHADOW", "SOURCE_KINDS",
    "STATUS_APPLIED", "STATUS_NO_CHANGE", "STATUS_REJECTED", "STATUS_UNKNOWN", "build_pipeline",
]
