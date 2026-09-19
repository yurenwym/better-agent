"""Bounded M4 live acceptance against the current PostgreSQL database.

Without both --execute and M4_LIVE_APPROVED=1 this module performs no database,
model, or source-network I/O. Test rows are batch-prefixed and precisely removed.
"""
from __future__ import annotations

import argparse, asyncio, hashlib, json, os, time, uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg

from app.behavior import BehaviorBundleService
from app.config import load_llm_ap, load_model_profile_from_env
from app.costs import PriceSnapshot
from app.db import POSTGRES_SCHEMA_HEAD
from app.model_admin import ModelAdminService
from app.research.engine import ResearchEngine
from app.research.live import LiveResearchModel
from app.research.models import ResearchLimits, ResearchRequest, Source
from app.research.retriever import RetrievalError, validate_public_url
from app.research.web import WebSearchRetriever
from app.research.worker import ManagedResearchWorker
from app.startup import build_runtime

BATCH_ID = "M4-LIVE-20260909-003"
MAX_ROUNDS, MAX_MODEL_ATTEMPTS_PER_ROUND, MAX_MODEL_ATTEMPTS = 3, 10, 30
MAX_SOURCE_REQUESTS, MAX_SECONDS = 12, 30 * 60
WORST_ATTEMPT_MICROUSD = 25_232
MAX_COST_MICROUSD = WORST_ATTEMPT_MICROUSD * MAX_MODEL_ATTEMPTS
ROUND_COST_MICROUSD = WORST_ATTEMPT_MICROUSD * MAX_MODEL_ATTEMPTS_PER_ROUND
CASES = (
    {"id":"python-asyncio-v1","topic":"仅依据给定的 Python 官方文档研究 asyncio，必须分别说明：TaskGroup 内某任务首次发生非 CancelledError 异常后其余任务如何处理；asyncio.timeout 如何处理取消并在何处转换为 TimeoutError。","hosts":("docs.python.org",),"urls":("https://docs.python.org/3/library/asyncio-task.html","https://docs.python.org/3/library/asyncio-exceptions.html"),"evidence_terms":(("TaskGroup","cancel"),("TimeoutError","CancelledError"))},
    {"id":"postgres-explain-v1","topic":"仅依据给定的 PostgreSQL 官方文档研究 EXPLAIN，必须分别说明：EXPLAIN ANALYZE 是否实际执行语句以及由此产生的副作用边界；BUFFERS 选项报告什么，以及启用 ANALYZE 时它的默认行为。","hosts":("www.postgresql.org",),"urls":("https://www.postgresql.org/docs/current/sql-explain.html","https://www.postgresql.org/docs/current/using-explain.html"),"evidence_terms":(("ANALYZE","execut"),("BUFFERS","buffer"))},
    {"id":"git-undo-v1","topic":"仅依据给定的 Git 官方文档比较撤销方式，必须分别说明：git revert 如何通过新提交逆转既有提交；git reset 对 HEAD、索引和工作区的作用边界。","hosts":("git-scm.com",),"urls":("https://git-scm.com/docs/git-revert","https://git-scm.com/docs/git-reset"),"evidence_terms":(("revert","commit"),("reset","HEAD"))},
)
class AcceptanceFailure(RuntimeError): pass
def _require(value, message):
    if not value: raise AcceptanceFailure(message)
def _digest(value): return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
def _suite(): return [{k:v for k,v in case.items() if k!="evidence_terms"} for case in CASES]
def _source_manifest():
    root=Path(__file__).resolve().parents[1]
    paths=[*root.joinpath("app","research").rglob("*.py"),root/"app"/"model_control.py",root/"app"/"costs.py",root/"app"/"db.py",Path(__file__).resolve(),root/"alembic"/"versions"/"20260909_0004_research_partial_status.py"]
    files={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths)) if p.exists()}
    return {"files":files,"digest":_digest(files)}

def _profile():
    path=os.getenv("LLM_AP_PATH"); profile=load_llm_ap(path) if path else load_model_profile_from_env()
    _require(profile.model=="deepseek-v4-flash","M4 requires deepseek-v4-flash")
    _require(profile.context_window==32768 and profile.max_output_tokens==8192,"M4 requires frozen context/output limits")
    return replace(profile,max_attempts=1,network_retries=0,timeout_seconds=120,provider_name="deepseek")

