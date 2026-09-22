"""The V3 pipeline end to end: shadow mode, idempotency, leases, revocation.

These tests drive the real `LearningService` (jobs, leases, dispatch) and the
real target adapters, and stub only the two remote seams: the JEV decision
service and the Learning LLM. That is the whole point of the split — the harness
is testable without a network.
"""
import json
from types import SimpleNamespace

import pytest

from app.learning_contract import TARGET_IGNORE, TARGET_MEMORY
from app.learning_decision import LearningDecision
from app.learning_pipeline import (
    MODE_ACTIVE,
    MODE_SHADOW,
    STATUS_APPLIED,
    STATUS_NO_CHANGE,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    LearningPipeline,
    build_pipeline,
)
from app.learning_targets import build_adapters
from app.startup import build_runtime

from tests.test_learning_targets import memory_draft, runtime_with_message


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch):
    for name in ("DATABASE_URL", "AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL", "LLM_AP_PATH"):
        monkeypatch.delenv(name, raising=False)


class StubDecisions:
    """The JEV seam. Replaces the network client but keeps the real audit write."""

    def __init__(self, db, decision: LearningDecision | None = None, *, error: Exception | None = None) -> None:
        from app.learning_decision import JevDecisionService

        self.service = JevDecisionService(db=db, client=None)
        self.decision = decision or LearningDecision(learn=True, target=TARGET_MEMORY, subtype="",
                                                     confidence=1.0, importance=0.8, risk="low",
                                                     reason_codes=("explicit_user_constraint",),
                                                     model_identity="stub:jev")
        self.error = error
        self.calls = 0

    def decide(self, *, owner_id, job_id, experience, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        from app.learning_decision import _digest, build_state

        state = build_state(experience, task_result=kwargs.get("task_result") or {},
                            failure_tags=kwargs.get("failure_tags") or [],
                            tool_trace=kwargs.get("tool_trace") or [],
                            user_feedback=kwargs.get("user_feedback"),
                            assets_summary=kwargs.get("assets_summary"))
        self.service.record(owner_id=owner_id, job_id=job_id, decision=self.decision,
                            input_digest=_digest(state))
        return self.decision

    def get(self, owner_id, job_id):
        return self.service.get(owner_id, job_id)


def decisions_for(runtime, decision=None, *, error=None):
    return StubDecisions(runtime.db, decision, error=error)


class StubAgent:
    """The Learning LLM seam. Produces one draft per call, or raises."""

    def __init__(self, draft_factory, *, error: Exception | None = None) -> None:
        self.draft_factory = draft_factory
        self.error = error
        self.calls = 0

    def generate(self, *, decision, experience, evidence_ids=(), source_refs=(), **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.draft_factory(evidence_ids, source_refs)


def prepare(tmp_path, *, content="以后生产数据库不能由 Agent 自动重启，必须人工批准。"):
    """A runtime with learning unpaused, plus the message the job will point at."""
    runtime, message_id = runtime_with_message(tmp_path, content)
    runtime.learning.configure("local-user", expected_version=0, paused=False,
                               allowed_assets=["memory", "skill", "prompt"])
    return runtime, message_id


def enqueue(runtime, message_id, content="以后生产数据库不能由 Agent 自动重启，必须人工批准。", key=None):
    from app.learning import digest

    return runtime.learning.enqueue("local-user", "thread_message", message_id,
                                    key or digest(content), message_id)


def stub_budget(monkeypatch, runtime, *, root="offline-budget", allowed=True):
    monkeypatch.setattr(runtime.learning, "dispatch", lambda job: root)
    monkeypatch.setattr(runtime.learning, "assert_learning_call_allowed", lambda owner, root_id: allowed)


def pipeline_for(runtime, decisions, agent, *, mode=MODE_SHADOW, **kwargs):
    return build_pipeline(runtime, mode=mode, decisions=decisions, agent=agent, **kwargs)


def asset_snapshot(runtime):
    with runtime.db.connection() as connection:
        return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("memory_entries", "memory_revisions", "skill_versions", "skills",
                              "evolution_candidates", "runtime_channels", "canary_deployments")}


# --------------------------------------------------------------- shadow mode

def test_shadow_cycle_decides_and_evaluates_without_touching_assets(tmp_path, monkeypatch):
    runtime, message_id = prepare(tmp_path)
    job_id = enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    decisions = decisions_for(runtime)
    agent = StubAgent(lambda evidence, refs: memory_draft(message_id))
    before = asset_snapshot(runtime)

    result = pipeline_for(runtime, decisions, agent).run_once()

    assert result is not None and result.job_id == job_id
    assert result.status == STATUS_NO_CHANGE
    assert result.outcome == "SHADOW"
    assert result.promotion["outcome"] == "SHADOW"
    assert result.promotion["detail"]["would_be"] == "PROMOTED"
    assert result.target == TARGET_MEMORY
    # A proposal exists, an entry does not: the gate never applied anything.
    proposals = runtime.memory_store.list_proposals()
    assert len(proposals) == 1 and proposals[0].status == "PENDING"
    assert runtime.memory_store.list_entries() == []
    assert asset_snapshot(runtime)["memory_entries"] == before["memory_entries"]
    assert decisions.calls == 1 and agent.calls == 1


def test_shadow_cycle_records_the_v3_observability_fields(tmp_path, monkeypatch):
    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    result = pipeline_for(runtime, decisions_for(runtime),
                          StubAgent(lambda evidence, refs: memory_draft(message_id))).run_once()

    recorded = result.observability
    for field in ("learning_job_id", "decision_id", "jev_model", "target", "candidate_id",
                  "judge_model", "promotion_id", "runtime_bundle_before", "runtime_bundle_after",
                  "cost_microusd", "latency_seconds", "outcome", "mode"):
        assert field in recorded, field
    assert recorded["jev_model"] == "stub:jev"
    assert recorded["mode"] == MODE_SHADOW
    assert recorded["outcome"] == "SHADOW"


def test_an_ignored_decision_creates_no_candidate(tmp_path, monkeypatch):
    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    decisions = decisions_for(runtime, LearningDecision.ignored_default(reason="transient_operational"))
    agent = StubAgent(lambda evidence, refs: memory_draft(message_id))

    result = pipeline_for(runtime, decisions, agent).run_once()

    assert result.outcome == "IGNORE"
    assert result.status == STATUS_NO_CHANGE
    assert agent.calls == 0
    assert runtime.memory_store.list_proposals() == []


# ------------------------------------------------------------- budget (§51)

def test_an_exhausted_learning_budget_stops_before_any_remote_call(tmp_path, monkeypatch):
    from app.learning import LearningConflict

    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    monkeypatch.setattr(runtime.learning, "dispatch",
                        lambda job: (_ for _ in ()).throw(LearningConflict("learning aggregate budget exhausted")))
    decisions = decisions_for(runtime)
    agent = StubAgent(lambda evidence, refs: memory_draft(message_id))

    result = pipeline_for(runtime, decisions, agent).run_once()

    assert result.status == STATUS_REJECTED
    assert "budget" in result.reason.lower()
    assert decisions.calls == 0 and agent.calls == 0
    assert runtime.memory_store.list_proposals() == []


def test_a_disallowed_call_is_blocked_without_a_decision(tmp_path, monkeypatch):
    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime, allowed=False)
    decisions = decisions_for(runtime)

    result = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id))).run_once()

    assert result.outcome == "BUDGET_BLOCKED"
    assert result.status == STATUS_NO_CHANGE
    assert decisions.calls == 0


