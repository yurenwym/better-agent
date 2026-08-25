from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.research.engine import InsufficientEvidence, ResearchCancelled, ResearchEngine, TopicCoverageError, UnknownCitation
from app.research.models import Evidence, ResearchLimits, ResearchPlan, ResearchRequest, Source
from app.research.retriever import RetrievalError, filter_sources, validate_public_url
from app.research.live import LiveResearchModel, relevant_excerpt


class FakeModel:
    def __init__(self, *, bad_plan: bool = False, bad_citation: bool = False) -> None:
        self.bad_plan = bad_plan
        self.bad_citation = bad_citation
        self.reflections = 0

    async def plan(self, topic: str, limits: ResearchLimits):
        if self.bad_plan:
            raise ValueError("broken json")
        return ResearchPlan(topic, ("背景", "结论"), (topic, f"{topic} 证据"))

    async def distill(self, source: Source, topic: str, sections: tuple[str, ...]):
        return [Evidence(f"e-{source.id}", source.id, f"{source.title} 支持核心事实", None, .9)]

    async def reflect(self, topic: str, plan: ResearchPlan, evidence: list[Evidence], used_queries: tuple[str, ...]):
        self.reflections += 1
        return (f"{topic} 补充", f"{topic} 补充", "")

    async def curate(self, plan: ResearchPlan, evidence: list[Evidence]):
        return [(heading, f"{heading}论点", tuple(item.id for item in evidence)) for heading in plan.sections]

    async def write(self, heading: str, thesis: str, evidence: list[Evidence], prior_summary: str):
        source_id = "missing" if self.bad_citation else evidence[0].source_id
        return f"## {heading}\n\n{evidence[0].text} [[source:{source_id}]]", thesis

    async def summarize(self, sections: list[str]):
        return "证据支持主要结论。", ("主要事实已由来源支持",)

    async def audit(self, topic: str, plan: ResearchPlan, report: str):
        return True, ()


class FakeRetriever:
    async def retrieve(self, query: str, request: ResearchRequest):
        suffix = "extra" if "补充" in query else "base"
        return [Source(f"s-{suffix}", 0, "web", f"https://example.com/{suffix}#part", None, f"来源{suffix}", "有效正文" * 120, None, "2026-01-01T00:00:00+00:00", .8)]


@pytest.mark.asyncio
async def test_engine_fallback_reflects_once_and_emits_cited_report() -> None:
    model = FakeModel(bad_plan=True)
    engine = ResearchEngine(model, FakeRetriever())
    events = [event async for event in engine.run_research(ResearchRequest("job-1", "本地 Agent", ("web",), ResearchLimits()))]
    report = next(event.data["markdown"] for event in events if event.type == "report")
    assert model.reflections == 1
    assert "https://example.com/base" in report
    assert "[[source:" not in report
    assert [event.phase for event in events if event.type == "phase"][-1] == "completed"


@pytest.mark.asyncio
async def test_engine_skips_reflection_when_source_budget_is_full() -> None:
    model = FakeModel()
    events = [event async for event in ResearchEngine(model, FakeRetriever()).run_research(
        ResearchRequest("full-sources", "AI Agent 秋招", ("web",), ResearchLimits(max_sources=1))
    )]

    assert model.reflections == 0
    assert any(event.type == "report" for event in events)


@pytest.mark.asyncio
async def test_zero_evidence_and_unknown_citation_cannot_finalize() -> None:
    class Empty(FakeModel):
        async def distill(self, *args): return []

    with pytest.raises(InsufficientEvidence):
        _ = [event async for event in ResearchEngine(Empty(), FakeRetriever()).run_research(ResearchRequest("j", "x", ("web",), ResearchLimits()))]
    with pytest.raises(UnknownCitation):
        _ = [event async for event in ResearchEngine(FakeModel(bad_citation=True), FakeRetriever()).run_research(ResearchRequest("j2", "x", ("web",), ResearchLimits(reflection_rounds=0)))]