def preflight():
    approved=os.getenv("M4_LIVE_APPROVED")=="1"
    result={"batch_id":BATCH_ID,"status":"READY" if approved else "NOT_AUTHORISED","network_started":False,"storage":"current_better_agent_with_batch_scoped_cleanup","source_manifest":_source_manifest(),"case_digest":_digest(_suite()),"cases":_suite(),"limits":{"rounds":MAX_ROUNDS,"model_attempts_per_round":MAX_MODEL_ATTEMPTS_PER_ROUND,"model_attempts":MAX_MODEL_ATTEMPTS,"source_requests":MAX_SOURCE_REQUESTS,"max_seconds":MAX_SECONDS,"fallback_models":0,"network_retries":0,"structured_output_retries":0,"worst_attempt_microusd":WORST_ATTEMPT_MICROUSD,"worst_batch_microusd":MAX_COST_MICROUSD},"price_snapshot":{"source":"M3-LIVE-20260909-022 frozen DeepSeek profile rates","uncached_input_rate":440000,"cache_read_rate":14000,"cache_write_rate":0,"output_rate":1320000,"reasoning_rate":0},"stop_policy":"first failure stops the batch; no automatic rerun"}
    if not approved: result["reason"]="M3 passes; set M4_LIVE_APPROVED=1 only after approving this exact M4 batch and budget"
    else:
        p=_profile(); result["profile"]={"provider":p.provider_name,"model":p.model,"base_url":p.base_url,"context_window":p.context_window,"max_output_tokens":p.max_output_tokens}
    return result

@dataclass
class BatchBudget:
    started:float=field(default_factory=time.monotonic)
    model_attempts:list=field(default_factory=list)
    source_requests:list=field(default_factory=list)
    def remaining(self):
        value=MAX_SECONDS-(time.monotonic()-self.started)
        if value<=0: raise AcceptanceFailure("batch duration exceeded")
        return value
    async def model_attempt(self,round_number,profile,request,**kwargs):
        self.remaining(); in_round=sum(x["round"]==round_number for x in self.model_attempts)
        _require(in_round<MAX_MODEL_ATTEMPTS_PER_ROUND,"round model-attempt limit reached")
        _require(len(self.model_attempts)<MAX_MODEL_ATTEMPTS,"batch model-attempt limit reached")
        record={"ordinal":len(self.model_attempts)+1,"round":round_number,"role":request.role,"purpose":request.purpose,"status":"started","request_digest":_digest({"messages":request.messages,"temperature":request.temperature,"max_tokens":request.max_tokens,"thinking":request.thinking,"response_format":getattr(request,"response_format",None)})}
        self.model_attempts.append(record)
        try: response=await asyncio.wait_for(runtime_attempt(profile,request,**kwargs),timeout=min(float(profile.timeout_seconds),self.remaining()))
        except Exception as exc: record.update(status="failed",error_kind=getattr(exc,"kind",type(exc).__name__)); raise
        record.update(status="succeeded",output_digest=_digest(response.message),finish_reason=response.finish_reason); return response
    def begin_source(self,round_number,url):
        self.remaining(); _require(len(self.source_requests)<MAX_SOURCE_REQUESTS,"source-request limit reached")
        record={"ordinal":len(self.source_requests)+1,"round":round_number,"url":url,"status":"started"}; self.source_requests.append(record); return record

async def runtime_attempt(profile,request,**kwargs):
    from app.model_control import RoutedModelGateway
    return await RoutedModelGateway._execute_http_attempt(profile,request,**kwargs)

