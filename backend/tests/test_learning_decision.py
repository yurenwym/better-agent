"""JEV decision layer: closed contract, fail-closed parsing, zero side effects."""
import json

import pytest

from app.learning_contract import TARGET_BEHAVIOR, TARGET_IGNORE, TARGET_MEMORY, TARGET_SKILL
from app.learning_decision import (
    DecisionSchemaError,
    JevDecisionService,
    JevUnavailable,
    LearningDecision,
    REASON_PROBES,
    build_questions,
    build_state,
)
from app.startup import build_runtime


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch):
    for name in ("DATABASE_URL", "AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL", "LLM_AP_PATH"):
        monkeypatch.delenv(name, raising=False)


def answers(target="SKILL", *, learn=0.9, confidence=0.9, risk="low", importance=7.0, **probes):
    payload = {
        "learn": {"noul": learn},
        "target": {"choice": target, "probabilities": {target: confidence}, "confidence": confidence},
        "risk": {"choice": risk},
        "importance": {"score": importance, "probabilities": {}, "confidence": 0.9},
    }
    for name, _ in REASON_PROBES:
        payload[name] = {"noul": probes.get(name, 0.1)}
    return payload


class FakeClient:
    """Records the exact state it was given so tests can assert what JEV may see."""

    def __init__(self, payload, model="jev-test"):
        self.payload, self.model = payload, model
        self.calls = []

    def ask(self, state, questions):
        self.calls.append({"state": state, "questions": questions})
        return {"answers": self.payload, "model": self.model}


def experience(**overrides):
    base = {"id": "experience_1", "task_type": "incident_diagnosis", "outcome": "failure",
            "failure_tags": ["database_timeout"], "root_task_id": "run_1", "observed_at": "2026-09-21T00:00:00+00:00",
            "provenance": "production"}
    base.update(overrides)
    return base


def service(runtime, payload, **kwargs):
    client = FakeClient(payload)
    return JevDecisionService(runtime.db, client, **kwargs), client


@pytest.mark.parametrize("target", [TARGET_MEMORY, TARGET_SKILL, TARGET_BEHAVIOR, TARGET_IGNORE])
def test_targets_are_routed_verbatim(tmp_path, target):
    runtime = build_runtime(tmp_path)
    decider, _ = service(runtime, answers(target))
    decision = decider.decide(owner_id="local-user", job_id="job_1", experience=experience())
    assert decision.target == target
    assert decision.learn is (target != TARGET_IGNORE)
    if target == TARGET_BEHAVIOR:
        # JEV names the top-level target; the Behavior adapter refines the surface later.
        assert decision.subtype == ""


def test_behavior_choice_needs_no_subtype_at_decision_time():
    decision = LearningDecision.from_answers(answers(TARGET_BEHAVIOR, decision_behavior_defect=0.9))
    assert decision.target == TARGET_BEHAVIOR and decision.subtype == "" and decision.learn is True


@pytest.mark.parametrize("payload,label", [
    ({"target": {"choice": "MEMORY", "confidence": 0.9}, "risk": {"choice": "low"}, "importance": {"score": 5},
      "learn": {}}, "learn answer is not noul"),
    ({}, "empty answers"),
])
def test_missing_fields_fail_closed(payload, label):
    decision = LearningDecision.ignored_default(reason="schema_invalid")
    assert decision.ignored and decision.target == TARGET_IGNORE
    with pytest.raises(DecisionSchemaError):
        LearningDecision.from_answers(payload)


@pytest.mark.parametrize("mutate", [
    lambda payload: payload.pop("learn"),
    lambda payload: payload.pop("target"),
    lambda payload: payload.pop("risk"),
    lambda payload: payload.pop("importance"),
    lambda payload: payload.pop(REASON_PROBES[0][0]),
    lambda payload: payload.update(target={"choice": "PROMPT", "confidence": 0.9}),
    lambda payload: payload.update(target={"choice": "MEMORY", "confidence": float("nan")}),
    lambda payload: payload.update(target={"choice": "MEMORY", "confidence": 1.5}),
    lambda payload: payload.update(target={"choice": "MEMORY", "confidence": 0.9, "probabilities": {"MEMORY": "high"}}),
    lambda payload: payload.update(risk={"choice": "extreme"}),
    lambda payload: payload.update(risk={}),
    lambda payload: payload.update(importance={"score": 10.0}),
    lambda payload: payload.update(importance={"score": float("inf")}),
    lambda payload: payload.update(learn={"noul": -0.5}),
])
def test_every_malformed_field_is_rejected(mutate):
    payload = answers()
    mutate(payload)
    with pytest.raises(DecisionSchemaError):
        LearningDecision.from_answers(payload)