# -------------------------------------------------------- crash recovery §48

def test_a_crash_after_dispatch_is_unknown_and_never_replayed(tmp_path, monkeypatch):
    runtime, message_id = prepare(tmp_path)
    job_id = enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    decisions = decisions_for(runtime, error=RuntimeError("jev connection reset"))

    result = pipeline_for(runtime, decisions, StubAgent(lambda e, r: memory_draft(message_id))).run_once()

    assert result.status == STATUS_UNKNOWN
    assert "RuntimeError" in result.reason
    with runtime.db.connection() as connection:
        row = connection.execute("SELECT status FROM learning_jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == STATUS_UNKNOWN
    # A dispatched-but-uncommitted request is never replayed: the job is not
    # QUEUED again, so the next cycle finds nothing to do.
    assert pipeline_for(runtime, decisions, StubAgent(lambda e, r: memory_draft(message_id))).run_once() is None


def test_a_crash_before_dispatch_is_rejected_not_unknown(tmp_path, monkeypatch):
    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    monkeypatch.setattr(runtime.learning, "dispatch",
                        lambda job: (_ for _ in ()).throw(RuntimeError("no budget")))
    result = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id))).run_once()
    # Nothing was dispatched, so this is an ordinary refusal, not an UNKNOWN.
    assert result.status == STATUS_REJECTED
    assert result.observability["decision_id"] == ""


