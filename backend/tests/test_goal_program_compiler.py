from datetime import date, timedelta
import asyncio
import json
from types import SimpleNamespace

import pytest


def fixture(days: int = 7, *, daily_minutes: int = 60):
    start = date(2026, 9, 1)
    return {
        "objective_title": "一周力扣训练",
        "objective_summary": "每天完成一道题",
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=days - 1)).isoformat(),
        "assumptions": [],
        "milestones": [{"logical_key": "m1", "title": "完成周期", "target_date": (start + timedelta(days=days - 1)).isoformat()}],
        "actions": [
            {"logical_key": f"d{i + 1}", "scheduled_date": (start + timedelta(days=i)).isoformat(), "position": 1,
             "title": f"第 {i + 1} 天", "description": "练习", "estimated_minutes": daily_minutes,
             "completion_criteria": "提交通过", "required": True}
            for i in range(days)
        ],
    }


def test_schema_accepts_seven_and_twenty_eight_day_scenarios() -> None:
    from app.goal_program_compiler import validate_program_structure

    for days in (7, 28):
        value = fixture(days)
        result = validate_program_structure(value, value["start_date"], value["end_date"], 60)
        assert len(result["actions"]) == days


@pytest.mark.parametrize("mutation,code", [
    (lambda value: value.update({"unknown": True}), "UNKNOWN_FIELDS"),
    (lambda value: value["actions"].append({**value["actions"][0]}), "DUPLICATE_KEY"),
    (lambda value: value["actions"][0].update(title=" "), "INVALID_TEXT"),
    (lambda value: value["actions"][0].update(scheduled_date="2026-10-01"), "DATE_OUT_OF_RANGE"),
])
def test_schema_rejects_untrusted_model_output(mutation, code) -> None:
    from app.goal_program_compiler import GoalCompilationError, validate_program_structure

    value = fixture()
    mutation(value)
    with pytest.raises(GoalCompilationError) as error:
        validate_program_structure(value, value["start_date"], value["end_date"], 60)
    assert error.value.code == code


def test_schema_rejects_more_than_120_actions() -> None:
    from app.goal_program_compiler import GoalCompilationError, validate_program_structure

    value = fixture()
    value["actions"] = [{**value["actions"][0], "logical_key": f"a{i}", "position": i + 1} for i in range(121)]
    with pytest.raises(GoalCompilationError) as error:
        validate_program_structure(value, value["start_date"], value["end_date"], 60)
    assert error.value.code == "ACTION_COUNT"


def test_schema_rejects_an_over_budget_day_even_with_assumptions() -> None:
    from app.goal_program_compiler import GoalCompilationError, validate_program_structure

    value=fixture(days=1,daily_minutes=80);value["assumptions"]=["User accepted the schedule"]
    with pytest.raises(GoalCompilationError) as error:
        validate_program_structure(value,value["start_date"],value["end_date"],60)
    assert error.value.code=="DAILY_BUDGET"


def test_compiler_repairs_schema_invalid_json_once() -> None:
    from app.goal_program_compiler import GoalProgramCompiler

    invalid = fixture()
    invalid["unknown"] = True

    class Gateway:
        def __init__(self) -> None:
            self.requests = []

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            value = invalid if len(self.requests) == 1 else fixture()
            return SimpleNamespace(message=json.dumps(value, ensure_ascii=False))

    gateway = Gateway()
    result = asyncio.run(GoalProgramCompiler(gateway).compile(
        "# 一周力扣训练",
        {
            "start_date": "2026-09-01",
            "end_date": "2026-09-07",
            "daily_minutes": 60,
        },
    ))

    assert len(gateway.requests) == 2
    assert len(result["actions"]) == 7
    assert "UNKNOWN_FIELDS" in gateway.requests[1].messages[-1]["content"]
    prompt=gateway.requests[0].messages[0]["content"]
    assert "5 到 180 分钟" in prompt
    assert "每日总时长不得超过给定的 daily_minutes" in prompt


def test_compiler_accepts_a_fenced_json_object() -> None:
    from app.goal_program_compiler import GoalProgramCompiler

    class Gateway:
        async def complete(self, request, **kwargs):
            return SimpleNamespace(
                message=f"```json\n{json.dumps(fixture(), ensure_ascii=False)}\n```",
                finish_reason="stop",
            )

    result = asyncio.run(GoalProgramCompiler(Gateway()).compile(
        "# 一周力扣训练",
        {"start_date": "2026-09-01", "end_date": "2026-09-07", "daily_minutes": 60},
    ))

    assert len(result["actions"]) == 7


