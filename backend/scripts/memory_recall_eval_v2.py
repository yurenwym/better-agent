"""Frozen legacy vs opt-in RRF evaluation on v2; no query rewriting or tuning."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

from memory_recall_eval import (
    ROOT, ANSWER_SYSTEM, LiveClient, answer_score, digest, retrieve, seed, write_json,
)
from app.memory_v2 import MemoryContextProvider

DATA = ROOT / "docs/evaluation/memory-recall-v2"


class SharedQueryEmbedding:
    """Give both arms the exact same real vector; cache is evaluation-only."""
    def __init__(self, provider):
        self.provider, self.cache = provider, {}

    def embed(self, texts):
        key = tuple(texts)
        if key not in self.cache:
            self.cache[key] = self.provider.embed(texts)
        return self.cache[key]


def summarize(rows):
    summary = {}
    for split in ("all", "dev", "holdout"):
        summary[split] = {}
        for arm in ("legacy", "rrf"):
            items = [x for x in rows if x["arm"] == arm and (split == "all" or x["split"] == split)]
            if not items:
                continue
            def mean(key):
                values = [x[key] for x in items if x[key] is not None]
                return round(sum(values) / len(values), 4) if values else None
            summary[split][arm] = dict(cases=len(items), recall_at_5=mean("recall_at_5"),
                injection_recall=mean("injection_recall"), precision=mean("precision"),
                mrr=mean("reciprocal_rank"), correct=sum(x.get("answer_score", {}).get("correct", False) for x in items),
                answered=sum("answer_score" in x for x in items), forbidden_hits=sum(len(x["forbidden_hits"]) for x in items),
                categories={category: {"n": sum(x["category"] == category for x in items),
                    "correct": sum(x["category"] == category and x.get("answer_score", {}).get("correct", False) for x in items)}
                    for category in sorted({x["category"] for x in items})})
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("real PostgreSQL/embedding/DeepSeek experiment requires --execute")
    args.out.mkdir(parents=True, exist_ok=False)
    memories = json.loads((DATA / "memories.json").read_text(encoding="utf-8"))
    cases = json.loads((DATA / "cases.json").read_text(encoding="utf-8"))
    report = dict(status="initializing", rows=[], hashes={f: digest((DATA / f).read_bytes())
        for f in ("memories.json", "cases.json", "manifest.json")},
        source_hashes={f: digest((ROOT / f).read_bytes()) for f in (
            "backend/app/memory_v2.py", "backend/app/config.py", "backend/app/embedding.py",
            "backend/scripts/memory_recall_eval.py", "backend/scripts/memory_recall_eval_v2.py")},
        protocol="100 frozen constructed cases; entity-disjoint question split; shared real query vectors; no reranker/rewrite")
    write_json(args.out / "results.json", report)
    live = None
    try:
        baseline, mapping = seed(args.out, memories, postgres=True, embedding_call_limit=268)
        shared = SharedQueryEmbedding(baseline.embedding_provider)
        baseline.embedding_provider = shared
        providers = dict(legacy=baseline, rrf=MemoryContextProvider(baseline.db,
            embedding_provider=shared, ranking_strategy="rrf"))
        live = LiveClient(args.out, max_calls=200, max_seconds=1800)
        for i, case in enumerate(cases):
            # Alternate execution order; retrieval shares the same query vector.
            for arm in (("legacy", "rrf") if i % 2 == 0 else ("rrf", "legacy")):
                row = dict(case_id=case["id"], split=case["split"], category=case["category"],
                    arm=arm, budget_bytes=case["budget_bytes"],
                    **retrieve(providers[arm], mapping, case, case["query"], memories, case["budget_bytes"]))
                report["rows"].append(row)
                reason = row["trace"].get("fallback_reason")
                if reason not in ("", "no_semantic_candidates", "semantic_quality_below_threshold"):
                    raise RuntimeError("embedding_degraded")
                answer = live.call(case["id"], "answer_" + arm, ANSWER_SYSTEM,
                    {"history": case["history"], "query": case["query"], "memories": row["injected_text"]})
                row["answer"] = answer
                row["answer_score"] = answer_score(case, answer, row["injected_ids"])
            report["summary"] = summarize(report["rows"])
            report["status"] = "running"
            write_json(args.out / "results.json", report)
            print(json.dumps({"completed_cases": i + 1, "calls": len(live.calls)}), flush=True)
        report["status"] = "live_complete"
    except Exception as exc:
        report.update(status="live_incomplete", error_type=type(exc).__name__)
    finally:
        if live:
            live.http.close()
            report["successful_calls"] = sum(c.get("status") == "succeeded" for c in live.calls)
            report["attempted_calls"] = len(live.calls)
            report["usage"] = {key: sum((c.get("usage") or {}).get(key, 0) or 0 for c in live.calls)
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        report["summary"] = summarize(report["rows"])
        write_json(args.out / "results.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False))
    return 0 if report["status"] == "live_complete" else 2


if __name__ == "__main__":
    sys.exit(main())