@pytest.mark.asyncio
async def test_missing_planned_sections_cannot_be_published_as_completed() -> None:
    class Partial(FakeModel):
        async def plan(self, topic: str, limits: ResearchLimits):
            return ResearchPlan(topic, ("在招公司", "岗位要求", "投递渠道"), (topic,))

        async def curate(self, plan: ResearchPlan, evidence: list[Evidence]):
            return [("在招公司", "公司清单", tuple(item.id for item in evidence))]

    with pytest.raises(TopicCoverageError, match="missing planned sections"):
        _ = [event async for event in ResearchEngine(Partial(), FakeRetriever()).run_research(
            ResearchRequest("coverage-sections", "AI Agent 秋招", ("web",), ResearchLimits(reflection_rounds=0))
        )]


@pytest.mark.asyncio
async def test_invalid_model_curation_falls_back_to_all_planned_sections() -> None:
    class InvalidCuration(FakeModel):
        async def curate(self, plan: ResearchPlan, evidence: list[Evidence]):
            return [("unrequested", "wrong", ("missing-evidence",))]

    events = [event async for event in ResearchEngine(InvalidCuration(), FakeRetriever()).run_research(
        ResearchRequest("curation-fallback", "主动回忆", ("web",), ResearchLimits(reflection_rounds=0))
    )]

    assert len([event for event in events if event.type == "section"]) == 2


@pytest.mark.asyncio
async def test_failed_topic_audit_cannot_be_published_as_completed() -> None:
    class OffTopic(FakeModel):
        async def audit(self, topic: str, plan: ResearchPlan, report: str):
            return False, ("投递渠道",)

    with pytest.raises(TopicCoverageError, match="投递渠道"):
        _ = [event async for event in ResearchEngine(OffTopic(), FakeRetriever()).run_research(
            ResearchRequest("coverage-audit", "AI Agent 秋招", ("web",), ResearchLimits(reflection_rounds=0))
        )]


@pytest.mark.asyncio
async def test_source_hash_citation_is_normalized_to_the_exact_source_id() -> None:
    class HashOnlyCitationModel(FakeModel):
        async def write(self, heading: str, thesis: str, evidence: list[Evidence], prior_summary: str):
            source_id = evidence[0].source_id.removeprefix("source_")
            return f"## {heading}\n\n{evidence[0].text} [[source:{source_id}]]", thesis

    class PrefixedRetriever:
        async def retrieve(self, query: str, request: ResearchRequest):
            return [Source("source_exacthash", 0, "web", "https://example.com/exact", None, "Exact", "body" * 100, None, "n", .9)]

    events = [event async for event in ResearchEngine(HashOnlyCitationModel(), PrefixedRetriever()).run_research(
        ResearchRequest("j3", "x", ("web",), ResearchLimits(reflection_rounds=0))
    )]
    report = next(event.data["markdown"] for event in events if event.type == "report")
    assert "https://example.com/exact" in report
    assert "[[source:" not in report


@pytest.mark.asyncio
async def test_hallucinated_summary_citation_does_not_discard_a_valid_report() -> None:
    class HallucinatedSummary(FakeModel):
        async def summarize(self, sections):
            return "摘要 [[source:invented]]", ("要点 [[source:invented]]",)

    events = [event async for event in ResearchEngine(HallucinatedSummary(), FakeRetriever()).run_research(
        ResearchRequest("summary-citation", "x", ("web",), ResearchLimits(reflection_rounds=0))
    )]

    report = next(event.data["markdown"] for event in events if event.type == "report")
    assert "invented" not in report
    assert "https://example.com/base" in report