def test_a_revoked_source_is_refused_before_dispatch(tmp_path, monkeypatch):
    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    monkeypatch.setattr(runtime.learning, "dispatch",
                        lambda job: (_ for _ in ()).throw(AssertionError("dispatch must not run")))
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE thread_messages SET content='changed' WHERE id=?", (message_id,))

    result = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id))).run_once()

    assert result.status == STATUS_REJECTED
    assert "changed" in result.reason


# --------------------------------------------------------- idempotency §49

def test_ten_submissions_of_one_experience_produce_one_job(tmp_path, monkeypatch):
    runtime, message_id = prepare(tmp_path)
    for _ in range(10):
        enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    pipeline = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id)))

    results = pipeline.drain()

    with runtime.db.connection() as connection:
        jobs = connection.execute("SELECT COUNT(*) FROM learning_jobs").fetchone()[0]
        decisions = connection.execute("SELECT COUNT(*) FROM learning_decisions").fetchone()[0]
    assert jobs == 1
    assert len(results) == 1
    assert decisions == 1
    assert len(runtime.memory_store.list_proposals()) == 1


def test_running_the_same_job_twice_is_refused_by_the_lease(tmp_path, monkeypatch):
    from app.learning import LearningConflict

    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    pipeline = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id)))
    job = runtime.learning.claim("local-user", lease_seconds=60)

    pipeline.run_job(job)

    with pytest.raises(LearningConflict, match="lease lost"):
        pipeline.run_job(job)


# ---------------------------------------------------------------- lease §50

def test_an_expired_lease_is_taken_over_and_the_old_worker_cannot_commit(tmp_path, monkeypatch):
    from app.learning import LearningConflict

    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    first = runtime.learning.claim("local-user", lease_seconds=60)
    assert first is not None
    assert runtime.learning.claim("local-user", lease_seconds=60) is None  # held

    with runtime.db.transaction() as connection:
        connection.execute("UPDATE learning_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
                           (first["id"],))
    second = runtime.learning.claim("local-user", lease_seconds=60)
    assert second is not None and second["lease_token"] != first["lease_token"]

    pipeline = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id)))
    with pytest.raises(LearningConflict, match="lease lost"):
        pipeline.run_job(first)


# ------------------------------------------------------ source revocation §47

def test_a_deleted_experience_cannot_produce_a_candidate(tmp_path, monkeypatch):
    runtime = build_runtime(tmp_path)
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["memory", "skill"])
    bundle = runtime.behavior.active("stable")
    experience = runtime.evolution.record_experience(
        task_type="incident_diagnosis", outcome="failure", lineage_group_hash="lineage-revoked",
        root_task_id="root-revoked", source_content_hash="source-revoked", runtime_bundle_id=bundle.id,
        dataset_partition="DISCOVERY", source_kind="manual", source_id="job-revoked",
        source_event_id="event-revoked", signal_type="run_failed", severity="error",
        provenance="production", idempotency_key="v3-revoked-1",
    )
    job_id = runtime.learning.enqueue("local-user", "experience", experience["id"],
                                      experience["source_content_hash"], experience["id"])
    monkeypatch.setattr(runtime.learning, "dispatch",
                        lambda job: (_ for _ in ()).throw(AssertionError("dispatch must not run")))
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE evolution_experiences SET source_state='DELETED' WHERE id=?", (experience["id"],))

    result = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: None)).run_once()

    assert result.job_id == job_id
    assert result.status == STATUS_REJECTED
    assert "revoked" in result.reason
    assert runtime.memory_store.list_proposals() == []