class FrozenOfficialRetriever:
    def __init__(self,case,round_number,budget): self.case,self.round_number,self.budget,self._task=case,round_number,budget,None
    async def retrieve(self,query,request):
        if self._task is None: self._task=asyncio.create_task(self._load(request.job_id))
        return await asyncio.shield(self._task)
    async def _load(self,job_id):
        headers={"User-Agent":"Better-Agent-M4-Acceptance/1.0","Accept-Language":"en-US,en;q=0.8"}; sources=[]
        async with httpx.AsyncClient(timeout=25,follow_redirects=False,headers=headers) as client:
            for ordinal,requested in enumerate(self.case["urls"],1):
                current=await validate_public_url(requested); response=None
                for _ in range(3):
                    record=self.budget.begin_source(self.round_number,current)
                    try: response=await asyncio.wait_for(client.get(current),timeout=min(25,self.budget.remaining()))
                    except Exception as exc: record.update(status="failed",error=type(exc).__name__); raise RetrievalError("source_unavailable") from exc
                    record.update(status="received",http_status=response.status_code)
                    if response.status_code not in {301,302,303,307,308}: break
                    current=await validate_public_url(str(httpx.URL(current).join(response.headers.get("location",""))))
                    _require(urlsplit(current).hostname in self.case["hosts"],"official source redirected outside frozen host")
                _require(response is not None and response.status_code==200,f"official source unavailable: {requested}")
                _require(urlsplit(current).hostname in self.case["hosts"],"official source host escaped frozen scope")
                _require(any(x in response.headers.get("content-type","").lower() for x in ("text/html","text/plain")),"official source is not text")
                raw=response.content[:1000000].decode(response.encoding or "utf-8",errors="replace"); text=WebSearchRetriever._text(raw)[:60000]
                _require(len(text)>=300,"official source extracted text is too short")
                digest=hashlib.sha256(text.encode()).hexdigest(); source_id="source_"+hashlib.sha256(f"{BATCH_ID}:{self.case['id']}:{current}:{digest}".encode()).hexdigest()
                record.update(status="succeeded",final_url=current,content_hash=digest,bytes=len(response.content))
                sources.append(Source(source_id,ordinal,"web",current,requested,self.case["id"]+f" official source {ordinal}",text,None,datetime.now(timezone.utc).isoformat(),.95,digest,{"provider":"frozen_direct_url","case_id":self.case["id"],"query":self.case["topic"]}))
        return sources

def _current_database():
    url=os.getenv("DATABASE_URL","").strip(); _require(url,"DATABASE_URL must identify current PostgreSQL")
    with psycopg.connect(url) as c:
        name=c.execute("SELECT current_database()").fetchone()[0]; head=c.execute("SELECT version_num FROM alembic_version").fetchone(); stable=c.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()
    _require(name=="better_agent",f"M4 requires current better_agent database, got {name}"); _require(head and head[0]==POSTGRES_SCHEMA_HEAD,"PostgreSQL schema is not at required head"); _require(stable and stable[0],"stable bundle missing")
    return url,str(name),str(stable[0])

def _configure(runtime,profile,owner):
    os.environ["AGENT_MODEL_CAPABILITIES"]="streaming,tool_calling,json_object"; admin=ModelAdminService(runtime.db,owner_id=owner); registered=admin.ensure_profile(profile)
    policy=admin.ensure_policy(f"{BATCH_ID} researcher route",{"researcher":{"primary":registered.registered_profile_version_id,"fallback":[]}})
    snapshot_id=f"{BATCH_ID}-price-{uuid.uuid4().hex[:12]}"; runtime.costs.register_price(registered.registered_profile_version_id,PriceSnapshot(snapshot_id,440000,14000,0,1320000,0),"2026-09-09T00:00:00+00:00")
    runtime.costs.set_budget(owner,"INVOCATION","default",WORST_ATTEMPT_MICROUSD); runtime.costs.set_budget(owner,"DAILY",runtime.costs.today_period(),ROUND_COST_MICROUSD)
    manifest={**runtime.behavior.active("stable").manifest,"code":_source_manifest()["digest"],"fallback_models":[],"model_routing":{"policy_id":policy["id"],"digest":policy["policy_digest"]},"model_role_bindings":policy["roles"],"model_price_snapshot_id":snapshot_id,"acceptance_batch":BATCH_ID}
    return BehaviorBundleService(runtime.db).ensure(manifest).id,snapshot_id

