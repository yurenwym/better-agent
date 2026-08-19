from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


_DOMAIN_TERMS = (
    "训练",
    "学习",
    "骑行",
    "跑步",
    "游泳",
    "健身",
    "瑜伽",
    "备考",
    "语言",
    "英语",
    "减脂",
    "增肌",
    "cycling",
    "running",
    "swimming",
    "fitness",
    "exercise",
    "training",
    "learning",
    "learn",
    "study",
    "exam",
    "language",
)

_PLAN_TERMS = (
    "计划",
    "规划",
    "方案",
    "安排",
    "课程",
    "目标",
    "plan",
    "schedule",
    "program",
    "roadmap",
    "learning",
    "learn",
    "study",
)

_PERSONAL_INTENT_TERMS = (
    "我想",
    "我希望",
    "帮我",
    "为我",
    "我的",
    "i want",
    "i need",
    "help me",
    "for me",
    "my",
    "create",
    "make",
    "build",
)

_LONG_HORIZON_TERMS = (
    "长期",
    "系统",
    "持续",
    "习惯",
    "每周",
    "long-term",
    "weekly",
    "six-week",
)

_TEMPLATE_TERMS = ("模板", "示例", "范例", "template", "example")


def requires_context_collection(
    content: str,
    history: Sequence[Mapping[str, Any]],
) -> bool:
    """Return whether a new personalized plan needs the first context AskCard."""
    normalized = "".join((content or "").lower().split())
    if not normalized or _latest_item_is_ask_result(history):
        return False
    if _contains_any(normalized, _TEMPLATE_TERMS):
        return False
    has_domain = _contains_any(normalized, _DOMAIN_TERMS)
    has_personal_intent = _contains_any(normalized, _PERSONAL_INTENT_TERMS)
    has_plan_intent = _contains_any(normalized, _PLAN_TERMS)
    has_long_horizon = _contains_any(normalized, _LONG_HORIZON_TERMS)
    return has_domain and has_personal_intent and (has_plan_intent or has_long_horizon)


def _latest_item_is_ask_result(history: Sequence[Mapping[str, Any]]) -> bool:
    if not history:
        return False
    latest = history[-1]
    return latest.get("role") == "tool" and bool(latest.get("tool_call_id"))


def _contains_any(value: str, terms: Sequence[str]) -> bool:
    compact_terms = (term.lower().replace(" ", "") for term in terms)
    return any(term in value for term in compact_terms)
