from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from dataclasses import replace
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from .models import Source
from datetime import datetime, timezone
from pathlib import Path
import re
import uuid


class RetrievalError(RuntimeError):
    def __init__(self, reason_code: str, *, retryable: bool = False) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retryable = retryable


def canonicalize_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = host if not port or (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80) else f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path.rstrip("/") or "/", parsed.query, ""))


def filter_sources(sources: list[Source], *, min_chars: int, max_sources: int, per_domain: int = 3) -> list[Source]:
    ranked = sorted(sources, key=lambda item: (-item.quality_score, item.title, item.id))
    seen: set[tuple[str, str]] = set()
    domains: dict[str, int] = {}
    accepted: list[Source] = []
    for source in ranked:
        if len(source.content.strip()) < min_chars: continue
        canonical = canonicalize_url(source.canonical_url) if source.canonical_url else None
        digest = source.content_hash or hashlib.sha256(source.content.encode()).hexdigest()
        key = (source.kind, canonical or source.locator or digest)
        if key in seen: continue
        domain = urlsplit(canonical).hostname if canonical else source.kind
        if domains.get(domain or "", 0) >= per_domain: continue
        seen.add(key); domains[domain or ""] = domains.get(domain or "", 0) + 1
        accepted.append(replace(source, ordinal=len(accepted) + 1, canonical_url=canonical, content_hash=digest))
        if len(accepted) >= max_sources: break
    return accepted


async def _resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    rows = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({row[4][0] for row in rows})


async def validate_public_url(value: str, *, resolver: Callable[[str], Awaitable[list[str]]] = _resolve) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}: raise ValueError("unsupported URL scheme")
    if not parsed.hostname: raise ValueError("URL host is required")
    addresses = await resolver(parsed.hostname)
    if not addresses: raise ValueError("URL host did not resolve")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if not address.is_global:
            raise ValueError("private or local URL is not allowed")
    return canonicalize_url(value)


class LocalNoteRetriever:
    def __init__(self, root: str | Path, *, max_chars: int = 20_000) -> None:
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True);self.max_chars=max_chars

    async def retrieve(self,query:str,request)->list[Source]:
        if "local_note" not in request.source_scopes:return []
        terms={item.lower() for item in re.findall(r"[\w\u4e00-\u9fff]{2,}",query)}
        result=[]
        for path in sorted((*self.root.rglob("*.md"),*self.root.rglob("*.txt"))):
            resolved=path.resolve()
            if self.root not in resolved.parents:continue
            try:text=resolved.read_text(encoding="utf-8")[:self.max_chars]
            except (OSError,UnicodeError):continue
            lowered=(path.name+" "+text).lower();score=sum(term in lowered for term in terms)
            if terms and score==0:continue
            digest=hashlib.sha256(text.encode()).hexdigest();stable=hashlib.sha256(f"{request.job_id}:local_note:{path.relative_to(self.root)}:{digest}".encode()).hexdigest()
            result.append(Source(f"source_{stable}",0,"local_note",None,str(path.relative_to(self.root)).replace("\\","/"),path.stem,text,None,datetime.now(timezone.utc).isoformat(),min(.5+score*.08,1),digest))
        return result[:20]


class CombinedRetriever:
    def __init__(self,*retrievers):self.retrievers=retrievers
    async def retrieve(self,query,request):
        batches=await asyncio.gather(*(item.retrieve(query,request) for item in self.retrievers),return_exceptions=True)
        sources=[source for batch in batches if isinstance(batch,list) for source in batch]
        if sources:return sources
        failures=[batch for batch in batches if isinstance(batch,RetrievalError)]
        if failures:
            retryable=any(item.retryable for item in failures)
            reason=next((item.reason_code for item in failures if item.retryable),failures[0].reason_code)
            raise RetrievalError(reason,retryable=retryable)
        return []
