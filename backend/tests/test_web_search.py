import httpx
import pytest

from app.research.models import ResearchLimits, ResearchRequest
from app.research.retriever import RetrievalError
from app.research.web import WebSearchRetriever


def request() -> ResearchRequest:
    return ResearchRequest("job", "pgvector HNSW", ("web",), ResearchLimits())


@pytest.mark.asyncio
async def test_search_falls_back_after_primary_timeout(monkeypatch) -> None:
    async def public_url(value: str) -> str:
        return value

    monkeypatch.setattr("app.research.web.validate_public_url", public_url)

    def handler(incoming: httpx.Request) -> httpx.Response:
        if incoming.url.host == "html.duckduckgo.com":
            raise httpx.ConnectTimeout("timed out", request=incoming)
        if incoming.url.host == "cn.bing.com":
            assert incoming.url.params["format"] == "rss"
            return httpx.Response(200, text="""<?xml version="1.0"?><rss><channel><item><title>pgvector</title><link>https://example.com/pgvector</link></item></channel></rss>""")
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<main>" + "pgvector HNSW tuning. " * 40 + "</main>")

    retriever = WebSearchRetriever(
        search_base_url="https://html.duckduckgo.com/html/",
        fallback_search_base_url="https://cn.bing.com/search",
        transport=httpx.MockTransport(handler),
    )

    sources = await retriever.retrieve("pgvector HNSW", request())

    assert len(sources) == 1
    assert sources[0].canonical_url == "https://example.com/pgvector"
    assert sources[0].quality_score > .5
    assert "pgvector" in sources[0].metadata["matched_terms"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("<html><div class='anomaly-modal'>Verify you are human</div></html>", "search_blocked"),
        ("<html><p>No results found</p></html>", "search_no_results"),
        ("<html><p>unexpected response</p></html>", "search_response_unparseable"),
    ],
)
async def test_search_reports_distinct_zero_result_failures(body: str, reason: str) -> None:
    transport = httpx.MockTransport(lambda incoming: httpx.Response(200, text=body))
    retriever = WebSearchRetriever(fallback_search_base_url=None, transport=transport)

    with pytest.raises(RetrievalError) as caught:
        await retriever.retrieve("x", request())

    assert caught.value.reason_code == reason


def test_duckduckgo_parser_accepts_attribute_order_and_decodes_redirect() -> None:
    body = '<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs" class="result__a"><b>Vector</b> docs</a>'

    assert WebSearchRetriever._links(body) == [("Vector docs", "https://example.com/docs")]


def test_bing_rss_parser_uses_structured_xml() -> None:
    body = """<?xml version="1.0"?><rss><channel><item><title>HNSW &amp; pgvector</title><link>https://example.com/hnsw</link></item></channel></rss>"""

    assert WebSearchRetriever._links(body, rss=True) == [("HNSW & pgvector", "https://example.com/hnsw")]


@pytest.mark.asyncio
async def test_search_strips_tracking_marker_before_provider_request(monkeypatch) -> None:
    async def public_url(value: str) -> str:
        return value

    monkeypatch.setattr("app.research.web.validate_public_url", public_url)
    observed_queries = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        if incoming.url.host == "cn.bing.com":
            observed_queries.append(incoming.url.params["q"])
            return httpx.Response(200, text="""<?xml version="1.0"?><rss><channel><item><title>pgvector HNSW</title><link>https://example.com/hnsw</link></item></channel></rss>""")
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<main>" + "PostgreSQL pgvector HNSW tuning. " * 40 + "</main>")

    retriever = WebSearchRetriever(
        search_base_url="https://cn.bing.com/search",
        fallback_search_base_url=None,
        transport=httpx.MockTransport(handler),
    )
    marked = "[ACCEPT-FULL-JOURNEY-001] PostgreSQL pgvector HNSW"

    sources = await retriever.retrieve(marked, ResearchRequest("job", marked, ("web",), ResearchLimits()))

    assert sources
    assert observed_queries == ["pgvector HNSW"]


@pytest.mark.asyncio
async def test_search_rejects_pages_unrelated_to_the_cleaned_topic(monkeypatch) -> None:
    async def public_url(value: str) -> str:
        return value

    monkeypatch.setattr("app.research.web.validate_public_url", public_url)

    def handler(incoming: httpx.Request) -> httpx.Response:
        if incoming.url.host == "cn.bing.com":
            return httpx.Response(200, text="""<?xml version="1.0"?><rss><channel><item><title>accept dictionary</title><link>https://example.com/dictionary</link></item></channel></rss>""")
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<main>accept means to receive or agree to something</main>")

    retriever = WebSearchRetriever(
        search_base_url="https://cn.bing.com/search",
        fallback_search_base_url=None,
        transport=httpx.MockTransport(handler),
    )
    marked = "[ACCEPT-FULL-JOURNEY-001] PostgreSQL pgvector HNSW"

    with pytest.raises(RetrievalError) as caught:
        await retriever.retrieve(marked, ResearchRequest("job", marked, ("web",), ResearchLimits()))

    assert caught.value.reason_code == "search_results_irrelevant"


def test_query_cleaning_preserves_non_tracking_bracketed_topics() -> None:
    assert WebSearchRetriever._clean_query("[Python] typing guide") == "[Python] typing guide"


def test_html_extraction_preserves_paragraph_boundaries():
    assert WebSearchRetriever._text('<script>ignore()</script><h2>EXPLAIN</h2><p>First fact.</p><p>Second &amp; third.</p>') == 'EXPLAIN\n\nFirst fact.\n\nSecond & third.'


