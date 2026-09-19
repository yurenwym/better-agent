from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ResearchLimits:
    max_sections: int = 6
    max_queries: int = 12
    max_sources: int = 20
    max_evidence: int = 80
    min_source_chars: int = 300
    reflection_rounds: int = 1


@dataclass(frozen=True)
class ResearchRequest:
    job_id: str
    topic: str
    source_scopes: tuple[str, ...]
    limits: ResearchLimits
    cancel_event: Any | None = None
    completed_sections: dict[int, dict[str, str]] = field(default_factory=dict)
    recovered_sources: tuple[Source, ...] = ()
    recovered_evidence: tuple[Evidence, ...] = ()
    recovered_plan: ResearchPlan | None = None
    stop_condition: str = "none"


@dataclass(frozen=True)
class ResearchPlan:
    title: str
    sections: tuple[str, ...]
    queries: tuple[str, ...]


@dataclass(frozen=True)
class Source:
    id: str
    ordinal: int
    kind: str
    canonical_url: str | None
    locator: str | None
    title: str
    content: str
    published_at: str | None
    retrieved_at: str
    quality_score: float
    content_hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Evidence:
    id: str
    source_id: str
    text: str
    date_hint: str | None
    relevance: float


@dataclass(frozen=True)
class CuratedSection:
    ordinal: int
    heading: str
    thesis: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResearchEvent:
    type: str
    phase: str
    data: dict[str, Any]