def _round_cost(url,owner):
    with psycopg.connect(url) as c: rows=c.execute("SELECT a.status,a.error_kind,a.cost_status,a.cost_microusd,a.price_snapshot_id,i.purpose FROM model_attempts a JOIN model_invocations i ON i.id=a.invocation_id WHERE i.owner_id=%s ORDER BY a.started_at,a.id",(owner,)).fetchall()
    return {"attempt_count":len(rows),"charged_microusd":sum(int(r[3] or 0) for r in rows),"all_priced":all(r[3] is not None for r in rows),"attempts":[{"status":r[0],"error_kind":r[1],"cost_status":r[2],"cost_microusd":r[3],"price_snapshot_id":r[4],"purpose":r[5]} for r in rows]}

def _audit_round(job,sources,evidence,case,bundle_id,database_url):
    matrix=list(job.traceability); _require(job.status=="COMPLETED",f"core case ended as {job.status}"); _require(len(matrix)==2 and all(r.get("supported") is True for r in matrix),"traceability matrix incomplete")
    ids={s.id for s in sources}; _require(len(sources)==2 and all(urlsplit(s.canonical_url or "").hostname in case["hosts"] for s in sources),"source scope mismatch"); _require(all(s.content_hash and s.retrieved_at for s in sources),"source version missing")
    evidence_ids={e.id for e in evidence}
    for row in matrix:
        _require(set(row["source_ids"])==set(row["citation_source_ids"]),"citation/source mismatch"); _require(set(row["source_ids"])<=ids,"non-frozen source cited"); _require(row.get("evidence_locations") and all(x["char_start"]>=0 for x in row["evidence_locations"]),"original evidence location missing"); _require(all(x in evidence_ids for x in row["evidence_ids"]),"matrix evidence missing")
    texts=[e.text.casefold() for e in evidence]; semantic=[any(all(term.casefold() in text for term in terms) for text in texts) for terms in case["evidence_terms"]]; _require(all(semantic),"frozen semantic evidence gate failed")
    with psycopg.connect(database_url) as c: pinned=c.execute("SELECT runtime_bundle_id FROM turns WHERE id=%s",(job.source_turn_id,)).fetchone()[0]
    _require(pinned==bundle_id,"research turn lost runtime bundle")
    return {"status":"PASSED","job_status":job.status,"runtime_bundle_id":pinned,"source_versions":[{"url":s.canonical_url,"content_hash":s.content_hash,"retrieved_at":s.retrieved_at} for s in sources],"evidence_count":len(evidence),"traceability":matrix,"semantic_checks":semantic,"report_digest":_digest(job.report_markdown or "")}

async def _run_round(root,database_url,profile,case,round_number,budget):
    owner=f"{BATCH_ID.lower()}-round-{round_number}-{uuid.uuid4().hex[:8]}"; runtime=build_runtime(root,profile=profile,database_url=database_url,activate_stable=False); runtime.evolution=None; bundle_id,snapshot_id=_configure(runtime,profile,owner)
    gateway=runtime.conversation.route_model.gateway; gateway._execute_attempt=lambda p,r,**kw:budget.model_attempt(round_number,p,r,**kw)
    runtime.research.engine=ResearchEngine(LiveResearchModel(gateway,structured_attempts=1),FrozenOfficialRetriever(case,round_number,budget)); worker=ManagedResearchWorker(runtime.research,lease_seconds=180,limits=ResearchLimits(max_sections=2,max_queries=2,max_sources=2,max_evidence=12,reflection_rounds=0))
    thread=runtime.conversation.create_thread(f"{BATCH_ID} {case['id']}",owner_id=owner); job=runtime.research.create_manual(thread.id,case["topic"],f"{BATCH_ID}:{case['id']}",( "web",),runtime_bundle_id=bundle_id)
    try:
        _require(await worker.run_once(job.id),"dedicated worker did not claim job"); finished=runtime.research.get(job.id); _,sources,evidence,_=runtime.research.recovery_context(job.id)
        try: audit=_audit_round(finished,sources,evidence,case,bundle_id,database_url)
        except Exception as exc:
            exc.round_diagnostics={"round":round_number,"case_id":case["id"],"job_status":finished.status,"phase":finished.phase,"missing_requirements":list(finished.missing_requirements),"source_count":finished.source_count,"evidence_count":finished.evidence_count,"traceability":[{"requirement":row.get("requirement"),"supported":row.get("supported"),"evidence_ids":row.get("evidence_ids",[]),"source_ids":row.get("source_ids",[]),"citation_source_ids":row.get("citation_source_ids",[])} for row in finished.traceability]}
            raise
        cost=_round_cost(database_url,owner); _require(cost["attempt_count"]<=MAX_MODEL_ATTEMPTS_PER_ROUND,"round attempt gate failed"); _require(cost["all_priced"] and cost["charged_microusd"]<=ROUND_COST_MICROUSD,"round cost gate failed")
        return {"round":round_number,"case_id":case["id"],"owner":owner,"price_snapshot_id":snapshot_id,"audit":audit,"cost":cost}
    finally: runtime.db.close()

