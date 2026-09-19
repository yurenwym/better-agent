"""Dev-only 32-config top-k sweep, then frozen finalists on new live holdout."""
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import statistics
import sys
from urllib.parse import urlsplit

from memory_recall_eval import ROOT, ANSWER_SYSTEM, LiveClient, RecordingEmbedding, answer_score, digest, retrieve, write_json
from memory_recall_eval_v2 import SharedQueryEmbedding
from app.db import Database
from app.embedding import OpenAICompatibleEmbeddingClient, load_embedding_profile_from_env
from app.embedding_worker import EmbeddingWorker
from app.memory_v2 import MemoryContextProvider, MemoryStore

DATA = ROOT / "docs/evaluation/memory-recall-v3"


def config_name(config):
    return f"{config['ranking_strategy']}_d{config['semantic_candidate_limit']}_l{config['lexical_candidate_limit']}"


def stats(rows):
    positives = [r for r in rows if r["injection_recall"] is not None]
    def mean(items, key):
        return sum(r[key] for r in items) / len(items) if items else None
    times = sorted(r["retrieval_ms"] for r in rows)
    return dict(n=len(rows), positive_n=len(positives),
        complete_evidence=sum(r["injection_recall"] == 1 for r in positives),
        injection_recall=mean(positives,"injection_recall"), recall_at_5=mean(positives,"recall_at_5"),
        positive_precision=mean(positives,"precision"),
        forbidden_hits=sum(len(r["forbidden_hits"]) for r in rows),
        correct=sum(r.get("answer_score",{}).get("correct",False) for r in rows),
        answered=sum("answer_score" in r for r in rows),
        retrieval_p50_ms=statistics.median(times), retrieval_p95_ms=times[math.ceil(.95*len(times))-1],
        average_injected=sum(len(r["injected_ids"]) for r in rows)/len(rows),
        average_python_fallback=sum(r["trace"]["python_fallback_candidate_count"] for r in rows)/len(rows),
        categories={category:dict(n=len(items),correct=sum(r.get("answer_score",{}).get("correct",False) for r in items))
            for category in sorted({r["category"] for r in rows}) if (items:=[r for r in rows if r["category"]==category])})


def choose(configs, summaries, strategy):
    eligible=[c for c in configs if c["ranking_strategy"]==strategy and not summaries[config_name(c)]["forbidden_hits"]]
    def order(c):
        s=summaries[config_name(c)]
        d,l=c["semantic_candidate_limit"],c["lexical_candidate_limit"]
        return (-s["complete_evidence"],-s["injection_recall"],-s["positive_precision"],d+l,d,l)
    return min(eligible,key=order)