def test_recurring_experiences_are_loaded_as_evidence(tmp_path, monkeypatch):
    """JEV must see the recurrence, and the adapter needs three independent sources."""
    runtime = build_runtime(tmp_path)
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["memory", "skill"])
    bundle = runtime.behavior.active("stable")
    experiences = [
        runtime.evolution.record_experience(
            task_type="incident_diagnosis", outcome="failure", lineage_group_hash=f"lineage-{index}",
            root_task_id=f"root-{index}", source_content_hash=f"source-{index}", runtime_bundle_id=bundle.id,
            dataset_partition="DISCOVERY", source_kind="manual", source_id=f"job-{index}",
            source_event_id=f"event-{index}", signal_type="run_failed", severity="error",
            provenance="production", idempotency_key=f"v3-recurrence-{index}",
        ) for index in range(3)
    ]
    runtime.learning.enqueue("local-user", "experience", experiences[0]["id"],
                             experiences[0]["source_content_hash"], experiences[0]["id"])
    stub_budget(monkeypatch, runtime)
    seen = {}

    class RecordingAgent(StubAgent):
        def generate(self, *, decision, experience, evidence_ids=(), source_refs=(), **kwargs):
            seen["evidence_ids"] = list(evidence_ids)
            seen["recurrence"] = experience.get("recurrence")
            return super().generate(decision=decision, experience=experience,
                                    evidence_ids=evidence_ids, source_refs=source_refs, **kwargs)

    pipeline_for(runtime, decisions_for(runtime),
                 RecordingAgent(lambda e, r: memory_draft(experiences[0]["id"]))).run_once()

    assert seen["recurrence"]["count"] == 3
    assert len(seen["evidence_ids"]) == 3


# -------------------------------------------------------------- active mode

def test_active_mode_applies_a_memory_candidate(tmp_path, monkeypatch):
    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    pipeline = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id)),
                            mode=MODE_ACTIVE)

    result = pipeline.run_once()

    assert result.status == STATUS_APPLIED
    assert result.outcome == "PROMOTED"
    assert len(runtime.memory_store.list_entries()) == 1


def test_shadow_mode_is_the_default_and_can_be_changed(tmp_path):
    runtime, message_id = prepare(tmp_path)
    pipeline = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id)))
    assert pipeline.mode == MODE_SHADOW and pipeline.shadow is True
    assert pipeline.set_mode(MODE_ACTIVE) == MODE_ACTIVE and pipeline.shadow is False
    with pytest.raises(ValueError, match="unknown learning pipeline mode"):
        pipeline.set_mode("LIVE")


def test_canaries_are_not_advanced_in_shadow_mode(tmp_path):
    runtime, message_id = prepare(tmp_path)
    pipeline = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id)))
    assert pipeline.advance_canaries("local-user") == []


# --------------------------------------------------------------- dashboard

def test_dashboard_counters_reflect_the_recorded_cycles(tmp_path, monkeypatch):
    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    pipeline = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id)))
    pipeline.run_once()

    metrics = pipeline.metrics("local-user")
    assert metrics["decision_count"] == 1
    assert metrics["learn_rate"] == 1.0
    assert metrics["ignore_rate"] == 0.0
    assert metrics["memory_candidate_rate"] == 1.0
    assert metrics["skill_candidate_rate"] == 0.0
    assert metrics["mode"] == MODE_SHADOW


def test_build_pipeline_registers_exactly_the_three_target_adapters(tmp_path):
    runtime, message_id = prepare(tmp_path)
    pipeline = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id)))
    assert set(pipeline.adapters) == {"MEMORY", "SKILL", "BEHAVIOR"}
    assert isinstance(pipeline.gate, object)
    assert pipeline.gate.db is runtime.db


def test_the_pipeline_holds_no_decision_or_judge_of_its_own(tmp_path):
    """The pipeline sequences; it must not be able to invent a verdict."""
    runtime, message_id = prepare(tmp_path)
    pipeline = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id)))
    assert not hasattr(pipeline, "client")
    assert not hasattr(pipeline, "gateway")
    assert pipeline.judge is None


def test_an_out_of_contract_target_cannot_be_recorded_at_all(tmp_path, monkeypatch):
    """The storage layer is the last line: an unknown target never becomes a candidate."""
    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    agent = StubAgent(lambda e, r: memory_draft(message_id))
    decisions = decisions_for(runtime, LearningDecision(learn=True, target="EVOLUTION", subtype="", confidence=1.0,
                                                       importance=0.5, risk="low", reason_codes=()))

    result = pipeline_for(runtime, decisions, agent).run_once()

    assert result.status == STATUS_UNKNOWN
    assert "IntegrityError" in result.reason
    assert agent.calls == 0
    assert runtime.memory_store.list_proposals() == []
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM learning_decisions").fetchone()[0] == 0


