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
    "training",
    "learning",
    "learn",
    "study",
    "train",
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
    if any(term in normalized for term in _TEMPLATE_TERMS):
        return False
    has_domain = any(term in normalized for term in _DOMAIN_TERMS)
    has_plan_intent = any(term in normalized for term in _PLAN_TERMS)
    return has_domain and has_plan_intent


def _latest_item_is_ask_result(history: Sequence[Mapping[str, Any]]) -> bool:
    if not history:
        return False
    latest = history[-1]
    return latest.get("role") == "tool" and bool(latest.get("tool_call_id"))
