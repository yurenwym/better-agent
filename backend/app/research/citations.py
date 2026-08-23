from __future__ import annotations

import re

from .models import Source


CITATION = re.compile(r"\[\[source:([^\]]+)\]\]")


def render_citations(markdown: str, sources: list[Source]) -> str:
    by_id = {source.id: source for source in sources}
    unknown = {match.group(1) for match in CITATION.finditer(markdown)} - set(by_id)
    if unknown: raise KeyError(sorted(unknown)[0])
    def replace(match: re.Match[str]) -> str:
        source = by_id[match.group(1)]
        return f"[{source.title}]({source.canonical_url})" if source.canonical_url else f"[来源 {source.ordinal}]"
    return CITATION.sub(replace, markdown)
