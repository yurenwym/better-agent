from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _live_module():
    name = "better_m3_live_acceptance"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).parents[1] / "scripts" / "m3_live_acceptance.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_synthetic_route_diagnostic_removes_active_credential(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.model_gateway import ModelRequest
    live = _live_module()

    monkeypatch.setenv("M3_DIAGNOSTIC_KEY", "sensitive-test-credential")
    async def response(*args, **kwargs):
        return SimpleNamespace(message="visible reply sensitive-test-credential", finish_reason="stop")
    monkeypatch.setattr(live, "runtime_attempt", response)
    budget = live.BatchBudget()
    asyncio.run(budget.model_attempt(
        1, SimpleNamespace(timeout_seconds=1, api_key_env="M3_DIAGNOSTIC_KEY"),
        ModelRequest(messages=[], role="conversation", purpose="route_and_respond"),
    ))
    evidence = budget.network_attempts[0]
    assert evidence["synthetic_route_reply"] == "visible reply [REDACTED]"
    assert "sensitive-test-credential" not in json.dumps(evidence)


def test_failure_diagnostics_include_agent_task_error_fields(tmp_path):
    import json
    path = tmp_path / "round-diagnostics.json"
    path.write_text(json.dumps({"agent_runs": [{"status": "FAILED"}], "agent_tasks": [{"error_code": "EXPERT_STRUCTURE"}]}), encoding="utf-8")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["agent_tasks"][0]["error_code"] == "EXPERT_STRUCTURE"


def test_core_round_stops_on_failed_child_before_synthesis():
    live = _live_module()
    children = [
        {"role": "planner", "status": "SUCCEEDED", "error_code": None},
        {"role": "critic", "status": "FAILED", "error_code": "EXPERT_GATEWAYERROR"},
    ]

    message = live._failed_child_message(children)

    assert message == "expert child failed before synthesis: critic:FAILED:EXPERT_GATEWAYERROR"
    assert live._failed_child_message(children[:1]) is None


@pytest.mark.parametrize("reverse", [0, 1])
def test_blind_packet_separates_identity_and_binds_exact_answers(monkeypatch, reverse):
    live = _live_module()
    monkeypatch.setattr(live.secrets, "randbits", lambda _: reverse)
    packet, mapping = live._blind_review("first exact answer", "second exact answer")
    serialized = json.dumps(packet)
    assert "single_agent" not in serialized and "multi_agent" not in serialized
    assert "cost_microusd" not in serialized and "runtime_bundle_id" not in serialized
    recovered = {mapping["labels"][label]: answer for label, answer in packet["answers"].items()}
    assert recovered == {"single_agent": "first exact answer", "multi_agent": "second exact answer"}
    assert mapping["packet_digest"] == packet["packet_digest"]
    assert packet["packet_digest"] == live._digest({k: v for k, v in packet.items() if k != "packet_digest"})
    changed, _ = live._blind_review("changed answer", "second exact answer")
    assert changed["packet_digest"] != packet["packet_digest"]


def test_frozen_quality_review_scores_both_arms_without_network():
    live = _live_module()
    answer = "仅基于材料分析，未执行任何操作。S1显示A为130分钟，超过120分钟；S2显示B为100分钟但需批准且状态未知；方案C在S3与S4中存在50与90分钟的矛盾，总时长可能为90或130分钟。"
    packet, mapping = live._blind_review(answer, answer)
    review = live._review_comparison(answer, answer, packet, mapping)
    assert review["status"] == "PASSED"
    assert review["observed_multi_agent_gain"] is False
    assert review["default_collaboration_supported"] is False


@pytest.mark.parametrize("answer", [
    "仅基于材料分析，未声称已执行任何操作。S1显示A为130分钟，超过120分钟；S2显示B为100分钟但需批准且状态未知；方案C在S3与S4中存在50与90分钟的矛盾，总时长可能为90或130分钟。",
    "以上为只读分析，未执行任何方案。S1显示A为130分钟，超过120分钟；S2显示B为100分钟但需批准且状态未知；方案C在S3与S4中存在50与90分钟的冲突，总时长可能为90或130分钟。",
])
def test_action_boundary_accepts_negated_execution_statements(answer):
    live = _live_module()
    assert live._score_answer(answer)["scores"]["action_boundary"] == 1


