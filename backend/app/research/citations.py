from __future__ import annotations

import re

from .models import Source


CITATION = re.compile(r"\[\[source:([^\]]+)\]\]")


def render_citations(markdown: str, sources: list[Source]) -> str:
    by_id = {source.id: source for source in sources}
    aliases: dict[str, Source] = {}
    ambiguous: set[str] = set()
    for source in sources:
        alias = source.id.removeprefix("source_")
        if alias in aliases and aliases[alias].id != source.id:
            ambiguous.add(alias)
        else:
            aliases[alias] = source
    for alias in ambiguous:
        aliases.pop(alias, None)
    cited = {match.group(1) for match in CITATION.finditer(markdown)}
    unknown = cited - set(by_id) - set(aliases)
    if unknown: raise KeyError(sorted(unknown)[0])
    def replace(match: re.Match[str]) -> str:
        source = by_id.get(match.group(1)) or aliases[match.group(1)]
        return f"[{source.title}]({source.canonical_url})" if source.canonical_url else f"[来源 {source.ordinal}]"
    return CITATION.sub(replace, markdown)