@pytest.mark.asyncio
async def test_recovery_keeps_persisted_source_identity_for_completed_section() -> None:
    old_source=Source("old-source",1,"web","https://example.com/old",None,"Old","old body"*80,None,"old",.8,"old-hash")
    old_evidence=Evidence("old-evidence",old_source.id,"old fact",None,.9)
    request=ResearchRequest("same-job","x",("web",),ResearchLimits(reflection_rounds=0),completed_sections={1:{"markdown":"## Old\n\nold fact [[source:old-source]]","summary":"old"}},recovered_sources=(old_source,),recovered_evidence=(old_evidence,))
    events=[event async for event in ResearchEngine(FakeModel(),FakeRetriever()).run_research(request)]
    report=next(event.data["markdown"] for event in events if event.type=="report")
    assert "https://example.com/old" in report


@pytest.mark.asyncio
async def test_recovery_uses_persisted_plan_and_completed_section_identity() -> None:
    class Changed(FakeModel):
        async def plan(self,*args):return ResearchPlan("changed",("changed one","changed two"),("changed",))
        async def curate(self,plan,evidence):return [("changed", "changed", tuple(item.id for item in evidence))]
    source=Source("old-source",1,"web","https://example.com/old",None,"Old","old body"*80,None,"old",.8,"old-hash")
    evidence=Evidence("old-evidence",source.id,"old fact",None,.9)
    request=ResearchRequest("same-job","x",("web",),ResearchLimits(reflection_rounds=0),completed_sections={2:{"heading":"Stable two","markdown":"## Stable two\n\nold fact [[source:old-source]]","summary":"old"}},recovered_sources=(source,),recovered_evidence=(evidence,),recovered_plan=ResearchPlan("stable",("Stable one","Stable two"),("stable",)))
    events=[event async for event in ResearchEngine(Changed(),FakeRetriever()).run_research(request)]
    report=next(event.data["markdown"] for event in events if event.type=="report")
    assert "# stable" in report and "## Stable two" in report


@pytest.mark.asyncio
async def test_cancel_after_summarize_prevents_report_commit() -> None:
    cancel=asyncio.Event()
    class Cancelling(FakeModel):
        async def summarize(self,sections):cancel.set();return await super().summarize(sections)
    request=ResearchRequest("cancel-late","x",("web",),ResearchLimits(reflection_rounds=0),cancel_event=cancel)
    with pytest.raises(ResearchCancelled):
        _=[event async for event in ResearchEngine(Cancelling(),FakeRetriever()).run_research(request)]


def test_source_filter_is_stable_deduplicated_and_domain_bounded() -> None:
    sources = [
        Source(f"s{i}", 0, "web", f"https://example.com/a{i}#fragment", None, f"Title {i}", "body" * 100, None, "n", 1 - i / 100)
        for i in range(5)
    ] + [Source("duplicate", 0, "web", "https://example.com/a0", None, "duplicate", "body" * 100, None, "n", .1)]
    result = filter_sources(sources, min_chars=100, max_sources=20, per_domain=3)
    assert [item.id for item in result] == ["s0", "s1", "s2"]
    assert [item.ordinal for item in result] == [1, 2, 3]
    assert all("#" not in (item.canonical_url or "") for item in result)


@pytest.mark.asyncio
async def test_retrieval_failure_keeps_safe_query_diagnostics():
    class BrokenRetriever:
        async def retrieve(self, query, request):
            raise RetrievalError("search_provider_unavailable")

    engine = ResearchEngine(FakeModel(), BrokenRetriever())
    with pytest.raises(InsufficientEvidence) as caught:
        _ = [event async for event in engine.run_research(
            ResearchRequest("diagnostic", "x", ("web",), ResearchLimits(reflection_rounds=0))
        )]

    assert caught.value.reason_code == "search_provider_unavailable"
    assert caught.value.diagnostics == {
        "attempted_queries": 2,
        "successful_queries": 0,
        "raw_sources": 0,
        "accepted_sources": 0,
        "failure_counts": {"search_provider_unavailable": 2},
    }