def test_ignore_target_never_reaches_an_adapter(tmp_path, monkeypatch):
    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    agent = StubAgent(lambda e, r: memory_draft(message_id))
    decisions = decisions_for(runtime, LearningDecision(learn=False, target=TARGET_IGNORE, subtype="", confidence=0.1,
                                                      importance=0.1, risk="low", reason_codes=("transient",)))
    result = pipeline_for(runtime, decisions, agent).run_once()
    assert result.outcome == "IGNORE" and agent.calls == 0


# ------------------------------------------- the real generator on a message
#
# The tests above stub the Learning LLM, which means the draft they feed back is
# already well-formed. These two use the *real* `LearningAgent` with a stub
# gateway, so the evidence-binding and scope rules in `parse_draft` actually run.
# A stubbed generator is how a message-sourced candidate shipped with an empty
# evidence binding and died with `DraftError` on every non-IGNORE decision.

class StubGateway:
    """Stands in for the routed model gateway; records what the agent was given."""

    profile = SimpleNamespace(provider_name="deepseek", model="deepseek-flash")

    def __init__(self, reply):
        self.reply = json.dumps(reply, ensure_ascii=False)
        self.requests = []

    async def complete(self, request, context=None, **kwargs):
        self.requests.append(request)
        return SimpleNamespace(message=self.reply)

    def payloads(self):
        return [json.loads(message["content"]) for request in self.requests
                for message in request.messages if message["role"] == "user"]


def memory_reply(message_id, **change_over):
    change = {"operation": "ADD", "kind": "constraint", "scope_type": "user", "scope_id": "",
              "content": "生产数据库不能由 Agent 自动重启，必须人工批准。"}
    change.update(change_over)
    return {"target": "MEMORY", "problem": "用户显式要求生产库不能自动重启",
            "root_cause": "该约束此前没有被持久化", "generalizable_lesson": "生产库重启必须人工批准",
            "proposed_change": change, "expected_effect": {"future_restarts": "require approval"},
            "risks": ["约束过宽会阻碍正常运维"], "evidence_refs": [message_id], "experience_ids": []}


def test_the_real_generator_turns_a_message_into_a_candidate(tmp_path, monkeypatch):
    from app.learning_agent import LearningAgent

    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    gateway = StubGateway(memory_reply(message_id))
    agent = LearningAgent(gateway)

    result = pipeline_for(runtime, decisions_for(runtime), agent, mode=MODE_ACTIVE).run_once()

    assert result.candidate_id, result.reason
    assert result.outcome == "PROMOTED", result.reason
    # The message is the only evidence, and the draft cited it.
    assert result.decision["target"] == TARGET_MEMORY
    payload = gateway.payloads()[0]
    assert payload["available_source_refs"] == [message_id]
    assert payload["available_evidence_ids"] == []
    assert payload["safety_constraints"] == {"memory_scopes": []}
    assert payload["current_asset_state"]["memory_entries"] == 0


def test_the_real_generator_cannot_name_a_project_scope_it_was_not_given(tmp_path, monkeypatch):
    """Fail closed: an unresolvable project scope is a rejected draft, not a guess."""
    from app.learning_agent import LearningAgent

    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    gateway = StubGateway(memory_reply(message_id, scope_type="project", scope_id="project-a"))
    agent = LearningAgent(gateway)

    result = pipeline_for(runtime, decisions_for(runtime), agent, mode=MODE_ACTIVE).run_once()

    # The request was already dispatched when the draft was refused, so the job
    # is UNKNOWN: it must not be replayed (V3 §48).
    assert result.status == STATUS_UNKNOWN
    assert "was not offered to the generator" in result.reason
    assert runtime.memory_store.list_entries() == []
    assert runtime.memory_store.list_proposals() == []


# ------------------------------------------------- the cut-over (V3 §63/§65)

def test_run_once_routes_to_the_pipeline_when_one_is_attached(tmp_path, monkeypatch):
    """`learning.py` schedules; the pipeline decides what is learned."""
    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)
    pipeline = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id)),
                            mode=MODE_ACTIVE)
    runtime.learning.pipeline = pipeline

    assert runtime.learning.run_once() is True
    job = runtime.learning.history()[0]
    assert job["status"] == STATUS_APPLIED
    assert json.loads(job["change_set_json"])[0]["target"] == "MEMORY"
    assert json.loads(job["change_set_json"])[0]["outcome"] == "PROMOTED"


