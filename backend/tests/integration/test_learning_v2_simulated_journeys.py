"""Frozen synthetic journeys through the production compiler and memory service."""
import asyncio
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

TASKS = [
    ("英语听力", "听一段新闻，记录三个未听懂的表达，复听并核对文字稿"),
    ("线性代数", "完成矩阵乘法练习，检查维度并整理错题"),
    ("Python 异步", "阅读协程示例，运行两个任务并记录取消行为"),
    ("数据库索引", "比较两条查询的执行计划，写出索引选择依据"),
    ("研究报告", "根据已有资料列出结论、证据和未知点，形成报告提纲"),
    ("文献对照", "对照两篇已提供文章的方法和局限，完成比较表"),
    ("接口调试", "复现接口错误，检查输入与日志并写出复现步骤"),
    ("数据清理", "检查给定表格的缺失值与重复项，记录处理规则"),
    ("项目文档", "整理现有模块职责，补充一个调用链说明"),
    ("资料归档", "为本地学习资料分类，生成目录和未分类清单"),
    ("算法复习", "完成一道二分查找题，验证边界并整理错误原因"),
    ("演讲准备", "整理三点核心结论，制作提纲并计时演练"),
]


@pytest.mark.skipif(os.getenv("RUN_LEARNING_V2_SIMULATED") != "1", reason="explicit simulated live journeys required")
def test_simulated_live_journeys(tmp_path, migrated_postgres_url):
    from app.config import load_llm_ap
    from app.costs import PriceSnapshot
    from app.startup import build_runtime

    output = Path(os.environ["LEARNING_V2_SIMULATED_REPORT"])
    frozen_path = output.with_suffix(".plan.json")
    if output.exists() or frozen_path.exists():
        pytest.fail("frozen journey batch already exists; no silent rerun")
    cap = 900000  # Prior authorized live experiments used $0.010607; combined maximum < $1.
    frozen_path.write_text(json.dumps({"tasks": TASKS, "provenance": "acceptance", "cap_microusd": cap,
                                       "stages": ["baseline", "learn_30", "correct_20"], "max_calls": 72,
                                       "checks": ["nonempty_actions", "daily_total_60", "each_action_effective_limit", "revision_in_request", "forget_not_resurrected"]},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
    profile = replace(load_llm_ap(os.environ["LLM_AP_PATH"], model_id="deepseek-flash"), max_attempts=1, network_retries=0, timeout_seconds=90, provider_name="deepseek")
    runtime = build_runtime(tmp_path, database_url=migrated_postgres_url, profile=profile)
    runtime.goal_programs.expert_advisor = None
    base = runtime.behavior.active("stable")
    prices = json.loads(Path(os.environ["LEARNING_V2_LIVE_PRICE_FILE"]).read_text(encoding="utf-8"))
    assert prices["base_url"] == profile.base_url and prices["model_name"] == profile.model
    for version in {item["primary"] for item in base.manifest["model_role_bindings"].values()}:
        runtime.costs.register_price(version, PriceSnapshot("journey-" + version, **prices["rates_microusd_per_million_tokens"]))
    runtime.costs.set_budget("local-user", "DAILY", runtime.costs.today_period(), cap)
    runtime.costs.set_budget("local-user", "MONTHLY", runtime.costs.today_period()[:7], cap)
    root = runtime.costs.create_root_budget("local-user", "evaluation", "simulated-v2-journeys", max_attempts=72,
                                           deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(), limit_microusd=cap)
    original = runtime.goal_programs._model_context
    runtime.goal_programs._model_context = lambda *a, **k: replace(original(*a, **k), root_budget_id=root["id"])
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["memory"])
    report = {"status": "RUNNING", "provenance": "acceptance", "real_model": True, "production_release_eligible": False,
              "records": [], "root_budget_id": root["id"], "limitations": "Synthetic task/feedback, real planner; does not measure humans actually completing work or full research retrieval."}
    def save():
        report["costs"] = runtime.costs.summary("local-user", "ROOT", root["id"])
        with runtime.db.connection() as connection:
            report["attempts"] = connection.execute("SELECT attempts_started FROM task_budget_roots WHERE id=?", (root["id"],)).fetchone()[0]
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    def thread(title, project):
        item = runtime.conversation.create_thread(title)
        with runtime.db.transaction() as connection:
            connection.execute("UPDATE threads SET project_id=? WHERE id=?", (project, item.id))
        return item
    def teach(project, minutes):
        item = thread("模拟用户反馈", project)
        runtime.conversation.accept_turn(item.id, "feedback", f"这个项目每次练习最多{minutes}分钟")
        runtime.learning.run_once()
        constraint = runtime.learning.planning_constraints("local-user", project)
        assert constraint and constraint["value"] == minutes
        return constraint
    async def compile_task(title, objective, project, stage):
        item = thread(title, project)
        document = runtime.plan_documents.save_model_revision(thread_id=item.id, title=title,
            markdown_content=f"# {title}\n今天的目标：{objective}。总时长不超过60分钟，保留这些交付，按可独立完成的步骤安排。",
            source_turn_id=None, source_message_id=None, actor="user")
        return await runtime.goal_programs.preview(document.plan_document_id, start_date="2026-09-10", requested_end_date="2026-09-10",
            timezone_name="Asia/Shanghai", daily_minutes=60, idempotency_key=project + stage)
    try:
        save()
        for index, (title, objective) in enumerate(TASKS):
            project = f"acceptance-project-{index}"
            record = {"task": title, "stages": [], "failures": []}
            report["records"].append(record)
            for stage, limit in (("baseline", 60), ("learn_30", 30), ("correct_20", 20)):
                constraint = teach(project, limit) if stage != "baseline" else None
                try:
                    result = asyncio.run(compile_task(title, objective, project, stage))
                    actions = result["structure"]["actions"]
                    passed = bool(actions) and sum(a["estimated_minutes"] for a in actions) <= 60 and all(a["estimated_minutes"] <= limit for a in actions)
                    with runtime.db.connection() as connection:
                        events = connection.execute("SELECT data_json FROM goal_program_events WHERE program_id=? AND type='memory.context_included'", (result["id"],)).fetchall()
                    included = [json.loads(row[0]) for row in events]
                    if constraint:
                        passed = passed and any(constraint["revision_id"] in e["revision_ids"] for e in included)
                    record["stages"].append({"stage": stage, "passed": passed, "actions": actions, "memory_included": included})
                    if not passed:
                        record["failures"].append(stage + ": output contract failed")
                except Exception as exc:
                    record["failures"].append(stage + ": " + type(exc).__name__ + ": " + str(exc)[:200])
                    if getattr(exc, "kind", "") in {"payment", "authentication", "budget", "transport"}:
                        raise
                save()
            constraint = runtime.learning.planning_constraints("local-user", project)
            assert runtime.learning.planning_constraints("local-user", "unrelated-project") is None
            assert runtime.learning.planning_constraints("other-owner", project) is None
            assert runtime.learning.matching_skills("local-user", project, "查询天气") == []
            runtime.memory_store.purge(constraint["entry_id"], "local-user", idempotency_key=project + ":forget")
            runtime.learning.run_once()
            record["forget_not_resurrected"] = runtime.learning.planning_constraints("local-user", project) is None
            record["passed"] = not record["failures"] and record["forget_not_resurrected"]
            save()
        report["passed_tasks"] = sum(item["passed"] for item in report["records"])
        report["status"] = "PASS" if report["passed_tasks"] == len(TASKS) else "FAIL"
        assert runtime.behavior.active("stable").id == base.id
        save()
    except BaseException as exc:
        report.update(status="STOPPED", error_type=type(exc).__name__)
        save()
        raise
    finally:
        runtime.db.close()
