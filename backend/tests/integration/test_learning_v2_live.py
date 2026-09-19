"""Opt-in real-model acceptance; isolated PostgreSQL fixture, explicit spend cap."""
import os
import json
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.skipif(os.getenv("RUN_LEARNING_V2_LIVE") != "1", reason="explicit real-model acceptance and spend cap required")
def test_real_model_compiler_consumes_learned_constraint(tmp_path, migrated_postgres_url):
    from app.startup import build_runtime
    from app.goal_program_compiler import GoalProgramCompiler
    from app.costs import PriceSnapshot
    from app.config import load_llm_ap
    from test_learning_v2 import feedback, compile_plan

    cap = int(os.environ["LEARNING_V2_LIVE_MAX_MICROUSD"])
    assert 0 < cap <= 10_000_000, "acceptance cap must be explicit, positive and at most $10"
    selected_profile = replace(load_llm_ap(os.environ["LLM_AP_PATH"], model_id="deepseek-flash"), max_attempts=1, network_retries=0, provider_name="deepseek")
    runtime = build_runtime(tmp_path, database_url=migrated_postgres_url, profile=selected_profile)
    assert isinstance(runtime.goal_programs.compiler, GoalProgramCompiler), "real model must be configured"
    runtime.goal_programs.expert_advisor = None
    price_config = json.loads(Path(os.environ["LEARNING_V2_LIVE_PRICE_FILE"]).read_text(encoding="utf-8"))
    manifest = runtime.behavior.active("stable").manifest
    planner_id = manifest["model_role_bindings"]["planner"]["primary"]
    profile = runtime.model_admin.version(planner_id)
    assert price_config["model_name"] == profile["model_name"] and price_config["base_url"] == profile["base_url"], "price file must bind the actual provider/model"
    rates = price_config["rates_microusd_per_million_tokens"]
    assert all(type(value) is int and value >= 0 for value in rates.values())
    runtime.costs.register_price(planner_id, PriceSnapshot(id="learning-v2-live-price", **rates))
    owner = "local-user"
    runtime.costs.set_budget(owner, "DAILY", runtime.costs.today_period(), cap)
    runtime.costs.set_budget(owner, "MONTHLY", runtime.costs.today_period()[:7], cap)
    # Existing model price configuration is mandatory; never invent a price.
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_price_snapshots").fetchone()[0] > 0, "configure authoritative model prices in the isolated test setup"
    root = runtime.costs.create_root_budget(owner, "evaluation", "learning-v2-live", max_attempts=3,
                                            deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), limit_microusd=cap)
    original = runtime.goal_programs._model_context
    runtime.goal_programs._model_context = lambda *args, **kwargs: replace(original(*args, **kwargs), root_budget_id=root["id"])
    runtime.learning.configure(owner, expected_version=0, paused=False, allowed_assets=["memory"])
    try:
        feedback(runtime, "以后每次练习最多30分钟")
        plan = compile_plan(runtime, "live-constraint")
        assert plan["structure"]["actions"]
        assert all(action["estimated_minutes"] <= 30 for action in plan["structure"]["actions"])
        with runtime.db.connection() as connection:
            invocations = connection.execute("SELECT id,status FROM model_invocations WHERE root_budget_id=?", (root["id"],)).fetchall()
            assert invocations and any(row["status"] == "SUCCEEDED" for row in invocations)
            snapshots = connection.execute("SELECT assets_json FROM learning_snapshots WHERE task_kind='invocation'").fetchall()
            assert any('"kind":"revision"' in row[0] for row in snapshots)
        costs = runtime.costs.summary(owner, "ROOT", root["id"])
        assert costs["charged_microusd"] <= cap
        output = os.getenv("LEARNING_V2_LIVE_REPORT")
        if output:
            Path(output).write_text(json.dumps({"status": "PASS", "provenance": "acceptance", "real_model": True,
                                                "model": selected_profile.model, "costs": costs, "root_budget_id": root["id"],
                                                "invocations": [dict(row) for row in invocations], "plan": plan["structure"],
                                                "request_assets": [json.loads(row[0]) for row in snapshots]}, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        runtime.db.close()
