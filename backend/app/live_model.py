from __future__ import annotations

import json
from typing import Any

from .model_gateway import GatewayError, ModelGateway, ModelRequest
from .runtime import ModelDecision, PlanDraft


class LiveRuntimeModel:
    """Small structured-output adapter around the single V1 ModelGateway."""

    def __init__(self, gateway: ModelGateway, tool_schemas: list[dict[str, Any]] | None = None) -> None:
        self.gateway = gateway
        self.tool_schemas = tool_schemas or []
        self.last_response = None
        self.context_hash = None
        self.context_text = ""

    def set_context_snapshot(self, snapshot_hash: str, text: str) -> None:
        self.context_hash = snapshot_hash
        self.context_text = text

    async def needs_clarification(self, goal: dict[str, Any], interactions: list[str]) -> bool:
        payload = await self._json(
            "Return JSON only: {\"needs_clarification\": true|false}. Ask for clarification only when the objective or deliverable is genuinely missing. "
            "A broad content request is enough for an assumption-based first version; do not ask for personal details before providing it. "
            "For example, a request for a 7-day diet plan can be planned with clearly stated assumptions and safety notes. Do not ask for personal details.",
            {"goal": goal, "interactions": interactions},
        )
        return bool(payload.get("needs_clarification", False))

    async def plan(self, goal: dict[str, Any], interactions: list[str]) -> PlanDraft:
        payload = await self._json(
            "Return JSON only with keys summary and steps. steps must be a non-empty array of objects with id, title, description. "
            "Make the plan tailored to the exact user goal and its deliverable, using the user's language. "
            "For content or planning requests, put concrete deliverable details in summary, step titles, or descriptions; "
            "do not return generic checklist steps such as clarify the goal, execute each item, or summarize progress unless the user explicitly asks for a workflow.",
            {"goal": goal, "interactions": interactions},
        )
        steps = payload.get("steps")
        if not isinstance(steps, list) or not steps:
            raise GatewayError("structured plan output is invalid", "structure")
        normalized = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or not str(step.get("title", "")).strip():
                raise GatewayError("structured plan step is invalid", "structure")
            normalized.append({
                "id": str(step.get("id") or f"step-{index + 1}"),
                "title": str(step["title"]).strip(),
                "description": str(step.get("description", "")),
            })
        return PlanDraft(normalized, str(payload.get("summary", "")))

    async def decide(self, step: dict[str, Any], observation: str, iteration: int) -> ModelDecision:
        payload = await self._json(
            "Return JSON only. action must be one of complete_step, continue, await_outcome, tool_call, blocked. "
            "Put the user-visible result in output or summary; do not use a generic label when a concrete result is available. "
            "For a content deliverable, return the visible answer as soon as the step and observation contain enough information. "
            "Do not call tools just to fill assumptions, get the current time, or repeat a result already present in observation. "
            "Use at most one tool only when the current step explicitly requires it. "
            "For tool_call include tool_call: {id,name,params}. Never return hidden reasoning.",
            {"step": step, "observation": observation, "iteration": iteration},
            allow_tool_calls=True,
        )
        action = str(payload.get("action", "blocked"))
        if action == "complete_step":
            return ModelDecision.complete(_visible_result(payload, "completed"))
        if action == "continue":
            return ModelDecision.continue_(_visible_result(payload, "continue", "observation"))
        if action == "await_outcome":
            return ModelDecision.await_outcome(_visible_result(payload, "awaiting outcome", "observation"))
        if action == "blocked":
            return ModelDecision.blocked(_visible_result(payload, "blocked"))
        if action == "tool_call":
            tool = payload.get("tool_call")
            if not isinstance(tool, dict) or not isinstance(tool.get("params"), dict) or not tool.get("name"):
                raise GatewayError("structured tool call is invalid", "structure")
            from .tools import ToolCall

            return ModelDecision.tool(
                ToolCall(str(tool.get("id") or f"model-call-{iteration}"), str(tool["name"]), tool["params"]),
                _visible_result(payload, "tool proposed"),
            )
        raise GatewayError("unknown structured model action", "structure")

    async def reflect(self, goal: dict[str, Any], plan: Any, run_id: str) -> list[dict[str, Any]]:
        payload = await self._json(
            "Return JSON only with key candidates. candidates is an array of optional preference or habit objects with kind, content, scope, confidence, evidence_event_ids. Do not invent a candidate when evidence is absent.",
            {"goal": goal, "plan": _plan_json(plan), "run_id": run_id},
        )
        candidates = payload.get("candidates", [])
        if not isinstance(candidates, list):
            return []
        valid: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            kind = candidate.get("kind")
            content = candidate.get("content")
            scope = candidate.get("scope")
            confidence = candidate.get("confidence", 0.5)
            evidence = candidate.get("evidence_event_ids", [])
            if kind not in {"preference", "habit"} or not isinstance(content, str) or not content.strip():
                continue
            if scope not in {"global", "project", "skill"}:
                continue
            if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                continue
            if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
                continue
            if scope == "project" and not candidate.get("project_id"):
                continue
            if scope == "skill" and not candidate.get("skill_name"):
                continue
            valid.append(candidate)
        return valid

    async def _json(
        self,
        instruction: str,
        input_data: dict[str, Any],
        *,
        allow_tool_calls: bool = False,
    ) -> dict[str, Any]:
        self.last_response = None
        messages = [
            {"role": "system", "content": instruction},
            {"role": "user", "content": json.dumps(input_data, ensure_ascii=False)},
        ]
        request_data = dict(input_data)
        if self.context_hash:
            request_data["context_snapshot"] = {"hash": self.context_hash, "text": self.context_text}
            messages[1] = {"role": "user", "content": json.dumps(request_data, ensure_ascii=False)}
        response = await self.gateway.complete(ModelRequest(messages=messages, tools=self.tool_schemas))
        self.last_response = response
        if allow_tool_calls and response.tool_calls:
            return _tool_call_payload(response.tool_calls)
        try:
            return _parse_json(response.message)
        except (ValueError, json.JSONDecodeError) as exc:
            repair = await self.gateway.complete(ModelRequest(messages=messages + [
                {"role": "user", "content": "The previous response was not valid JSON. Return only the requested JSON object."},
            ], tools=self.tool_schemas))
            self.last_response = repair
            if allow_tool_calls and repair.tool_calls:
                return _tool_call_payload(repair.tool_calls)
            try:
                return _parse_json(repair.message)
            except (ValueError, json.JSONDecodeError) as repair_exc:
                raise GatewayError("structured model output is invalid", "structure", response.attempts + repair.attempts) from repair_exc


