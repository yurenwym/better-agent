"""Freeze new entity holdout before the candidate-limit sweep; no model calls."""
import json
from pathlib import Path
import random

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs/evaluation/memory-recall-v3"


def main():
    DATA.mkdir(parents=True, exist_ok=False)
    old = ROOT / "docs/evaluation/memory-recall-v2"
    memories = json.loads((old / "memories.json").read_text(encoding="utf-8"))
    cases = json.loads((old / "cases.json").read_text(encoding="utf-8"))
    for case in cases:
        case.update(id="v2_" + case["id"], split="dev")
    rng = random.Random(934721)
    names = ["苍穹", "远帆", "晨露", "流萤", "沧海", "秋水", "逐日", "星芒"]
    def memory(mid, content, status="ACTIVE", owner="eval-user", project="better-eval"):
        memories.append(dict(id=mid, content=content, origin="synthetic", source=None,
            owner=owner, project=project, kind="fact", status=status))
    for i, name in enumerate(names):
        entity = f"{name}服务 SVC{101+i}"
        zone = f"zone-{rng.randrange(110,199)}-{rng.choice('abcd')}"
        timeout, retention, latency = rng.randrange(400,950), rng.randrange(15,65), rng.randrange(105,300)
        maintenance = f"{rng.randrange(1,6):02}:{rng.randrange(60):02}"
        code = f"ERR{rng.randrange(100000,999999)}"
        reason = rng.choice(["任务租约失效", "只读副本同步中断", "索引版本冲突", "分区配额不足"])
        facts = [f"正式环境唯一部署分区为 {zone}。", f"外部查询超时上限为 {timeout} 毫秒。",
            f"回滚备份保留期为 {retention} 天。", f"允许的每日数据库维护开始时间是 {maintenance}。",
            f"错误代号 {code} 表示{reason}。", f"当前检索响应时延目标为 {latency} 毫秒。",
            f"旧检索响应时延目标为 {latency+200} 毫秒，现已失效。", f"旧回滚备份保留期为 {retention+100} 天，已归档。"]
        ids = [f"n{i:02}_{j}" for j in range(8)]
        for j, fact in enumerate(facts):
            memory(ids[j], entity + "的" + fact, "EXPIRED" if j == 6 else "ARCHIVED" if j == 7 else "ACTIVE")
        questions = [
            ("exact", f"{entity}外部查询最多等待多少毫秒？", [ids[1]], [str(timeout)], []),
            ("synonym", f"{entity}目前要求检索多快返回，目标是多少毫秒？", [ids[5]], [str(latency)], []),
            ("code", f"出现 {code} 时具体是什么故障？", [ids[4]], [reason], []),
            ("multi", f"{entity}正式实例在哪个分区，回滚备份保留多少天？", [ids[0],ids[2]], [zone,str(retention)], []),
            ("reference", "那它每天从几点开始可以维护数据库？", [ids[3]], [maintenance],
             [{"role":"user", "content":f"接下来讨论{entity}的正式环境。"}]),
            ("negative", f"{entity}值班负责人的手机号码是多少？", [], [], []),
        ]
        for j, (category, query, gold, answer, history) in enumerate(questions):
            cases.append(dict(id=f"v3_q{i:02}_{j}", split="holdout", entity=entity, category=category,
                owner="eval-user", project="better-eval", query=query, history=history,
                gold_memory_ids=gold, answer_contains_all=answer, forbidden_memory_ids=[],
                expected_abstention=not gold, budget_bytes=(450,750,1500)[(i+j)%3], origin="synthetic"))
        # Same-name alternate environments plus owner/project isolation traps.
        for j in range(5):
            owner = "other-user" if j == 0 else "eval-user"
            project = "other-project" if j == 1 else "better-eval"
            memory(f"nd{i:02}_{j}", f"{entity}在预发布模拟环境的部署分区为 zone-sim-{j}，回滚备份保留 {retention+70+j} 天，"
                f"外部查询超时 {timeout+300+j} 毫秒，检索响应时延目标 {latency+400+j} 毫秒。"
                "这份记录仅用于演练，正式生产环境的部署分区、回滚策略和检索指标必须查阅正式配置。",
                owner=owner, project=project)
    for i, name in enumerate(["霜叶", "碧泉", "松涛", "月桥"]):
        cases.append(dict(id=f"v3_unknown_{i}", split="holdout", entity="unknown", category="negative",
            owner="eval-user", project="better-eval", query=f"{name}服务的正式部署分区和查询超时是多少？",
            history=[], gold_memory_ids=[], answer_contains_all=[], forbidden_memory_ids=[],
            expected_abstention=True, budget_bytes=450, origin="synthetic"))
    forbidden = [m["id"] for m in memories if m["owner"]!="eval-user" or m["project"]!="better-eval" or m["status"]!="ACTIVE"]
    for case in cases:
        case["forbidden_memory_ids"] = sorted(set(case["forbidden_memory_ids"]) | set(forbidden))
    manifest = dict(version="memory-recall-v3", seed=934721, origin="constructed scenarios, not production logs",
        memories=len(memories), cases=len(cases), dev=100, holdout=52,
        split="All v2 cases are dev now; 8 new service entities plus 4 unknowns are untouched holdout; shared corpus",
        strategies=["legacy","rrf"], candidate_limits=[10,20,50,100], rrf_k=60, semantic_min_score=.35,
        embedding_timeout_seconds=5, max_new_embedding_calls=240, max_deepseek_calls=208,
        budgets=[450,750,1500], max_output_tokens=256,
        selection_rule="Within each strategy maximize dev positive full-gold injection rate, then injection recall, then positive precision; tie: smallest dense+lexical limit, then smaller dense, then smaller lexical",
        finalists="legacy50/50, rrf50/50, best dev legacy, best dev rrf; deduplicate identical configs; freeze before any holdout retrieval",
        release_gate="Compare selected finalists to legacy50/50 on new holdout; require more strict correct answers, zero scope leaks, no exact/code/negative count regression; report paired changes, do not tune on holdout",
        unchanged="No query rewrite, scope SQL redesign, reranker or Python fallback removal; no production defaults changed")
    for name, value in (("memories.json",memories),("cases.json",cases),("manifest.json",manifest)):
        (DATA / name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(manifest,ensure_ascii=False))


if __name__ == "__main__":
    main()
