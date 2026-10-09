"""Real Research model entrypoints audited at the committed transport boundary."""
from __future__ import annotations

import json

import pytest

from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
from app.execution_context import create_root_context
from app.model_gateway import GatewayError
from app.research.live import LiveResearchModel
from app.research.models import Evidence, ResearchLimits, ResearchPlan, Source
from snapshot_entrypoint_helpers import CommittedSnapshotTransport
from test_snapshot_gateway import _answer, _configured_control_plane


@pytest.mark.asyncio
async def test_t37_research_worker_with_missing_source_turn_never_sends(tmp_path, monkeypatch):
    from app.conversation import ConversationService
    from app.research.service import ResearchService
    from app.research.worker import ManagedResearchWorker
    from test_research_service import CompletingEngine

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, owner_id="research-owner")
    observer = CommittedSnapshotTransport(db, "research-owner", "CS-RS-01", _answer("unused"))
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
    engine = CompletingEngine()
    engine.model = LiveResearchModel(gateway)
    conversation = ConversationService(db)
    service = ResearchService(db, conversation.events, engine)
    thread = conversation.create_thread("Research", owner_id="research-owner")
    job = service.create_manual(thread.id, "Research", "worker", ("web",), runtime_bundle_id=bundle.id)
    with db.connection() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("UPDATE research_jobs SET source_turn_id='missing-source' WHERE id=?", (job.id,))
        connection.commit()
    await ManagedResearchWorker(service).run_once(job.id)
    assert observer.send_count == 0 and gateway.current_call_context() is None
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_t07_sections_and_report_repair_pin_bundle_and_preserve_inputs(tmp_path, monkeypatch):
    from app.behavior import BehaviorBundleService
    from app.model_input_snapshot_store import ModelInputSnapshotStore

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, owner_id="research-owner")
    observer = CommittedSnapshotTransport(db, "research-owner", "CS-RS-02", _answer("## Result\nEvidence [[source:s1]]"))
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
    model = LiveResearchModel(gateway)
    context = ModelCallContext("researcher", "research", owner_id="research-owner", runtime_bundle_id=bundle.id)
    token = gateway.set_call_context(context)
    evidence = [Evidence("e1", "s1", "First evidence", None, .9)]
    try:
        await model.write("First", "Supported", evidence, "")
        first = ModelInputSnapshotStore(db).load("research-owner", observer.observations[0]["snapshot_id"])
        evidence.append(Evidence("e2", "s1", "Later evidence", None, .8))
        bundles = BehaviorBundleService(db)
        changed = bundles.ensure({**bundle.manifest, "prompt": "changed stable"})
        bundles.activate("stable", changed.id, "change-stable")
        await model.write("Second", "Supported", evidence, "First summary")
        await model.repair("Research", ResearchPlan("Research", ("First", "Second"), ("query",)), ("missing",), [("s1", "Evidence")])
    finally:
        gateway.reset_call_context(token)
    assert len(observer.observations) == 3
    assert len({item["snapshot_id"] for item in observer.observations}) == 3
    snapshots = [ModelInputSnapshotStore(db).load("research-owner", item["snapshot_id"]) for item in observer.observations]
    assert all(snapshot.runtime_bundle_id == bundle.id for snapshot in snapshots)
    assert snapshots[0].content_json == first.content_json and "Later evidence" not in first.content_json
    assert "Later evidence" in snapshots[1].content_json


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["exception", "cancel"])
async def test_t08_t36_research_worker_restores_context_and_judges_recovered_job(tmp_path, monkeypatch, interruption):
    import asyncio
    from app.conversation import ConversationService
    from app.evolution import LiveSafetyJudge
    from app.research.service import ResearchService
    from app.research.worker import ManagedResearchWorker
    from test_research_service import CompletingEngine

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, owner_id="research-owner")
    conversation = ConversationService(db)
    recorder = CommittedSnapshotTransport(db, "research-owner", "CS-RS-01", _answer(
        '{"title":"Research","sections":["Conclusion"],"queries":["query"]}'
    ))
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=recorder)
    model = LiveResearchModel(gateway)

    class Engine(CompletingEngine):
        def __init__(self):
            self.model = model
            self.interrupt = True

        async def run_research(self, request):
            await model.plan(request.topic, request.limits)
            if self.interrupt:
                self.interrupt = False
                if interruption == "cancel":
                    raise asyncio.CancelledError
                raise TimeoutError("temporary failure")
            async for event in super().run_research(request):
                yield event

    engine = Engine()
    service = ResearchService(db, conversation.events, engine)
    thread = conversation.create_thread("Research", owner_id="research-owner")
    job = service.create_manual(thread.id, "Research", "worker", ("web",), runtime_bundle_id=bundle.id)
    verdicts = []

    class Evolution:
        def assign_role_task(self, *args, **kwargs):
            return bundle.id, None

        def finish_run_exposure(self, *args, **kwargs):
            verdicts.append(kwargs)

    judge_recorder = CommittedSnapshotTransport(db, "research-owner", "CS-RS-03", _answer("safe"))

    async def execute(profile, request, **kwargs):
        return await (judge_recorder if request.purpose == "judge_research_output" else recorder)(profile, request, **kwargs)

    monkeypatch.setattr(gateway, "_execute_attempt", execute)
    worker = ManagedResearchWorker(service, evolution=Evolution(), safety_judge=LiveSafetyJudge(gateway))
    sentinel = ModelCallContext(role="conversation", purpose="unrelated", owner_id="other-owner")
    token = gateway.set_call_context(sentinel)
    try:
        if interruption == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await worker.run_once(job.id)
        else:
            assert await worker.run_once(job.id)
        assert gateway.current_call_context() is sentinel
        assert len(recorder.observations) == 1
        old = recorder.observations[0]["snapshot_id"]
        from app.model_input_snapshot_store import ModelInputSnapshotStore
        frozen = ModelInputSnapshotStore(db).load("research-owner", old)
        with db.transaction() as connection:
            connection.execute("UPDATE research_jobs SET lease_until='2000-01-01T00:00:00+00:00',available_at='2000-01-01T00:00:00+00:00' WHERE id=?", (job.id,))
        assert await worker.run_once()
        assert gateway.current_call_context() is sentinel
        assert service.get(job.id).status == "COMPLETED"
        assert len(recorder.observations) == 2 and len(judge_recorder.observations) == 1
        assert ModelInputSnapshotStore(db).load("research-owner", old).content_json == frozen.content_json
        assert verdicts[-1]["safety_pass"] is True
        with db.connection() as connection:
            row = connection.execute("SELECT * FROM model_invocations WHERE purpose='judge_research_output'").fetchone()
        assert row["role"] == "judge_safety" and row["owner_id"] == "research-owner"
        assert row["context_snapshot_id"] not in {item["snapshot_id"] for item in recorder.observations}
    finally:
        gateway.reset_call_context(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["plan", "distill", "reflect", "curate", "write", "summarize", "audit", "repair"])