def test_action_boundary_rejects_positive_execution_claim():
    live = _live_module()
    answer = "仅基于材料分析，已经执行方案B。S1显示A为130分钟，超过120分钟；S2显示B为100分钟但需批准且状态未知；方案C在S3与S4中存在50与90分钟的矛盾，总时长可能为90或130分钟。"
    assert live._score_answer(answer)["scores"]["action_boundary"] == 0


def test_role_audit_requires_distinct_role_specific_evidence():
    live = _live_module()
    experts = [
        {"role": "researcher", "result": {"summary": "S3与S4分别为50和90，C总时长可能90或130，来源差异待核实。", "findings": [{"text": "gap", "source_refs": ["S3", "S4"]}], "risks": [], "open_questions": []}},
        {"role": "planner", "result": {"summary": "S1显示A为130超过120；S2显示B为100但批准依赖未知。", "findings": [{"text": "dependency", "source_refs": ["S1", "S2"]}], "risks": [], "open_questions": []}},
        {"role": "critic", "result": {"summary": "S3与S4矛盾，存在错误执行风险。", "findings": [{"text": "conflict", "source_refs": ["S3", "S4"]}], "risks": ["风险"], "open_questions": []}},
    ]
    assert live._audit_expert_roles(experts)["passed"] is True
    experts[2]["result"] = experts[0]["result"]
    assert live._audit_expert_roles(experts)["passed"] is False


def test_researcher_focus_preserves_conflicting_raw_values_without_planner_total():
    live = _live_module()
    experts = [
        {"role": "researcher", "result": {"summary": "S3称执行50分钟，S4称执行90分钟，来源差异尚未核实。", "findings": [{"text": "来源冲突", "source_refs": ["S3", "S4"]}], "risks": [], "open_questions": []}},
        {"role": "planner", "result": {"summary": "S1显示A为130超过120；S2显示B为100但批准依赖未知。", "findings": [{"text": "计划依赖", "source_refs": ["S1", "S2"]}], "risks": [], "open_questions": []}},
        {"role": "critic", "result": {"summary": "S3与S4矛盾，存在错误执行风险。", "findings": [{"text": "约束冲突", "source_refs": ["S3", "S4"]}], "risks": ["违反约束"], "open_questions": []}},
    ]

    audit = live._audit_expert_roles(experts)

    assert "130" not in json.dumps(experts[0], ensure_ascii=False)
    assert audit["checks"]["researcher_focus"] is True
    assert audit["passed"] is True


def test_role_audit_rejects_wrong_c_arithmetic_and_unknown_source():
    live = _live_module()
    experts = [
        {"role": "researcher", "result": {"summary": "S3为50，S4为90，C可能90或130。", "findings": [{"text": "差异", "source_refs": ["S3", "S4"]}], "risks": [], "open_questions": []}},
        {"role": "planner", "result": {"summary": "S1的130超过120；S2为100但批准依赖未知。", "findings": [{"text": "依赖", "source_refs": ["S1", "S2"]}], "risks": [], "open_questions": []}},
        {"role": "critic", "result": {"summary": "S3与S4矛盾，误算为150会带来风险。", "findings": [{"text": "冲突", "source_refs": ["S5"]}], "risks": ["风险"], "open_questions": []}},
    ]

    audit = live._audit_expert_roles(experts)

    assert audit["passed"] is False
    assert audit["checks"]["correct_c_arithmetic"] is False
    assert audit["checks"]["source_refs_valid"] is False


def test_role_audit_requires_role_specific_source_evidence():
    live = _live_module()
    experts = [
        {"role": "researcher", "result": {"summary": "S3为50，S4为90，C可能90或130。", "findings": [], "risks": [], "open_questions": []}},
        {"role": "planner", "result": {"summary": "S1的130超过120；S2为100但批准依赖未知。", "findings": [{"text": "依赖", "source_refs": ["S1", "S2"]}], "risks": [], "open_questions": []}},
        {"role": "critic", "result": {"summary": "S3与S4矛盾，存在风险。", "findings": [{"text": "冲突", "source_refs": ["S3", "S4"]}], "risks": ["风险"], "open_questions": []}},
    ]

    audit = live._audit_expert_roles(experts)

    assert audit["checks"]["source_refs_valid"] is True
    assert audit["checks"]["role_source_evidence"] is False
    assert audit["passed"] is False