def test_a_pipeline_refuses_a_legacy_only_source_kind(tmp_path, monkeypatch):
    """A job the pipeline cannot read is refused, not misread as a message."""
    runtime, message_id = prepare(tmp_path)
    job_id = runtime.learning.enqueue("local-user", "goal_review", "review-1", "hash", "review-1")
    stub_budget(monkeypatch, runtime)
    pipeline = pipeline_for(runtime, decisions_for(runtime), StubAgent(lambda e, r: memory_draft(message_id)))

    result = pipeline.run_job(runtime.learning.claim())

    # The source is read before anything is dispatched, so this costs no call.
    assert result.status == STATUS_REJECTED
    assert "cannot read source_kind 'goal_review'" in result.reason
    assert job_id  # the job existed; the refusal is about its source, not its identity


def test_collect_stops_enqueueing_legacy_sources_once_a_pipeline_is_attached(tmp_path, monkeypatch):
    runtime, message_id = prepare(tmp_path)
    from app.learning_workflow import METHOD
    from tests.test_learning_v2 import completed_program

    completed_program(runtime, "english", method=True)
    completed_program(runtime, "math", method=True)
    before = {job["source_kind"] for job in runtime.learning.history()}
    runtime.learning.collect()
    assert "method_feedback" in {job["source_kind"] for job in runtime.learning.history()}

    runtime.learning.pipeline = pipeline_for(runtime, decisions_for(runtime),
                                             StubAgent(lambda e, r: memory_draft(message_id)))
    runtime.learning.collect()
    after = {job["source_kind"] for job in runtime.learning.history()}
    assert after == before | {"method_feedback"}  # no new legacy kinds appeared
    assert METHOD  # the method learner is what the legacy scan looks for


# ------------------------------------------- what shadow does and does not do

def test_shadow_stages_a_candidate_without_enabling_it(tmp_path, monkeypatch):
    """V3 §41 runs the chain to Candidate and withholds only the promotion.

    A staged skill version is the chain working; an ENABLED one would be a
    production change. The first version of the acceptance gate counted the
    staged row as an asset change — which made it pass vacuously while every
    cycle died before reaching an adapter, and then fail once the Skill
    classifier started working.
    """
    from app.learning_contract import TARGET_SKILL

    from tests.test_learning_targets import skill_draft

    runtime, message_id = prepare(tmp_path)
    enqueue(runtime, message_id)
    stub_budget(monkeypatch, runtime)

    def surfaces():
        with runtime.db.connection() as connection:
            return {
                "staged": connection.execute("SELECT COUNT(*) FROM skill_versions").fetchone()[0],
                "enabled": connection.execute("SELECT COUNT(*) FROM skills WHERE status='ENABLED'").fetchone()[0],
                "defaults": connection.execute(
                    "SELECT COUNT(*) FROM skills WHERE default_version_id IS NOT NULL").fetchone()[0],
                "promotions": connection.execute(
                    "SELECT COUNT(*) FROM learning_promotions WHERE outcome='PROMOTED'").fetchone()[0],
                "bundle": runtime.behavior.active("stable").id,
            }

    before = surfaces()
    decision = LearningDecision(learn=True, target=TARGET_SKILL, subtype="", confidence=1.0, support=1.0,
                                importance=0.8, risk="low", reason_codes=("repeatable_procedure",),
                                model_identity="stub:jev")
    runtime.learning.pipeline = pipeline_for(runtime, decisions_for(runtime, decision),
                                             StubAgent(lambda e, r: skill_draft()), mode=MODE_SHADOW)

    assert runtime.learning.run_once() is True
    after = surfaces()

    assert after["staged"] == before["staged"] + 1, "shadow must still run the chain through the target adapter"
    assert after["enabled"] == before["enabled"], "shadow must not enable anything"
    assert after["defaults"] == before["defaults"], "shadow must not move a skill's default version"
    assert after["promotions"] == 0 and before["promotions"] == 0
    assert after["bundle"] == before["bundle"], "shadow must not switch the runtime bundle"
