"""Bounded execution settings, separate from model routing and learning authority."""
from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import replace


def validate_task_policy(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("unsupported task_policy schema")
    ranges = {
        "action_max_minutes": (5, 180),
        "research_max_queries": (1, 12),
        "research_max_sources": (1, 20),
        "research_reflection_rounds": (0, 1),
    }
    if set(value) - {"schema_version", "estimate_multiplier", "research_stop_condition", *ranges}:
        raise ValueError("task_policy contains an unknown or unconsumed field")
    for key, (low, high) in ranges.items():
        if key in value and (type(value[key]) is not int or not low <= value[key] <= high):
            raise ValueError(f"invalid task_policy {key}")
    multiplier = value.get("estimate_multiplier", 1)
    if value.get("research_stop_condition", "none") not in {"none", "coverage_satisfied"}:
        raise ValueError("invalid research stopping condition")
    if type(multiplier) not in (int, float) or not math.isfinite(multiplier) or not 1 <= multiplier <= 3:
        raise ValueError("invalid estimate_multiplier")
    return deepcopy(value)


def research_limits(limits, policy: dict):
    policy = validate_task_policy(policy)
    fields = {
        key: min(getattr(limits, key), policy[f"research_{key}"])
        for key in ("max_queries", "max_sources", "reflection_rounds")
        if f"research_{key}" in policy
    }
    return replace(limits, **fields)


def initial_coverage_satisfied(plan, evidence, sources):
    """Skip optional exploration only; final delivery/audit remains mandatory."""
    from .research.engine import ResearchEngine
    if len(sources) < 2 or not plan.sections:
        return False
    for heading in plan.sections:
        terms = ResearchEngine._search_terms(heading)
        if not terms or not any(any(term in item.text.lower() for term in terms) for item in evidence):
            return False
    return True


def planning_request(request: dict, policy: dict) -> dict:
    policy = validate_task_policy(policy)
    result = deepcopy(request)
    result["task_policy"] = policy
    result["action_max_minutes"] = min(request["daily_minutes"], policy.get("action_max_minutes", 180))
    return result


def calibrate_minutes(minutes: int, multiplier: float) -> int:
    return math.ceil(minutes * multiplier)


def validate_planned_actions(structure: dict, request: dict) -> dict:
    """Calibrate estimates without deleting required work or relaxing hard caps."""
    from .goal_program_compiler import GoalCompilationError, validate_program_structure

    result = deepcopy(structure)
    policy = validate_task_policy(request["task_policy"])
    for action in result["actions"]:
        minutes = action["estimated_minutes"]
        if type(minutes) is not int:
            raise GoalCompilationError("INVALID_MINUTES", "estimated_minutes must be an integer")
        action["estimated_minutes"] = calibrate_minutes(minutes, policy.get("estimate_multiplier", 1))
        if action["estimated_minutes"] > request["action_max_minutes"]:
            raise GoalCompilationError("ACTION_POLICY_LIMIT", "action exceeds the effective duration constraint; replan required")
    return validate_program_structure(result, request["start_date"], request["end_date"], request["daily_minutes"])
