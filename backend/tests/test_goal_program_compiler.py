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