def _parse_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0].strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("JSON response must be an object")
    return value


def _visible_result(payload: dict[str, Any], fallback: str, secondary: str = "summary") -> str:
    """Normalize provider naming variants into the user-visible decision text."""
    for key in ("output", secondary, "summary", "observation"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return fallback


def _tool_call_payload(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    if not tool_calls:
        raise GatewayError("streamed tool call is empty", "structure")
    # Some compatible providers still emit a batch even when V1 must execute
    # actions sequentially. Keep the first action and let the next ReAct turn
    # decide whether another action is needed; never execute the batch in parallel.
    call = tool_calls[0]
    function = call.get("function") or {}
    name = function.get("name")
    arguments = function.get("arguments", "{}")
    if not isinstance(name, str) or not name.strip():
        raise GatewayError("streamed tool call is missing a name", "structure")
    if isinstance(arguments, str):
        try:
            params = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            raise GatewayError("streamed tool call arguments are invalid", "structure") from exc
    else:
        params = arguments
    if not isinstance(params, dict):
        raise GatewayError("streamed tool call arguments must be an object", "structure")
    return {
        "action": "tool_call",
        "tool_call": {
            "id": str(call.get("id") or ""),
            "name": name,
            "params": params,
        },
    }


def _plan_json(plan: Any) -> dict[str, Any]:
    return {
        "version": getattr(plan, "version", None),
        "summary": getattr(plan, "summary", ""),
        "steps": [
            {"id": step.id, "title": step.title, "status": step.status}
            for step in getattr(plan, "steps", ())
        ],
    }
