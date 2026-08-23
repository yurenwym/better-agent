from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from typing import AsyncIterator

from .citations import render_citations
from .models import CuratedSection, Evidence, ResearchEvent, ResearchPlan, ResearchRequest, Source
from .retriever import filter_sources


class InsufficientEvidence(RuntimeError): pass
class UnknownCitation(RuntimeError): pass
class ResearchCancelled(RuntimeError): pass


class ResearchEngine:
    def __init__(self, model, retriever) -> None:
        self.model = model
        self.retriever = retriever

    async def run_research(self, request: ResearchRequest) -> AsyncIterator[ResearchEvent]:
        self._cancel(request)
        yield ResearchEvent("phase", "planning", {"detail": "正在规划研究范围"})
        try:
            plan = await self.model.plan(request.topic, request.limits)
            plan = self._valid_plan(plan, request)
        except Exception:
            plan = self._fallback_plan(request.topic, request.limits.max_sections, request.limits.max_queries)
        yield ResearchEvent("plan", "planning", {"title": plan.title, "sections": list(plan.sections), "queries": list(plan.queries)})

        sources: list[Source] = []
        used_queries = list(plan.queries)
        yield ResearchEvent("phase", "retrieving", {"detail": "正在检索来源"})
        sources.extend(await self._retrieve(used_queries, request))
        sources = filter_sources(sources, min_chars=request.limits.min_source_chars, max_sources=request.limits.max_sources)
        yield ResearchEvent("sources", "retrieving", {"count": len(sources), "items": sources})

        yield ResearchEvent("phase", "distilling", {"detail": "正在逐条提炼证据"})
        evidence = await self._distill(sources, request, plan)
        if not evidence: raise InsufficientEvidence("insufficient_evidence")
        yield ResearchEvent("evidence", "distilling", {"count": len(evidence), "items": evidence})

        if request.limits.reflection_rounds > 0:
            yield ResearchEvent("phase", "reflecting", {"detail": "正在检查证据缺口"})
            queries = await self.model.reflect(request.topic, plan, evidence, tuple(used_queries))
            normalized = []
            seen = {self._query_key(item) for item in used_queries}
            for query in queries[:3]:
                query = str(query).strip()
                if query and self._query_key(query) not in seen:
                    seen.add(self._query_key(query)); normalized.append(query)
            if normalized:
                extra = await self._retrieve(normalized, request)
                combined = filter_sources([*sources, *extra], min_chars=request.limits.min_source_chars, max_sources=request.limits.max_sources)
                new_ids = {item.id for item in combined} - {item.id for item in sources}
                evidence.extend(await self._distill([item for item in combined if item.id in new_ids], request, plan))
                sources = combined; used_queries.extend(normalized)
                yield ResearchEvent("sources", "reflecting", {"count": len(sources), "items": sources})
                yield ResearchEvent("evidence", "reflecting", {"count": len(evidence), "items": evidence})
        evidence = evidence[:request.limits.max_evidence]

        yield ResearchEvent("phase", "curating", {"detail": "正在组织证据大纲"})
        raw_sections = await self.model.curate(plan, evidence)
        valid_ids = {item.id for item in evidence}
        sections = []
        for index, raw in enumerate(raw_sections[:request.limits.max_sections], 1):
            heading, thesis, ids = raw
            ids = tuple(dict.fromkeys(item for item in ids if item in valid_ids))
            if ids: sections.append(CuratedSection(index, str(heading), str(thesis), ids))
        if not sections:
            sections = [CuratedSection(1, plan.sections[0], "基于现有证据", tuple(item.id for item in evidence))]

        yield ResearchEvent("phase", "writing", {"detail": "正在撰写报告"})
        evidence_by_id = {item.id: item for item in evidence}
        bodies, summaries = [], []
        for section in sections:
            self._cancel(request)
            body, summary = await self.model.write(section.heading, section.thesis, [evidence_by_id[item] for item in section.evidence_ids], summaries[-1] if summaries else "")
            bodies.append(body); summaries.append(summary)
            yield ResearchEvent("section", "writing", {"ordinal": section.ordinal, "heading": section.heading, "markdown": body, "summary": summary})

        yield ResearchEvent("phase", "summarizing", {"detail": "正在生成摘要"})
        tldr, points = await self.model.summarize(bodies)
        raw = f"# {plan.title}\n\n> {tldr}\n\n## 核心要点\n\n" + "".join(f"- {item}\n" for item in points) + "\n" + "\n\n".join(bodies)
        try: body = render_citations(raw, sources)
        except KeyError as exc: raise UnknownCitation(f"unknown source citation: {exc.args[0]}") from exc
        references = "\n\n## 研究限制\n\n结论仅覆盖已成功检索和提炼的来源。\n\n## 参考来源\n\n" + "".join(
            f"{source.ordinal}. [{source.title}]({source.canonical_url})\n" if source.canonical_url else f"{source.ordinal}. {source.title}（{source.locator}）\n" for source in sources
        )
        report = body + references
        if not sections or not sources or not evidence: raise InsufficientEvidence("insufficient_evidence")
        yield ResearchEvent("phase", "finalizing", {"detail": "正在校验引用"})
        yield ResearchEvent("report", "completed", {"title": plan.title, "markdown": report, "source_count": len(sources), "evidence_count": len(evidence)})
        yield ResearchEvent("phase", "completed", {"detail": "研究完成"})

    async def _retrieve(self, queries: list[str], request: ResearchRequest) -> list[Source]:
        semaphore = asyncio.Semaphore(4)
        async def one(query: str):
            async with semaphore:
                self._cancel(request)
                try: return await self.retriever.retrieve(query, request)
                except Exception: return []
        results = await asyncio.gather(*(one(query) for query in queries))
        return [item for batch in results for item in batch]

    async def _distill(self, sources: list[Source], request: ResearchRequest, plan: ResearchPlan) -> list[Evidence]:
        semaphore = asyncio.Semaphore(4)
        async def one(source: Source):
            async with semaphore:
                self._cancel(request)
                try: raw = await self.model.distill(source, request.topic, plan.sections)
                except Exception: return []
                return [replace(item, source_id=source.id) for item in raw[:6] if 0 <= item.relevance <= 1 and item.relevance >= .25 and item.text.strip()]
        return [item for batch in await asyncio.gather(*(one(source) for source in sources)) for item in batch]

    @staticmethod
    def _valid_plan(plan: ResearchPlan, request: ResearchRequest) -> ResearchPlan:
        if not plan.title.strip() or not 2 <= len(plan.sections) <= request.limits.max_sections: raise ValueError("invalid plan")
        return ResearchPlan(plan.title.strip(), tuple(plan.sections[:request.limits.max_sections]), tuple(dict.fromkeys(q.strip() for q in plan.queries if q.strip()))[:request.limits.max_queries])

    @staticmethod
    def _fallback_plan(topic: str, max_sections: int, max_queries: int) -> ResearchPlan:
        sections = ("背景与定义", "核心事实与证据", "争议与限制", "结论与建议")[:max_sections]
        queries = tuple(dict.fromkeys([topic, *(f"{topic} {section}" for section in sections)]))[:max_queries]
        return ResearchPlan(topic[:120], sections, queries)

    @staticmethod
    def _query_key(query: str) -> str: return re.sub(r"\s+", "", query).lower()

    @staticmethod
    def _cancel(request: ResearchRequest) -> None:
        if request.cancel_event is not None and request.cancel_event.is_set(): raise ResearchCancelled("research cancelled")