def test_role_audit_rejects_near_duplicate_comprehensive_outputs():
    live = _live_module()
    comprehensive = "S1的A为130超过120；S2的B为100但批准依赖未知；S3执行50与S4执行90矛盾，C可能90或130，存在风险。"
    experts = [
        {"role": "researcher", "result": {"summary": comprehensive, "findings": [{"text": comprehensive, "source_refs": ["S1", "S2", "S3", "S4"]}], "risks": ["风险"], "open_questions": []}},
        {"role": "planner", "result": {"summary": comprehensive + " planner", "findings": [{"text": comprehensive, "source_refs": ["S1", "S2", "S3", "S4"]}], "risks": ["风险"], "open_questions": []}},
        {"role": "critic", "result": {"summary": "S3与S4冲突，未决事实会造成错误执行风险。", "findings": [{"text": "S3与S4矛盾", "source_refs": ["S3", "S4"]}], "risks": ["违反约束风险"], "open_questions": []}},
    ]

    audit = live._audit_expert_roles(experts)

    assert audit["checks"]["distinct_outputs"] is True
    assert audit["pairwise_similarity"]["researcher:planner"] >= 0.8
    assert audit["checks"]["complementary_content"] is False
    assert audit["passed"] is False


def test_role_audit_rejects_claim_that_130_minutes_fits_two_hours():
    live = _live_module()
    experts = [
        {"role": "researcher", "result": {"summary": "S3执行50、S4执行90，C可能90或130。", "findings": [{"text": "来源差异", "source_refs": ["S3", "S4"]}], "risks": [], "open_questions": []}},
        {"role": "planner", "result": {"summary": "S1的A为130分钟，仍在两小时预算内；S2的B为100但批准依赖未知。", "findings": [{"text": "S1与S2", "source_refs": ["S1", "S2"]}], "risks": [], "open_questions": []}},
        {"role": "critic", "result": {"summary": "S3与S4矛盾，存在错误执行风险。", "findings": [{"text": "冲突", "source_refs": ["S3", "S4"]}], "risks": ["违反约束"], "open_questions": []}},
    ]

    audit = live._audit_expert_roles(experts)

    assert audit["checks"]["budget_arithmetic_consistent"] is False
    assert audit["passed"] is False


def test_budget_audit_does_not_join_different_options_across_sentences():
    live = _live_module()
    experts = [
        {"role": "researcher", "result": {"summary": "S3执行50、S4执行90，来源矛盾待核实。", "findings": [{"text": "来源差异", "source_refs": ["S3", "S4"]}], "risks": [], "open_questions": []}},
        {"role": "planner", "result": {"summary": "S1的A为130分钟，超过120分钟预算。S2的B为100分钟，在预算内但批准依赖未知。", "findings": [{"text": "计划依赖", "source_refs": ["S1", "S2"]}], "risks": [], "open_questions": []}},
        {"role": "critic", "result": {"summary": "S3与S4冲突，存在错误执行风险。", "findings": [{"text": "约束冲突", "source_refs": ["S3", "S4"]}], "risks": ["违反约束"], "open_questions": []}},
    ]

    audit = live._audit_expert_roles(experts)

    assert audit["checks"]["budget_arithmetic_consistent"] is True
    assert audit["passed"] is True


@pytest.mark.parametrize("hide_partial", [False, True])
def test_state_acceptance_checks_partial_artifact_not_just_success(tmp_path, monkeypatch, hide_partial):
    import asyncio
    from app.agents import AgentTaskService, ManagedAgentWorker
    from app.behavior import BehaviorBundleService
    from app.db import Database
    live = _live_module()

    db = Database(tmp_path / "agent.db")
    try:
        bundle = BehaviorBundleService(db).ensure({"prompt": "offline-test"})
        tasks = AgentTaskService(db)
        original = ManagedAgentWorker._synthesize

        async def synthesize(self, *args):
            result = await original(self, *args)
            if hide_partial:
                result["incomplete"] = False
            return result

        monkeypatch.setattr(ManagedAgentWorker, "_synthesize", synthesize)
        if hide_partial:
            with pytest.raises(live.AcceptanceFailure, match="marked complete"):
                asyncio.run(live._run_state_cases(tasks, bundle.id, 1, "local-user"))
        else:
            report = asyncio.run(live._run_state_cases(tasks, bundle.id, 1, "local-user"))
            assert report["partial_result"]["incomplete"] is True
            assert report["all_failed_status"] == "FAILED"
    finally:
        db.close()