def test_compiler_uses_full_profile_budget_to_repair_a_truncated_response() -> None:
    from app.goal_program_compiler import GoalProgramCompiler

    class Gateway:
        def __init__(self) -> None:
            self.requests = []

        def output_limit(self) -> int:
            return 4096

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            if len(self.requests) == 1:
                return SimpleNamespace(message='{"objective_title":"截断', finish_reason="length")
            return SimpleNamespace(message=json.dumps(fixture(), ensure_ascii=False), finish_reason="stop")

    gateway = Gateway()
    result = asyncio.run(GoalProgramCompiler(gateway).compile(
        "# 一周力扣训练",
        {"start_date": "2026-09-01", "end_date": "2026-09-07", "daily_minutes": 60},
    ))

    assert len(result["actions"]) == 7
    assert gateway.requests[0].max_tokens < gateway.requests[1].max_tokens
    assert gateway.requests[1].max_tokens == 4096
    assert "保持简洁" in gateway.requests[1].messages[-1]["content"]


def test_compiler_reports_when_both_responses_are_truncated() -> None:
    from app.goal_program_compiler import GoalCompilationError, GoalProgramCompiler

    class Gateway:
        def output_limit(self) -> int:
            return 8192

        async def complete(self, request, **kwargs):
            return SimpleNamespace(message='{"objective_title":"截断', finish_reason="length")

    with pytest.raises(GoalCompilationError) as error:
        asyncio.run(GoalProgramCompiler(Gateway()).compile(
            "# 一周力扣训练",
            {"start_date": "2026-09-01", "end_date": "2026-09-07", "daily_minutes": 60},
        ))

    assert error.value.code == "MODEL_OUTPUT_TRUNCATED"


@pytest.mark.parametrize(("days", "expected_max_tokens"), [(2, 3000), (7, 3500), (28, 12000)])
def test_compiler_bounds_output_tokens_by_program_length(days: int, expected_max_tokens: int) -> None:
    from app.goal_program_compiler import GoalProgramCompiler

    class Gateway:
        def __init__(self) -> None:
            self.requests = []

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            return SimpleNamespace(message=json.dumps(fixture(days), ensure_ascii=False))

    gateway = Gateway()
    value = fixture(days)
    asyncio.run(GoalProgramCompiler(gateway).compile(
        "# Training plan",
        {
            "start_date": value["start_date"],
            "end_date": value["end_date"],
            "daily_minutes": 60,
        },
    ))

    assert gateway.requests[0].max_tokens == expected_max_tokens


def test_adjust_prompt_repeats_the_exact_program_schema() -> None:
    from app.goal_program_compiler import GoalProgramCompiler

    class Gateway:
        def __init__(self) -> None: self.requests=[]
        async def complete(self,request,**kwargs):self.requests.append(request);return SimpleNamespace(message=json.dumps(fixture(),ensure_ascii=False))

    gateway=Gateway();asyncio.run(GoalProgramCompiler(gateway).adjust(fixture(),"reduce future load"))
    prompt=gateway.requests[0].messages[0]["content"]
    assert "objective_title, objective_summary, start_date, end_date, assumptions, milestones, actions" in prompt
    assert "不要返回 current_program 或 reason 外层对象" in prompt


def test_compiler_drops_optional_advice_when_the_input_budget_is_tight() -> None:
    from app.goal_program_compiler import GoalProgramCompiler

    class Gateway:
        def __init__(self) -> None: self.requests = []
        def input_limit(self) -> int: return 3000
        def output_limit(self) -> int: return 4096
        async def complete(self, request, **kwargs):
            self.requests.append(request)
            return SimpleNamespace(message=json.dumps(fixture(), ensure_ascii=False), finish_reason="stop")

    gateway = Gateway()
    result = asyncio.run(GoalProgramCompiler(gateway).compile(
        "# 一周力扣训练\n每天一道题",
        {
            "start_date": "2026-09-01",
            "end_date": "2026-09-07",
            "daily_minutes": 60,
            "expert_advice": "专家建议" * 2000,
        },
    ))

    assert len(result["actions"]) == 7
    assert "专家建议" not in gateway.requests[0].messages[1]["content"]


