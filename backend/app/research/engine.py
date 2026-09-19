from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import replace
from datetime import date
from typing import AsyncIterator

from .citations import CITATION, render_citations
from .models import CuratedSection, Evidence, ResearchEvent, ResearchPlan, ResearchRequest, Source
from .retriever import RetrievalError,filter_sources


class InsufficientEvidence(RuntimeError):
    def __init__(self,reason_code="insufficient_evidence",diagnostics=None,*,retryable=False):
        super().__init__(reason_code);self.reason_code=reason_code;self.diagnostics=diagnostics or {};self.retryable=retryable
class UnknownCitation(RuntimeError): pass
class TopicCoverageError(RuntimeError):
    reason_code = "topiccoverageerror"

    def __init__(self, message: str, missing_requirements=(), repair_error: str | None = None) -> None:
        super().__init__(message)
        self.diagnostics = {
            "missing_requirements": [str(item)[:300] for item in missing_requirements if str(item).strip()][:12]
        }
        if repair_error:
            self.diagnostics["repair_error"] = repair_error
class ResearchCancelled(RuntimeError): pass


class ResearchEngine:
    DISTILL_TIMEOUT_SECONDS = 90
    RETRIEVE_TIMEOUT_SECONDS = 40
    MODEL_STAGE_TIMEOUT_SECONDS = 90
    REPAIR_TIMEOUT_SECONDS = 90

    def __init__(self, model, retriever) -> None:
        self.model = model
        self.retriever = retriever

    @staticmethod
    def _raise_if_permanent_model_error(exc: Exception) -> None:
        kind = getattr(exc, "kind", None)
        if kind == "cancelled":
            raise ResearchCancelled("research cancelled") from exc
        if kind in {"authentication", "payment", "configuration", "budget"}:
            raise exc

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
            except Exception as exc:
                self._raise_if_permanent_model_error(exc)
                plan = self._fallback_plan(request.topic, request.limits.max_sections, request.limits.max_queries)
        yield ResearchEvent("plan", "planning", {"title": plan.title, "sections": list(plan.sections), "queries": list(plan.queries)})

        sources: list[Source] = list(request.recovered_sources)
        used_queries = list(plan.queries)
        yield ResearchEvent("phase", "retrieving", {"detail": "正在检索来源"})
        retrieved,diagnostics = await self._retrieve(used_queries, request)
        sources = self._merge_sources(sources, retrieved, request)
        diagnostics={**diagnostics,"accepted_sources":len(sources)}
        yield ResearchEvent("sources", "retrieving", {"count": len(sources), "items": sources,"diagnostics":diagnostics})

        yield ResearchEvent("phase", "distilling", {"detail": "正在逐条提炼证据"})
        recovered_ids={item.id for item in request.recovered_sources}
        evidence = [*request.recovered_evidence, *await self._distill([item for item in sources if item.id not in recovered_ids], request, plan)]
        if not evidence:
            failures=diagnostics.get("failure_counts",{})
            priority=("search_auth_failed","search_rate_limited","search_blocked","search_timeout","search_provider_unavailable","search_response_unparseable","search_results_irrelevant","search_results_rejected","search_no_results","search_request_failed")
            reason=next((item for item in priority if item in failures),next(iter(failures),"insufficient_evidence")) if not sources else "insufficient_evidence"
            if retrieved and not sources:reason="search_results_filtered"
            retryable=reason in {"search_timeout","search_rate_limited","search_blocked","search_provider_unavailable"}
            raise InsufficientEvidence(reason,diagnostics,retryable=retryable)
        yield ResearchEvent("evidence", "distilling", {"count": len(evidence), "items": evidence})

        from ..task_policy import initial_coverage_satisfied
        covered = initial_coverage_satisfied(plan, evidence, sources)
        skip_exploration = request.stop_condition == "coverage_satisfied" and covered
        yield ResearchEvent("policy_decision", "reflecting", {
            "stop_condition": request.stop_condition, "initial_coverage": covered,
            "initial_queries": len(used_queries), "initial_sources": len(sources),
            "optional_exploration_skipped": skip_exploration, "required_delivery_preserved": True,
        })
        if not skip_exploration and request.limits.reflection_rounds > 0 and len(sources) < request.limits.max_sources:
            yield ResearchEvent("phase", "reflecting", {"detail": "正在检查证据缺口"})
            try:
                queries = await asyncio.wait_for(
                    self.model.reflect(request.topic, plan, evidence, tuple(used_queries)),
                    timeout=self.MODEL_STAGE_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                self._raise_if_permanent_model_error(exc)
                queries = ()
            normalized = []
            seen = {self._query_key(item) for item in used_queries}
            for query in queries[:3]:
                query = str(query).strip()
                if query and self._query_key(query) not in seen:
                    seen.add(self._query_key(query)); normalized.append(query)
            normalized = normalized[:max(0, request.limits.max_queries - len(used_queries))]
            if normalized:
                extra,_ = await self._retrieve(normalized, request)
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
        except Exception as exc:
            self._raise_if_permanent_model_error(exc)
            raw_sections = self._fallback_curated_sections(plan, evidence, sources)
        valid_ids = {item.id for item in evidence}
        evidence_by_id_for_curation = {item.id:item for item in evidence}
        fallback_by_heading = {
            self._heading_key(heading): ids
            for heading, _, ids in self._fallback_curated_sections(plan, evidence, sources)
        }
        curated = {}
        unmatched = []
        for raw in raw_sections[:request.limits.max_sections]:
            heading, thesis, ids = raw
            ids = tuple(dict.fromkeys(item for item in ids if item in valid_ids))
            if ids:
                key = self._heading_key(str(heading))
                heading_terms = self._search_terms(str(heading))
                assigned_score = sum(
                    sum(term in evidence_by_id_for_curation[item].text.lower() for term in heading_terms)
                    for item in ids
                )
                fallback_ids = fallback_by_heading.get(key, ())
                fallback_score = sum(
                    sum(term in evidence_by_id_for_curation[item].text.lower() for term in heading_terms)
                    for item in fallback_ids
                )
                if fallback_score > assigned_score:
                    ids = fallback_ids
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
            raw_sections = self._fallback_curated_sections(plan, evidence, sources)
            curated = {self._heading_key(heading):(str(thesis),tuple(ids)) for heading,thesis,ids in raw_sections}
            unmatched = []
            sections = build_sections()
        by_ordinal={item.ordinal:item for item in sections}
        for ordinal,saved in request.completed_sections.items():
            by_ordinal[ordinal]=CuratedSection(ordinal,saved.get("heading",f"Section {ordinal}"),saved.get("summary",""),())
        sections=[by_ordinal[item] for item in sorted(by_ordinal)]
        missing = [heading for index,heading in enumerate(plan.sections,1) if index not in by_ordinal]
        if not sections:
            raise InsufficientEvidence("insufficient_evidence", {"missing_requirements": missing})

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
            except Exception as exc:
                self._raise_if_permanent_model_error(exc)
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
        except Exception as exc:
            self._raise_if_permanent_model_error(exc)
            tldr, points = "研究结论详见各章节及其来源标注。", ()
        known_citations={item.id for item in sources}|{item.id.removeprefix("source_") for item in sources}
        def has_valid_citations(value):
            markers=CITATION.findall(value)
            return bool(markers) and all(item in known_citations for item in markers)
        if not has_valid_citations(tldr): tldr = "以下结论均基于正文中标注的来源证据。"
        points = tuple(item for item in points if has_valid_citations(item))
        self._cancel(request)
        raw = f"# {plan.title}\n\n> {tldr}\n\n## 核心要点\n\n" + "".join(f"- {item}\n" for item in points) + "\n" + "\n\n".join(bodies)
        try: body = render_citations(raw, sources)
        except KeyError as exc: raise UnknownCitation(f"unknown source citation: {exc.args[0]}") from exc
        unresolved = ""
        if missing:
            unresolved = "\n\n## 未解决要求\n\n" + "".join(
                f"- {item}：当前来源与证据不足，未作为完整结论发布。\n" for item in missing
            )
        references = unresolved + "\n\n## 研究限制\n\n结论仅覆盖已成功检索和提炼的来源。\n\n## 参考来源\n\n" + "".join(
            f"{source.ordinal}. [{source.title}]({source.canonical_url})\n" if source.canonical_url else f"{source.ordinal}. {source.title}（{source.locator}）\n" for source in sources
        )
        report = body + references
        if not sections or not sources or not evidence: raise InsufficientEvidence("insufficient_evidence")
        yield ResearchEvent("phase", "finalizing", {"detail": "正在校验引用"})
        if missing:
            passed, missing_requirements = False, tuple(missing)
        else:
            try:
                passed, missing_requirements = await asyncio.wait_for(
                    self.model.audit(request.topic, plan, report),
                    timeout=self.MODEL_STAGE_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                self._raise_if_permanent_model_error(exc)
                passed = self._deterministic_topic_audit(request.topic, plan, sections, bodies, sources)
                missing_requirements = () if passed else ("topic requirements",)
        repair_traceability = []
        if not passed and not missing:
            repair = getattr(self.model, "repair", None)
            repair_error = None
            if repair is not None:
                try:
                    repair_requirements = tuple(missing_requirements)
                    evidence_context = self._repair_evidence(repair_requirements, evidence, sources)
                    supplement = await asyncio.wait_for(
                        repair(request.topic, plan, repair_requirements, evidence_context),
                        timeout=self.REPAIR_TIMEOUT_SECONDS,
                    )
                    if isinstance(supplement, str) and supplement.strip():
                        evidence_supplement = self._fallback_repair_supplement(repair_requirements, evidence_context)
                        traceable_supplement = supplement.strip()+"\n\n"+evidence_supplement
                        rendered_supplement = render_citations(traceable_supplement, sources)
                        report = report.replace("\n\n## 研究限制", "\n\n"+rendered_supplement+"\n\n## 研究限制", 1)
                        passed, missing_requirements = await asyncio.wait_for(
                            self.model.audit(request.topic, plan, report),
                            timeout=self.MODEL_STAGE_TIMEOUT_SECONDS,
                        )
                        repair_traceability = [(item, traceable_supplement) for item in repair_requirements]
                except asyncio.TimeoutError:
                    repair_error = "timeout"
                    supplement = self._fallback_repair_supplement(repair_requirements, evidence_context)
                    report = report.replace("\n\n## 研究限制", "\n\n"+render_citations(supplement, sources)+"\n\n## 研究限制", 1)
                    repair_traceability = [(item, supplement) for item in repair_requirements]
                    try:
                        passed, missing_requirements = await asyncio.wait_for(
                            self.model.audit(request.topic, plan, report),
                            timeout=self.MODEL_STAGE_TIMEOUT_SECONDS,
                        )
                    except Exception as exc:
                        self._raise_if_permanent_model_error(exc)
                        passed = False
                except KeyError:
                    repair_error = "unknowncitation"
                    passed = False
                except Exception as exc:
                    self._raise_if_permanent_model_error(exc)
                    repair_error = "failed"
                    passed = False
            detail = ", ".join(str(item) for item in missing_requirements if str(item).strip()) or "topic requirements"
            if not passed and not bodies:
                raise TopicCoverageError(f"report does not cover: {detail}", missing_requirements, repair_error)
        missing_requirements = tuple(dict.fromkeys(
            str(item).strip() for item in missing_requirements if str(item).strip()
        )) if not passed else ()
        traceability = self._traceability(
            plan, sections, bodies, summaries, evidence, sources, missing_requirements,
            repair_traceability,
        )
        unsupported = tuple(
            str(item["requirement"]) for item in traceability if not item["supported"]
        )
        if unsupported:
            missing_requirements = tuple(dict.fromkeys((*missing_requirements, *unsupported)))
            traceability = self._traceability(
                plan, sections, bodies, summaries, evidence, sources, missing_requirements,
                repair_traceability,
            )
        if missing_requirements and "## 未解决要求" not in report:
            report += "\n\n## 未解决要求\n\n" + "".join(
                f"- {item}：当前来源与证据不足，未作为完整结论发布。\n"
                for item in missing_requirements
            )
        completion_status = "PARTIAL" if missing_requirements else "COMPLETED"
        phase = "partial" if missing_requirements else "completed"
        yield ResearchEvent("report", phase, {
            "title": plan.title, "markdown": report, "source_count": len(sources),
            "evidence_count": len(evidence), "completion_status": completion_status,
            "missing_requirements": missing_requirements, "traceability": traceability,
        })
        yield ResearchEvent("phase", phase, {"detail": "研究部分完成" if missing_requirements else "研究完成"})

    async def _retrieve(self, queries: list[str], request: ResearchRequest) -> tuple[list[Source],dict]:
        semaphore = asyncio.Semaphore(4)
        async def one(query_index: int, query: str):
            async with semaphore:
                self._cancel(request)
                call=asyncio.create_task(self.retriever.retrieve(query, request))
                cancel_wait=asyncio.create_task(request.cancel_event.wait()) if request.cancel_event else None
                try:
                    waiting={call,*([] if cancel_wait is None else [cancel_wait])}
                    done,_=await asyncio.wait(waiting,timeout=self.RETRIEVE_TIMEOUT_SECONDS,return_when=asyncio.FIRST_COMPLETED)
                    if cancel_wait is not None and cancel_wait in done:
                        call.cancel()
                        await asyncio.gather(call,return_exceptions=True)
                        raise ResearchCancelled("research cancelled")
                    if call not in done:
                        call.cancel()
                        await asyncio.gather(call,return_exceptions=True)
                        raise RetrievalError("search_timeout",retryable=True)
                    items=call.result()
                    return [replace(item, metadata={**item.metadata, "query": query, "query_index": query_index}) for item in items],None
                except ResearchCancelled:raise
                except RetrievalError as exc:return [],exc
                except Exception:return [],RetrievalError("search_provider_unavailable",retryable=True)
                finally:
                    if cancel_wait is not None:
                        cancel_wait.cancel()
                        await asyncio.gather(cancel_wait,return_exceptions=True)
        results = await asyncio.gather(*(one(index,query) for index,query in enumerate(queries)))
        failures={};sources=[];successful=0
        for batch,error in results:
            sources.extend(batch)
            if error is None:successful+=1
            else:
                failures[error.reason_code]=failures.get(error.reason_code,0)+1
        return sources,{"attempted_queries":len(queries),"successful_queries":successful,"raw_sources":len(sources),"failure_counts":failures}

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
                except Exception as exc:
                    self._raise_if_permanent_model_error(exc)
                    fallback = getattr(self.model, "fallback_distill", None)
                    raw = fallback(source, request.topic, plan.sections) if fallback else []
                fallback = getattr(self.model, "fallback_distill", None)
                extra = fallback(source, request.topic, plan.sections) if fallback else []
                by_text = {item.text: item for item in [*raw, *extra]}
                return [replace(item, source_id=source.id) for item in list(by_text.values())[:18] if 0 <= item.relevance <= 1 and item.relevance >= .25 and item.text.strip()]
        return [item for batch in await asyncio.gather(*(one(source) for source in sources)) for item in batch]

    @staticmethod
    def _merge_sources(existing: list[Source], new: list[Source], request: ResearchRequest) -> list[Source]:
        accepted=list(existing[:request.limits.max_sources])
        keys={(item.kind,item.canonical_url or item.locator or item.content_hash) for item in accepted}
        content_hashes={item.content_hash for item in accepted if item.content_hash}
        ids={item.id for item in accepted};next_ordinal=max((item.ordinal for item in accepted),default=0)+1
        query_hits = {}; excerpt_hits = {}
        for source in new:
            queries = [*source.metadata.get("queries", []), source.metadata.get("query")]
            for identity in (source.id, source.canonical_url, source.content_hash):
                if identity:
                    query_hits.setdefault(identity, []).extend(item for item in queries if item)
                    excerpt = source.metadata.get("search_excerpt")
                    if excerpt:
                        excerpt_hits.setdefault(identity, []).append(excerpt)
        enriched = []
        for source in new:
            queries = []
            for identity in (source.id, source.canonical_url, source.content_hash):
                queries.extend(query_hits.get(identity, ()))
            excerpts = []
            for identity in (source.id, source.canonical_url, source.content_hash):
                excerpts.extend(excerpt_hits.get(identity, ()))
            excerpts = list(dict.fromkeys(excerpts))
            metadata = {**source.metadata, "queries": list(dict.fromkeys(queries)), "query_excerpts": excerpts}
            content = ("\n\n".join(excerpts)+"\n\n"+source.content)[:20_000] if excerpts else source.content
            enriched.append(replace(source, content=content, metadata=metadata))
        filtered = filter_sources(enriched,min_chars=request.limits.min_source_chars,max_sources=max(len(new),request.limits.max_sources))
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
        if len(plan.title) > 160 or any(not isinstance(heading, str) or not heading.strip() or len(heading) > 100 or "\n" in heading for heading in plan.sections):
            raise ValueError("research headings must be short single-line strings")
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
        markers = list(re.finditer(r"(?:分别(?:说明|研究|分析)|重点(?:说明|研究|分析)|包括|涵盖)\s*", deliverables))
        if markers:
            deliverables = deliverables[markers[-1].end():]
        return tuple(
            item.strip(" 。.!！?？")
            for item in re.split(r"[、,，;；]\s*(?:以及|与|和|及)?|(?<!以)(?<=\S)(?:以及|与|和|及)(?=\S)", deliverables)
            if item.strip(" 。.!！?？")
        )

    @staticmethod
    def _fallback_curated_sections(plan, evidence, sources=()):
        source_context = {
            item.id: f"{item.title} {item.metadata.get('query', '')} {' '.join(item.metadata.get('queries', ()))}".lower()
            for item in sources
        }
        section_terms = []
        for heading in plan.sections:
            terms = ResearchEngine._search_terms(heading)
            terms.update(
                run[index:index + 2]
                for run in re.findall(r"[\u4e00-\u9fff]{4,}", heading)
                for index in range(len(run) - 1)
            )
            section_terms.append(terms)
        assigned = [[] for _ in plan.sections]
        for item in evidence:
            searchable = item.text.lower()+" "+source_context.get(item.source_id,"")
            scores = [sum(term in searchable for term in terms) for terms in section_terms]
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
    def _search_terms(value: str) -> set[str]:
        stop = {"the","and","for","with","guidance","specific","concrete","provided","importance","steps"}
        return {item.lower() for item in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}|[\u4e00-\u9fff]{2,}", value) if item.lower() not in stop}

    @classmethod
    def _repair_evidence(cls, missing_requirements, evidence, sources=()) -> tuple[tuple[str, str], ...]:
        source_context = {
            item.id: f"{item.title} {item.metadata.get('query', '')} {' '.join(item.metadata.get('queries', ()))}".lower()
            for item in sources
        }
        selected = []
        seen = set()
        for requirement in missing_requirements:
            terms = cls._search_terms(str(requirement))
            ranked = sorted(
                evidence,
                key=lambda item: (
                    -sum(term in (item.text.lower()+" "+source_context.get(item.source_id,"")) for term in terms),
                    -item.relevance,
                    item.id,
                ),
            )
            for item in ranked[:2]:
                key = (item.source_id, item.text)
                if key not in seen:
                    seen.add(key); selected.append(key)
        return tuple(selected[:20])

    @classmethod
    def _fallback_repair_supplement(cls, missing_requirements, evidence_context) -> str:
        lines = ["## 审计缺口的证据补充"]
        for requirement in missing_requirements:
            terms = cls._search_terms(str(requirement))
            ranked = sorted(
                evidence_context,
                key=lambda item: (-sum(term in item[1].lower() for term in terms), item[0], item[1]),
            )
            lines.extend(("", f"### {str(requirement).strip()[:200]}"))
            for source_id, text in ranked[:2]:
                lines.append(f"- {text.strip()[:700]} [[source:{source_id}]]")
        return "\n".join(lines)

    @staticmethod
    def _heading_key(heading: str) -> str: return re.sub(r"[^\w\u4e00-\u9fff]+", "", heading).lower()

    @staticmethod
    def _traceability(
        plan, sections, bodies, summaries, evidence, sources, missing_requirements, supplements=(),
    ):
        evidence_by_id = {item.id: item for item in evidence}
        source_by_id = {item.id: item for item in sources}
        missing_keys = {
            ResearchEngine._heading_key(str(item)) for item in missing_requirements if str(item).strip()
        }

        def normalize_citations(value):
            return tuple(dict.fromkeys(
                next(
                    (source.id for source in sources if marker in {source.id, source.id.removeprefix("source_")}),
                    marker,
                )
                for marker in CITATION.findall(value)
            ))

        def source_versions(source_ids):
            return [
                {
                    "source_id": source_id,
                    "content_hash": source_by_id[source_id].content_hash,
                    "retrieved_at": source_by_id[source_id].retrieved_at,
                }
                for source_id in source_ids if source_id in source_by_id
            ]

        def evidence_locations(evidence_items):
            locations = []
            for item in evidence_items:
                source = source_by_id.get(item.source_id)
                start, end = ResearchEngine._excerpt_location(source.content, item.text) if source else (-1, -1)
                locations.append({
                    "evidence_id": item.id,
                    "source_id": item.source_id,
                    "char_start": start,
                    "char_end": end,
                    "exact_quote_hash": hashlib.sha256(item.text.encode()).hexdigest(),
                })
            return locations

        rows = []
        for section, body, conclusion in zip(sections, bodies, summaries):
            evidence_items = [evidence_by_id[item] for item in section.evidence_ids if item in evidence_by_id]
            source_ids = tuple(dict.fromkeys(item.source_id for item in evidence_items))
            normalized_citations = normalize_citations(body)
            locations = evidence_locations(evidence_items)
            rows.append({
                "requirement": section.heading,
                "conclusion": conclusion,
                "evidence_ids": [item.id for item in evidence_items],
                "source_ids": list(source_ids),
                "citation_source_ids": list(normalized_citations),
                "source_versions": source_versions(source_ids),
                "evidence_locations": locations,
                "supported": bool(
                    evidence_items and normalized_citations
                    and set(normalized_citations) <= set(source_ids)
                    and all(item["char_start"] >= 0 for item in locations)
                    and ResearchEngine._heading_key(section.heading) not in missing_keys
                ),
            })
        for requirement, supplement in supplements:
            requirement = str(requirement).strip()
            normalized_citations = normalize_citations(str(supplement))
            evidence_items = [item for item in evidence if item.source_id in normalized_citations]
            source_ids = tuple(dict.fromkeys(item.source_id for item in evidence_items))
            locations = evidence_locations(evidence_items)
            supplement_row = {
                "requirement": requirement,
                "conclusion": str(supplement).strip()[:4000],
                "evidence_ids": [item.id for item in evidence_items],
                "source_ids": list(source_ids),
                "citation_source_ids": list(normalized_citations),
                "source_versions": source_versions(source_ids),
                "evidence_locations": locations,
                "supported": bool(
                    evidence_items and normalized_citations
                    and set(normalized_citations) <= set(source_ids)
                    and all(item["char_start"] >= 0 for item in locations)
                    and ResearchEngine._heading_key(requirement) not in missing_keys
                ),
            }
            existing = next(
                (item for item in rows if ResearchEngine._heading_key(item["requirement"]) == ResearchEngine._heading_key(requirement)),
                None,
            )
            if existing is None:
                rows.append(supplement_row)
            else:
                existing["conclusion"] = "\n\n".join(
                    item for item in (existing["conclusion"], supplement_row["conclusion"]) if item
                )[:4000]
                for key in ("evidence_ids", "source_ids", "citation_source_ids"):
                    existing[key] = list(dict.fromkeys((*existing[key], *supplement_row[key])))
                existing["source_versions"] = source_versions(existing["source_ids"])
                existing["evidence_locations"] = evidence_locations([
                    evidence_by_id[item] for item in existing["evidence_ids"] if item in evidence_by_id
                ])
                existing["supported"] = bool(
                    existing["evidence_ids"] and existing["citation_source_ids"]
                    and set(existing["citation_source_ids"]) <= set(existing["source_ids"])
                    and all(item["char_start"] >= 0 for item in existing["evidence_locations"])
                    and ResearchEngine._heading_key(requirement) not in missing_keys
                )
        covered = {item["requirement"] for item in rows}
        for requirement in missing_requirements:
            if requirement not in covered:
                rows.append({
                    "requirement": requirement, "conclusion": "", "evidence_ids": [],
                    "source_ids": [], "citation_source_ids": [], "source_versions": [],
                    "evidence_locations": [],
                    "supported": False,
                })
        return rows

    @staticmethod
    def _excerpt_location(content: str, excerpt: str) -> tuple[int, int]:
        start = content.find(excerpt)
        if start >= 0:
            return start, start + len(excerpt)
        normalized = []
        original_positions = []
        previous_space = False
        for index, character in enumerate(content):
            if character.isspace():
                if not previous_space:
                    normalized.append(" ")
                    original_positions.append(index)
                previous_space = True
            else:
                normalized.append(character)
                original_positions.append(index)
                previous_space = False
        needle = " ".join(excerpt.split())
        normalized_content = "".join(normalized)
        normalized_start = normalized_content.find(needle)
        if normalized_start < 0:
            return -1, -1
        normalized_end = normalized_start + len(needle) - 1
        return original_positions[normalized_start], original_positions[normalized_end] + 1

    @staticmethod
    def _cancel(request: ResearchRequest) -> None:
        if request.cancel_event is not None and request.cancel_event.is_set(): raise ResearchCancelled("research cancelled")
