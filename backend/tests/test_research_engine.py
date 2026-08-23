from __future__ import annotations

import asyncio

import pytest

from app.research.engine import InsufficientEvidence, ResearchEngine, UnknownCitation
from app.research.models import Evidence, ResearchLimits, ResearchPlan, ResearchRequest, Source
from app.research.retriever import filter_sources, validate_public_url


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
async def test_zero_evidence_and_unknown_citation_cannot_finalize() -> None:
    class Empty(FakeModel):
        async def distill(self, *args): return []

    with pytest.raises(InsufficientEvidence):
        _ = [event async for event in ResearchEngine(Empty(), FakeRetriever()).run_research(ResearchRequest("j", "x", ("web",), ResearchLimits()))]
    with pytest.raises(UnknownCitation):
        _ = [event async for event in ResearchEngine(FakeModel(bad_citation=True), FakeRetriever()).run_research(ResearchRequest("j2", "x", ("web",), ResearchLimits(reflection_rounds=0)))]


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
async def test_url_safety_rejects_private_and_loopback_hosts(monkeypatch) -> None:
    async def addresses(host: str): return ["127.0.0.1"] if host == "localhost" else ["93.184.216.34"]
    assert await validate_public_url("https://example.com/path", resolver=addresses) == "https://example.com/path"
    with pytest.raises(ValueError, match="private"):
        await validate_public_url("http://localhost/admin", resolver=addresses)
    with pytest.raises(ValueError, match="scheme"):
        await validate_public_url("file:///etc/passwd", resolver=addresses)