def test_compiler_reports_a_specific_error_when_the_plan_does_not_fit() -> None:
    from app.goal_program_compiler import GoalCompilationError, GoalProgramCompiler

    class Gateway:
        def input_limit(self) -> int: return 16
        def output_limit(self) -> int: return 4096
        async def complete(self, request, **kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("request must not be dispatched")

    with pytest.raises(GoalCompilationError) as error:
        asyncio.run(GoalProgramCompiler(Gateway()).compile(
            "# 一周力扣训练\n每天一道题",
            {"start_date": "2026-09-01", "end_date": "2026-09-07", "daily_minutes": 60},
        ))

    assert error.value.code == "MODEL_INPUT_TOO_LARGE"


@pytest.mark.parametrize(("operation", "role", "purpose", "limit"), [
    ("compile", "planner", "compile_goal_program", 4096),
    ("adjust", "planner", "adjust_goal_program", 4096),
    ("review", "reflector", "daily_review", 800),
    ("period_review", "reflector", "period_review", 900),
])
def test_operations_route_limits_and_repairs_consistently(operation, role, purpose, limit):
    from app.goal_program_compiler import GoalProgramCompiler

    class Gateway:
        supports_role_routing = True

        def __init__(self):
            self.requests = []
            self.limit_routes = []

        def input_limit(self, **route):
            self.limit_routes.append(route)
            return 20000

        def output_limit(self, **route):
            self.limit_routes.append(route)
            return 4096

        async def complete(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return SimpleNamespace(message="{}", finish_reason="stop")
            value = fixture()
            if operation == "review":
                value = {"summary": "Recorded progress", "encouragement": "Keep a sustainable pace",
                         "needs_adjustment": False, "adjustment_reason": ""}
            elif operation == "period_review":
                value = {"summary": "Recorded progress across this program"}
            return SimpleNamespace(message=json.dumps(value), finish_reason="stop")

    gateway = Gateway()
    compiler = GoalProgramCompiler(gateway)
    if operation == "compile":
        invocation = compiler.compile("# Training", {"start_date": "2026-09-01", "end_date": "2026-09-07", "daily_minutes": 60})
    elif operation == "adjust":
        invocation = compiler.adjust(fixture(), "Reduce future load")
    else:
        invocation = getattr(compiler, operation)({"signals": []})
    asyncio.run(invocation)

    assert len(gateway.requests) == 2
    assert all((request.role, request.purpose) == (role, purpose) for request in gateway.requests)
    if role == "reflector":
        assert all(request.thinking is False for request in gateway.requests)
    assert gateway.limit_routes == [{"role": role, "purpose": purpose}] * 3
    assert all(request.max_tokens <= limit for request in gateway.requests)
    assert gateway.requests[-1].max_tokens == limit


@pytest.mark.asyncio
async def test_review_uses_reflector_profile_and_preserves_pinned_context(tmp_path, monkeypatch):
    from app.behavior import BehaviorBundleService
    from app.goal_program_compiler import GoalProgramCompiler
    from app.model_admin import ModelAdminService
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import ModelResponse, Timing, UsageBuckets
    from test_routed_model_gateway import _configured_control_plane

    db, bundle, versions = _configured_control_plane(tmp_path, monkeypatch, context_windows={"fallback": 4096})
    # Production persists this field in PostgreSQL; legacy SQLite fixtures omit it.
    with db.transaction() as connection:
        db._add_column(connection, "model_invocations", "root_budget_id TEXT")
    manifest = dict(bundle.manifest)
    policy = ModelAdminService(db).create_policy("review-runtime", {
        **manifest["model_role_bindings"], "reflector": {"primary": versions["fallback"], "fallback": []},
    })
    manifest["model_role_bindings"] = policy["roles"]
    manifest["model_routing"] = {"policy_id": policy["id"], "digest": policy["policy_digest"]}
    pinned = BehaviorBundleService(db).ensure(manifest)
    selected = []

    async def execute(profile, request, **kwargs):
        selected.append(profile.registered_profile_version_id)
        return ModelResponse(json.dumps({"summary": "Review", "encouragement": "Continue",
                                        "needs_adjustment": False, "adjustment_reason": ""}),
                             [], "stop", UsageBuckets(1, 0, 0, 1, 0), Timing(0, 0, 1), 1)

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    context = ModelCallContext("planner", "compile_goal_program", runtime_bundle_id=pinned.id,
                               root_budget_id="preserved-budget")
    token = gateway.set_call_context(context)
    try:
        assert gateway.input_limit(role="reflector", purpose="daily_review") < gateway.input_limit()
        await GoalProgramCompiler(gateway).review({"signals": []})
        assert gateway._call_context.get() == context
    finally:
        gateway.reset_call_context(token)

    assert selected == [versions["fallback"]]
    with db.connection() as connection:
        invocation = connection.execute("SELECT * FROM model_invocations").fetchone()
        assert (invocation["role"], invocation["purpose"]) == ("reflector", "daily_review")
        assert invocation["runtime_bundle_id"] == pinned.id
        assert invocation["root_budget_id"] == "preserved-budget"
    db.close()
