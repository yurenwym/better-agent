from types import SimpleNamespace

import httpx
import pytest

from app.research.models import ResearchLimits, ResearchRequest
from app.research.tavily import TavilySearchRetriever


@pytest.mark.asyncio
async def test_tavily_search_maps_results_without_exposing_key():
    seen={}
    def handler(request:httpx.Request):
        seen["authorization"]=request.headers.get("authorization")
        seen["body"]=request.read().decode()
        return httpx.Response(200,json={"results":[{"title":"SQLite WAL","url":"https://sqlite.org/wal.html","content":"evidence "*80,"score":.91}]})
    retriever=TavilySearchRetriever("secret",transport=httpx.MockTransport(handler))
    request=ResearchRequest("job","SQLite WAL",("web",),ResearchLimits())
    result=await retriever.retrieve("SQLite WAL",request)
    assert len(result)==1 and result[0].canonical_url=="https://sqlite.org/wal.html"
    assert result[0].quality_score==.91 and "secret" not in repr(result)
    assert seen["authorization"]=="Bearer secret" and "secret" not in seen["body"]


@pytest.mark.asyncio
async def test_tavily_search_rejects_private_result_urls():
    transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"results":[{"title":"private","url":"http://127.0.0.1/admin","content":"x"*500,"score":1}]}))
    result=await TavilySearchRetriever("secret",transport=transport).retrieve("x",ResearchRequest("job","x",("web",),ResearchLimits()))
    assert result==[]


def test_tavily_requires_key():
    with pytest.raises(ValueError,match="TAVILY_API_KEY"):
        TavilySearchRetriever("")
