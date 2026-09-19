"""Real HTTP entry -> expert workers -> synthesis -> visible thread message."""
import asyncio
import json
import os
import re
from dataclasses import replace
from pathlib import Path

import pytest


@pytest.mark.skipif(os.getenv("RUN_EXPERT_JOURNEY_LIVE") != "1", reason="explicit live journey flag required")
def test_bicycle_creation_via_expert_http_entry(tmp_path, migrated_postgres_url):
    from fastapi.testclient import TestClient
    from app.config import load_llm_ap
    from app.costs import PriceSnapshot
    from app.main import create_app
    from app.startup import build_runtime

    output = Path(os.environ["EXPERT_JOURNEY_REPORT"])
    assert not output.exists(), "do not overwrite a live run"
    profile = replace(load_llm_ap(os.environ["LLM_AP_PATH"], model_id="deepseek-flash"), max_attempts=1, network_retries=0, timeout_seconds=90, provider_name="deepseek")
    runtime = build_runtime(tmp_path, database_url=migrated_postgres_url, profile=profile)
    runtime.agent_worker.model.thinking = False
    prices = json.loads(Path(os.environ["LEARNING_V2_LIVE_PRICE_FILE"]).read_text(encoding="utf-8"))
    for version in {item["primary"] for item in runtime.behavior.active("stable").manifest["model_role_bindings"].values()}:
        runtime.costs.register_price(version, PriceSnapshot("expert-journey-" + version, **prices["rates_microusd_per_million_tokens"]))
    runtime.costs.set_budget("local-user", "DAILY", runtime.costs.today_period(), 150000)
    runtime.costs.set_budget("local-user", "MONTHLY", runtime.costs.today_period()[:7], 150000)
    client = TestClient(create_app(runtime=runtime))
    headers = {"host":"127.0.0.1:8000", "content-type":"application/json", "x-csrf-token":client.app.state.csrf_token}
    thread = client.post("/api/threads", json={"title":"骑车计划全流程验收"}, headers=headers).json()
    response = client.post(f"/api/threads/{thread['id']}/expert-runs", json={"objective":"制定一个7天的骑车计划", "idempotency_key":"bicycle-real-user-flow"}, headers=headers)
    assert response.status_code == 202
    run_id = response.json()["id"]
    async def execute():
        for _ in range(12):
            if runtime.agent_tasks.get_run(run_id)["status"] in {"SUCCEEDED","FAILED","CANCELLED"}:
                return
            await runtime.agent_worker.run_once()
    report = {"provenance":"acceptance", "entry":"POST /api/threads/{id}/expert-runs", "run_id":run_id}
    try:
        asyncio.run(execute())
        run = runtime.agent_tasks.get_run(run_id)
        tasks = client.get(f"/api/agent-runs/{run_id}/tasks",headers={"host":"127.0.0.1:8000"}).json()["tasks"]
        artifacts = client.get(f"/api/agent-runs/{run_id}/artifacts",headers={"host":"127.0.0.1:8000"}).json()["artifacts"]
        with runtime.db.connection() as connection:
            messages = connection.execute("SELECT content FROM thread_messages WHERE thread_id=? AND role='assistant' ORDER BY message_seq",(thread['id'],)).fetchall()
        summary = next((a['content']['summary'] for a in artifacts if a['artifact_type']=='expert_synthesis'), "")
        days = [bool(re.search(rf"(?:第\s*{i}\s*天|Day\s*{i}\b|D{i}\b|第{ch}天)", summary, re.I)) for i,ch in enumerate("一二三四五六七",1)]
        checks = {"worker_succeeded":run['status']=='SUCCEEDED', "all_seven_days":all(days),
                  "actual_thread_message":bool(messages) and summary in messages[-1][0],
                  "no_internal_diagnostics":not any(s in summary for s in ('source_ref','input_artifacts','10080','无法核验')),
                  "rest_or_recovery":any(s in summary for s in ('休息','恢复')), "duration":'分钟' in summary}
        report.update(status="PASS" if all(checks.values()) else "FAIL",checks=checks,tasks=tasks,artifacts=artifacts,
                      messages=[row[0] for row in messages],costs=runtime.costs.summary("local-user","DAILY",runtime.costs.today_period()))
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        assert all(checks.values()), checks
    finally:
        runtime.db.close()