def test_html_extraction_prefers_document_content_over_navigation():
    html = '<div>PostgreSQL releases</div><div id="docContent"><div class="navheader">Previous Next</div><p>Indexes improve lookup.</p></div><footer>Copyright</footer>'
    assert WebSearchRetriever._text(html) == 'Indexes improve lookup.'


def test_technical_query_compaction_prefers_specific_anchors() -> None:
    query = "PostgreSQL pgvector 的 HNSW 参数 m ef_construction ef_search 的作用和建议 Web 来源"

    variants, anchors = WebSearchRetriever._query_variants(query)

    assert variants[0] == "pgvector HNSW ef_construction ef_search"
    assert anchors == ("pgvector", "hnsw", "ef_construction", "ef_search")
    assert len(variants) == 2


def test_query_compaction_preserves_site_scope_and_checks_boundaries():
    variants, anchors = WebSearchRetriever._query_variants(
        "pgvector HNSW ef_search site:github.com/pgvector/pgvector"
    )
    assert all("site:github.com/pgvector/pgvector" in query for query in variants)
    assert "github.com" not in anchors
    sites = ("github.com/pgvector/pgvector", "postgresql.org/docs")
    assert WebSearchRetriever._matches_sites("https://github.com/pgvector/pgvector/blob/master/README.md", sites)
    assert WebSearchRetriever._matches_sites("https://www.postgresql.org/docs/current/indexes.html", sites)
    assert not WebSearchRetriever._matches_sites("https://github.com/pgvector/pgvector-fork", sites)
    assert not WebSearchRetriever._matches_sites("https://postgresql.org.evil.test/docs", sites)


@pytest.mark.asyncio
async def test_search_rejects_out_of_scope_results(monkeypatch):
    async def public_url(value):
        return value
    monkeypatch.setattr("app.research.web.validate_public_url", public_url)
    def handler(incoming):
        if incoming.url.host == "cn.bing.com":
            return httpx.Response(200, text='<rss><channel><item><title>pgvector HNSW</title><link>https://blog.example.com/pgvector</link></item><item><title>pgvector HNSW</title><link>https://github.com/pgvector/pgvector</link></item></channel></rss>')
        assert incoming.url.host == "github.com"
        return httpx.Response(200, headers={"content-type":"text/html"}, text="pgvector HNSW " * 50)
    sources = await WebSearchRetriever(transport=httpx.MockTransport(handler)).retrieve(
        "pgvector HNSW site:github.com/pgvector/pgvector", request()
    )
    assert [source.canonical_url for source in sources] == ["https://github.com/pgvector/pgvector"]


@pytest.mark.asyncio
async def test_explicit_source_url_is_fetched_without_search(monkeypatch):
    checked = []
    async def public_url(value):
        checked.append(value)
        return value
    monkeypatch.setattr("app.research.web.validate_public_url", public_url)
    def handler(incoming):
        assert incoming.url.host == "github.com"
        return httpx.Response(200, headers={"content-type":"text/html"}, text="<title>pgvector HNSW</title>" + "pgvector HNSW " * 50)
    topic = "pgvector HNSW https://github.com/pgvector/pgvector"
    sources = await WebSearchRetriever(transport=httpx.MockTransport(handler)).retrieve(
        "pgvector HNSW", ResearchRequest("job", topic, ("web",), ResearchLimits())
    )
    assert checked == ["https://github.com/pgvector/pgvector"]
    assert sources[0].metadata["provider"] == "direct_url"


@pytest.mark.asyncio
async def test_long_query_uses_compact_variant_before_generic_long_query(monkeypatch) -> None:
    async def public_url(value: str) -> str:
        return value

    monkeypatch.setattr("app.research.web.validate_public_url", public_url)
    queries = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        if incoming.url.host == "cn.bing.com":
            query = incoming.url.params["q"]
            queries.append(query)
            if query.startswith("pgvector HNSW"):
                return httpx.Response(200, text="""<?xml version="1.0"?><rss><channel><item><title>pgvector HNSW guide</title><link>https://example.com/pgvector</link></item></channel></rss>""")
            return httpx.Response(200, text="""<?xml version="1.0"?><rss><channel><item><title>PostgreSQL home</title><link>https://postgresql.example.com/</link></item></channel></rss>""")
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<main>" + "pgvector HNSW ef_construction ef_search. " * 40 + "</main>")

    retriever = WebSearchRetriever(transport=httpx.MockTransport(handler))
    query = "PostgreSQL pgvector 的 HNSW 参数 m ef_construction ef_search 的作用和建议 Web 来源"

    sources = await retriever.retrieve(query, ResearchRequest("job", query, ("web",), ResearchLimits()))

    assert sources
    assert queries == ["pgvector HNSW ef_construction ef_search"]
    assert sources[0].metadata["matched_anchors"] == ["ef_construction", "ef_search", "hnsw", "pgvector"]


def test_multi_anchor_relevance_rejects_a_generic_postgresql_page() -> None:
    score, _, matched = WebSearchRetriever._relevance(
        "PostgreSQL home", "https://postgresql.org/", "PostgreSQL database documentation",
        "pgvector HNSW", "PostgreSQL pgvector HNSW", ("pgvector", "hnsw"),
    )

    assert score == 0
    assert matched == []


def test_single_anchor_requires_title_and_body_match() -> None:
    accepted, _, _ = WebSearchRetriever._relevance(
        "PostgreSQL docs", "https://postgresql.org/", "PostgreSQL database documentation",
        "PostgreSQL", "PostgreSQL", ("postgresql",),
    )
    rejected, _, _ = WebSearchRetriever._relevance(
        "Database docs", "https://example.com/", "PostgreSQL database documentation",
        "PostgreSQL", "PostgreSQL", ("postgresql",),
    )

    assert accepted > 0
    assert rejected == 0
