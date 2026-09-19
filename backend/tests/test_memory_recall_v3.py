import importlib.util
import json
from pathlib import Path
import sys


def test_new_holdout_entities_and_labels():
    data = Path(__file__).resolve().parents[2] / "docs/evaluation/memory-recall-v3"
    memories = {m["id"]:m for m in json.loads((data / "memories.json").read_text(encoding="utf-8"))}
    cases = json.loads((data / "cases.json").read_text(encoding="utf-8"))
    assert len(memories)==272 and len(cases)==152
    assert sum(c["split"]=="dev" for c in cases)==100
    dev={c["entity"] for c in cases if c["split"]=="dev" and c["entity"]!="unknown"}
    holdout={c["entity"] for c in cases if c["split"]=="holdout" and c["entity"]!="unknown"}
    assert len(holdout)==8 and not dev & holdout
    for c in cases:
        assert bool(c["gold_memory_ids"]) != c["expected_abstention"]
        for mid in c["gold_memory_ids"]:
            m=memories[mid]
            assert (m["owner"],m["project"],m["status"])==(c["owner"],c["project"],"ACTIVE")
            assert mid not in c["forbidden_memory_ids"]
        for m in memories.values():
            if m["owner"]!=c["owner"] or m["project"]!=c["project"] or m["status"]!="ACTIVE":
                assert m["id"] in c["forbidden_memory_ids"]


def test_predeclared_selection_prioritizes_evidence_then_smaller_limits(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    import memory_recall_eval_v3 as evaluation
    configs=[dict(ranking_strategy="legacy",semantic_candidate_limit=d,lexical_candidate_limit=l)
        for d,l in ((10,10),(20,20),(50,50),(20,10))]
    summaries={evaluation.config_name(c):dict(complete_evidence=7,injection_recall=.7,positive_precision=.2,forbidden_hits=0) for c in configs}
    assert evaluation.choose(configs,summaries,"legacy")==configs[0]
    summaries[evaluation.config_name(configs[2])]["complete_evidence"]=8
    assert evaluation.choose(configs,summaries,"legacy")==configs[2]
    summaries[evaluation.config_name(configs[2])]["forbidden_hits"]=1
    assert evaluation.choose(configs,summaries,"legacy")==configs[0]
