"""V3 Promotion Gate: the harness decides, never the judge.

V3 §21 fixes the gate as plain rules over structured evaluation output:

    rule_eval == PASS
    AND judge candidate >= baseline
    AND safety_regression == false
    AND permission_expansion == false
    AND cost_increase <= configured_limit

Each target then has its own apply path (V3 §39):

    MEMORY   Eval Pass -> Proposal Accept
    SKILL    Eval Pass -> Human/Policy Approval -> Enable
    BEHAVIOR Eval Pass -> Approval -> Canary -> Promote stable

Three properties matter more than the individual checks:

* **No LLM in the loop.** The gate reads only booleans, counts and metrics the
  evaluator already computed. A judge verdict is an *input*; the gate is the
  decision. `LearningJudge` is never called from here.
* **Nothing is applied twice.** Every outcome is written to the append-only
  `learning_promotions` table, and a partial unique index makes a second
  `PROMOTED` row for one candidate impossible (V3 §61 "Duplicate promotion = 0").
* **A refusal is a decision, not a crash.** Every precondition that is not met
  produces a recorded outcome with a reason, so the pipeline stays auditable
  (V3 §60: "禁止只输出 PASS").

Behavior promotions deliberately do **not** re-implement the release lifecycle.
This build only accepts an approvable `prompt` candidate through the M5
role-paired release contract (`EvolutionService.evaluate_research_replay`), and
`EvolutionService` already enforces approval -> canary -> promote with its own
exposure minimums. The gate therefore sequences those calls and reports exactly
which precondition is missing instead of duplicating the checks.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .learning_contract import TARGET_BEHAVIOR, TARGET_MEMORY, TARGET_SKILL
from .learning_eval import EvaluationResult

OUTCOME_PROMOTED = "PROMOTED"
OUTCOME_CANARY_STARTED = "CANARY_STARTED"
OUTCOME_CANARY_PENDING = "CANARY_PENDING"
OUTCOME_PENDING_APPROVAL = "PENDING_APPROVAL"
OUTCOME_PENDING_RELEASE_EVALUATION = "PENDING_RELEASE_EVALUATION"
OUTCOME_NEEDS_REPLAY = "NEEDS_REPLAY"
OUTCOME_REJECTED = "REJECTED"
OUTCOME_ROLLED_BACK = "ROLLED_BACK"
#: Shadow mode: the gate reached a decision but was not allowed to apply it.
#: The row records what *would* have happened, so a shadow run is auditable
#: without polluting the promotion metrics (V3 §41/§42).
OUTCOME_SHADOW = "SHADOW"

OUTCOMES: tuple[str, ...] = (
    OUTCOME_PROMOTED,
    OUTCOME_CANARY_STARTED,
    OUTCOME_CANARY_PENDING,
    OUTCOME_PENDING_APPROVAL,
    OUTCOME_PENDING_RELEASE_EVALUATION,
    OUTCOME_NEEDS_REPLAY,
    OUTCOME_REJECTED,
    OUTCOME_ROLLED_BACK,
    OUTCOME_SHADOW,
)

#: Outcomes where the harness wrote to production.
APPLIED_OUTCOMES: tuple[str, ...] = (OUTCOME_PROMOTED,)

GATE_CHECKS: tuple[str, ...] = (
    "rule_eval_pass",
    "evidence_complete",
    "judge_present",
    "judge_not_worse",
    "no_safety_regression",
    "no_permission_expansion",
    "cost_within_limit",
)

#: Rule checks that assert the candidate did not grow its authority. Present
#: only on the targets that can grant authority, so the gate reads what exists.
PERMISSION_CHECKS: tuple[str, ...] = (
    "no_permission_expansion",
    "no_requested_tools",
    "no_connectors",
    "no_grant",
    "permissions_subset",
)


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PromotionPolicy:
    """The configured limits the gate enforces. Plain data, no behaviour."""

    #: Targets whose gate demands a judge verdict. Memory is rule-only (V3 §36).
    judge_required: tuple[str, ...] = (TARGET_SKILL, TARGET_BEHAVIOR)
    #: Targets the gate may apply without a separate approval step.
    auto_promote: tuple[str, ...] = (TARGET_MEMORY,)
    #: Fractional cost increase the gate tolerates. `None` means "not configured",
    #: in which case the measured increase is reported but not enforced.
    cost_increase_limit: float | None = None

    canary_allocation_percent: int = 10
    canary_max_calls: int = 200
    canary_budget_microusd: int | None = None
    canary_deadline_hours: int = 72
    canary_target_role: str = "researcher"
    canary_target_purpose: str = "write_research_section"

    minimum_canary_samples: int = 20
    minimum_champion_samples: int = 20


DEFAULT_PROMOTION_POLICY = PromotionPolicy()


@dataclass(frozen=True)
class PromotionDecision:
    promotion_id: str
    target: str
    candidate_id: str
    outcome: str
    gate: dict[str, bool] = field(default_factory=dict)
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def promoted(self) -> bool:
        return self.outcome in APPLIED_OUTCOMES

    @property
    def settled(self) -> bool:
        """True when the gate reached a final state and no step is outstanding."""
        return self.outcome in {
            OUTCOME_PROMOTED, OUTCOME_REJECTED, OUTCOME_ROLLED_BACK, OUTCOME_NEEDS_REPLAY,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"promotion_id": self.promotion_id, "target": self.target, "candidate_id": self.candidate_id,
                "outcome": self.outcome, "gate": dict(self.gate), "reason": self.reason,
                "detail": dict(self.detail)}


# ------------------------------------------------------------------ the rules

def cost_increase(metrics: Mapping[str, Any]) -> float | None:
    """Fractional cost change of candidate over baseline, or None if unmeasured."""
    base = metrics.get("cost_baseline_microusd")
    candidate = metrics.get("cost_candidate_microusd")
    if isinstance(base, int) and isinstance(candidate, int) and base > 0:
        return (candidate - base) / base
    return None


def _judge_not_worse(judge: Any) -> bool:
    """The judge is a soft signal: candidate must not lose to the baseline."""
    if judge is None:
        return True
    if not isinstance(judge, Mapping):
        return False
    wins = judge.get("wins")
    losses = judge.get("losses")
    if isinstance(wins, int) and isinstance(losses, int):
        return wins + losses + int(judge.get("ties", 0)) > 0 and wins >= losses
    winner = judge.get("winner")
    return winner in (None, "candidate", "tie")


def _no_permission_expansion(evaluation: EvaluationResult) -> bool:
    if evaluation.metrics.get("permission_expansion"):
        return False
    present = [bool(evaluation.rule_checks[name]) for name in PERMISSION_CHECKS if name in evaluation.rule_checks]
    return all(present)


def _cost_within_limit(metrics: Mapping[str, Any], policy: PromotionPolicy) -> bool:
    limit = policy.cost_increase_limit
    if limit is None:
        return True
    measured = cost_increase(metrics)
    if measured is None:
        # A configured limit with nothing measured is not evidence of compliance.
        return False
    return measured <= limit


def promotion_gate(evaluation: EvaluationResult, *, target: str = "",
                   policy: PromotionPolicy = DEFAULT_PROMOTION_POLICY) -> dict[str, bool]:
    """Evaluate the V3 §21 gate. Pure function of structured evaluation output."""
    resolved = target or evaluation.target
    metrics = evaluation.metrics or {}
    judge = evaluation.judge
    checks = {
        "rule_eval_pass": bool(evaluation.rule_pass) and evaluation.outcome == "PASS",
        "evidence_complete": evaluation.outcome != OUTCOME_NEEDS_REPLAY,
        "judge_present": judge is not None if resolved in policy.judge_required else True,
        "judge_not_worse": _judge_not_worse(judge) if resolved in policy.judge_required else True,
        "no_safety_regression": int(metrics.get("safety_regressions", 0) or 0) == 0,
        "no_permission_expansion": _no_permission_expansion(evaluation),
        "cost_within_limit": _cost_within_limit(metrics, policy),
    }
    return {name: bool(checks[name]) for name in GATE_CHECKS}


def failed_checks(checks: Mapping[str, bool]) -> list[str]:
    return [name for name in GATE_CHECKS if not checks.get(name, False)]


# ------------------------------------------------------------------ the gate

class LearningPromotionGate:
    """Applies a passing evaluation to the target that owns the candidate."""

    def __init__(self, adapters: Mapping[str, Any], *, evolution: Any = None,
                 policy: PromotionPolicy = DEFAULT_PROMOTION_POLICY, db: Any = None,
                 clock: Any = None) -> None:
        self.adapters = dict(adapters)
        self.evolution = evolution if evolution is not None else self._resolve("evolution")
        self.db = db if db is not None else self._resolve("db")
        self.policy = policy
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _resolve(self, attribute: str) -> Any:
        for adapter in self.adapters.values():
            value = getattr(adapter, attribute, None)
            if value is not None:
                return value
        return None

    def adapter_for(self, target: str) -> Any:
        adapter = self.adapters.get(target)
        if adapter is None:
            raise KeyError(f"no promotion adapter for target {target!r}")
        return adapter

    # ------------------------------------------------------------- read-only

    def gate(self, evaluation: EvaluationResult, *, target: str = "") -> dict[str, bool]:
        return promotion_gate(evaluation, target=target, policy=self.policy)

    def history(self, owner_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM learning_promotions WHERE owner_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (owner_id, int(limit)),
            ).fetchall()
        return [self._row(row) for row in rows]

    def metrics(self, owner_id: str) -> dict[str, Any]:
        """Dashboard counters (V3 §54). `promotion_regression_rate` is the one that matters."""
        rows = self.history(owner_id, limit=10_000)
        promoted = sum(1 for row in rows if row["outcome"] == OUTCOME_PROMOTED)
        rolled_back = sum(1 for row in rows if row["outcome"] == OUTCOME_ROLLED_BACK)
        rejected = sum(1 for row in rows if row["outcome"] == OUTCOME_REJECTED)
        decided = promoted + rejected
        return {
            "promotion_attempts": len(rows),
            "promoted": promoted,
            "rejected": rejected,
            "rolled_back": rolled_back,
            "candidate_promote_rate": None if not decided else round(promoted / decided, 4),
            "rollback_rate": None if not promoted else round(rolled_back / promoted, 4),
            "promotion_regression_rate": None if not promoted else round(rolled_back / promoted, 4),
        }

    def canary_status(self, candidate: Mapping[str, Any], *, owner_id: str) -> dict[str, Any]:
        """The V3 §40 canary gate, read straight from `canary_exposures`."""
        if self.evolution is None or self.db is None:
            return {"ready": False, "reason": "no canary runtime configured"}
        stored = self.evolution.get_candidate(candidate["candidate_id"], owner_id)
        deployment_id = stored.get("deployment_id")
        if not deployment_id:
            return {"ready": False, "reason": "no canary deployment for this candidate", "checks": {}}
        with self.db.connection() as connection:
            deployment = connection.execute("SELECT status,deadline_at FROM canary_deployments WHERE id=?", (deployment_id,)).fetchone()
            rows = connection.execute(
                "SELECT cohort,success,safety_pass,finished_at,cost_microusd FROM canary_exposures WHERE deployment_id=?",
                (deployment_id,),
            ).fetchall()
        completed = [row for row in rows if row["finished_at"] is not None and row["success"] is not None and row["safety_pass"] is not None]
        challenger = [row for row in completed if row["cohort"] == "challenger"]
        champion = [row for row in completed if row["cohort"] == "champion"]
        pending_safety = len(rows) - len(completed)
        safety_failures = sum(1 for row in rows if row["safety_pass"] == 0)
        success_failures = sum(1 for row in rows if row["success"] == 0)
        permission_violation = bool((stored.get("permission_diff") or {}).get("added"))
        checks = {
            "challenger_minimum": len(challenger) >= self.policy.minimum_canary_samples,
            "champion_minimum": len(champion) >= self.policy.minimum_champion_samples,
            "pending_safety": pending_safety == 0,
            "safety_failures": safety_failures == 0,
            "success_failures": success_failures == 0,
            "permission_violation": not permission_violation,
            "deployment_active": bool(deployment and deployment["status"] == "ACTIVE"),
            "deadline_valid": bool(deployment and (not deployment["deadline_at"] or datetime.fromisoformat(deployment["deadline_at"]) > self._clock())),
        }
        return {
            "ready": all(checks.values()),
            "stopped": not checks["deployment_active"] or not checks["deadline_valid"],
            "deployment_id": deployment_id,
            "challenger": len(challenger),
            "champion": len(champion),
            "pending_safety": pending_safety,
            "safety_failures": safety_failures,
            "success_failures": success_failures,
            "permission_violation": permission_violation,
            "checks": checks,
        }

    # -------------------------------------------------------------- applying

    def promote(self, candidate: Mapping[str, Any], evaluation: EvaluationResult, *, owner_id: str,
                approval: Any = None, job_id: str = "", canary: Mapping[str, Any] | None = None,
                evaluation_id: str = "", shadow: bool = False) -> PromotionDecision:
        """Run the gate, then apply the candidate on its target's own path.

        `shadow=True` runs everything except the apply step (V3 §41): the gate
        still decides, so the recorded `would_be` is the real answer, but no
        production asset is touched.
        """
        target = evaluation.target or candidate.get("target", "")
        if target not in (TARGET_MEMORY, TARGET_SKILL, TARGET_BEHAVIOR):
            # The audit table only admits the three contracted targets, so an
            # unknown value means the contract layer was bypassed. Fail loudly.
            raise ValueError(f"unknown promotion target: {target!r}")
        candidate_id = candidate["candidate_id"]
        prior = self._existing_promotion(owner_id, candidate_id)
        if prior is not None:
            return self._decision(prior, reason="candidate was already promoted")
        checks = self.gate(evaluation, target=target)
        context = {"job_id": job_id, "evaluation_id": evaluation_id or str(candidate.get("evaluation_id", ""))}

        if evaluation.outcome == OUTCOME_NEEDS_REPLAY:
            return self._record(target=target, candidate_id=candidate_id, owner_id=owner_id,
                                outcome=OUTCOME_NEEDS_REPLAY, checks=checks, reason=evaluation.reason, **context)
        failed = failed_checks(checks)
        if failed:
            return self._record(target=target, candidate_id=candidate_id, owner_id=owner_id,
                                outcome=OUTCOME_REJECTED, checks=checks,
                                reason=f"promotion gate failed: {', '.join(failed)}", **context)
        if shadow:
            return self._record(target=target, candidate_id=candidate_id, owner_id=owner_id,
                                outcome=OUTCOME_SHADOW, checks=checks, reason="",
                                detail={"would_be": self._would_be(target, approval), "shadow": True}, **context)

        if target == TARGET_MEMORY:
            return self._apply(target=target, candidate=candidate, owner_id=owner_id, checks=checks, **context)
        if target == TARGET_SKILL:
            if approval is None and TARGET_SKILL not in self.policy.auto_promote:
                return self._record(target=target, candidate_id=candidate_id, owner_id=owner_id,
                                    outcome=OUTCOME_PENDING_APPROVAL, checks=checks,
                                    reason="skill promotion requires human or policy approval", **context)
            return self._apply(target=target, candidate=candidate, owner_id=owner_id, checks=checks,
                               authority=_approval_actor(approval), **context)
        return self._request_canary(candidate=candidate, owner_id=owner_id, checks=checks,
                                    approval=approval, canary=canary, **context)

    @staticmethod
    def _would_be(target: str, approval: Any) -> str:
        """What the gate would have applied, had shadow mode not blocked it."""
        if target == TARGET_MEMORY:
            return OUTCOME_PROMOTED
        if target == TARGET_SKILL:
            return OUTCOME_PROMOTED if approval is not None else OUTCOME_PENDING_APPROVAL
        return OUTCOME_CANARY_STARTED if approval is not None else OUTCOME_PENDING_APPROVAL

    def complete_canary(self, candidate: Mapping[str, Any], *, owner_id: str, job_id: str = "",
                        evaluation_id: str = "") -> PromotionDecision:
        """Finish a Behavior promotion once the canary has enough clean exposures."""
        prior = self._existing_promotion(owner_id, candidate["candidate_id"])
        if prior is not None:
            return self._decision(prior)
        status = self.canary_status(candidate, owner_id=owner_id)
        target = str(candidate.get("target") or TARGET_BEHAVIOR)
        if not status.get("ready"):
            return self._record(target=target, candidate_id=candidate["candidate_id"], owner_id=owner_id,
                                outcome=OUTCOME_CANARY_PENDING, checks={},
                                reason=status.get("reason") or _canary_reason(status), detail=status,
                                job_id=job_id, evaluation_id=evaluation_id)
        return self._apply(target=target, candidate=candidate, owner_id=owner_id, checks={},
                           detail=status, job_id=job_id, evaluation_id=evaluation_id)

    def rollback(self, candidate: Mapping[str, Any], *, owner_id: str, reason: str,
                 job_id: str = "") -> PromotionDecision:
        target = str(candidate.get("target") or TARGET_BEHAVIOR)
        try:
            applied = self.adapter_for(target).rollback(candidate, owner_id=owner_id, reason=reason)
        except (KeyError, ValueError) as exc:
            return self._record(target=target, candidate_id=candidate["candidate_id"], owner_id=owner_id,
                                outcome=OUTCOME_REJECTED, checks={}, reason=f"rollback refused: {exc}",
                                job_id=job_id)
        return self._record(target=target, candidate_id=candidate["candidate_id"], owner_id=owner_id,
                            outcome=OUTCOME_ROLLED_BACK, checks={}, reason=reason, detail={"applied": applied},
                            job_id=job_id)

    # ------------------------------------------------------------- internals

    def _existing_promotion(self, owner_id: str, candidate_id: str) -> Any:
        """A candidate is promoted at most once; a repeat call is a no-op."""
        if self.db is None:
            return None
        with self.db.connection() as connection:
            return connection.execute(
                "SELECT * FROM learning_promotions WHERE owner_id=? AND candidate_id=? AND outcome='PROMOTED'",
                (owner_id, candidate_id),
            ).fetchone()

    def _apply(self, *, target: str, candidate: Mapping[str, Any], owner_id: str,
               checks: Mapping[str, bool], detail: Mapping[str, Any] | None = None, job_id: str = "",
               evaluation_id: str = "", authority: str = "learning_promotion") -> PromotionDecision:
        try:
            applied = self.adapter_for(target).promote(candidate, owner_id=owner_id, authority=authority)
        except (KeyError, ValueError, RuntimeError) as exc:
            return self._record(target=target, candidate_id=candidate["candidate_id"], owner_id=owner_id,
                                outcome=OUTCOME_REJECTED, checks=checks,
                                reason=f"{type(exc).__name__}: {exc}", job_id=job_id, evaluation_id=evaluation_id)
        payload = dict(detail or {})
        payload["applied"] = applied
        return self._record(target=target, candidate_id=candidate["candidate_id"], owner_id=owner_id,
                            outcome=OUTCOME_PROMOTED, checks=checks, reason="", detail=payload,
                            job_id=job_id, evaluation_id=evaluation_id)

    def _request_canary(self, *, candidate: Mapping[str, Any], owner_id: str, checks: Mapping[str, bool],
                        approval: Any, canary: Mapping[str, Any] | None, job_id: str = "",
                        evaluation_id: str = "") -> PromotionDecision:
        candidate_id = candidate["candidate_id"]
        if approval is None:
            return self._record(target=TARGET_BEHAVIOR, candidate_id=candidate_id, owner_id=owner_id,
                                outcome=OUTCOME_PENDING_APPROVAL, checks=checks,
                                reason="behavior promotion requires approval before canary",
                                job_id=job_id, evaluation_id=evaluation_id)
        if self.evolution is None:
            return self._record(target=TARGET_BEHAVIOR, candidate_id=candidate_id, owner_id=owner_id,
                                outcome=OUTCOME_REJECTED, checks=checks,
                                reason="no evolution runtime configured", job_id=job_id, evaluation_id=evaluation_id)
        stored = self.evolution.get_candidate(candidate_id, owner_id)
        status = stored.get("status")
        if status == "READY_FOR_EVAL":
            return self._record(target=TARGET_BEHAVIOR, candidate_id=candidate_id, owner_id=owner_id,
                                outcome=OUTCOME_PENDING_RELEASE_EVALUATION, checks=checks,
                                reason="a behavior candidate needs an approvable release evaluation before approval",
                                job_id=job_id, evaluation_id=evaluation_id)
        if status != "EVALUATED":
            return self._record(target=TARGET_BEHAVIOR, candidate_id=candidate_id, owner_id=owner_id,
                                outcome=OUTCOME_REJECTED, checks=checks,
                                reason=f"behavior candidate is {status!r}, not awaiting approval",
                                job_id=job_id, evaluation_id=evaluation_id)
        settings = dict(canary or {})
        try:
            decision = self.evolution.approve_current(
                candidate_id, expires_at=self._expiry(),
                idempotency_key=f"{candidate_id}:promote-approval", owner_id=owner_id)
            stored = self.evolution.get_candidate(candidate_id, owner_id)
            deployment = self.evolution.start_canary(
                candidate_id, expected_version=stored["version"], approval_id=decision["id"],
                allocation_percent=int(settings.get("allocation_percent", self.policy.canary_allocation_percent)),
                assignment_unit="run", idempotency_key=f"{candidate_id}:canary", owner_id=owner_id,
                target_role=str(settings.get("target_role", self.policy.canary_target_role)),
                target_purpose=str(settings.get("target_purpose", self.policy.canary_target_purpose)),
                budget_microusd=settings.get("budget_microusd", self.policy.canary_budget_microusd),
                deadline_at=settings.get("deadline_at") or self._canary_deadline(),
                max_calls=int(settings.get("max_calls", self.policy.canary_max_calls)),
            )
        except (KeyError, ValueError, RuntimeError) as exc:
            return self._record(target=TARGET_BEHAVIOR, candidate_id=candidate_id, owner_id=owner_id,
                                outcome=OUTCOME_REJECTED, checks=checks,
                                reason=f"{type(exc).__name__}: {exc}", job_id=job_id, evaluation_id=evaluation_id)
        return self._record(target=TARGET_BEHAVIOR, candidate_id=candidate_id, owner_id=owner_id,
                            outcome=OUTCOME_CANARY_STARTED, checks=checks, reason="",
                            detail={"deployment_id": deployment["id"], "approval_id": decision["id"]},
                            job_id=job_id, evaluation_id=evaluation_id)

    def _expiry(self) -> str:
        return (self._clock() + timedelta(hours=1)).isoformat()

    def _canary_deadline(self) -> str:
        return (self._clock() + timedelta(hours=self.policy.canary_deadline_hours)).isoformat()

    def _record(self, *, target: str, candidate_id: str, owner_id: str, outcome: str,
                checks: Mapping[str, bool], reason: str = "", detail: Mapping[str, Any] | None = None,
                job_id: str = "", evaluation_id: str = "") -> PromotionDecision:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown promotion outcome: {outcome!r}")
        gate = {name: bool(checks[name]) for name in GATE_CHECKS if name in checks}
        payload = dict(detail or {})
        evidence = {"owner_id": owner_id, "target": target, "candidate_id": candidate_id, "outcome": outcome,
                    "gate": gate, "reason": reason, "detail": payload, "evaluation_id": evaluation_id}
        digest = _digest(evidence)
        key = f"{target}:{candidate_id}:{outcome}:{digest[:16]}"
        if self.db is None:
            return PromotionDecision(promotion_id=f"promotion_{uuid.uuid4().hex}", target=target,
                                     candidate_id=candidate_id, outcome=outcome, gate=gate,
                                     reason=reason, detail=payload)
        with self.db.transaction() as connection:
            prior = connection.execute(
                "SELECT * FROM learning_promotions WHERE owner_id=? AND idempotency_key=?", (owner_id, key)
            ).fetchone()
            if prior is not None:
                return self._decision(prior)
            if outcome == OUTCOME_PROMOTED:
                existing = connection.execute(
                    "SELECT * FROM learning_promotions WHERE owner_id=? AND candidate_id=? AND outcome='PROMOTED'",
                    (owner_id, candidate_id),
                ).fetchone()
                if existing is not None:
                    # The DB index would refuse the insert anyway; report it as a decision.
                    return self._decision(existing, reason="candidate was already promoted")
            promotion_id = f"promotion_{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO learning_promotions(id,owner_id,job_id,target,candidate_id,evaluation_id,outcome,"
                "gate_json,reason,detail_json,evidence_digest,idempotency_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (promotion_id, owner_id, job_id, target, candidate_id, evaluation_id, outcome,
                 json.dumps(gate, sort_keys=True), reason, json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                 digest, key, _now()),
            )
            row = connection.execute("SELECT * FROM learning_promotions WHERE id=?", (promotion_id,)).fetchone()
        return self._decision(row)

    @staticmethod
    def _decision(row: Any, *, reason: str | None = None) -> PromotionDecision:
        return PromotionDecision(
            promotion_id=row["id"], target=row["target"], candidate_id=row["candidate_id"],
            outcome=row["outcome"], gate=json.loads(row["gate_json"] or "{}"),
            reason=row["reason"] if reason is None else reason,
            detail=json.loads(row["detail_json"] or "{}"),
        )

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return {"promotion_id": row["id"], "owner_id": row["owner_id"], "job_id": row["job_id"],
                "target": row["target"], "candidate_id": row["candidate_id"],
                "evaluation_id": row["evaluation_id"], "outcome": row["outcome"],
                "gate": json.loads(row["gate_json"] or "{}"), "reason": row["reason"],
                "detail": json.loads(row["detail_json"] or "{}"),
                "evidence_digest": row["evidence_digest"], "created_at": row["created_at"]}


def _approval_actor(approval: Any) -> str:
    if isinstance(approval, Mapping):
        actor = approval.get("actor")
        if isinstance(actor, str) and actor:
            return actor
    return "learning_promotion"


def _canary_reason(status: Mapping[str, Any]) -> str:
    failed = [name for name, passed in (status.get("checks") or {}).items() if not passed]
    return f"canary gate not met: {', '.join(failed)}" if failed else "canary is not ready"


__all__ = [
    "APPLIED_OUTCOMES", "DEFAULT_PROMOTION_POLICY", "GATE_CHECKS", "LearningPromotionGate", "OUTCOMES",
    "OUTCOME_CANARY_PENDING", "OUTCOME_CANARY_STARTED", "OUTCOME_NEEDS_REPLAY", "OUTCOME_PENDING_APPROVAL",
    "OUTCOME_PENDING_RELEASE_EVALUATION", "OUTCOME_PROMOTED", "OUTCOME_REJECTED", "OUTCOME_ROLLED_BACK",
    "OUTCOME_SHADOW", "PromotionDecision", "PromotionPolicy", "cost_increase", "failed_checks",
    "promotion_gate",
]
