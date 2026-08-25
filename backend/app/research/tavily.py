from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

import httpx

from .models import ResearchRequest, Source
from .retriever import RetrievalError,validate_public_url


class TavilySearchRetriever:
    def __init__(self,api_key:str,*,endpoint="https://api.tavily.com/search",timeout=30,max_chars=20_000,transport=None)->None:
        if not api_key.strip():raise ValueError("TAVILY_API_KEY is required for the Tavily search provider")
        self._api_key=api_key.strip();self.endpoint=endpoint;self.timeout=timeout;self.max_chars=max_chars;self.transport=transport

    async def retrieve(self,query:str,request:ResearchRequest)->list[Source]:
        if "web" not in request.source_scopes:return []
        try:
            async with httpx.AsyncClient(timeout=self.timeout,transport=self.transport,headers={"Authorization":f"Bearer {self._api_key}","User-Agent":"BetterAgent/1.0 local research"}) as client:
                response=await client.post(self.endpoint,json={"query":query,"search_depth":"advanced","chunks_per_source":3,"max_results":5,"include_raw_content":True})
                response.raise_for_status();payload=response.json()
        except httpx.TimeoutException as exc:raise RetrievalError("search_timeout",retryable=True) from exc
        except httpx.HTTPStatusError as exc:
            status=exc.response.status_code
            if status in {401,403}:reason,retryable="search_auth_failed",False
            elif status==429:reason,retryable="search_rate_limited",True
            elif status>=500:reason,retryable="search_provider_unavailable",True
            else:reason,retryable="search_request_failed",False
            raise RetrievalError(reason,retryable=retryable) from exc
        except (httpx.HTTPError,ValueError,TypeError) as exc:raise RetrievalError("search_provider_unavailable",retryable=True) from exc
        items=payload.get("results",[])
        if not isinstance(items,list) or not items:raise RetrievalError("search_no_results")
        async def one(item):
            try:
                url=await asyncio.wait_for(validate_public_url(str(item.get("url","")).strip()),5)
                content=str(item.get("raw_content") or item.get("content") or "").strip()[:self.max_chars]
                if not content:return None
                digest=hashlib.sha256(content.encode()).hexdigest();stable=hashlib.sha256(f"{request.job_id}:tavily:{url}:{digest}".encode()).hexdigest()
                score=max(0,min(float(item.get("score",.5)),1))
                return Source(f"source_{stable}",0,"web",url,None,str(item.get("title") or url),content,item.get("published_date"),datetime.now(timezone.utc).isoformat(),score,digest,{"provider":"tavily"})
            except (TimeoutError,TypeError,ValueError):return None
        results=await asyncio.gather(*(one(item) for item in items[:5]))
        accepted=[item for item in results if item is not None]
        if not accepted:raise RetrievalError("search_results_rejected")
        return accepted