@pytest.mark.asyncio
async def test_retrieval_stops_promptly_when_research_is_cancelled():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class HangingRetriever:
        async def retrieve(self, query, request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    stop = asyncio.Event()
    engine = ResearchEngine(FakeModel(), HangingRetriever())
    task = asyncio.create_task(engine._retrieve(["x"], ResearchRequest("cancel-retrieve", "x", ("web",), ResearchLimits(), stop)))
    await started.wait()
    stop.set()

    with pytest.raises(ResearchCancelled):
        await asyncio.wait_for(task, .2)
    assert cancelled.is_set()


def test_merge_sources_never_exceeds_the_research_limit() -> None:
    existing = [Source(f"old-{i}", i, "web", f"https://old{i}.example/item", None, f"Old {i}", "body" * 100, None, "n", .9) for i in range(2)]
    new = [Source("new", 0, "web", "https://new.example/item", None, "New", "body" * 100, None, "n", .9)]
    request = ResearchRequest("j", "x", ("web",), ResearchLimits(max_sources=2))
    assert len(ResearchEngine._merge_sources(existing, new, request)) == 2


def test_merge_sources_reserves_one_result_per_search_intent() -> None:
    sources = [
        Source("company", 0, "web", "https://company.example/job", None, "Company", "company body" * 100, None, "n", .6, metadata={"query_index": 0}),
        Source("requirements", 0, "web", "https://requirements.example/jd", None, "Requirements", "requirements body" * 100, None, "n", .5, metadata={"query_index": 1}),
        Source("channel", 0, "web", "https://channel.example/apply", None, "Channel", "channel body" * 100, None, "n", .4, metadata={"query_index": 2}),
        Source("generic", 0, "web", "https://generic.example/trend", None, "Generic", "generic body" * 100, None, "n", .99, metadata={"query_index": 0}),
    ]
    request = ResearchRequest("balanced", "AI Agent 秋招", ("web",), ResearchLimits(max_sources=3))
    result = ResearchEngine._merge_sources([], sources, request)
    assert {item.id for item in result} == {"generic", "requirements", "channel"}


def test_merge_sources_deduplicates_identical_content_from_different_urls() -> None:
    sources = [
        Source("first", 0, "web", "https://first.example/article", None, "First", "same body" * 100, None, "n", .9, "same-hash", {"query_index": 0}),
        Source("mirror", 0, "web", "https://mirror.example/article", None, "Mirror", "same body" * 100, None, "n", .8, "same-hash", {"query_index": 1}),
    ]

    result = ResearchEngine._merge_sources([], sources, ResearchRequest("dedupe", "x", ("web",), ResearchLimits()))

    assert [item.id for item in result] == ["first"]


def test_relevant_excerpt_prefers_topic_paragraphs_and_bounds_model_input() -> None:
    noise = "网站导航与无关广告。" * 1000
    requirements = "岗位要求：熟悉 Python、LLM 应用开发和 Agent 工作流。"
    channel = "投递渠道：请通过公司招聘官网提交简历。"
    source = Source("s", 1, "web", "https://example.com/job", None, "AI Agent 招聘", f"{noise}\n\n{requirements}\n\n{channel}\n\n{noise}", None, "n", .8, metadata={"query": "AI Agent 岗位要求 投递渠道"})

    excerpt = relevant_excerpt(source, "AI Agent 秋招", ("在招公司", "岗位要求", "投递渠道"), max_chars=1200)

    assert requirements in excerpt and channel in excerpt
    assert len(excerpt) <= 1200


def test_live_research_fallback_distill_only_extracts_relevant_source_text() -> None:
    source = Source(
        "fallback-source", 1, "web", "https://example.com/job", None, "Example AI 招聘",
        "网站导航。\n\n在招公司：示例科技正在招聘 AI Agent 开发工程师。"
        "岗位要求：熟悉 Python、RAG 与 Agent 工作流。"
        "投递渠道：通过公司招聘官网提交简历。\n\n无关广告。",
        None, "n", .8, metadata={"query": "AI Agent 在招公司 岗位要求 投递渠道"},
    )

    evidence = LiveResearchModel.fallback_distill(
        source, "AI Agent 秋招", ("在招公司", "岗位要求", "投递渠道")
    )

    assert evidence
    assert all(item.source_id == source.id for item in evidence)
    assert all(item.text in source.content for item in evidence)
    assert any("投递渠道" in item.text for item in evidence)


@pytest.mark.asyncio
async def test_live_research_json_calls_bound_model_output() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.requests = []

        async def complete(self, request):
            self.requests.append(request)
            return SimpleNamespace(message='{"evidence": []}')

    gateway = Gateway()
    model = LiveResearchModel(gateway)
    source = Source("s", 1, "web", "https://example.com", None, "Job", "AI Agent job requirements " * 100, None, "n", .8)

    await model.distill(source, "AI Agent 秋招", ("岗位要求",))

    assert gateway.requests[0].max_tokens == 1200


@pytest.mark.asyncio
async def test_live_research_plan_prompt_forbids_scope_expansion() -> None:
    class Gateway:
        def __init__(self) -> None: self.requests = []
        async def complete(self, request):
            self.requests.append(request)
            return SimpleNamespace(message='{"title":"主动回忆","sections":["为什么有效","如何应用"],"queries":["主动回忆 为什么有效","主动回忆 如何应用"]}')

    gateway = Gateway()
    await LiveResearchModel(gateway).plan("研究主动回忆为什么有效，以及如何应用", ResearchLimits())

    prompt = gateway.requests[0].messages[0]["content"]
    assert "Do not expand the scope" in prompt
    assert "systematic review" in prompt


@pytest.mark.asyncio
async def test_live_research_write_normalizes_shorthand_source_markers() -> None:
    class Gateway:
        async def complete(self, request):
            return SimpleNamespace(message="岗位事实 [[source_exacthash]]")

    evidence = Evidence("e", "source_exacthash", "岗位事实", None, .9)
    body, _ = await LiveResearchModel(Gateway()).write("岗位要求", "要求", [evidence], "")

    assert "[[source:source_exacthash]]" in body
    assert "[[source_exacthash]]" not in body


@pytest.mark.asyncio
async def test_audit_timeout_can_only_fallback_for_fully_cited_planned_sections() -> None:
    class AuditTimeout(FakeModel):
        async def plan(self, topic, limits):
            return ResearchPlan(topic, ("在招公司", "岗位要求", "投递渠道"), (topic,))

        async def audit(self, topic, plan, report):
            raise TimeoutError

    events = [event async for event in ResearchEngine(AuditTimeout(), FakeRetriever()).run_research(
        ResearchRequest("audit-timeout", "调研 AI Agent 秋招：在招公司、岗位要求与投递渠道", ("web",), ResearchLimits(reflection_rounds=0))
    )]

    assert any(event.type == "report" for event in events)


def test_deterministic_topic_audit_rejects_an_uncited_section() -> None:
    plan = ResearchPlan("x", ("在招公司", "岗位要求", "投递渠道"), ("x",))
    sections = [
        SimpleNamespace(heading="在招公司"),
        SimpleNamespace(heading="岗位要求"),
        SimpleNamespace(heading="投递渠道"),
    ]
    source = Source("source_x", 1, "web", "https://example.com", None, "x", "body", None, "n", .8)

    assert not ResearchEngine._deterministic_topic_audit(
        "调研：在招公司、岗位要求与投递渠道",
        plan,
        sections,
        ["[[source:source_x]]", "没有引用", "[[source:source_x]]"],
        [source],
    )


def test_fallback_curation_assigns_evidence_to_the_matching_section() -> None:
    company = Evidence("company", "s1", "在招公司：示例科技正在招聘 AI Agent 工程师。", None, .8)
    requirement = Evidence("requirement", "s2", "岗位要求：熟悉 Python 与 RAG。", None, .8)
    channel = Evidence("channel", "s3", "投递渠道：通过公司招聘官网提交简历。", None, .8)
    plan = ResearchPlan("x", ("在招公司", "岗位要求", "投递渠道"), ("x",))

    sections = ResearchEngine._fallback_curated_sections(plan, [company, requirement, channel])

    assert sections[0][2] == (company.id,)
    assert sections[1][2] == (requirement.id,)
    assert sections[2][2] == (channel.id,)


@pytest.mark.asyncio
async def test_distill_skips_a_source_that_exceeds_its_time_budget() -> None:
    class Slow(FakeModel):
        async def distill(self, source, topic, sections):
            await asyncio.sleep(1)

    engine = ResearchEngine(Slow(), FakeRetriever())
    engine.DISTILL_TIMEOUT_SECONDS = .01
    source = Source("slow", 1, "web", "https://example.com", None, "Slow", "body" * 100, None, "n", .8)
    request = ResearchRequest("bounded", "AI Agent 秋招", ("web",), ResearchLimits(reflection_rounds=0))

    assert await engine._distill([source], request, ResearchPlan("x", ("岗位要求",), ("x",))) == []


@pytest.mark.asyncio
async def test_distill_uses_relevant_fallback_when_model_returns_no_evidence() -> None:
    class Empty(FakeModel):
        async def distill(self, source, topic, sections): return []
        fallback_distill = staticmethod(LiveResearchModel.fallback_distill)

    source = Source(
        "fallback-empty", 1, "local_note", None, "learning.md", "主动回忆",
        "主动回忆有助于长期保持。高等数学复习时应合上资料写出定义与步骤。" * 20,
        None, "n", .8,
    )
    request = ResearchRequest("empty-fallback", "主动回忆 高等数学复习", ("local_note",), ResearchLimits(reflection_rounds=0))

    evidence = await ResearchEngine(Empty(), FakeRetriever())._distill(
        [source], request, ResearchPlan("主动回忆", ("为什么有效", "如何应用"), ("主动回忆",))
    )

    assert evidence
    assert all(item.source_id == source.id for item in evidence)


@pytest.mark.asyncio
async def test_curate_timeout_falls_back_to_all_planned_sections() -> None:
    class SlowCurate(FakeModel):
        async def curate(self, plan, evidence):
            await asyncio.sleep(1)

    engine = ResearchEngine(SlowCurate(), FakeRetriever())
    engine.MODEL_STAGE_TIMEOUT_SECONDS = .01
    events = [event async for event in engine.run_research(
        ResearchRequest("curate-timeout", "AI Agent 秋招", ("web",), ResearchLimits(reflection_rounds=0))
    )]

    assert len([event for event in events if event.type == "section"]) == 2


def test_fallback_plan_turns_explicit_deliverables_into_sections() -> None:
    plan = ResearchEngine._fallback_plan(
        "调研 AI Agent 开发岗秋招：在招公司、岗位要求与投递渠道",
        max_sections=6,
        max_queries=12,
    )

    assert plan.sections[:3] == ("在招公司", "岗位要求", "投递渠道")
    assert all(any(section in query for query in plan.queries) for section in plan.sections)
    assert any("官方 原始来源" in query for query in plan.queries)
    assert any("2026" in query for query in plan.queries)


@pytest.mark.asyncio
async def test_url_safety_rejects_private_and_loopback_hosts(monkeypatch) -> None:
    async def addresses(host: str): return ["127.0.0.1"] if host == "localhost" else ["93.184.216.34"]
    assert await validate_public_url("https://example.com/path", resolver=addresses) == "https://example.com/path"
    with pytest.raises(ValueError, match="private"):
        await validate_public_url("http://localhost/admin", resolver=addresses)
    with pytest.raises(ValueError, match="scheme"):
        await validate_public_url("file:///etc/passwd", resolver=addresses)