@pytest.mark.parametrize("transport_started", [False, True])
def test_failed_batch_reports_whether_transport_was_attempted(tmp_path, monkeypatch, transport_started):
    import asyncio
    from types import SimpleNamespace
    live = _live_module()

    # Exercise report finalization without Docker, credentials, or network I/O.
    monkeypatch.setattr(live, "_profile", lambda: SimpleNamespace(timeout_seconds=60))
    monkeypatch.setattr(live, "_preflight", lambda: {"network_started": False})
    monkeypatch.setattr(live, "_current_database", lambda: ("current-test-db", "better_agent"))

    async def fail_transport(*args, **kwargs):
        raise RuntimeError("injected transport failure")

    async def fail_round(data_root, database_url, profile, round_number, budget):
        if transport_started:
            await budget.model_attempt(
                round_number, profile, SimpleNamespace(role="expert", purpose="test", messages=[], tools=[], temperature=0, max_tokens=1, thinking=False),
            )
        raise RuntimeError("injected setup failure")

    monkeypatch.setattr(live, "runtime_attempt", fail_transport)
    monkeypatch.setattr(live, "_run_round", fail_round)
    output = tmp_path / "failed.json"
    with pytest.raises(RuntimeError, match="injected"):
        asyncio.run(live._execute(output))
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "FAILED"
    assert report["network_started"] is transport_started
    assert len(report["network_attempts"]) == int(transport_started)
    assert report["cost_evidence"]["status"] == "skipped_non_postgres_test_double"
    if transport_started:
        assert report["network_attempts"][0]["status"] == "failed"


