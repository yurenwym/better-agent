from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import httpx

from .models import ResearchRequest, Source
from .retriever import RetrievalError, validate_public_url


class _DuckDuckGoResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str]] = []
        self._href: str | None = None
        self._title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = dict(attrs)
        if "result__a" in (values.get("class") or "").split():
            self._href = values.get("href")
            self._title = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._title.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.results.append((" ".join(" ".join(self._title).split()), self._href))
            self._href = None
            self._title = []


class _DocumentTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.main_parts = []
        self.stack = []
        self.ignored = 0
        self.main = 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        ignored = tag in {"script", "style", "form", "svg", "noscript", "nav", "footer"} or bool(
            set((values.get("class") or "").split()) & {"navheader", "navfooter", "toc"}
        )
        main = tag in {"main", "article"} or values.get("id") == "docContent"
        if tag not in {"br", "hr", "img", "meta", "link", "input", "source", "wbr", "area", "base", "embed", "param", "track", "col"}:
            self.stack.append((tag, ignored, main))
            self.ignored += ignored
            self.main += main
        if tag == "br":
            self.handle_data("\n\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "section", "article", "li", "pre", "h1", "h2", "h3", "h4", "tr"}:
            self.handle_data("\n\n")
        if any(item[0] == tag for item in self.stack):
            while self.stack:
                name, ignored, main = self.stack.pop()
                self.ignored -= ignored
                self.main -= main
                if name == tag:
                    break

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)
            if self.main:
                self.main_parts.append(data)


