from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.evolution import (
    M5_ALLOWED_PATH,
    M5_TARGET_ROLE,
    EvolutionCandidateGenerator,
    EvolutionConflict,
    EvolutionGateError,
)
from app.real_evaluation import ResearchRoleReplayEvaluator
from app.startup import build_runtime


@pytest.fixture(autouse=True)
def memory_only_database(monkeypatch):
    """M5 offline checks never create another database or contact a model."""
    from app.db import Database
    connections = []
    class MemoryConnection(sqlite3.Connection):
        def close(self):
            pass
    def connect(db):
        if not hasattr(db, "_m5_connection"):
            db._m5_connection = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False, factory=MemoryConnection)
            db._m5_connection.row_factory = sqlite3.Row
            db._m5_connection.execute("PRAGMA foreign_keys=ON")
            connections.append(db._m5_connection)
        return db._m5_connection
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(Database, "_connect", connect)
    yield
    for connection in connections:
        sqlite3.Connection.close(connection)


def future(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def evidence(runtime, *, tag: str = "constraint_omitted"):
    base = runtime.behavior.active("stable")
    return [
        runtime.evolution.record_experience(
            task_type="research", outcome="failure", lineage_group_hash=f"lineage-{tag}-{index}",
            root_task_id=f"root-{tag}-{index}", source_content_hash=f"source-{tag}-{index}",
            runtime_bundle_id=base.id, dataset_partition="DISCOVERY", source_kind="research",
            source_id=f"job-{tag}-{index}", source_event_id=f"event-{tag}-{index}", signal_type="research_failed",
            severity="error", failure_tags=[tag], target_role="researcher", provenance="production",
            source_version="research-v1", idempotency_key=f"m5-experience-{tag}-{index}",
        )
        for index in range(3)
    ]


def approved_batch(runtime, items, *, key="batch-1", authorization_id=None, deadline=None):
    return runtime.evolution.authorize_generation_batch(
        experience_ids=[item["id"] for item in items], base_bundle_id=runtime.behavior.active("stable").id,
        problem_fingerprint="missing-evidence-boundary", root_budget_id="approved-root-budget",
        max_calls=1, budget_microusd=50_000, deadline_at=deadline or future(),
        generation_config={"model": "configured", "temperature": 0, "network_retries": 0},
        content_authorization_id=authorization_id, idempotency_key=key,
    )


class Proposer:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def __call__(self, current, pattern, bundle_id):
        self.calls += 1
        assert pattern["allowed_path"] == M5_ALLOWED_PATH
        assert all("text" not in item for item in pattern["evidence_refs"])
        if self.fail:
            raise RuntimeError("provider failed")
        return {
            "prompt": current + " 结论必须逐句绑定给定证据；证据不足时明确说明边界。",
            "reason": "三个独立研究任务均遗漏证据边界。",
            "root_cause_hypothesis": "写作片段未明确要求声明证据不足。",
            "confidence_limitations": "仅覆盖深度研究章节写作，不能外推到其他角色。",
        }


class OwnerAwareProposer(Proposer):
    def __init__(self):
        super().__init__()
        self.owner_id = None

    def __call__(self, current, pattern, bundle_id):
        self.owner_id = pattern.get("owner_id")
        return super().__call__(current, pattern, bundle_id)


def test_observer_is_projection_only_uses_real_cursor_and_marks_unknown_bundle(tmp_path):
    runtime = build_runtime(tmp_path)
    assert runtime.observer_worker.candidate_generator is None
    run = asyncio.run(runtime.create_goal("目标", "验收未知版本"))
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE runs SET runtime_bundle_id=NULL WHERE id=?", (run.id,))
    event = runtime.events.append(run.id, run.goal_id, "run.failed", "runtime", {"reason": "quality"})

    result = runtime.observer.observe()
    record = runtime.evolution.list_experiences()[0]
    with runtime.db.connection() as connection:
        cursor = connection.execute(
            "SELECT * FROM evolution_observer_offsets WHERE stream_kind='run' AND owner_id='local-user'"
        ).fetchone()
        event_row = connection.execute("SELECT row_id FROM events WHERE event_id=?", (event.event_id,)).fetchone()
    assert result["created"] == 1
    assert record["runtime_bundle_status"] == "UNKNOWN"
    assert cursor["last_row_id"] == event_row["row_id"] == cursor["last_success_row_id"]
    assert cursor["last_error"] is None


def test_generation_batch_is_explicit_bounded_idempotent_and_stops_after_first_failure(tmp_path):
    runtime = build_runtime(tmp_path)
    items = evidence(runtime)
    with pytest.raises(EvolutionGateError, match="positive"):
        runtime.evolution.authorize_generation_batch(
            experience_ids=[item["id"] for item in items], base_bundle_id=runtime.behavior.active("stable").id,
            problem_fingerprint="zero", root_budget_id="budget", max_calls=1, budget_microusd=0,
            deadline_at=future(), generation_config={}, idempotency_key="zero-budget",
        )

    proposer = Proposer()
    runtime.candidate_generator = EvolutionCandidateGenerator(runtime.evolution, runtime.behavior, proposer)
    deadline = future()
    batch = approved_batch(runtime, items, deadline=deadline)
    assert approved_batch(runtime, items, deadline=deadline)["id"] == batch["id"]
    candidate = runtime.candidate_generator.run_batch(batch["id"])
    assert proposer.calls == 1
    assert candidate["contract"]["allowed_path"] == M5_ALLOWED_PATH
    assert candidate["release_contract_version"] == "researcher-prompt-release-v2"
    assert runtime.evolution.get_generation_batch(batch["id"])["status"] == "COMPLETED"
    with pytest.raises(EvolutionConflict, match="not runnable"):
        runtime.candidate_generator.run_batch(batch["id"])
    assert proposer.calls == 1

    failed_items = evidence(runtime, tag="unsupported_claim")
    failing = Proposer(fail=True)
    runtime.candidate_generator = EvolutionCandidateGenerator(runtime.evolution, runtime.behavior, failing)
    failed_batch = runtime.evolution.authorize_generation_batch(
        experience_ids=[item["id"] for item in failed_items], base_bundle_id=runtime.behavior.active("stable").id,
        problem_fingerprint="unsupported-claim", root_budget_id="approved-root-budget", max_calls=1,
        budget_microusd=50_000, deadline_at=future(), generation_config={"model": "configured-v2"},
        idempotency_key="failed-batch",
    )
    with pytest.raises(RuntimeError, match="provider failed"):
        runtime.candidate_generator.run_batch(failed_batch["id"])
    with pytest.raises(EvolutionConflict, match="not runnable"):
        runtime.candidate_generator.run_batch(failed_batch["id"])
    assert failing.calls == 1
    assert runtime.evolution.get_generation_batch(failed_batch["id"])["status"] == "STOPPED"


def test_generation_batch_propagates_its_owner_to_the_paid_proposer(tmp_path):
    runtime = build_runtime(tmp_path)
    owner = "m5-live-owner"
    base = runtime.behavior.active("stable")
    items = [
        runtime.evolution.record_experience(
            task_type="research", outcome="failure", lineage_group_hash=f"owner-lineage-{index}",
            root_task_id=f"owner-root-{index}", source_content_hash=f"owner-source-{index}",
            runtime_bundle_id=base.id, dataset_partition="DISCOVERY", source_kind="research",
            source_id=f"owner-job-{index}", source_event_id=f"owner-event-{index}",
            signal_type="research_failed", severity="error", failure_tags=["constraint_omitted"],
            target_role="researcher", provenance="production", source_version="research-v1",
            idempotency_key=f"owner-experience-{index}", owner_id=owner,
        )
        for index in range(3)
    ]
    batch = runtime.evolution.authorize_generation_batch(
        experience_ids=[item["id"] for item in items], base_bundle_id=base.id,
        problem_fingerprint="owner-boundary", root_budget_id="owner-root-budget",
        max_calls=1, budget_microusd=100, deadline_at=future(), generation_config={"model": "x"},
        idempotency_key="owner-generation", owner_id=owner,
    )
    proposer = OwnerAwareProposer()
    EvolutionCandidateGenerator(runtime.evolution, runtime.behavior, proposer).run_batch(batch["id"], owner)
    assert proposer.owner_id == owner


def test_requesting_batch_recovers_unknown_and_never_replays(tmp_path):
    runtime = build_runtime(tmp_path)
    batch = approved_batch(runtime, evidence(runtime))
    runtime.evolution.begin_generation_batch(batch["id"])
    assert runtime.evolution.recover_generation_batches() == 1
    assert runtime.evolution.get_generation_batch(batch["id"])["status"] == "UNKNOWN"
    with pytest.raises(EvolutionConflict, match="not runnable"):
        runtime.evolution.begin_generation_batch(batch["id"])


def test_operational_402_cannot_authorize_behavior_generation(tmp_path):
    runtime = build_runtime(tmp_path)
    items = evidence(runtime, tag="http_402")
    with pytest.raises(EvolutionGateError, match="eligible"):
        approved_batch(runtime, items)


def test_content_authorization_revocation_and_source_deletion_block_before_proposer(tmp_path):
    runtime = build_runtime(tmp_path)
    items = evidence(runtime)
    scope = [{"source_kind": item["source_kind"], "source_id": item["source_id"]} for item in items]
    auth = runtime.evolution.grant_content_authorization(
        subject_id="local-user", source_scope=scope, purpose="m5_candidate_generation",
        expires_at=future(), idempotency_key="content-auth",
    )
    batch = approved_batch(runtime, items, authorization_id=auth["id"])
    runtime.evolution.revoke_content_authorization(auth["id"])
    proposer = Proposer()
    generator = EvolutionCandidateGenerator(runtime.evolution, runtime.behavior, proposer)
    with pytest.raises(EvolutionGateError, match="authorization"):
        generator.run_batch(batch["id"])
    assert proposer.calls == 0

    other = evidence(runtime, tag="citation_gap")
    second = runtime.evolution.authorize_generation_batch(
        experience_ids=[item["id"] for item in other], base_bundle_id=runtime.behavior.active("stable").id,
        problem_fingerprint="citation-gap", root_budget_id="budget", max_calls=1, budget_microusd=100,
        deadline_at=future(), generation_config={"model": "x"}, idempotency_key="delete-source-batch",
    )
    runtime.evolution.invalidate_source(other[0]["source_kind"], other[0]["source_id"])
    with pytest.raises(EvolutionGateError, match="eligible"):
        generator.run_batch(second["id"])
    assert proposer.calls == 0


def test_role_replay_hits_real_production_builder_and_insufficient_dev_cannot_publish(tmp_path):
    runtime = build_runtime(tmp_path)
    proposer = Proposer()
    generator = EvolutionCandidateGenerator(runtime.evolution, runtime.behavior, proposer)
    candidate = generator.run_batch(approved_batch(runtime, evidence(runtime))["id"])
    base = runtime.behavior.get(candidate["base_bundle_id"]).manifest
    target = runtime.behavior.get(candidate["target_bundle_id"]).manifest
    case = {"id": "dev-1", "partition": "DEV", "heading": "结论", "thesis": "仅按证据", "evidence": [("事实", "source_1")]}
    pair = ResearchRoleReplayEvaluator.render_pair(base, target, case)
    assert pair["single_allowed_fragment"] is True
    assert pair["base_prompt_digest"] != pair["candidate_prompt_digest"]

    def runner(messages, bundle_id, _case):
        assert messages[0]["role"] == "system"
        from app.real_evaluation import _digest
        return {"text": "事实 [[source:source_1]]", "cost_microusd": 1, "ttft_seconds": 0.01,
                "prompt_digest": _digest(messages[0]["content"]), "model_identity": "offline"}

    evaluation = runtime.evolution.evaluate_research_replay(
        candidate["id"], expected_version=0, cases=[case], runner=runner,
        judge=lambda _: {"winner": "tie", "candidate_safe": True, "cost_microusd": 1},
        idempotency_key="dev-role-replay",
    )
    assert evaluation["metrics"]["kind"] == "role_paired_dev_evaluation"
    assert evaluation["metrics"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    with pytest.raises(EvolutionGateError, match="deterministic"):
        runtime.evolution.approve_current(candidate["id"], expires_at=future(), idempotency_key="must-not-publish")


def test_m5_patch_cannot_change_tools_or_another_prompt_role(tmp_path):
    runtime = build_runtime(tmp_path)
    items = evidence(runtime)
    base = runtime.behavior.active("stable")
    target = runtime.behavior.ensure({**base.manifest, "tools": "changed"})
    with pytest.raises(EvolutionGateError, match="single allowed"):
        runtime.evolution.propose_candidate(
            candidate_type="prompt", experience_ids=[item["id"] for item in items], base_bundle_id=base.id,
            target_bundle_id=target.id, proposed_content={"tools": "changed"}, permission_diff={"added": []},
            reason="bad", problem_fingerprint="bad", root_cause_hypothesis="bad",
            confidence_limitations="bad", target_role=M5_TARGET_ROLE, allowed_path=M5_ALLOWED_PATH,
            idempotency_key="out-of-bound",
        )


def test_release_contract_canary_targets_only_real_research_write_tasks(tmp_path):
    runtime = build_runtime(tmp_path)
    cases = []
    for partition, count in (("DEV", 20), ("HOLDOUT", 30), ("SAFETY", 10)):
        for index in range(count):
            cases.append({
                "id": f"{partition.lower()}-{index}", "partition": partition, "heading": f"结论 {partition} {index}",
                "thesis": "仅按证据", "evidence": [("事实", "source_1")], "lineage_id": f"{partition}-{index}",
                "rubric": {"deterministic_required": ["[[source:source_1]]"]},
            })
    runtime.evolution.freeze_research_suite(cases)
    candidate = EvolutionCandidateGenerator(runtime.evolution, runtime.behavior, Proposer()).run_batch(
        approved_batch(runtime, evidence(runtime))["id"]
    )

    def runner(messages, bundle_id, case):
        marker = "边界" if "证据不足时明确说明边界" in messages[0]["content"] else "基线"
        from app.real_evaluation import _digest
        return {"text": marker + " [[source:source_1]]", "cost_microusd": 1, "ttft_seconds": 0.01,
                "prompt_digest": _digest(messages[0]["content"]), "model_identity": "offline"}

    evaluation = runtime.evolution.evaluate_research_replay(
        candidate["id"], expected_version=0, cases=cases, runner=runner,
        judge=lambda value: {
            "winner": "candidate" if value["case"]["partition"] == "HOLDOUT" else "tie",
            "candidate_safe": True, "baseline_safe": True, "cost_microusd": 1,
        },
        holdout_frozen_before_candidate=True, idempotency_key="release-role-replay",
    )
    assert evaluation["metrics"]["kind"] == "role_paired_release_evaluation"
    approval = runtime.evolution.approve_current(candidate["id"], expires_at=future(), idempotency_key="release-approval")
    current = runtime.evolution.get_candidate(candidate["id"])
    with pytest.raises(EvolutionGateError, match="role target"):
        runtime.evolution.start_canary(
            candidate["id"], expected_version=current["version"], approval_id=approval["id"],
            allocation_percent=10, assignment_unit="run", idempotency_key="bad-canary",
        )
    deployment = runtime.evolution.start_canary(
        candidate["id"], expected_version=current["version"], approval_id=approval["id"],
        allocation_percent=10, assignment_unit="run", idempotency_key="targeted-canary",
        target_role="researcher", target_purpose="write_research_section", budget_microusd=1000,
        deadline_at=future(), max_calls=100,
    )
    stable = runtime.behavior.active("stable").id
    assert runtime.evolution.assign_run("conversation-run", "thread") == (stable, None)
    assert runtime.evolution.assign_role_task(
        "research-plan", "job", role="researcher", purpose="research_structured_step"
    ) == (stable, None)
    bundle_id, deployment_id = runtime.evolution.assign_role_task(
        "research-write", "job", role="researcher", purpose="write_research_section"
    )
    assert deployment_id == deployment["id"]
    assert bundle_id in {candidate["base_bundle_id"], candidate["target_bundle_id"]}
    with runtime.db.connection() as connection:
        exposure = connection.execute("SELECT * FROM canary_exposures WHERE run_id='research-write'").fetchone()
    assert exposure["target_role"] == "researcher"
    assert exposure["target_purpose"] == "write_research_section"
    assert exposure["prompt_hit"] == 0
