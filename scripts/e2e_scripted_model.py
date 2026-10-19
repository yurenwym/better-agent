"""Deterministic model boundary for the page E2E server.

The production gateway, AgentLoop, tool executor, approvals and domain
services stay in place. This replaces only the HTTP attempt. It never reads
a developer provider key.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PLAN_TITLE = "页面创建的两天计划"
PLAN_MARKDOWN = (
    "# 页面创建的两天计划\n\n"
    "## 目标\n用两天完成一个可验证的小目标。\n\n"
    "## 每日安排\n- 每天完成一个行动并记录结果。\n"
)
_PLAN_REF = re.compile(r"\[plan document_id=(\S+) version_id=(\S+) version=\d+\]")
_USER_MESSAGE = re.compile(r'"message_id": "([^"]+)", "role": "user"')
_ANY_MESSAGE = re.compile(r'"message_id": "([^"]+)"')


def _shanghai(offset: int = 0) -> str:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return (today + timedelta(days=offset)).isoformat()


def _tool(name: str, arguments: dict) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _objects(texts: list[str]) -> list[dict]:
    found: list[dict] = []
    decoder = json.JSONDecoder()
    for text in texts:
        index = 0
        while True:
            start = text.find("{", index)
            if start < 0:
                break
            try:
                value, consumed = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                index = start + 1
                continue
            if isinstance(value, dict):
                found.append(value)
            index = start + consumed
    return found


def _current_user(messages: list[dict]) -> tuple[int, str]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return index, message["content"]
    return -1, ""


def _turn(messages: list[dict]) -> tuple[list[str], list[dict]]:
    index, _ = _current_user(messages)
    names: list[str] = []
    bodies: list[str] = []
    if index < 0:
        return names, []
    for message in messages[index + 1:]:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                names.append(function["name"])
        if message.get("role") == "tool" and isinstance(message.get("content"), str):
            bodies.append(message["content"])
    return names, _objects(bodies)


def _plan_ref(messages: list[dict]) -> tuple[str, str]:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        match = _PLAN_REF.search(content)
        if match:
            return match.group(1), match.group(2)
    raise RuntimeError("saved plan document id is not in the model context")


def _payloads(objects: list[dict]) -> list[dict]:
    """Same-turn observations put fields at the top. Replayed history wraps them in data."""
    payloads: list[dict] = []
    for item in objects:
        payloads.append(item)
        data = item.get("data")
        if isinstance(data, dict):
            payloads.append(data)
    return payloads


def _program(objects: list[dict]) -> dict:
    for item in _payloads(objects):
        if isinstance(item.get("program_id"), str) and isinstance(item.get("program_version"), int):
            return item
    raise RuntimeError("preview did not return a program id")


def _program_id(messages: list[dict]) -> str:
    contents = [message.get("content") for message in messages if isinstance(message.get("content"), str)]
    for item in reversed(_payloads(_objects([content for content in contents if isinstance(content, str)]))):
        if isinstance(item.get("program_id"), str):
            return item["program_id"]
    raise RuntimeError("program id is not in the conversation")


def _action(objects: list[dict]) -> dict:
    for item in _payloads(objects):
        for action in item.get("items") or []:
            if not isinstance(action, dict):
                continue
            title = str(action.get("title") or "")
            if action.get("category") == "today" or "第 1 天" in title:
                if isinstance(action.get("id"), str) and isinstance(action.get("version"), int):
                    return action
                raise RuntimeError(f"today action has no id or version: {action}")
    raise RuntimeError("get_today_tasks did not return today's action")


def _review_summary(objects: list[dict]) -> str | None:
    for item in _payloads(objects):
        for group in item.get("groups") or []:
            review = group.get("review") if isinstance(group, dict) else None
            if isinstance(review, dict) and isinstance(review.get("summary"), str) and review["summary"]:
                return review["summary"]
    return None


def _conversation(request) -> dict:
    messages = list(request.messages)
    _, user = _current_user(messages)
    names, objects = _turn(messages)
    if user.startswith("归档填充"):
        return {"message": "已记下。", "tool_calls": []}
    if user.startswith("归档触发句E2E"):
        return {"message": "整理完成后继续。", "tool_calls": []}
    if "待确认的工具操作已经有了结果" in user:
        # Tool results in one turn share a sequence and are ordered by call id,
        # so the last tool message is not always the call that was just approved.
        # The last result whose own summary matches is the one to answer.
        matched = ""
        for message in messages:
            content = message.get("content")
            if message.get("role") != "tool" or not isinstance(content, str):
                continue
            if "行动已标记完成" in content:
                matched = "complete"
            elif "当天复盘已登记" in content:
                matched = "close"
            elif "目标已激活" in content:
                matched = "activate"
        if matched == "complete":
            return {"message": "今天的行动已标记完成。", "tool_calls": []}
        if matched == "close":
            return {"message": "已提交今天的收尾。", "tool_calls": []}
        if matched == "activate":
            return {"message": "执行已激活。", "tool_calls": []}
        raise RuntimeError("continuation did not include a recognized tool result")
    if "请创建并保存" in user:
        return {
            "message": PLAN_MARKDOWN,
            "tool_calls": [_tool("publish_plan_document", {"title": PLAN_TITLE, "source": "this_response"})],
        }
    if "生成执行预览" in user:
        if "preview_goal_plan" not in names:
            document_id, version_id = _plan_ref(messages)
            return {"message": "", "tool_calls": [_tool("preview_goal_plan", {
                "document_id": document_id,
                "expected_document_version_id": version_id,
                "start_date": _shanghai(),
                "end_date": _shanghai(1),
                "timezone": "Asia/Shanghai",
                "daily_minutes": 60,
            })]}
        if "activate_goal_plan" not in names:
            preview = _program(objects)
            return {"message": "", "tool_calls": [_tool("activate_goal_plan", {
                "mode": "activate",
                "program_id": preview["program_id"],
                "expected_version": preview["program_version"],
            })]}
        return {"message": "执行已激活。", "tool_calls": []}
    if "标记为完成" in user:
        if "get_today_tasks" not in names:
            return {"message": "", "tool_calls": [_tool("get_today_tasks", {
                "program_id": _program_id(messages), "date": _shanghai(),
            })]}
        if "complete_action" not in names:
            action = _action(objects)
            return {"message": "", "tool_calls": [_tool("complete_action", {
                "action_id": action["id"],
                "expected_version": action["version"],
                "completed_work": "页面标记完成",
            })]}
        return {"message": "今天的行动已标记完成。", "tool_calls": []}
    if "结束今天并保存复盘" in user:
        if "close_day" not in names:
            return {"message": "", "tool_calls": [_tool("close_day", {
                "program_id": _program_id(messages), "local_date": _shanghai(),
            })]}
        return {"message": "已提交今天的收尾。", "tool_calls": []}
    if "读取今天的复盘" in user:
        if "get_today_tasks" not in names:
            return {"message": "", "tool_calls": [_tool("get_today_tasks", {
                "program_id": _program_id(messages), "date": _shanghai(),
            })]}
        summary = _review_summary(objects)
        if not summary:
            raise RuntimeError("review summary is not in the tool result")
        return {"message": f"今天的复盘：{summary}", "tool_calls": []}
    raise RuntimeError(f"no scripted conversation response for {user[:80]!r}")


async def _wait_release() -> None:
    gate = Path(os.environ["BETTER_AGENT_DATA_ROOT"]) / "e2e-archive-release"
    for _ in range(1800):
        if gate.exists():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("e2e archive release was not signaled")


def _episode_summary(request) -> str:
    text = "\n".join(
        message.get("content", "")
        for message in request.messages
        if isinstance(message.get("content"), str)
    )
    ids = _USER_MESSAGE.findall(text) or _ANY_MESSAGE.findall(text)
    if not ids:
        raise RuntimeError("archive transcript has no message id")
    return json.dumps({
        "synopsis": [{"text": "已整理这段对话。", "source_message_ids": [ids[0]]}],
        "topics": [],
        "decisions": [],
        "outcomes": [],
        "open_loops": [],
        "sensitivity": "normal",
    }, ensure_ascii=False)


def _response(message: str, tool_calls: list | None = None):
    from app.model_gateway import ModelResponse, Timing, UsageBuckets

    return ModelResponse(
        message, tool_calls or [], "stop",
        UsageBuckets(1, 0, 0, 1, 0), Timing(0, 0, 1), 1,
    )


async def execute_attempt(profile, request, *, cancel_event=None, on_text_delta=None, on_output_started=None):
    del profile, cancel_event
    if request.role == "reflector" and request.purpose == "summarize_episode":
        await _wait_release()
        message = _episode_summary(request)
        if on_output_started is not None:
            on_output_started("message")
        return _response(message)
    if request.role != "conversation":
        raise RuntimeError(f"no scripted response for role={request.role!r} purpose={request.purpose!r}")
    scripted = _conversation(request)
    message = scripted["message"]
    if message and on_text_delta is not None:
        on_text_delta(message)
    if on_output_started is not None:
        on_output_started("message" if message else "tool_call")
    return _response(message, scripted["tool_calls"])