class WebSearchRetriever:
    DEFAULT_SEARCH_URL = "https://cn.bing.com/search"
    _TRACKING_PREFIX = re.compile(
        r"^\s*\[(?:accept|trace|test|case)(?:[-_:][a-z0-9_.-]+)+\]\s*",
        re.IGNORECASE,
    )
    _TERM_STOPWORDS = {
        "about", "and", "current", "deep", "explain", "for", "from", "official",
        "recommendation", "reliable", "report", "research", "source", "the", "web",
        "以及", "作用", "取舍", "可靠", "引用", "当前", "建议", "报告", "来源", "深度",
        "研究", "说明", "重点",
    }

    def __init__(
        self,
        *,
        search_base_url: str = DEFAULT_SEARCH_URL,
        fallback_search_base_url: str | None = None,
        timeout: float = 15,
        search_timeout: float = 8,
        max_bytes: int = 1_000_000,
        max_chars: int = 60_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.search_base_url = search_base_url
        self.fallback_search_base_url = fallback_search_base_url
        self.timeout = timeout
        self.search_timeout = min(search_timeout, timeout)
        self.max_bytes = max_bytes
        self.max_chars = max_chars
        self.transport = transport
        self._disabled_endpoints: set[str] = set()

    async def retrieve(self, query: str, request: ResearchRequest) -> list[Source]:
        if "web" not in request.source_scopes: return []
        search_query = self._clean_query(query)
        variants, anchors = self._query_variants(search_query)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.8",
        }
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, headers=headers, transport=self.transport) as client:
            errors: list[RetrievalError] = []
            for variant in variants:
                try:
                    return await self._retrieve_variant(client, variant, request, anchors)
                except RetrievalError as exc:
                    errors.append(exc)
                    if exc.reason_code not in {
                        "search_no_results", "search_response_unparseable",
                        "search_results_irrelevant", "search_results_rejected",
                    }:
                        raise
            preferred = next((item for item in reversed(errors) if item.reason_code == "search_results_irrelevant"), errors[-1])
            raise preferred

    async def _retrieve_variant(
        self,
        client: httpx.AsyncClient,
        search_query: str,
        request: ResearchRequest,
        anchors: tuple[str, ...],
    ) -> list[Source]:
        explicit_urls = re.findall(r"https?://[^\s<>\"，。；）)]+", request.topic)
        candidates = [(url, url) for url in dict.fromkeys(explicit_urls)] if explicit_urls else await self._search(client, search_query)
        sites = self._sites(search_query) or self._sites(request.topic)
        sources = []
        fetched_pages = irrelevant_pages = skipped_candidates = 0
        for rank, (title, url) in enumerate(candidates, 1):
            if sites and not self._matches_sites(url, sites):
                skipped_candidates += 1
                continue
            preview = f"{title} {url}".lower()
            if not explicit_urls and anchors and not any(anchor in preview for anchor in anchors):
                skipped_candidates += 1
                continue
            try:
                current = await validate_public_url(url)
                fetched = await client.get(current)
                for _ in range(3):
                    if fetched.status_code not in {301,302,303,307,308}: break
                    location = fetched.headers.get("location", "")
                    current = await validate_public_url(str(httpx.URL(current).join(location)))
                    fetched = await client.get(current)
                fetched.raise_for_status()
                if sites and not self._matches_sites(current, sites):
                    continue
                content_type = fetched.headers.get("content-type", "").lower()
                if not any(x in content_type for x in ("text/html", "text/plain")): continue
                raw = fetched.content[:self.max_bytes].decode(fetched.encoding or "utf-8", errors="replace")
                text = self._text(raw)[:self.max_chars]
                if explicit_urls:
                    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
                    if title_match:
                        title = html.unescape(re.sub(r"\s+", " ", title_match[1])).strip()
                fetched_pages += 1
                relevance, matched_terms, matched_anchors = self._relevance(title, current, text, search_query, request.topic, anchors)
                if relevance <= 0:
                    irrelevant_pages += 1
                    continue
                digest=hashlib.sha256(text.encode()).hexdigest();stable=hashlib.sha256(f"{request.job_id}:web:{current}:{digest}".encode()).hexdigest()
                metadata = {
                    "provider": "direct_url" if explicit_urls else urlsplit(self.search_base_url).hostname or "web",
                    "search_query": search_query,
                    "search_rank": rank,
                    "matched_terms": matched_terms,
                    "required_anchors": min(2, len(anchors)) if anchors else 0,
                    "matched_anchors": matched_anchors,
                }
                sources.append(Source(f"source_{stable}", 0, "web", current, None, title, text, None, datetime.now(timezone.utc).isoformat(), relevance, digest, metadata))
            except (ValueError, httpx.HTTPError, UnicodeError):
                continue
        if sources:
            return sources
        if skipped_candidates or (fetched_pages and irrelevant_pages == fetched_pages):
            raise RetrievalError("search_results_irrelevant")
        raise RetrievalError("search_results_rejected")

    @classmethod
    def _clean_query(cls, query: str) -> str:
        cleaned = cls._TRACKING_PREFIX.sub("", query).strip()
        return cleaned or query.strip()

    @classmethod
    def _query_variants(cls, query: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        sites = cls._sites(query)
        terms_query = re.sub(r"\bsite:[^\s]+", "", query, flags=re.I)
        raw_terms = [
            token for token in re.findall(r"[A-Za-z][A-Za-z0-9_.+#-]{2,}", terms_query)
            if token.lower() not in cls._TERM_STOPWORDS and not token.lower().startswith(("http", "www"))
        ]
        strong = [
            index for index, token in enumerate(raw_terms)
            if any(marker in token for marker in ("_", ".", "+", "#", "-"))
            or any(character.isdigit() for character in token)
            or (token.isupper() and 2 <= len(token) <= 12)
        ]
        selected: list[str] = []
        if strong:
            first = strong[0]
            if first > 0:
                selected.append(raw_terms[first - 1])
            selected.extend(raw_terms[index] for index in strong)
            if len(selected) < 2:
                selected.extend(token for token in raw_terms if token not in selected)
        elif re.search(r"[\u4e00-\u9fff]", query) and raw_terms:
            selected = raw_terms[-6:]
        selected = list(dict.fromkeys(selected))[:4]
        compact = " ".join(selected).strip()
        if compact and sites:
            compact += " " + " OR ".join(f"site:{site}" for site in sites)
        variants = [compact, query] if compact and compact.casefold() != query.casefold() else [query]
        anchors = tuple(token.lower() for token in selected)
        if not anchors and len(raw_terms) <= 4:
            anchors = tuple(token.lower() for token in raw_terms)
        return tuple(dict.fromkeys(variants[:2])), anchors

    @staticmethod
    def _sites(query: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(re.findall(r"\bsite:([a-zA-Z0-9.-]+(?:/[^\s,)]+)?)", query, re.I)))

    @staticmethod
    def _matches_sites(url: str, sites: tuple[str, ...]) -> bool:
        parsed = urlsplit(url)
        for site in sites:
            scope = urlsplit("https://" + site)
            host = (parsed.hostname or "").lower()
            root = (scope.hostname or "").lower()
            path = scope.path.rstrip("/")
            if (host == root or host.endswith("." + root)) and (
                not path or parsed.path == path or parsed.path.startswith(path + "/")
            ):
                return True
        return False

    @classmethod
    def _relevance(
        cls,
        title: str,
        url: str,
        content: str,
        query: str,
        topic: str,
        anchors: tuple[str, ...],
    ) -> tuple[float, list[str], list[str]]:
        terms = cls._relevance_terms(f"{query} {cls._clean_query(topic)}")
        if not terms:
            return .5, [], []
        title_text = f"{title} {url}".lower()
        body_text = content.lower()
        title_hits = {term for term in terms if term in title_text}
        body_hits = {term for term in terms if term in body_text}
        matched = title_hits | body_hits
        matched_anchors = {anchor for anchor in anchors if anchor in title_text or anchor in body_text}
        if len(anchors) >= 2 and len(matched_anchors) < 2:
            return 0, sorted(matched), sorted(matched_anchors)
        if len(anchors) == 1 and not (anchors[0] in title_text and anchors[0] in body_text):
            return 0, sorted(matched), sorted(matched_anchors)
        if not anchors and not matched:
            return 0, [], []
        coverage = len(matched) / len(terms)
        title_coverage = len(title_hits) / len(terms)
        score = min(.98, .45 + .4 * coverage + .13 * title_coverage)
        return score, sorted(matched, key=lambda item: (-len(item), item))[:20], sorted(matched_anchors)

    @classmethod
    def _relevance_terms(cls, value: str) -> set[str]:
        lowered = value.lower()
        terms = {
            token for token in re.findall(r"[a-z][a-z0-9_.+#-]{2,}", lowered)
            if token not in cls._TERM_STOPWORDS and not token.startswith(("http", "www"))
        }
        for run in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
            if run not in cls._TERM_STOPWORDS:
                terms.add(run)
            terms.update(
                run[index:index + 2]
                for index in range(len(run) - 1)
                if run[index:index + 2] not in cls._TERM_STOPWORDS
            )
        return terms

    async def _search(self, client: httpx.AsyncClient, query: str) -> list[tuple[str, str]]:
        endpoints = tuple(dict.fromkeys(filter(None, (self.search_base_url, self.fallback_search_base_url))))
        errors: list[RetrievalError] = []
        for endpoint in endpoints:
            if endpoint in self._disabled_endpoints:
                continue
            try:
                is_bing = "bing." in (urlsplit(endpoint).hostname or "")
                params = {"q": query, **({"format": "rss"} if is_bing else {})}
                response = await client.get(endpoint, params=params, timeout=self.search_timeout, follow_redirects=True)
                self._raise_for_search_status(response)
                body = response.text
                if self._is_blocked(body):
                    raise RetrievalError("search_blocked", retryable=True)
                candidates = self._links(body, rss=is_bing or "application/rss+xml" in response.headers.get("content-type", ""))
                if candidates:
                    return candidates[:5]
                if self._is_no_results(body):
                    raise RetrievalError("search_no_results")
                raise RetrievalError("search_response_unparseable")
            except RetrievalError as exc:
                errors.append(exc)
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
                self._disabled_endpoints.add(endpoint)
                errors.append(RetrievalError("search_timeout", retryable=True))
            except httpx.HTTPError:
                self._disabled_endpoints.add(endpoint)
                errors.append(RetrievalError("search_provider_unavailable", retryable=True))
        if not errors:
            raise RetrievalError("search_provider_unavailable", retryable=True)
        preferred = next((item for item in reversed(errors) if item.reason_code not in {"search_timeout", "search_provider_unavailable"}), errors[-1])
        raise preferred

    @staticmethod
    def _raise_for_search_status(response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        if status == 429:
            raise RetrievalError("search_rate_limited", retryable=True)
        if status in {401, 403}:
            raise RetrievalError("search_blocked", retryable=True)
        if status >= 500:
            raise RetrievalError("search_provider_unavailable", retryable=True)
        raise RetrievalError("search_request_failed")

    @staticmethod
    def _links(body: str, *, rss: bool = False) -> list[tuple[str, str]]:
        if rss:
            try:
                root = ElementTree.fromstring(body)
            except ElementTree.ParseError:
                return []
            return [
                ((item.findtext("title") or "").strip(), (item.findtext("link") or "").strip())
                for item in root.findall(".//item")
                if (item.findtext("title") or "").strip() and (item.findtext("link") or "").strip()
            ]
        parser = _DuckDuckGoResultParser()
        try:
            parser.feed(body)
        except (ValueError, TypeError):
            return []
        found = []
        for title, href in parser.results:
            href = html.unescape(href)
            if "uddg=" in href: href = parse_qs(urlsplit(href).query).get("uddg", [href])[0]
            found.append((html.unescape(title).strip(), href))
        return found

    @staticmethod
    def _is_blocked(body: str) -> bool:
        lowered = body.lower()
        return any(marker in lowered for marker in ("anomaly-modal", "captcha", "verify you are human", "automated requests"))

    @staticmethod
    def _is_no_results(body: str) -> bool:
        lowered = body.lower()
        return any(marker in lowered for marker in ("no results.", "no results found", "did not match any documents"))

    @staticmethod
    def _text(body: str) -> str:
        parser = _DocumentTextParser()
        parser.feed(body)
        text = "".join(parser.main_parts or parser.parts)
        paragraphs = [re.sub(r"\s+", " ", item).strip() for item in text.split("\n\n")]
        return "\n\n".join(item for item in paragraphs if item)