async def test_research_entrypoint_sends_only_after_committed_snapshot(tmp_path, monkeypatch, entrypoint):
    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, owner_id="research-owner")
    responses = {
        "plan": {"title": "研究", "sections": ["结论", "依据"], "queries": ["研究结论"]},
        "distill": {"evidence": [{"text": "PostgreSQL 使用 MVCC。", "relevance": .9}]},
        "reflect": {"queries": ["补充研究"]},
        "curate": {"sections": [{"heading": "结论", "thesis": "支持", "evidence_ids": ["e1"]}]},
        "audit": {"passes": True, "missing_requirements": []},
        "summarize": {"tldr": "完成", "points": ["依据 [[source:s1]]"]},
    }
    response = _answer(json.dumps(responses[entrypoint], ensure_ascii=False) if entrypoint in responses else "## 结论\n\n完成 [[source:s1]]")
    callsite = "CS-RS-02" if entrypoint in {"write", "repair"} else "CS-RS-01"
    observer = CommittedSnapshotTransport(db, "research-owner", callsite, response)
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
    model = LiveResearchModel(gateway)
    model.runtime_prompt_policy = lambda: {"researcher": {"write_research_section": {"evidence_statement": "严格引用证据"}}}
    context = ModelCallContext(role="researcher", purpose=f"acceptance_{entrypoint}", owner_id="research-owner",
                               runtime_bundle_id=bundle.id)
    token = gateway.set_call_context(context)
    source = Source("s1", 1, "web", "https://source.test", None, "研究来源", "PostgreSQL 使用 MVCC。", None, "2026-10-08", .9, "source-hash")
    evidence = [Evidence("e1", "s1", "PostgreSQL 使用 MVCC。", None, .9)]
    plan = ResearchPlan("研究", ("结论",), ("研究结论",))
    try:
        if entrypoint == "plan":
            result = await model.plan("研究", ResearchLimits())
            assert result.title == "研究"
        elif entrypoint == "distill":
            result = await model.distill(source, "研究", ("结论",))
            assert result and result[0].source_id == "s1"
        elif entrypoint == "reflect":
            assert await model.reflect("研究", plan, evidence, ()) == ("补充研究",)
        elif entrypoint == "curate":
            assert (await model.curate(plan, evidence))[0][0] == "结论"
        elif entrypoint == "write":
            assert (await model.write("结论", "支持", evidence, ""))[0].startswith("## 结论")
        elif entrypoint == "summarize":
            assert (await model.summarize(("结论 [[source:s1]]",)))[0] == "完成"
        elif entrypoint == "audit":
            assert (await model.audit("研究", plan, "报告"))[0]
        else:
            assert "完成" in await model.repair("研究", plan, ("缺少结论",), [("s1", "证据")])
    finally:
        gateway.reset_call_context(token)

    assert len(observer.observations) == 1
    observed = observer.observations[0]
    assert observed["owner_id"] == "research-owner"
    assert observed["snapshot_id"] and observed["snapshot_digest"]
    from app.model_input_snapshot_store import ModelInputSnapshotStore
    frozen = ModelInputSnapshotStore(db).load("research-owner", observed["snapshot_id"])
    assert frozen.role == "researcher"
    assert frozen.purpose == {"write":"write_research_section", "repair":"repair_research_report"}.get(entrypoint, "research_structured_step")
    if entrypoint in {"distill", "reflect", "curate", "write", "repair"}:
        import hashlib
        sources = frozen.provenance().sources
        text = "证据" if entrypoint == "repair" else "PostgreSQL 使用 MVCC。"
        assert len(sources) == 1 and sources[0].included
        assert sources[0].content_digest == hashlib.sha256(text.encode()).hexdigest()
        assert sources[0].location["scope"] == "excerpt"
        assert frozen.provenance().status == "partial"