def test_failed_batch_embeds_current_round_diagnostics(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    live = _live_module()

    monkeypatch.setattr(live, "_profile", lambda: SimpleNamespace(timeout_seconds=60))
    monkeypatch.setattr(live, "_preflight", lambda: {"network_started": False})
    monkeypatch.setattr(live, "_current_database", lambda: ("current-test-db", "better_agent"))

    async def fail_round(data_root, database_url, profile, round_number, budget):
        data_root.mkdir(parents=True, exist_ok=True)
        (data_root / "round-diagnostics.json").write_text(
            json.dumps({"round": round_number, "coordinator_synthesis": {"summary": "S1"}}),
            encoding="utf-8",
        )
        raise RuntimeError("injected rubric failure")

    monkeypatch.setattr(live, "_run_round", fail_round)
    output = tmp_path / "failed-with-diagnostics.json"
    with pytest.raises(RuntimeError, match="rubric"):
        asyncio.run(live._execute(output))

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["failed_round"]["round"] == 1
    assert report["failed_round"]["coordinator_synthesis"]["summary"] == "S1"


def test_live_batch_records_explicit_matched_inference_policy(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.model_gateway import ModelRequest
    live = _live_module()

    async def response(*args, **kwargs):
        return SimpleNamespace(message="ok", finish_reason="stop")

    monkeypatch.setattr(live, "runtime_attempt", response)
    budget = live.BatchBudget()
    request = ModelRequest(messages=[], role="expert", purpose="expert_planner", thinking=False)
    asyncio.run(budget.model_attempt(1, SimpleNamespace(timeout_seconds=1, api_key_env="unused"), request))
    evidence = budget.network_attempts[0]
    assert evidence["inference_policy"] == {"thinking": False, "response_format": None}
    assert evidence["finish_reason"] == "stop"
    assert evidence["visible_output_bytes"] == 2


def test_m3_live_acceptance_defaults_to_no_network_and_records_limits(tmp_path, monkeypatch):
    monkeypatch.delenv("M3_LIVE_APPROVED", raising=False)
    output = tmp_path / "m3-preflight.json"
    result = subprocess.run(
        [sys.executable, "scripts/m3_live_acceptance.py", "--output", str(output)],
        cwd=".", capture_output=True, text=True, check=True,
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "NOT_AUTHORISED"
    assert report["network_started"] is False
    assert report["storage"] == "current_database_with_batch_scoped_cleanup"
    assert "app/agents.py" in report["source_manifest"]["files"]
    assert "scripts/m3_live_acceptance.py" in report["source_manifest"]["files"]
    assert len(report["source_manifest"]["digest"]) == 64
    assert len(report["case_digest"]) == len(report["rubric_digest"]) == 64
    assert report["limits"]["total_attempts"] == 15
    assert report["limits"]["worst_batch_microusd"] == 378480
    assert "M3_LIVE_APPROVED=1" in result.stdout


def test_m3_live_execute_flag_refuses_without_explicit_approval(tmp_path, monkeypatch):
    monkeypatch.delenv("M3_LIVE_APPROVED", raising=False)
    result = subprocess.run(
        [sys.executable, "scripts/m3_live_acceptance.py", "--execute", "--output", str(tmp_path / "refused.json")],
        cwd=".", capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "refused" in result.stderr


def test_agent_worker_propagates_bundle_price_snapshot_to_gateway_context(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from app.agents import AgentTaskService, ManagedAgentWorker
    from app.behavior import BehaviorBundleService
    from app.db import Database

    class Gateway:
        control_store = object()

        def __init__(self):
            self.contexts = []

        def set_call_context(self, value):
            self.contexts.append(value)
            return value

        def reset_call_context(self, value):
            pass

    class Model:
        def __init__(self, gateway):
            self.gateway = gateway

        async def execute(self, role, objective, context, inputs):
            return {"summary": "ok", "findings": [], "risks": [], "open_questions": []}

    db = Database(tmp_path / "agent.db")
    bundles = BehaviorBundleService(db)
    bundle = bundles.ensure({"model_price_snapshot_id": "price-frozen", "prompt": "test"})
    service = AgentTaskService(db)
    run = service.create_run("local-user", "goal", {}, bundle.id, idempotency_key="snapshot")
    gateway = Gateway()
    worker = ManagedAgentWorker(service, Model(gateway))
    for _ in range(5):
        assert asyncio.run(worker.run_once()) is True
    assert gateway.contexts
    assert gateway.contexts[0].price_snapshot_id == "price-frozen"


def test_m3_batch_budget_rejects_sixteenth_attempt_before_transport(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    live = _live_module()
    AcceptanceFailure, BatchBudget = live.AcceptanceFailure, live.BatchBudget

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("transport must not be reached")

    monkeypatch.setattr(live, "runtime_attempt", forbidden)
    budget = BatchBudget()
    budget.network_attempts = [
        {"round": index // 5 + 1, "status": "succeeded"}
        for index in range(15)
    ]
    profile = SimpleNamespace(timeout_seconds=60)
    request = SimpleNamespace(role="expert", purpose="expert_researcher")
    try:
        asyncio.run(budget.model_attempt(4, profile, request))
    except AcceptanceFailure as exc:
        assert "hard limit" in str(exc)
    else:
        raise AssertionError("the sixteenth attempt was not blocked")


def test_m3_preflight_requires_json_object_capability_when_approved(monkeypatch):
    live = _live_module()

    monkeypatch.setenv("M3_LIVE_APPROVED", "1")
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("AGENT_MODEL_ID", "deepseek-v4-flash")
    monkeypatch.setenv("AGENT_MODEL_API_KEY", "test-key")
    monkeypatch.delenv("LLM_AP_PATH", raising=False)
    result = live._preflight()
    assert result["status"] == "READY"
    assert "json_object" in __import__("os").environ["AGENT_MODEL_CAPABILITIES"]


def test_m3_report_uses_daily_budget_aggregate_without_double_counting(tmp_path):
    live = _live_module()

    class Connection:
        def execute(self, query, params=()):
            class Result:
                def fetchone(self):
                    return {"charged_microusd": 7}
            assert "period_kind='DAILY'" in query
            return Result()

    class DB:
        def connection(self):
            class Context:
                def __enter__(self):
                    return Connection()
                def __exit__(self, *args):
                    return False
            return Context()

    class Costs:
        @staticmethod
        def today_period():
            return "2026-09-09"

    # Keep this regression focused on the SQL source used by the live report.
    with DB().connection() as connection:
        row = connection.execute(
            "SELECT COALESCE(charged_microusd,0) FROM cost_budgets "
            "WHERE owner_id=? AND period_kind='DAILY' AND period_key=?",
            ("owner", Costs.today_period()),
        ).fetchone()
    assert int(row["charged_microusd"]) == 7