def _batch_cost(url):
    with psycopg.connect(url) as c: rows=c.execute("SELECT a.cost_microusd FROM model_attempts a JOIN model_invocations i ON i.id=a.invocation_id WHERE i.owner_id LIKE %s",(BATCH_ID.lower()+"-%",)).fetchall()
    return {"attempt_count":len(rows),"charged_microusd":sum(int(r[0] or 0) for r in rows),"all_priced":all(r[0] is not None for r in rows)}

def cleanup_batch(url,stable):
    pattern=BATCH_ID.lower()+"-%"; removed=0
    with psycopg.connect(url) as c:
        def ex(sql,args=()):
            nonlocal removed
            cur=c.execute(sql,args); removed+=max(cur.rowcount,0)
        threads="SELECT id FROM threads WHERE owner_id LIKE %s"; turns=f"SELECT id FROM turns WHERE thread_id IN ({threads})"; invocations="SELECT id FROM model_invocations WHERE owner_id LIKE %s"; profiles="SELECT id FROM model_profiles WHERE owner_id LIKE %s"; versions=f"SELECT id FROM model_profile_versions WHERE profile_id IN ({profiles})"; bundles="SELECT id FROM runtime_bundles WHERE manifest_json::jsonb ->> 'acceptance_batch'=%s"
        c.execute("UPDATE runtime_channels SET bundle_id=%s,version=version+1,updated_at=clock_timestamp() WHERE name='stable' AND bundle_id IS DISTINCT FROM %s",(stable,stable))
        c.execute("ALTER TABLE thread_events DISABLE TRIGGER thread_events_append_only"); c.execute("ALTER TABLE cost_ledger DISABLE TRIGGER cost_ledger_append_only")
        ex(f"DELETE FROM notification_deliveries WHERE job_id IN (SELECT id FROM research_jobs WHERE thread_id IN ({threads}))",(pattern,)); ex(f"DELETE FROM research_jobs WHERE thread_id IN ({threads})",(pattern,)); ex(f"DELETE FROM turn_metrics WHERE turn_id IN ({turns})",(pattern,)); ex(f"DELETE FROM turn_asks WHERE turn_id IN ({turns})",(pattern,)); ex(f"DELETE FROM turn_jobs WHERE thread_id IN ({threads})",(pattern,)); ex(f"DELETE FROM thread_events WHERE thread_id IN ({threads})",(pattern,)); ex(f"DELETE FROM thread_messages WHERE thread_id IN ({threads})",(pattern,)); ex(f"DELETE FROM turns WHERE thread_id IN ({threads})",(pattern,)); ex("DELETE FROM threads WHERE owner_id LIKE %s",(pattern,)); ex("DELETE FROM memory_context_pins WHERE owner_id LIKE %s",(pattern,)); ex("DELETE FROM cost_ledger WHERE owner_id LIKE %s",(pattern,)); ex("DELETE FROM cost_budgets WHERE owner_id LIKE %s",(pattern,)); ex(f"DELETE FROM model_attempts WHERE invocation_id IN ({invocations})",(pattern,)); ex("DELETE FROM model_invocations WHERE owner_id LIKE %s",(pattern,)); ex("DELETE FROM task_budget_roots WHERE owner_id LIKE %s",(pattern,)); ex(f"DELETE FROM model_price_snapshots WHERE profile_version_id IN ({versions})",(pattern,)); ex(f"DELETE FROM model_profile_versions WHERE profile_id IN ({profiles})",(pattern,)); ex("DELETE FROM model_profiles WHERE owner_id LIKE %s",(pattern,)); ex("DELETE FROM model_routing_policies WHERE owner_id LIKE %s",(pattern,)); ex(f"DELETE FROM runtime_channel_events WHERE from_bundle_id IN ({bundles}) OR to_bundle_id IN ({bundles})",(BATCH_ID,BATCH_ID)); ex(f"DELETE FROM runtime_bundles WHERE id IN ({bundles})",(BATCH_ID,)); c.execute("ALTER TABLE cost_ledger ENABLE TRIGGER cost_ledger_append_only"); c.execute("ALTER TABLE thread_events ENABLE TRIGGER thread_events_append_only")
        _require(c.execute("SELECT COUNT(*) FROM threads WHERE owner_id LIKE %s",(pattern,)).fetchone()[0]==0,"M4 rows remain"); _require(c.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()[0]==stable,"stable bundle changed")
    return {"test_rows_removed":removed,"stable_bundle_unchanged":True}

