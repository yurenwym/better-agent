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


@pytest.mark.asyncio
async def test_t14_nondefault_evolution_owner_and_independent_judge(tmp_path, monkeypatch):
    from app.evolution import LiveBehaviorRunner, LivePromptCandidateProposer
    from app.model_input_snapshot_store import ModelInputSnapshotStore

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, owner_id="evolution-owner")
    arm = CommittedSnapshotTransport(db, "evolution-owner", "CS-EV-01", [_answer("Actual candidate output"), _answer("helpful")])
    proposal = CommittedSnapshotTransport(db, "evolution-owner", "CS-EV-04", _answer(
        '{"prompt":"new prompt","reason":"evidence","root_cause_hypothesis":"wording","confidence_limitations":"small sample"}'
    ))

    async def execute(profile, request, **kwargs):
        return await (proposal if request.purpose == "propose_evolution_candidate" else arm)(profile, request, **kwargs)

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    runner, proposer = LiveBehaviorRunner(gateway), LivePromptCandidateProposer(gateway)
    assert await runner._run({"prompt": "candidate policy"}, "Task", bundle.id, "evolution-owner") == "helpful"
    assert (await proposer._propose("old prompt", {"owner_id":"evolution-owner"}, bundle.id))["prompt"] == "new prompt"
    observations = arm.observations + proposal.observations
    assert len({item["invocation_id"] for item in observations}) == 3
    snapshots = [ModelInputSnapshotStore(db).load("evolution-owner", item["snapshot_id"]) for item in observations]
    assert all(snapshot.runtime_bundle_id == bundle.id for snapshot in snapshots)
    assert "Actual candidate output" in snapshots[1].content_json
    assert "candidate policy" not in snapshots[1].content_json
    for owner in (None, ""):
        with pytest.raises(PermissionError):
            await runner._run({}, "Task", bundle.id, owner)
        with pytest.raises(PermissionError):
            await proposer._propose("old", {"owner_id": owner}, bundle.id)
    assert arm.send_count == 2 and proposal.send_count == 1
    assert gateway.current_call_context() is None


@pytest.mark.asyncio
async def test_t23_learning_off_does_not_disable_other_snapshot_entries(tmp_path, monkeypatch):
    import httpx
    from app.startup import build_runtime
    from app.model_input_snapshot_store import ModelInputSnapshotStore
    from app.research.models import ResearchLimits
    from test_snapshot_gateway import _openai_stream, _registered_profile
    from test_snapshot_flow import _drive, _header

    for name in ("DATABASE_URL", "AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL", "LLM_AP_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BETTER_AGENT_TEST_ALLOW_SQLITE", "1")
    monkeypatch.setenv("BETTER_AGENT_LEARNING_V3", "OFF")
    db, _, versions = _configured_control_plane(tmp_path, monkeypatch)
    runtime = build_runtime(tmp_path, profile=_registered_profile(db, versions["chat"]), activate_stable=False)
    assert runtime.learning.pipeline is None
    gateway = runtime.model.gateway
    research = CommittedSnapshotTransport(runtime.db, "local-user", "CS-RS-01", _answer(
        '{"title":"Research","sections":["Result"],"queries":["query"]}'
    ))
    chat = CommittedSnapshotTransport(runtime.db, "local-user", "CS-CA-01", _answer(_header()))

    async def execute(profile, request, **kwargs):
        return await (research if request.role == "researcher" else chat)(profile, request, **kwargs)

    monkeypatch.setattr(gateway, "_execute_attempt", execute)
    token = gateway.set_call_context(ModelCallContext("researcher", "plan_research", owner_id="local-user"))
    try:
        await runtime.research.engine.model.plan("Research", ResearchLimits())
    finally:
        gateway.reset_call_context(token)
    await _drive(runtime.conversation_model, "Hello")
    admin_sends = []
    admin_observer = CommittedSnapshotTransport(runtime.db, "local-user", "CS-MD-01", None)

    def verify(request):
        with runtime.db.connection() as connection:
            row = connection.execute("SELECT i.* FROM model_invocations i JOIN model_attempts a ON a.invocation_id=i.id WHERE a.status='STARTED'").fetchone()
        assert row["owner_id"] == "local-user" and row["purpose"] == "verify_model_profile_version"
        frozen = ModelInputSnapshotStore(runtime.db).load("local-user", row["context_snapshot_id"])
        assert frozen.content_digest == row["context_snapshot_digest"]
        assert json.loads(request.content)["messages"] == frozen.to_request().messages
        admin_observer.record(_registered_profile(runtime.db, versions["chat"]), frozen.to_request())
        admin_sends.append(row["id"])
        return httpx.Response(200, content=_openai_stream())

    runtime.model_admin.verification_transport = httpx.MockTransport(verify)
    assert (await runtime.model_admin.verify(versions["chat"]))["verification_status"] == "VERIFIED"
    assert research.send_count == chat.send_count == len(admin_sends) == 1
    assert gateway.current_call_context() is None