def test_service_records_ignore_instead_of_raising_on_a_broken_response(tmp_path):
    runtime = build_runtime(tmp_path)
    payload = answers()
    payload["target"] = {"choice": "SOMETHING_NEW", "confidence": 0.9}
    decider, _ = service(runtime, payload)
    decision = decider.decide(owner_id="local-user", job_id="job_bad", experience=experience())
    assert decision.ignored and decision.learn is False
    assert decision.reason_codes == ("schema_invalid",)
    stored = decider.get("local-user", "job_bad")
    assert stored["target"] == TARGET_IGNORE and stored["learn"] == 0


def test_low_target_support_is_not_a_licence_to_learn(tmp_path):
    runtime = build_runtime(tmp_path)
    decider, _ = service(runtime, answers("SKILL", learn=0.9, confidence=0.2))
    decision = decider.decide(owner_id="local-user", job_id="job_unsure", experience=experience())
    assert decision.ignored
    assert "low_target_support" in decision.reason_codes
    assert decision.support == 0.2


def test_the_gate_reads_the_distribution_not_the_self_report(tmp_path):
    """JEV's self-reported confidence was measured to suppress correct calls.

    The routing gate reads the mass behind the chosen target; the self-report is
    kept for the audit only.
    """
    runtime = build_runtime(tmp_path)
    payload = answers("SKILL", learn=0.9, confidence=0.2)
    payload["target"] = {"choice": "SKILL", "confidence": 0.2, "probabilities": {"SKILL": 0.9}}
    decider, _ = service(runtime, payload)
    decision = decider.decide(owner_id="local-user", job_id="job_distribution", experience=experience())
    assert decision.target == TARGET_SKILL and decision.learn is True
    assert decision.support == 0.9 and decision.confidence == 0.2
    assert "low_target_support" not in decision.reason_codes


def test_a_chosen_target_with_no_distribution_mass_fails_closed(tmp_path):
    runtime = build_runtime(tmp_path)
    payload = answers("SKILL", learn=0.9, confidence=0.9)
    payload["target"] = {"choice": "MEMORY", "confidence": 0.9, "probabilities": {"SKILL": 0.9}}
    decider, _ = service(runtime, payload)
    decision = decider.decide(owner_id="local-user", job_id="job_massless", experience=experience())
    assert decision.ignored and decision.support == 0.0
    assert "low_target_support" in decision.reason_codes


def test_a_probe_that_argues_for_another_target_is_recorded(tmp_path):
    runtime = build_runtime(tmp_path)
    decider, _ = service(runtime, answers("SKILL", decision_behavior_defect=0.9))
    decision = decider.decide(owner_id="local-user", job_id="job_disagree", experience=experience())
    assert decision.target == TARGET_SKILL
    assert "probe_target_disagreement" in decision.reason_codes


def test_a_probe_that_agrees_is_not_recorded_as_a_disagreement(tmp_path):
    runtime = build_runtime(tmp_path)
    decider, _ = service(runtime, answers("SKILL", repeatable_procedure=0.9))
    decision = decider.decide(owner_id="local-user", job_id="job_agree", experience=experience())
    assert "probe_target_disagreement" not in decision.reason_codes


def test_the_gate_input_survives_the_database_round_trip(tmp_path):
    """The audit has to carry the number the gate read, not only the self-report.

    A row that stored `confidence=0.2` beside `target=SKILL` would read like a
    bypassed threshold; `support` is what makes it legible.
    """
    runtime = build_runtime(tmp_path)
    payload = answers("SKILL", learn=0.9, confidence=0.2)
    payload["target"] = {"choice": "SKILL", "confidence": 0.2, "probabilities": {"SKILL": 0.9}}
    decider, _ = service(runtime, payload)
    decider.decide(owner_id="local-user", job_id="job_support_roundtrip", experience=experience())
    stored = decider.get("local-user", "job_support_roundtrip")
    assert stored["support"] == 0.9
    assert stored["confidence"] == 0.2
    assert stored["target"] == TARGET_SKILL
    assert decider.history("local-user")[-1]["support"] == 0.9