async def _execute(output):
    prior=Path(__file__).resolve().parents[2]/"docs"/"acceptance"/"m3-live-results-2026-09-09-022.json"; _require(prior.exists() and json.loads(prior.read_text(encoding="utf-8"))["status"]=="PASSED","M3 final evidence missing")
    os.environ.update(ROOT_TASK_MAX_ATTEMPTS=str(MAX_MODEL_ATTEMPTS_PER_ROUND),ROOT_TASK_MAX_COST_MICROUSD=str(ROUND_COST_MICROUSD),ROOT_TASK_DEADLINE_MINUTES="30")
    for name in ("AGENT_FALLBACK_MODEL_BASE_URL","AGENT_FALLBACK_MODEL_ID","AGENT_FALLBACK_MODEL_API_KEY","AGENT_FALLBACK_MODEL_API_KEY_ENV"): os.environ.pop(name,None)
    profile=_profile(); url,name,stable=_current_database(); budget=BatchBudget(); report={**preflight(),"status":"RUNNING","database":{"name":name,"mode":"current_runtime"},"rounds":[]}
    try:
        for n,case in enumerate(CASES,1): report["rounds"].append(await _run_round(output.parent/f"m4-round-{n}",url,profile,case,n,budget))
        cost=_batch_cost(url); _require(cost["attempt_count"]<=MAX_MODEL_ATTEMPTS,"batch attempt gate failed"); _require(cost["all_priced"] and cost["charged_microusd"]<=MAX_COST_MICROUSD,"batch cost gate failed"); report.update(status="PASSED",stage_complete=True,cost_evidence=cost)
    except Exception as exc:
        report.update(status="FAILED",failure={"type":type(exc).__name__,"message":str(exc)[:500]},cost_evidence=_batch_cost(url))
        if getattr(exc,"round_diagnostics",None): report["failed_round"]=exc.round_diagnostics
        raise
    finally:
        try: report["cleanup"]=cleanup_batch(url,stable)
        except Exception as exc: report["cleanup"]={"error":str(exc)[:500]}; report["status"]="FAILED"
        report.update(elapsed_seconds=round(time.monotonic()-budget.started,6),network_started=bool(budget.model_attempts or budget.source_requests),model_network_attempts=budget.model_attempts,source_network_requests=budget.source_requests); output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    return 0

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--output",type=Path,required=True); parser.add_argument("--execute",action="store_true"); args=parser.parse_args()
    if not args.execute:
        result=preflight(); args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(result,ensure_ascii=False)); return 0
    if os.getenv("M4_LIVE_APPROVED")!="1": raise SystemExit("M4 live batch refused: approve exact budget and set M4_LIVE_APPROVED=1")
    return asyncio.run(_execute(args.output.resolve()))
if __name__=="__main__": raise SystemExit(main())