@pytest.mark.asyncio
async def test_research_invalid_json_repair_is_a_new_logical_call(tmp_path, monkeypatch):
    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, owner_id="research-owner")
    valid = _answer(json.dumps({"title": "研究", "sections": ["结论", "依据"], "queries": ["研究结论"]}, ensure_ascii=False))
    observer = CommittedSnapshotTransport(db, "research-owner", "CS-RS-01", [_answer("not-json"), valid])
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
    root = create_root_context(owner_id="research-owner", runtime_bundle_id=bundle.id)
    token = gateway.set_call_context(ModelCallContext.from_harness(root, role="researcher", purpose="research_job"))
    try:
        plan = await LiveResearchModel(gateway).plan("研究", ResearchLimits())
    finally:
        gateway.reset_call_context(token)

    assert plan.title == "研究"
    assert len(observer.observations) == 2
    first, repair = observer.observations
    assert first["invocation_id"] != repair["invocation_id"]
    assert first["snapshot_id"] != repair["snapshot_id"]
    assert first["execution_digest"] != repair["execution_digest"]


@pytest.mark.asyncio
async def test_research_network_retry_reuses_the_frozen_logical_call(tmp_path, monkeypatch):
    db, bundle, _ = _configured_control_plane(
        tmp_path, monkeypatch, owner_id="research-owner", max_attempts={"chat": 2},
    )
    valid = _answer(json.dumps({"title": "研究", "sections": ["结论", "依据"], "queries": ["研究结论"]}, ensure_ascii=False))
    observer = CommittedSnapshotTransport(
        db, "research-owner", "CS-RS-01", [GatewayError("timeout", "timeout"), valid],
    )
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
    root = create_root_context(owner_id="research-owner", runtime_bundle_id=bundle.id)
    token = gateway.set_call_context(ModelCallContext.from_harness(root, role="researcher", purpose="research_job"))
    try:
        plan = await LiveResearchModel(gateway).plan("研究", ResearchLimits())
    finally:
        gateway.reset_call_context(token)

    assert plan.title == "研究"
    assert len(observer.observations) == 2
    first, retry = observer.observations
    assert first["invocation_id"] == retry["invocation_id"]
    assert first["snapshot_id"] == retry["snapshot_id"]
    assert first["snapshot_digest"] == retry["snapshot_digest"]
    assert first["attempt_id"] != retry["attempt_id"]