def prepare_fixture(out, memories):
    dsn=os.getenv("MEMORY_EVAL_DATABASE_URL","")
    if not urlsplit(dsn).path.lstrip("/").startswith("better_memory_eval_") or dsn==os.getenv("DATABASE_URL"):
        raise ValueError("isolated evaluation database required")
    db=Database(dsn,workspace=out / "workspace")
    old=json.loads((ROOT / "docs/evaluation/memory-recall-v2/memories.json").read_text(encoding="utf-8"))
    expected={m["content"]:m for m in old}
    with db.connection() as c:
        rows=c.execute("SELECT e.owner_id,e.scope_id,e.status,e.valid_until,r.id,r.content FROM memory_entries e JOIN memory_revisions r ON r.id=e.current_revision_id").fetchall()
        active=c.execute("SELECT model,dimensions FROM embedding_profiles WHERE active=true").fetchone()
        unfinished=c.execute("SELECT count(*) FROM embedding_jobs WHERE status!='COMPLETED'").fetchone()[0]
    if len(rows)!=168 or unfinished:
        raise ValueError("fixture must be a fresh clone of complete v2 fixture")
    mapping={}
    for row in rows:
        m=expected[row["content"]]
        if row["owner_id"]!=m["owner"] or row["scope_id"]!=(m["project"] or "") or row["status"]!=("ACTIVE" if m["status"]=="EXPIRED" else m["status"]):
            raise ValueError("fixture metadata mismatch")
        if m["status"]=="EXPIRED" and not str(row["valid_until"]).startswith("2020-01-01"):
            raise ValueError("fixture expiry mismatch")
        mapping[row["id"]]=m["id"]
    profile=load_embedding_profile_from_env()
    if (active["model"],active["dimensions"])!=(profile.model,profile.dimensions) or profile.timeout_seconds!=5:
        raise ValueError("frozen embedding profile mismatch")
    embedding=RecordingEmbedding(OpenAICompatibleEmbeddingClient(profile),out,max_calls=240)
    store=MemoryStore(db,out / "memory")
    for m in memories:
        if m["id"] in mapping.values():
            continue
        entry=store.remember(m["owner"],m["kind"],"project" if m["project"] else "user",m["project"] or "",m["content"],m["id"])
        mapping[entry.revision_id]=m["id"]
        if m["status"]=="ARCHIVED":
            store.set_status(entry.id,m["owner"],"ARCHIVED")
        elif m["status"]=="EXPIRED":
            with db.transaction() as c:
                c.execute("UPDATE memory_entries SET valid_until=? WHERE id=?",("2020-01-01",entry.id))
    worker=EmbeddingWorker(db,embedding,owner="memory-recall-v3")
    for i in range(len(memories)-len(old)):
        lease=worker.claim()
        if lease is None:
            raise RuntimeError("missing fixture job")
        worker.process(lease)
        if (i+1)%20==0:
            print(json.dumps(dict(stage="fixture",attempted=i+1)),flush=True)
    with db.connection() as c:
        if c.execute("SELECT count(*) FROM embedding_jobs WHERE status!='COMPLETED'").fetchone()[0]:
            raise RuntimeError("fixture embedding incomplete; inspect logs before retrying")
    return db,mapping,SharedQueryEmbedding(embedding)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--execute",action="store_true")
    args=parser.parse_args()
    if not args.execute:
        parser.error("--execute required for real service calls")
    args.out.mkdir(parents=True,exist_ok=False)
    memories=json.loads((DATA / "memories.json").read_text(encoding="utf-8"))
    cases=json.loads((DATA / "cases.json").read_text(encoding="utf-8"))
    manifest=json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    configs=[dict(ranking_strategy=s,semantic_candidate_limit=d,lexical_candidate_limit=l)
        for s in manifest["strategies"] for d in manifest["candidate_limits"] for l in manifest["candidate_limits"]]
    report=dict(status="initializing",dataset_hashes={f:digest((DATA/f).read_bytes()) for f in ("memories.json","cases.json","manifest.json")},
        source_hashes={f:digest((ROOT/f).read_bytes()) for f in ("backend/app/memory_v2.py","backend/scripts/memory_recall_eval.py","backend/scripts/memory_recall_eval_v2.py","backend/scripts/memory_recall_eval_v3.py")},
        manifest=manifest,dev_rows=[],holdout_rows=[])
    live=None
    write_json(args.out / "results.json",report)
    try:
        db,mapping,shared=prepare_fixture(args.out,memories)
        providers={config_name(c):MemoryContextProvider(db,embedding_provider=shared,**c) for c in configs}
        def run_retrieval(case,config):
            name=config_name(config)
            row=dict(case_id=case["id"],category=case["category"],split=case["split"],arm=name,budget_bytes=case["budget_bytes"],
                **retrieve(providers[name],mapping,case,case["query"],memories,case["budget_bytes"]))
            trace=row["trace"]
            if trace["fallback_reason"] not in ("","no_semantic_candidates","semantic_quality_below_threshold"):
                raise RuntimeError("embedding degraded")
            if trace["semantic_candidate_count"]>config["semantic_candidate_limit"] or trace["lexical_raw_candidate_count"]>config["lexical_candidate_limit"]:
                raise RuntimeError("candidate SQL cap violated")
            return row
        for i,case in enumerate(c for c in cases if c["split"]=="dev"):
            shared.embed([case["query"].strip()])  # Every timed selector sees a cached identical vector.
            order=configs[i%len(configs):]+configs[:i%len(configs)]
            for config in order:
                report["dev_rows"].append(run_retrieval(case,config))
            if (i+1)%10==0:
                report["status"]="dev_running"
                write_json(args.out / "results.json",report)
                print(json.dumps(dict(stage="dev",completed_cases=i+1,selector_calls=len(report["dev_rows"]))),flush=True)
        summaries={config_name(c):stats([r for r in report["dev_rows"] if r["arm"]==config_name(c)]) for c in configs}
        best=[choose(configs,summaries,s) for s in manifest["strategies"]]
        fixed=[c for c in configs if c["semantic_candidate_limit"]==c["lexical_candidate_limit"]==50]
        finalists=list({config_name(c):c for c in fixed+best}.values())
        selection=dict(dev_summary=summaries,best_by_strategy=best,finalists=finalists,
            reason=manifest["selection_rule"],holdout_not_read_by_selector=True)
        write_json(args.out / "selection_frozen.json",selection)
        report.update(status="finalists_frozen",selection=selection,selection_sha256=digest((args.out / "selection_frozen.json").read_bytes()))
        write_json(args.out / "results.json",report)
        print(json.dumps(dict(stage="finalists_frozen",finalists=finalists)),flush=True)
        live=LiveClient(args.out,max_calls=208,max_seconds=1800)
        for i,case in enumerate(c for c in cases if c["split"]=="holdout"):
            shared.embed([case["query"].strip()])
            order=finalists[i%len(finalists):]+finalists[:i%len(finalists)]
            for config in order:
                row=run_retrieval(case,config)
                report["holdout_rows"].append(row)
                row["answer"]=live.call(case["id"],row["arm"],ANSWER_SYSTEM,
                    dict(history=case["history"],query=case["query"],memories=row["injected_text"]))
                row["answer_score"]=answer_score(case,row["answer"],row["injected_ids"])
            report["status"]="holdout_running"
            write_json(args.out / "results.json",report)
            print(json.dumps(dict(stage="holdout",completed_cases=i+1,calls=len(live.calls))),flush=True)
        report["holdout_summary"]={config_name(c):stats([r for r in report["holdout_rows"] if r["arm"]==config_name(c)]) for c in finalists}
        report["status"]="live_complete"
    except Exception as exc:
        report.update(status="live_incomplete",error_type=type(exc).__name__)
    finally:
        if live:
            live.http.close()
            report["successful_calls"]=sum(c.get("status")=="succeeded" for c in live.calls)
            report["attempted_calls"]=len(live.calls)
            report["usage"]={k:sum((c.get("usage") or {}).get(k,0) or 0 for c in live.calls) for k in ("prompt_tokens","completion_tokens","total_tokens")}
        write_json(args.out / "results.json",report)
    print(json.dumps({k:v for k,v in report.items() if k in ("status","error_type","holdout_summary","usage","successful_calls")},ensure_ascii=False))
    return 0 if report["status"]=="live_complete" else 2


if __name__=="__main__":
    sys.exit(main())
