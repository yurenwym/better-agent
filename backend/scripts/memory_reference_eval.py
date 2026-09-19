"""Real services: off vs rules vs conditional LLM; no holdout tuning."""
import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from memory_recall_eval import ROOT, ANSWER_SYSTEM, LiveClient, RecordingEmbedding, answer_score, digest, retrieve, write_json
from memory_recall_eval_v2 import SharedQueryEmbedding
from app.db import Database
from app.embedding import OpenAICompatibleEmbeddingClient, load_embedding_profile_from_env
from app.embedding_worker import EmbeddingWorker
from app.memory_v2 import MemoryContextProvider, MemoryStore
from app.memory_reference import resolve
from app.model_gateway import ModelGateway, ModelProfile

DATA = ROOT / "docs/evaluation/memory-reference-v1"


class RecordedGateway:
    def __init__(self, live, out):
        self.gateway = ModelGateway(ModelProfile(live.base, live.model, "DEEPSEEK_API_KEY",
            provider_name="deepseek", max_attempts=1, timeout_seconds=2))
        self.calls, self.out, self.case_id = [], out, None

    async def complete(self, request, **kwargs):
        if len(self.calls) >= 40:
            raise RuntimeError("resolver call limit")
        item = dict(case_id=self.case_id, request=asdict(request), status="started")
        self.calls.append(item)
        write_json(self.out / "resolver_calls.json", self.calls)
        started = time.perf_counter()
        try:
            response = await self.gateway.complete(request, **kwargs)
            item.update(status="succeeded", response=asdict(response))
            return response
        except BaseException as exc:
            item.update(status="failed", error_type=type(exc).__name__, error_kind=getattr(exc,"kind",None))
            raise
        finally:
            item["elapsed_ms"] = (time.perf_counter()-started)*1000
            write_json(self.out / "resolver_calls.json", self.calls)


def summarize(rows):
    result = {}
    for split in ("all", "dev", "holdout"):
        result[split] = {}
        for mode in ("off", "rules", "hybrid"):
            selected=[r for r in rows if r["mode"]==mode and (split=="all" or r["split"]==split)]
            if not selected:continue
            answerable=[r for r in selected if not r["expected_clarification"]]
            ambiguous=[r for r in selected if r["expected_clarification"]]
            result[split][mode] = dict(n=len(selected), answerable=len(answerable),
                correct=sum(r.get("answer_score",{}).get("correct",False) for r in answerable),
                ambiguous=len(ambiguous), correct_clarifications=sum(r["resolution"]["action"]=="clarify" for r in ambiguous),
                unnecessary_clarifications=sum(r["resolution"]["action"]=="clarify" for r in answerable),
                wrong_entity_completions=sum(r["wrong_entity"] for r in selected),
                forbidden_hits=sum(len(r.get("forbidden_hits",[])) for r in selected),
                categories={c:dict(n=len(items),correct=sum(r.get("answer_score",{}).get("correct",False) for r in items if not r["expected_clarification"]))
                    for c in sorted({r["category"] for r in selected}) if (items:=[r for r in selected if r["category"]==c])})
    return result