def test_the_target_question_states_its_precedence():
    """The ordering and the tie-break are the contract, not prompt decoration.

    Without them a lesson that is both a reusable procedure and a general defect
    is genuinely ambiguous, and JEV was measured to flip between SKILL and IGNORE
    on the same input.
    """
    instructions = build_questions()["target"]["instructions"]
    assert "(1) SKILL" in instructions and "(4) IGNORE" in instructions
    assert "is a SKILL" in instructions
    policy = build_state({})["classification_policy"]
    assert policy["precedence"] == ["SKILL", "MEMORY", "BEHAVIOR", "IGNORE"]
    assert "SKILL" in policy["tie_break"]


def test_reason_codes_come_from_the_dedicated_probes(tmp_path):
    runtime = build_runtime(tmp_path)
    decider, _ = service(runtime, answers("MEMORY", explicit_user_constraint=0.95, evidence_sufficient=0.8,
                                          transient_operational=0.05))
    decision = decider.decide(owner_id="local-user", job_id="job_reasons", experience=experience())
    assert set(decision.reason_codes) == {"explicit_user_constraint", "evidence_sufficient"}


def test_decision_audit_is_append_only(tmp_path):
    runtime = build_runtime(tmp_path)
    decider, _ = service(runtime, answers("SKILL"))
    decider.decide(owner_id="local-user", job_id="job_audit", experience=experience())
    with pytest.raises(Exception, match="append-only"):
        with runtime.db.transaction() as connection:
            connection.execute("UPDATE learning_decisions SET target='BEHAVIOR' WHERE job_id='job_audit'")
    with pytest.raises(Exception, match="append-only"):
        with runtime.db.transaction() as connection:
            connection.execute("DELETE FROM learning_decisions WHERE job_id='job_audit'")


def test_decision_is_idempotent_and_immutable_per_job(tmp_path):
    runtime = build_runtime(tmp_path)
    decider, _ = service(runtime, answers("SKILL"))
    first = decider.decide(owner_id="local-user", job_id="job_same", experience=experience())
    second = decider.decide(owner_id="local-user", job_id="job_same", experience=experience())
    assert first.to_dict() == second.to_dict()
    assert len(decider.history("local-user")) == 1
    other, _ = service(runtime, answers("MEMORY"))
    with pytest.raises(DecisionSchemaError, match="already recorded"):
        other.decide(owner_id="local-user", job_id="job_same", experience=experience())


def test_decision_has_no_side_effects(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["memory", "skill"])

    def snapshot():
        with runtime.db.connection() as connection:
            tables = {}
            for table in ("memory_entries", "memory_revisions", "memory_proposals", "skills", "skill_versions",
                          "skill_grants", "skill_bindings", "runtime_channels", "runtime_bundles",
                          "evolution_candidates", "evolution_decisions", "approvals", "canary_deployments", "learning_jobs"):
                tables[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return tables

    before = snapshot()
    decider, _ = service(runtime, answers(TARGET_MEMORY, explicit_user_constraint=0.95, confidence=0.95))
    decision = decider.decide(owner_id="local-user", job_id="job_side_effect", experience=experience())
    assert not decision.ignored
    assert snapshot() == before
    assert runtime.memory_store.list_entries() == []


def test_state_excludes_evaluator_material():
    state = build_state(experience(), task_result={"status": "FAILED"},
                        failure_tags=["database_timeout"], tool_trace=[{"tool": "sql_probe"}],
                        user_feedback="please remember this", assets_summary={"memory": 3, "skill": 1})
    blob = json.dumps(state, ensure_ascii=False).lower()
    for forbidden in ("holdout", "safety_eval", "judge_feedback", "judge_result", "rubric"):
        assert forbidden not in blob
    assert state["experience"]["task_type"] == "incident_diagnosis"
    assert state["failure_tags"] == ["database_timeout"]


def test_questions_cover_the_closed_target_set():
    questions = build_questions()
    assert set(questions) == {"learn", "target", "risk", "importance", *(name for name, _ in REASON_PROBES)}
    assert set(questions["target"]["criteria"]) == {"MEMORY", "SKILL", "BEHAVIOR", "IGNORE"}
    assert set(questions["risk"]["criteria"]) == {"low", "medium", "high"}
    assert len(questions["importance"]["criteria"]) == 10


def test_state_is_bounded():
    state = build_state(experience(), tool_trace=[{"tool": "t" * 5000} for _ in range(100)])
    assert len(state["tool_trace"]) == 12
    assert len(state["tool_trace"][0]["tool"]) == 1200


def test_missing_client_is_not_a_decision(tmp_path):
    runtime = build_runtime(tmp_path)
    decider = JevDecisionService(runtime.db, None)
    with pytest.raises(JevUnavailable):
        decider.decide(owner_id="local-user", job_id="job_none", experience=experience())
    assert decider.history("local-user") == []
