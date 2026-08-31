from __future__ import annotations

from typing import Any


RUNTIME_REASON_MESSAGES = {
    "STEP_WALL_TIME_EXHAUSTED": "当前步骤执行超时",
    "REACT_ITERATION_BUDGET_EXHAUSTED": "本步骤的执行轮次已用完",
    "IDENTICAL_ACTION_BUDGET_EXHAUSTED": "模型重复执行了相同操作，任务已暂停",
    "CONSECUTIVE_TOOL_ERRORS_EXHAUSTED": "工具连续执行失败，任务已暂停",
    "INVALID_MODEL_ACTION": "模型返回了无法识别的操作",
    "MODEL_REQUESTED_BLOCK": "模型请求暂停当前任务",
    "TOOL_AUTHORIZATION_DENIED": "工具调用未通过安全授权",
    "MODEL_AUTHENTICATION": "模型凭证未通过验证",
    "MODEL_RATE_LIMIT": "模型服务当前请求过多，请稍后重试",
    "MODEL_REQUEST": "模型服务拒绝了当前请求格式",
    "MODEL_SERVER": "模型服务暂时不可用",
    "MODEL_TIMEOUT": "模型响应超时",
    "MODEL_UNKNOWN": "模型请求未完成",
}

LEGACY_RUNTIME_REASONS = {
    "step wall time exhausted": "STEP_WALL_TIME_EXHAUSTED",
    "react iteration budget exhausted": "REACT_ITERATION_BUDGET_EXHAUSTED",
    "identical action budget exhausted": "IDENTICAL_ACTION_BUDGET_EXHAUSTED",
    "consecutive tool errors exhausted": "CONSECUTIVE_TOOL_ERRORS_EXHAUSTED",
    "invalid model action": "INVALID_MODEL_ACTION",
    "model requested block": "MODEL_REQUESTED_BLOCK",
}


def has_chinese(value: str) -> bool:
    return any("\u3400" <= character <= "\u9fff" for character in value)


def reason_message(reason_code: str, fallback: str = "任务已暂停，需要处理后继续") -> str:
    return RUNTIME_REASON_MESSAGES.get(reason_code, fallback)


def public_message(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if text in LEGACY_RUNTIME_REASONS:
        return reason_message(LEGACY_RUNTIME_REASONS[text], fallback)
    if text in RUNTIME_REASON_MESSAGES:
        return reason_message(text, fallback)
    return text if has_chinese(text) else fallback


def public_budget(budget: dict[str, Any]) -> dict[str, Any]:
    visible = {key: value for key, value in budget.items() if key not in {"identical_actions", "blocked_reason"}}
    legacy_reason = str(budget.get("blocked_reason") or "").strip()
    reason_code = str(budget.get("blocked_reason_code") or LEGACY_RUNTIME_REASONS.get(legacy_reason, "")).strip()
    if reason_code:
        visible["blocked_reason_code"] = reason_code
        visible["blocked_message"] = public_message(
            budget.get("blocked_message") or legacy_reason or reason_code,
            reason_message(reason_code),
        )
    elif legacy_reason:
        visible["blocked_reason_code"] = "LEGACY_RUNTIME_BLOCK"
        visible["blocked_message"] = public_message(legacy_reason, "任务已暂停，需要处理后继续")
    if "identical_actions" in budget:
        visible["identical_action_count"] = len(budget["identical_actions"])
    return visible