async def main():
    global DATA
    parser=argparse.ArgumentParser()
    parser.add_argument("--execute",action="store_true")
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--resume-after-dev-fix", action="store_true")
    parser.add_argument("--data", type=Path)
    args=parser.parse_args()
    if args.data is not None:
        DATA=args.data.resolve()
    if not args.execute:parser.error("--execute required")
    args.out.mkdir(parents=True,exist_ok=False)
    memories=json.loads((DATA / "memories.json").read_text(encoding="utf-8"))
    cases=json.loads((DATA / "cases.json").read_text(encoding="utf-8"))
    if args.resume_after_dev_fix:
        # Replay one full six-category dev group, then untouched holdout.
        cases=[c for c in cases if c["group"]==0 or c["split"]=="holdout"]
    report=dict(status="initializing",rows=[],hashes={f:digest((DATA/f).read_bytes()) for f in ("memories.json","cases.json","manifest.json")},
        sources={f:digest((ROOT/f).read_bytes()) for f in ("backend/app/memory_reference.py","backend/app/conversation.py","backend/app/memory_v2.py",
            "backend/app/model_gateway.py","backend/app/model_control.py","backend/scripts/memory_reference_eval.py")})
    write_json(args.out / "results.json",report)
    if args.resume_after_dev_fix:
        report["amendment"]="First run stopped after 51 dev cases, zero holdout rows: invalid combined action example fixed. Reuse indexed fixture; replay dev group 0 then all 60 untouched holdout. Initial failed protocol responses remain in live-01. No ranking/data/budget changes."
    live=None
    try:
        dsn=os.environ["MEMORY_EVAL_DATABASE_URL"]
        if not urlsplit(dsn).path.lstrip("/").startswith("better_memory_eval_") or dsn==os.getenv("DATABASE_URL"):
            raise ValueError("isolated DB required")
        db=Database(dsn,workspace=args.out / "workspace")
        by_content={m["content"]:m for m in memories}
        with db.connection() as c:
            old=c.execute("SELECT r.id,r.content FROM memory_entries e JOIN memory_revisions r ON r.id=e.current_revision_id").fetchall()
            active=c.execute("SELECT model,dimensions FROM embedding_profiles WHERE active=true").fetchone()
        if len(old)!=(312 if args.resume_after_dev_fix else 272):raise ValueError("fixture size mismatch")
        mapping={r["id"]:by_content[r["content"]]["id"] for r in old}
        profile=load_embedding_profile_from_env()
        assert (profile.model,profile.dimensions,profile.timeout_seconds)==(active["model"],active["dimensions"],5)
        embedding=RecordingEmbedding(OpenAICompatibleEmbeddingClient(profile),args.out,max_calls=160)
        store=MemoryStore(db,args.out / "memory")
        for m in memories:
            if m["id"] not in mapping.values():
                entry=store.remember(m["owner"],"fact","project",m["project"],m["content"],m["id"])
                mapping[entry.revision_id]=m["id"]
        worker=EmbeddingWorker(db,embedding,owner="reference-eval")
        for _ in range(0 if args.resume_after_dev_fix else 40):
            lease=worker.claim()
            assert lease is not None
            worker.process(lease)
        with db.connection() as c:
            assert c.execute("SELECT count(*) FROM embedding_jobs WHERE status!='COMPLETED'").fetchone()[0]==0
        shared=SharedQueryEmbedding(embedding)
        provider=MemoryContextProvider(db,embedding_provider=shared,ranking_strategy="rrf")
        live=LiveClient(args.out,max_calls=198 if args.resume_after_dev_fix else 360,max_seconds=1800)
        resolver_gateway=RecordedGateway(live,args.out)
        for i,case in enumerate(cases):
            modes=["off","rules","hybrid"]
            modes=modes[i%3:]+modes[:i%3]
            for mode in modes:
                resolver_gateway.case_id=case["id"]
                before=len(resolver_gateway.calls)
                resolution=await resolve(case["query"],case["history"],mode=mode,gateway=resolver_gateway)
                row=dict(case_id=case["id"],split=case["split"],category=case["category"],mode=mode,
                    expected_clarification=case["expected_clarification"],resolution=resolution.as_dict(),
                    llm_triggered=len(resolver_gateway.calls)>before,
                    wrong_entity=resolution.action=="resolved" and list(resolution.entities)!=[case["gold_entity"]])
                report["rows"].append(row)
                if resolution.action!="clarify":
                    row.update(retrieve(provider,mapping,case,resolution.query,memories,case["budget_bytes"]))
                    if row["trace"]["fallback_reason"] not in ("","no_semantic_candidates","semantic_quality_below_threshold"):
                        raise RuntimeError("embedding degraded")
                    answer=live.call(case["id"],mode,ANSWER_SYSTEM,dict(query=case["query"],
                        history=[{k:m[k] for k in ("role","content")} for m in case["history"]],memories=row["injected_text"]))
                    row["answer"]=answer
                    row["answer_score"]=answer_score(case,answer,row["injected_ids"])
            report.update(status="running",summary=summarize(report["rows"]))
            write_json(args.out / "results.json",report)
            print(json.dumps(dict(completed=i+1,split=case["split"],answer_calls=len(live.calls),resolver_calls=len(resolver_gateway.calls))),flush=True)
        report.update(status="live_complete",resolver_attempts=len(resolver_gateway.calls),
            resolver_successes=sum(c["status"]=="succeeded" for c in resolver_gateway.calls))
    except Exception as exc:
        report.update(status="live_incomplete",error_type=type(exc).__name__)
    finally:
        if live:
            live.http.close()
            report["answer_attempts"]=len(live.calls)
            report["answer_successes"]=sum(c.get("status")=="succeeded" for c in live.calls)
            report["answer_usage"]={k:sum((c.get("usage") or {}).get(k,0) or 0 for c in live.calls) for k in ("prompt_tokens","completion_tokens","total_tokens")}
        report["summary"]=summarize(report["rows"])
        write_json(args.out / "results.json",report)
    print(json.dumps({k:v for k,v in report.items() if k not in ("rows","sources","hashes")},ensure_ascii=False))
    return 0 if report["status"]=="live_complete" else 2


if __name__=="__main__":sys.exit(asyncio.run(main()))
