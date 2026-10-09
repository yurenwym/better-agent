"""Learning generation, extraction, and Judge through the real routed gateway."""
from __future__ import annotations

import json

import pytest

from app.learning_agent import LearningAgent
from app.learning_contract import TARGET_MEMORY
from app.learning_decision import LearningDecision
from app.learning_eval import LearningJudge
from app.learning_extraction import ConstraintExtractor
from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
from snapshot_entrypoint_helpers import CommittedSnapshotTransport
from test_snapshot_gateway import _answer, _configured_control_plane


def _gateway(tmp_path, monkeypatch, callsite, message):
    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, owner_id="learning-owner")
    observer = CommittedSnapshotTransport(db, "learning-owner", callsite, _answer(message))
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
    context = ModelCallContext(role="learning_generator", purpose="acceptance_learning", owner_id="learning-owner",
                               runtime_bundle_id=bundle.id)
    return db, observer, gateway, context


@pytest.mark.asyncio
async def test_learning_agent_generation_freezes_authorized_job_input(tmp_path, monkeypatch):
    payload = {
        "target": "MEMORY", "problem": "约束未保存", "root_cause": "缺少记录",
        "generalizable_lesson": "保留用户的明确要求", "proposed_change": {
            "operation": "ADD", "kind": "constraint", "scope_type": "user", "scope_id": "",
            "content": "必须人工批准。",
        }, "expected_effect": {"approval": "required"}, "risks": [],
        "evidence_refs": ["thread_message_1"], "experience_ids": ["experience_1"],
    }
    db, observer, gateway, context = _gateway(tmp_path, monkeypatch, "CS-LN-01", json.dumps(payload, ensure_ascii=False))
    decision = LearningDecision(learn=True, target=TARGET_MEMORY, subtype="", confidence=.9, importance=.7,
                                risk="low", reason_codes=("explicit_user_constraint",))
    token = gateway.set_call_context(context)
    try:
        draft = await LearningAgent(gateway).generate_async(
            decision=decision, experience={"id": "experience_1", "outcome": "success"},
            evidence_ids=("experience_1",), source_refs=("thread_message_1",), context=context,
        )
        assert draft.target == TARGET_MEMORY
    finally:
        gateway.reset_call_context(token)
    assert len(observer.observations) == 1
    assert observer.observations[0]["owner_id"] == "learning-owner"


@pytest.mark.asyncio
async def test_learning_constraint_extraction_freezes_source_excerpt(tmp_path, monkeypatch):
    text = "用户要求每次操作最多 30 分钟。"
    answer = json.dumps({"setting": "action_max_minutes", "value": 30, "project_only": False, "evidence": "最多 30 分钟"}, ensure_ascii=False)
    db, observer, gateway, _ = _gateway(tmp_path, monkeypatch, "CS-LN-01", answer)
    token = gateway.set_call_context(ModelCallContext(role="coordinator", purpose="extract_learning_constraint",
                                                       owner_id="learning-owner"))
    try:
        result = await ConstraintExtractor(gateway).extract({"id": "job-1", "owner_id": "learning-owner"}, text, None)
        assert result["value"] == 30
    finally:
        gateway.reset_call_context(token)
    assert len(observer.observations) == 1
    assert observer.observations[0]["owner_id"] == "learning-owner"


@pytest.mark.asyncio
async def test_learning_judge_uses_its_own_committed_call(tmp_path, monkeypatch):
    db, observer, gateway, context = _gateway(
        tmp_path, monkeypatch, "CS-LN-01",
        '{"winner":"left","correctness":0.9,"helpfulness":0.8,"safety":1,"efficiency":0.7,"reason_codes":["more_specific"]}',
    )
    token = gateway.set_call_context(context)
    try:
        verdict = await LearningJudge(gateway).compare_async(
            case_id="case-1", task="满足原始请求", candidate={"text": "候选"}, baseline={"text": "基线"},
            context=ModelCallContext(role="learning_judge", purpose="judge_learning_candidate", owner_id="learning-owner",
                                     runtime_bundle_id=context.runtime_bundle_id), flip=False,
        )
        assert verdict.winner == "candidate"
    finally:
        gateway.reset_call_context(token)
    assert len(observer.observations) == 1
    assert observer.observations[0]["owner_id"] == "learning-owner"
