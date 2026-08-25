from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import date
from typing import AsyncIterator

from .citations import CITATION, render_citations
from .models import CuratedSection, Evidence, ResearchEvent, ResearchPlan, ResearchRequest, Source
from .retriever import filter_sources


class InsufficientEvidence(RuntimeError): pass
class UnknownCitation(RuntimeError): pass
class TopicCoverageError(RuntimeError): pass
class ResearchCancelled(RuntimeError): pass


class ResearchEngine:
    DISTILL_TIMEOUT_SECONDS = 20
    MODEL_STAGE_TIMEOUT_SECONDS = 45

    def __init__(self, model, retriever) -> None:
        self.model = model
        self.retriever = retriever

    async def run_research(self, request: ResearchRequest) -> AsyncIterator[ResearchEvent]:
        self._cancel(request)
        yield ResearchEvent("phase", "planning", {"detail": "正在规划研究范围"})
        if request.recovered_plan:plan=request.recovered_plan
        else:
            try:
                plan = await asyncio.wait_for(
                    self.model.plan(request.topic, request.limits),
                    timeout=self.MODEL_STAGE_TIMEOUT_SECONDS,
                )
                plan = self._valid_plan(plan, request)
            except Exception:
                plan = self._fallback_plan(request.topic, request.limits.max_sections, request.limits.max_queries)
        yield ResearchEvent("plan", "planning", {"title": plan.title, "sections": list(plan.sections), "queries": list(plan.queries)})

        sources: list[Source] = list(request.recovered_sources)
        used_queries = list(plan.queries)
        yield ResearchEvent("phase", "retrieving", {"detail": "正在检索来源"})
        sources = self._merge_sources(sources, await self._retrieve(used_queries, request), request)
        yield ResearchEvent("sources", "retrieving", {"count": len(sources), "items": sources})

        yield ResearchEvent("phase", "distilling", {"detail": "正在逐条提炼证据"})
        recovered_ids={item.id for item in request.recovered_sources}
        evidence = [*request.recovered_evidence, *await self._distill([item for item in sources if item.id not in recovered_ids], request, plan)]
        if not evidence: raise InsufficientEvidence("insufficient_evidence")
        yield ResearchEvent("evidence", "distilling", {"count": len(evidence), "items": evidence})

        if request.limits.reflection_rounds > 0 and len(sources) < request.limits.max_sources:
            yield ResearchEvent("phase", "reflecting", {"detail": "正在检查证据缺口"})
            try:
                queries = await asyncio.wait_for(
                    self.model.reflect(request.topic, plan, evidence, tuple(used_queries)),
                    timeout=self.MODEL_STAGE_TIMEOUT_SECONDS,
                )
            except Exception:
                queries = ()
            normalized = []
            seen = {self._query_key(item) for item in used_queries}
            for query in queries[:3]:
                query = str(query).strip()
                if query and self._query_key(query) not in seen:
                    seen.add(self._query_key(query)); normalized.append(query)
            if normalized:
                extra = await self._retrieve(normalized, request)
                combined = self._merge_sources(sources, extra, request)
                new_ids = {item.id for item in combined} - {item.id for item in sources}
                evidence.extend(await self._distill([item for item in combined if item.id in new_ids], request, plan))
                sources = combined; used_queries.extend(normalized)
                yield ResearchEvent("sources", "reflecting", {"count": len(sources), "items": sources})
                yield ResearchEvent("evidence", "reflecting", {"count": len(evidence), "items": evidence})
        evidence = evidence[:request.limits.max_evidence]

        yield ResearchEvent("phase", "curating", {"detail": "正在组织证据大纲"})
        try:
            raw_sections = await asyncio.wait_for(
                self.model.curate(plan, evidence),
                timeout=self.MODEL_STAGE_TIMEOUT_SECONDS,
            )
        except Exception:
            raw_sections = self._fallback_curated_sections(plan, evidence)
        valid_ids = {item.id for item in evidence}
        curated = {}
        unmatched = []
        for raw in raw_sections[:request.limits.max_sections]:
            heading, thesis, ids = raw
            ids = tuple(dict.fromkeys(item for item in ids if item in valid_ids))
            if ids:
                key = self._heading_key(str(heading))
                value = (str(thesis), ids)
                if key in {self._heading_key(item) for item in plan.sections}: curated[key] = value
                else: unmatched.append(value)
        def build_sections():
            result = []
            extras = list(unmatched)
            for index, heading in enumerate(plan.sections, 1):
                match = next((value for key,value in curated.items() if key == self._heading_key(heading)), None)
                if match is not None:
                    result.append(CuratedSection(index, heading, match[0], match[1]))
                elif index not in request.completed_sections and extras:
                    thesis, ids = extras.pop(0)
                    result.append(CuratedSection(index, heading, thesis, ids))
            return result
        sections = build_sections()
        if not sections and not request.completed_sections:
            raw_sections = self._fallback_curated_sections(plan, evidence)
            curated = {self._heading_key(heading):(str(thesis),tuple(ids)) for heading,thesis,ids in raw_sections}
            unmatched = []
            sections = build_sections()
        by_ordinal={item.ordinal:item for item in sections}
        for ordinal,saved in request.completed_sections.items():
            by_ordinal[ordinal]=CuratedSection(ordinal,saved.get("heading",f"Section {ordinal}"),saved.get("summary",""),())
        sections=[by_ordinal[item] for item in sorted(by_ordinal)]
        missing = [heading for index,heading in enumerate(plan.sections,1) if index not in by_ordinal]
        if missing:
            raise TopicCoverageError(f"missing planned sections: {', '.join(missing)}")

        yield ResearchEvent("phase", "writing", {"detail": "正在撰写报告"})
        evidence_by_id = {item.id: item for item in evidence}
        bodies, summaries = [], []
        for section in sections:
            self._cancel(request)
            saved=request.completed_sections.get(section.ordinal)
            if saved:
                body,summary=saved["markdown"],saved["summary"]
                bodies.append(body);summaries.append(summary)
                continue
            section_evidence = [evidence_by_id[item] for item in section.evidence_ids]
            try:
                body, summary = await asyncio.wait_for(
                    self.model.write(section.heading, section.thesis, section_evidence, summaries[-1] if summaries else ""),
                    timeout=self.MODEL_STAGE_TIMEOUT_SECONDS,
                )
            except Exception:
                body = f"## {section.heading}\n\n" + "\n".join(
                    f"- {item.text} [[source:{item.source_id}]]" for item in section_evidence
                )
                summary = section.thesis[:240]
            bodies.append(body); summaries.append(summary)
            yield ResearchEvent("section", "writing", {"ordinal": section.ordinal, "heading": section.heading, "markdown": body, "summary": summary})

        yield ResearchEvent("phase", "summarizing", {"detail": "正在生成摘要"})
        try:
            tldr, points = await asyncio.wait_for(
                self.model.summarize(bodies),
                timeout=self.MODEL_STAGE_TIMEOUT_SECONDS,
            )
        except Exception:
            tldr, points = "研究结论详见各章节及其来源标注。", ()
        if "[[source:" not in tldr: tldr = "以下结论均基于正文中标注的来源证据。"
        points = tuple(item for item in points if "[[source:" in item)
        self._cancel(request)
        raw = f"# {plan.title}\n\n> {tldr}\n\n## 核心要点\n\n" + "".join(f"- {item}\n" for item in points) + "\n" + "\n\n".join(bodies)
        try: body = render_citations(raw, sources)
        except KeyError as exc: raise UnknownCitation(f"unknown source citation: {exc.args[0]}") from exc
        references = "\n\n## 研究限制\n\n结论仅覆盖已成功检索和提炼的来源。\n\n## 参考来源\n\n" + "".join(
            f"{source.ordinal}. [{source.title}]({source.canonical_url})\n" if source.canonical_url else f"{source.ordinal}. {source.title}（{source.locator}）\n" for source in sources
        )
        report = body + references
        if not sections or not sources or not evidence: raise InsufficientEvidence("insufficient_evidence")
        yield ResearchEvent("phase", "finalizing", {"detail": "正在校验引用"})
        try:
            passed, missing_requirements = await asyncio.wait_for(
                self.model.audit(request.topic, plan, report),
                timeout=self.MODEL_STAGE_TIMEOUT_SECONDS,
            )
        except Exception:
            passed = self._deterministic_topic_audit(request.topic, plan, sections, bodies, sources)
            missing_requirements = () if passed else ("topic requirements",)
        if not passed:
            detail = ", ".join(str(item) for item in missing_requirements if str(item).strip()) or "topic requirements"
            raise TopicCoverageError(f"report does not cover: {detail}")
        yield ResearchEvent("report", "completed", {"title": plan.title, "markdown": report, "source_count": len(sources), "evidence_count": len(evidence)})
        yield ResearchEvent("phase", "completed", {"detail": "研究完成"})

    async def _retrieve(self, queries: list[str], request: ResearchRequest) -> list[Source]:
        semaphore = asyncio.Semaphore(4)
        async def one(query_index: int, query: str):
            async with semaphore:
                self._cancel(request)
                try:
                    items = await self.retriever.retrieve(query, request)
                    return [replace(item, metadata={**item.metadata, "query": query, "query_index": query_index}) for item in items]
                except Exception: return []
        results = await asyncio.gather(*(one(index,query) for index,query in enumerate(queries)))
        return [item for batch in results for item in batch]

    async def _distill(self, sources: list[Source], request: ResearchRequest, plan: ResearchPlan) -> list[Evidence]:
        semaphore = asyncio.Semaphore(4)
        async def one(source: Source):
            async with semaphore:
                self._cancel(request)
                try:
                    raw = await asyncio.wait_for(
                        self.model.distill(source, request.topic, plan.sections),
                        timeout=self.DISTILL_TIMEOUT_SECONDS,
                    )
                except Exception:
                    fallback = getattr(self.model, "fallback_distill", None)
                    raw = fallback(source, request.topic, plan.sections) if fallback else []
                if not raw:
                    fallback = getattr(self.model, "fallback_distill", None)
                    raw = fallback(source, request.topic, plan.sections) if fallback else []
                return [replace(item, source_id=source.id) for item in raw[:6] if 0 <= item.relevance <= 1 and item.relevance >= .25 and item.text.strip()]
        return [item for batch in await asyncio.gather(*(one(source) for source in sources)) for item in batch]

    @staticmethod
    def _merge_sources(existing: list[Source], new: list[Source], request: ResearchRequest) -> list[Source]:
        accepted=list(existing[:request.limits.max_sources])
        keys={(item.kind,item.canonical_url or item.locator or item.content_hash) for item in accepted}
        content_hashes={item.content_hash for item in accepted if item.content_hash}
        ids={item.id for item in accepted};next_ordinal=max((item.ordinal for item in accepted),default=0)+1
        filtered = filter_sources(new,min_chars=request.limits.min_source_chars,max_sources=max(len(new),request.limits.max_sources))
        reserved = {}
        for source in filtered:
            query_index = source.metadata.get("query_index")
            if isinstance(query_index,int) and query_index not in reserved:
                reserved[query_index] = source
        ordered = [reserved[index] for index in sorted(reserved)]
        ordered.extend(source for source in filtered if source.id not in {item.id for item in ordered})
        for source in ordered:
            if len(accepted)>=request.limits.max_sources:break
            key=(source.kind,source.canonical_url or source.locator or source.content_hash)
            if source.id in ids or key in keys or (source.content_hash and source.content_hash in content_hashes):continue
            accepted.append(replace(source,ordinal=next_ordinal));ids.add(source.id);keys.add(key)
            if source.content_hash:content_hashes.add(source.content_hash)
            next_ordinal+=1
        return accepted

    @staticmethod
    def _valid_plan(plan: ResearchPlan, request: ResearchRequest) -> ResearchPlan:
        if not plan.title.strip() or not 2 <= len(plan.sections) <= request.limits.max_sections: raise ValueError("invalid plan")
        return ResearchPlan(plan.title.strip(), tuple(plan.sections[:request.limits.max_sections]), tuple(dict.fromkeys(q.strip() for q in plan.queries if q.strip()))[:request.limits.max_queries])

    @staticmethod
    def _fallback_plan(topic: str, max_sections: int, max_queries: int) -> ResearchPlan:
        explicit = ResearchEngine._explicit_deliverables(topic)
        sections = explicit[:max_sections] if len(explicit) >= 2 else ("背景与定义", "核心事实与证据", "争议与限制", "结论与建议")[:max_sections]
        year = date.today().year
        queries = tuple(dict.fromkeys([
            topic,
            *(f"{topic} {section} {year}" for section in sections),
            *(f"{topic} {section} 官方 原始来源" for section in sections),
        ]))[:max_queries]
        return ResearchPlan(topic[:120], sections, queries)

    @staticmethod
    def _explicit_deliverables(topic: str) -> tuple[str, ...]:
        normalized = re.sub(r"\s+", " ", topic)
        _, separator, deliverables = normalized.rpartition("：")
        if not separator:
            _, separator, deliverables = normalized.rpartition(":")
        if not separator:
            return ()
        return tuple(
            item.strip(" 。.!！?？")
            for item in re.split(r"[、,，;；]|(?<=[\u4e00-\u9fff])(?:以及|与|和|及)(?=[\u4e00-\u9fff])", deliverables)
            if item.strip(" 。.!！?？")
        )

    @staticmethod
    def _fallback_curated_sections(plan, evidence):
        section_terms = []
        for heading in plan.sections:
            terms = {heading.lower()}
            terms.update(
                run[index:index + 2]
                for run in re.findall(r"[\u4e00-\u9fff]{4,}", heading)
                for index in range(len(run) - 1)
            )
            section_terms.append(terms)
        assigned = [[] for _ in plan.sections]
        for item in evidence:
            scores = [sum(term in item.text.lower() for term in terms) for terms in section_terms]
            best = max(scores, default=0)
            if best > 0:
                assigned[scores.index(best)].append(item)
        result = []
        for index, heading in enumerate(plan.sections):
            matched = sorted(assigned[index], key=lambda item: (-item.relevance, item.id))[:12]
            if not matched:
                matched = sorted(evidence, key=lambda item: (-item.relevance, item.id))[:3]
            result.append((heading, heading, tuple(item.id for item in matched)))
        return result

    @classmethod
    def _deterministic_topic_audit(cls, topic, plan, sections, bodies, sources) -> bool:
        deliverables = cls._explicit_deliverables(topic)
        if len(deliverables) < 2 or len(sections) != len(plan.sections) or len(bodies) != len(sections):
            return False
        heading_keys = [cls._heading_key(section.heading) for section in sections]
        if any(not any(cls._heading_key(item) in heading for heading in heading_keys) for item in deliverables):
            return False
        valid_ids = {source.id for source in sources}
        aliases = {source.id.removeprefix("source_") for source in sources}
        for body in bodies:
            cited = {match.group(1) for match in CITATION.finditer(body)}
            if not cited or not cited <= valid_ids | aliases:
                return False
        return True

    @staticmethod
    def _query_key(query: str) -> str: return re.sub(r"\s+", "", query).lower()

    @staticmethod
    def _heading_key(heading: str) -> str: return re.sub(r"[^\w\u4e00-\u9fff]+", "", heading).lower()

    @staticmethod
    def _cancel(request: ResearchRequest) -> None:
        if request.cancel_event is not None and request.cancel_event.is_set(): raise ResearchCancelled("research cancelled")
