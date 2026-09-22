"""JEV decides *whether* an Experience is worth learning and *into which target*.

This module is deliberately narrow: it never produces a Memory, a Skill or a
Prompt, and it holds no production write authority. Its only durable effect is
an append-only row in `learning_decisions` recording what the harness decided.

Contract rules enforced here:

* the target set is closed (`MEMORY` / `SKILL` / `BEHAVIOR` / `IGNORE`);
* any malformed, missing, out-of-range or unknown field fails **closed** to
  `IGNORE` — a broken model response is never read as an instruction to learn;
* JEV sees the Experience, the task result, failure tags, key tool trace, user
  feedback and an asset summary — never HOLDOUT/SAFETY data or judge feedback.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .learning_contract import (
    RISKS,
    TARGET_BEHAVIOR,
    TARGET_IGNORE,
    TARGET_MEMORY,
    TARGET_SKILL,
    TARGETS,
    normalize_target,
)

TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DECISION_ROLE = "learning_decision"
DECISION_PURPOSE = "classify_learning_opportunity"

# Reason codes are derived from dedicated `noul` probes: JEV does not explain
# itself, so the codes come from questions whose meaning is fixed here.
REASON_PROBES: tuple[tuple[str, str], ...] = (
    ("transient_operational",
     "Is the observed failure explainable by transient operational conditions (network timeout, API authentication failure, third-party outage, user cancellation) rather than by the agent's own knowledge, method or decision-making?"),
    ("recurring_pattern",
     "Is there evidence that this problem has recurred across independent tasks or runs, rather than being a one-off occurrence?"),
    ("explicit_user_constraint",
     "Did the user state an explicit, durable constraint, preference or fact that the agent should remember for future tasks?"),
    ("repeatable_procedure",
     "Does the experience demonstrate a task-specific method or ordered procedure that produced a result and could be executed again on similar future tasks? An answer format or general evidence/uncertainty standard alone is not such a procedure."),
    ("decision_behavior_defect",
     "Is the problem a general defect in how the agent decides or acts (for example confirming a root cause with insufficient evidence) that no reusable procedure captures?"),
    ("evidence_sufficient",
     "Is the available evidence sufficient to justify a durable change to the agent's assets?"),
)

#: Which target each reason probe argues for, when it argues for one at all. The
#: probes and the `target` choice are separate questions and JEV does not
#: reconcile them, so a probe that fires while a different target wins is a
#: disagreement the audit has to carry. An empty string means the probe argues
#: against IGNORE without naming a replacement.
PROBE_TARGETS: dict[str, str] = {
    "transient_operational": TARGET_IGNORE,
    "recurring_pattern": "",
    "explicit_user_constraint": TARGET_MEMORY,
    "repeatable_procedure": TARGET_SKILL,
    "decision_behavior_defect": TARGET_BEHAVIOR,
    "evidence_sufficient": "",
}

#: Recorded when a probe argued for one target and the choice picked another.
DISAGREEMENT_REASON = "probe_target_disagreement"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


class DecisionSchemaError(ValueError):
    """A model response that cannot be read under the V3 contract."""


class JevUnavailable(RuntimeError):
    """The decision could not be obtained; the caller decides on retry/UNKNOWN."""


def _finite_number(value: Any, *, low: float = 0.0, high: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionSchemaError(f"expected a number, got {type(value).__name__}")
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise DecisionSchemaError("expected a finite number")
    if not low <= number <= high:
        raise DecisionSchemaError(f"number {number} outside [{low}, {high}]")
    return number


@dataclass(frozen=True)
class LearningDecision:
    """The routing verdict for one Experience."""

    learn: bool
    target: str
    subtype: str
    confidence: float
    importance: float
    risk: str
    reason_codes: tuple[str, ...]
    model_identity: str = ""
    probabilities: Mapping[str, float] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)
    #: The distribution mass behind the chosen target. This is the surface the
    #: routing gate reads; `confidence` is JEV's self-report and is kept only for
    #: the audit, because the two were measured to disagree.
    support: float = 0.0

    @property
    def ignored(self) -> bool:
        return not self.learn or self.target == TARGET_IGNORE

    def to_dict(self) -> dict[str, Any]:
        return {
            "learn": self.learn, "target": self.target, "subtype": self.subtype,
            "confidence": self.confidence, "support": self.support,
            "importance": self.importance, "risk": self.risk,
            "reason_codes": list(self.reason_codes), "model_identity": self.model_identity,
            "probabilities": dict(self.probabilities),
        }

    @classmethod
    def ignored_default(cls, *, reason: str = "schema_invalid", model_identity: str = "") -> "LearningDecision":
        """The fail-closed default. Never learn from a response we cannot read."""
        return cls(learn=False, target=TARGET_IGNORE, subtype="", confidence=0.0, importance=0.0,
                   risk="high", reason_codes=(reason,), model_identity=model_identity)

    @classmethod
    def from_answers(cls, answers: Any, *, model_identity: str = "",
                     min_target_confidence: float = 0.0) -> "LearningDecision":
        """Parse raw JEV answers into the closed contract, or fail closed."""
        if not isinstance(answers, Mapping):
            raise DecisionSchemaError("answers must be an object")
        learn_probability = answers.get("learn")
        if not isinstance(learn_probability, Mapping) or "noul" not in learn_probability:
            raise DecisionSchemaError("learn answer must be a noul result")
        learn_value = _finite_number(learn_probability["noul"])
        target_answer = answers.get("target")
        if not isinstance(target_answer, Mapping):
            raise DecisionSchemaError("target answer must be an object")
        chosen = target_answer.get("choice")
        if not isinstance(chosen, str) or chosen not in TARGETS:
            raise DecisionSchemaError(f"unknown target choice: {chosen!r}")
        target, subtype = normalize_target(chosen)
        probabilities = target_answer.get("probabilities") or {}
        if not isinstance(probabilities, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, (int, float)) or isinstance(value, bool)
            for key, value in probabilities.items()
        ):
            raise DecisionSchemaError("target probabilities must be a numeric object")
        confidence = _finite_number(target_answer.get("confidence"))
        # JEV returns the point estimate, the distribution over the closed target
        # set, and a self-reported confidence as three separate surfaces, and they
        # do not always agree. The routing gate reads the *distribution mass
        # behind the chosen target*, not the self-report: measured on
        # `evals/cases/learning-decision-v3.json`, the point estimate equals the
        # distribution's argmax on all 80 cases, while the self-report fell below
        # the threshold on 5 of them — suppressing correct calls the distribution
        # was confident about. A chosen target with no mass behind it fails closed.
        support = probabilities.get(chosen)
        support = _finite_number(support) if isinstance(support, (int, float)) and not isinstance(support, bool) else 0.0
        risk_answer = answers.get("risk")
        if not isinstance(risk_answer, Mapping) or risk_answer.get("choice") not in RISKS:
            raise DecisionSchemaError("risk answer must choose one of low/medium/high")
        risk = str(risk_answer["choice"])
        importance_answer = answers.get("importance")
        if not isinstance(importance_answer, Mapping) or "score" not in importance_answer:
            raise DecisionSchemaError("importance answer must be a score result")
        importance = _finite_number(importance_answer["score"], low=0.0, high=9.0) / 9.0
        reasons = []
        for name, _ in REASON_PROBES:
            probe = answers.get(name)
            if not isinstance(probe, Mapping) or "noul" not in probe:
                raise DecisionSchemaError(f"reason probe {name!r} must be a noul result")
            if _finite_number(probe["noul"]) >= 0.5:
                reasons.append(name)
        # The probes are diagnostics, not a second vote: the `target` choice is
        # authoritative. But when a probe argues for a different target the audit
        # must show it, otherwise "the model was unsure" and "the question set is
        # ambiguous" look identical in the record.
        if any(PROBE_TARGETS.get(name) not in ("", target) for name in reasons):
            reasons.append(DISAGREEMENT_REASON)
        if chosen == TARGET_IGNORE:
            return cls(learn=False, target=TARGET_IGNORE, subtype="", confidence=confidence, support=support,
                       importance=importance, risk=risk, reason_codes=tuple(sorted(set(reasons))),
                       model_identity=model_identity, probabilities=dict(probabilities))
        if support < min_target_confidence:
            # Low-support routing: a classification the model did not actually
            # commit to is not a licence to learn.
            reasons.append("low_target_support")
        elif learn_value >= 0.5:
            return cls(learn=True, target=target, subtype=subtype, confidence=confidence, support=support,
                       importance=importance, risk=risk, reason_codes=tuple(sorted(set(reasons))),
                       model_identity=model_identity, probabilities=dict(probabilities))
        return cls(learn=False, target=TARGET_IGNORE, subtype="", confidence=confidence, support=support,
                   importance=importance, risk=risk, reason_codes=tuple(sorted(set(reasons))),
                   model_identity=model_identity, probabilities=dict(probabilities))


def build_questions() -> dict[str, dict[str, Any]]:
    """The frozen JEV question set. Criteria must stay mutually distinguishable."""
    questions: dict[str, dict[str, Any]] = {
        "learn": {
            "type": "noul",
            "instructions": "Answer true only if this experience should durably change what the agent knows, how it works, or how it decides. Operational noise, one-off accidents and third-party outages are false.",
            "criteria": {
                "true": "The experience carries a durable, generalizable lesson worth persisting.",
                "false": "The experience is transient, ungrounded, or not worth persisting.",
            },
        },
        "target": {
            "type": "choice",
            "instructions": (
                "Choose the single surface this lesson belongs to. Apply these rules in order and stop at the "
                "first one that matches. (1) SKILL: the experience shows an ordered method or procedure that "
                "produced a result and would be reusable on a similar future task. (2) MEMORY: it records a "
                "durable fact, preference, constraint, decision or lesson to recall later. (3) BEHAVIOR: it "
                "shows a general defect in how the agent decides or acts that no reusable procedure captures. "
                "(4) IGNORE: nothing durable is worth learning. Two rules of precedence matter: a lesson that "
                "is both a reusable procedure and a sign of a general defect is a SKILL, because the procedure "
                "is the actionable artefact; and recurrence on its own is not a reason to choose IGNORE. "
                "A reusable procedure must be demonstrated task-specific work, not merely an answer format "
                "or a general standard for evidence, uncertainty or inference. Correcting those general "
                "decision standards belongs to BEHAVIOR when no demonstrated task procedure is supplied. "
                "A personal presentation preference without a decision defect remains MEMORY."
            ),
            "criteria": {
                "SKILL": "An ordered method or procedure that produced a result and is reusable on a similar future task.",
                "MEMORY": "A durable fact, user preference, constraint, decision or lesson to recall later, with no reusable procedure.",
                "BEHAVIOR": "A general decision/acting defect that applies across tasks and that no reusable procedure captures.",
                "IGNORE": "Nothing durable is worth learning from this experience.",
            },
        },
        "risk": {
            "type": "choice",
            "instructions": "Rate the blast radius of adopting this lesson if it turned out to be wrong.",
            "criteria": {
                "low": "Wrong adoption is cheap and easy to notice.",
                "medium": "Wrong adoption degrades quality or wastes effort before being noticed.",
                "high": "Wrong adoption could cause unsafe, destructive or permission-relevant action.",
            },
        },
        "importance": {
            "type": "score",
            "instructions": "Rate how much future task quality depends on learning this lesson. 0 = irrelevant, 9 = critical and repeatedly applicable.",
            "criteria": [
                "0 irrelevant to future tasks",
                "1 rarely relevant",
                "2 occasionally useful",
                "3 mildly useful",
                "4 moderately useful",
                "5 clearly useful",
                "6 frequently useful",
                "7 highly useful",
                "8 near critical",
                "9 critical and repeatedly applicable",
            ],
        },
    }
    for name, question in REASON_PROBES:
        questions[name] = {
            "type": "noul",
            "instructions": question,
            "criteria": {"true": "Yes, this describes the experience.", "false": "No, this does not describe the experience."},
        }
    return questions


def build_state(experience: Mapping[str, Any], *, task_result: Mapping[str, Any] | None = None,
                failure_tags: Any = (), tool_trace: Any = (), user_feedback: Any = None,
                assets_summary: Mapping[str, Any] | None = None, policy: Mapping[str, Any] | None = None,
                max_trace_items: int = 12, max_text: int = 1200) -> dict[str, Any]:
    """Assemble the JEV state. Never include holdout, safety answers or judge feedback."""

    def clip(value: Any) -> Any:
        if isinstance(value, str):
            return value[:max_text]
        if isinstance(value, Mapping):
            return {str(key): clip(item) for key, item in list(value.items())[:40]}
        if isinstance(value, (list, tuple)):
            return [clip(item) for item in list(value)[:max_trace_items]]
        return value

    return {
        "experience": clip(dict(experience)),
        "task_result": clip(dict(task_result or {})),
        "failure_tags": clip(list(failure_tags or [])),
        "tool_trace": clip(list(tool_trace or [])),
        "user_feedback": clip(user_feedback),
        "current_assets": clip(dict(assets_summary or {})),
        "classification_policy": clip(dict(policy or {
            "precedence": ["SKILL", "MEMORY", "BEHAVIOR", "IGNORE"],
            "SKILL": "Ordered method or procedure that produced a result and is reusable on a similar task.",
            "MEMORY": "Durable fact, preference, constraint, decision or lesson, with no reusable procedure.",
            "BEHAVIOR": "General decision/acting defect across tasks that no reusable procedure captures.",
            "IGNORE": "Transient error, network timeout, authentication failure, user cancellation, third-party outage.",
            "tie_break": "A lesson that is both a reusable procedure and a general defect is a SKILL.",
        })),
    }


class TypesafeClient:
    """Minimal synchronous client for the TypeSafe System One endpoint."""

    def __init__(self, api_key: str | None = None, *, endpoint: str = TYPESAFE_ENDPOINT,
                 model: str = DEFAULT_MODEL, timeout: float = 60.0, transport: Any = None) -> None:
        self.api_key = api_key or os.getenv("TYPESAFE_API_KEY") or ""
        if not self.api_key:
            raise JevUnavailable("TYPESAFE_API_KEY is not configured")
        self.endpoint, self.model, self.timeout, self.transport = endpoint, model, timeout, transport

    def ask(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> dict[str, Any]:
        import httpx

        payload = {"model": self.model, "state": dict(state), "questions": dict(questions)}
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                response = client.post(self.endpoint, json=payload,
                                       headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
                if response.status_code >= 400:
                    # Keep the provider's own explanation: it is the only way to
                    # tell a malformed question set from an auth or quota problem.
                    raise JevUnavailable(f"jev returned {response.status_code}: {response.text[:500]}")
                body = response.json()
        except JevUnavailable:
            raise
        except Exception as exc:  # network, JSON — all mean "no decision available"
            raise JevUnavailable(f"jev request failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(body, Mapping) or not isinstance(body.get("answers"), Mapping):
            raise JevUnavailable("jev response has no answers object")
        return {"answers": dict(body["answers"]), "model": str(body.get("model") or self.model)}


class JevDecisionService:
    """Decides whether an Experience is worth learning and into which target."""

    # 列名只声明一次：INSERT 的列清单、占位符个数、取值顺序全部由它派生，加列时不会三处不同步。
    # 占位符必须用 `?`（qmark）：`db._postgres_sql` 只把 `?` 转成 `%s`，`ON CONFLICT` 里的
    # `:name` 命名风格只有 SQLite 认，PostgreSQL 会直接 `syntax error at or near ":"`。
    DECISION_COLUMNS = (
        "id", "job_id", "owner_id", "learn", "target", "subtype", "confidence", "support",
        "importance", "risk", "reason_codes_json", "input_digest", "decision_digest",
        "model_identity", "created_at",
    )

    def __init__(self, db: Any, client: Any = None, *, model: str = DEFAULT_MODEL,
                 min_target_confidence: float = 0.5) -> None:
        self.db = db
        self.client = client
        self.model = model
        self.min_target_confidence = min_target_confidence

    def decide(self, *, owner_id: str, job_id: str, experience: Mapping[str, Any],
               task_result: Mapping[str, Any] | None = None, failure_tags: Any = (),
               tool_trace: Any = (), user_feedback: Any = None,
               assets_summary: Mapping[str, Any] | None = None) -> LearningDecision:
        """Classify one Experience. Writes only the append-only decision audit."""
        if not owner_id or not job_id:
            raise ValueError("owner_id and job_id are required")
        state = build_state(experience, task_result=task_result, failure_tags=failure_tags, tool_trace=tool_trace,
                            user_feedback=user_feedback, assets_summary=assets_summary)
        input_digest = _digest(state)
        if self.client is None:
            raise JevUnavailable("no JEV client is configured")
        response = self.client.ask(state, build_questions())
        identity = f"typesafe:{response.get('model') or self.model}"
        try:
            decision = LearningDecision.from_answers(response["answers"], model_identity=identity,
                                                     min_target_confidence=self.min_target_confidence)
        except DecisionSchemaError as exc:
            # Fail closed: a response we cannot read is not permission to learn.
            decision = LearningDecision.ignored_default(reason=f"schema_invalid:{type(exc).__name__}", model_identity=identity)
            decision = LearningDecision(**{**decision.to_dict(), "reason_codes": ("schema_invalid",)},
                                        raw={"error": str(exc)[:400]})
        self.record(owner_id=owner_id, job_id=job_id, decision=decision, input_digest=input_digest)
        return decision

    def record(self, *, owner_id: str, job_id: str, decision: LearningDecision, input_digest: str,
               connection: Any = None) -> dict[str, Any]:
        """Persist the decision. Idempotent per (owner, job); content must not drift."""
        row = {
            "id": "learning_decision_" + _digest([owner_id, job_id]),
            "job_id": job_id, "owner_id": owner_id,
            "learn": int(decision.learn), "target": decision.target, "subtype": decision.subtype,
            "confidence": float(decision.confidence), "support": float(decision.support),
            "importance": float(decision.importance),
            "risk": decision.risk, "reason_codes_json": _encode(list(decision.reason_codes)),
            "input_digest": input_digest, "decision_digest": _digest(decision.to_dict()),
            "model_identity": decision.model_identity or "unknown", "created_at": _now(),
        }
        if connection is not None:
            return self._insert(connection, row)
        with self.db.transaction() as owned:
            return self._insert(owned, row)

    @classmethod
    def _insert(cls, connection: Any, row: Mapping[str, Any]) -> dict[str, Any]:
        columns = cls.DECISION_COLUMNS
        connection.execute(
            f"INSERT INTO learning_decisions({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)}) "
            "ON CONFLICT(owner_id,job_id) DO NOTHING",
            tuple(row[name] for name in columns),
        )
        stored = connection.execute("SELECT * FROM learning_decisions WHERE owner_id=? AND job_id=?",
                                    (row["owner_id"], row["job_id"])).fetchone()
        if stored is None:
            raise DecisionSchemaError("decision audit row vanished")
        record = dict(stored)
        if record["decision_digest"] != row["decision_digest"] or record["input_digest"] != row["input_digest"]:
            raise DecisionSchemaError("a different decision is already recorded for this job")
        return record

    def get(self, owner_id: str, job_id: str) -> dict[str, Any] | None:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM learning_decisions WHERE owner_id=? AND job_id=?",
                                     (owner_id, job_id)).fetchone()
        return dict(row) if row else None

    def history(self, owner_id: str = "local-user") -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute("SELECT * FROM learning_decisions WHERE owner_id=? ORDER BY created_at,id",
                                      (owner_id,)).fetchall()
        return [{**dict(row), "reason_codes": json.loads(row["reason_codes_json"])} for row in rows]


def decision_from_experience_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a stored Experience row into the summary JEV is allowed to see."""
    def _load(value: Any, default: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return default
        return value if value is not None else default

    evidence = _load(row.get("evidence_json"), {})
    # `build_state` takes the tool trace as its own input, and the pipeline reads
    # it back off this projection. Hoisting it here keeps "what JEV is allowed to
    # see" in one place instead of letting the pipeline guess at `evidence`.
    trace = evidence.get("tool_trace") if isinstance(evidence, Mapping) else None
    return {
        "id": row.get("id"),
        "task_type": row.get("task_type"),
        "outcome": row.get("outcome"),
        "signal_type": row.get("signal_type"),
        "severity": row.get("severity"),
        "failure_tags": _load(row.get("failure_tags_json"), []),
        "tool_trace": trace if isinstance(trace, list) else [],
        "root_task_id": row.get("root_task_id"),
        "runtime_bundle_id": row.get("runtime_bundle_id"),
        "observed_at": row.get("observed_at"),
        "provenance": row.get("provenance"),
        "evidence": evidence,
        "source_content_hash": row.get("source_content_hash"),
    }


__all__ = [
    "DECISION_PURPOSE", "DECISION_ROLE", "DEFAULT_MODEL", "DISAGREEMENT_REASON", "DecisionSchemaError",
    "JevDecisionService", "JevUnavailable", "LearningDecision", "PROBE_TARGETS", "REASON_PROBES",
    "TYPESAFE_ENDPOINT", "TypesafeClient", "build_questions", "build_state", "decision_from_experience_row",
]
