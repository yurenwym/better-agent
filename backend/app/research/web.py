from __future__ import annotations

import hashlib
import html
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote_plus, urlsplit

import httpx

from .models import ResearchRequest, Source
from .retriever import validate_public_url


class WebSearchRetriever:
    def __init__(self, *, search_base_url: str = "https://html.duckduckgo.com/html/", timeout: float = 15, max_bytes: int = 1_000_000, max_chars: int = 20_000) -> None:
        self.search_base_url = search_base_url; self.timeout = timeout; self.max_bytes = max_bytes; self.max_chars = max_chars

    async def retrieve(self, query: str, request: ResearchRequest) -> list[Source]:
        if "web" not in request.source_scopes: return []
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, headers={"User-Agent": "BetterAgent/1.0 local research"}) as client:
            response = await client.get(self.search_base_url, params={"q": query})
            response.raise_for_status()
            candidates = self._links(response.text)[:5]
            sources = []
            for title, url in candidates:
                try:
                    current = await validate_public_url(url)
                    fetched = await client.get(current)
                    for _ in range(3):
                        if fetched.status_code not in {301,302,303,307,308}: break
                        location = fetched.headers.get("location", "")
                        current = await validate_public_url(str(httpx.URL(current).join(location)))
                        fetched = await client.get(current)
                    fetched.raise_for_status()
                    content_type = fetched.headers.get("content-type", "").lower()
                    if not any(x in content_type for x in ("text/html", "text/plain")): continue
                    raw = fetched.content[:self.max_bytes].decode(fetched.encoding or "utf-8", errors="replace")
                    text = self._text(raw)[:self.max_chars]
                    digest=hashlib.sha256(text.encode()).hexdigest();stable=hashlib.sha256(f"{request.job_id}:web:{current}:{digest}".encode()).hexdigest()
                    sources.append(Source(f"source_{stable}", 0, "web", current, None, title, text, None, datetime.now(timezone.utc).isoformat(), .5, digest))
                except (ValueError, httpx.HTTPError, UnicodeError):
                    continue
            return sources

    @staticmethod
    def _links(body: str):
        found = []
        for href, title in re.findall(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body, flags=re.I|re.S):
            href = html.unescape(href)
            if "uddg=" in href: href = parse_qs(urlsplit(href).query).get("uddg", [href])[0]
            found.append((re.sub(r"<[^>]+>", "", html.unescape(title)).strip(), href))
        return found

    @staticmethod
    def _text(body: str) -> str:
        body = re.sub(r"<(script|style|form|svg|noscript)[^>]*>.*?</\1>", " ", body, flags=re.I|re.S)
        body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
        body = re.sub(r"<[^>]+>", " ", body)
        return re.sub(r"\s+", " ", html.unescape(body)).strip()
