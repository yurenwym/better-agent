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

    async def needs_clarification(self, goal: dict[str, Any], interactions: list[str]) -> bool:
        payload = await self._json(
            "Return JSON only: {\"needs_clarification\": true|false}. Ask for clarification only when the goal lacks enough information to plan.",
            {"goal": goal, "interactions": interactions},
        )
        return bool(payload.get("needs_clarification", False))

    async def plan(self, goal: dict[str, Any], interactions: list[str]) -> PlanDraft:
        payload = await self._json(
            "Return JSON only with keys summary and steps. steps must be a non-empty array of objects with id, title, description. Keep steps atomic and short.",
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
            "For tool_call include tool_call: {id,name,params}. Never return hidden reasoning.",
            {"step": step, "observation": observation, "iteration": iteration, "tools": self.tool_schemas},
        )
        action = str(payload.get("action", "blocked"))
        if action == "complete_step":
            return ModelDecision.complete(str(payload.get("summary", "completed")))
        if action == "continue":
            return ModelDecision.continue_(str(payload.get("observation", "continue")))
        if action == "await_outcome":
            return ModelDecision.await_outcome(str(payload.get("observation", "awaiting outcome")))
        if action == "blocked":
            return ModelDecision.blocked(str(payload.get("summary", "blocked")))
        if action == "tool_call":
            tool = payload.get("tool_call")
            if not isinstance(tool, dict) or not isinstance(tool.get("params"), dict) or not tool.get("name"):
                raise GatewayError("structured tool call is invalid", "structure")
            from .tools import ToolCall

            return ModelDecision.tool(
                ToolCall(str(tool.get("id") or f"model-call-{iteration}"), str(tool["name"]), tool["params"]),
                str(payload.get("summary", "tool proposed")),
            )
        raise GatewayError("unknown structured model action", "structure")

    async def reflect(self, goal: dict[str, Any], plan: Any, run_id: str) -> list[dict[str, Any]]:
        payload = await self._json(
            "Return JSON only with key candidates. candidates is an array of optional preference or habit objects with kind, content, scope, confidence, evidence_event_ids. Do not invent a candidate when evidence is absent.",
            {"goal": goal, "plan": _plan_json(plan), "run_id": run_id},
        )
        candidates = payload.get("candidates", [])
        return [candidate for candidate in candidates if isinstance(candidate, dict)] if isinstance(candidates, list) else []

    async def _json(self, instruction: str, input_data: dict[str, Any]) -> dict[str, Any]:
        self.last_response = None
        messages = [
            {"role": "system", "content": instruction},
            {"role": "user", "content": json.dumps(input_data, ensure_ascii=False)},
        ]
        response = await self.gateway.complete(ModelRequest(messages=messages, tools=self.tool_schemas))
        self.last_response = response
        try:
            return _parse_json(response.message)
        except (ValueError, json.JSONDecodeError) as exc:
            repair = await self.gateway.complete(ModelRequest(messages=messages + [
                {"role": "user", "content": "The previous response was not valid JSON. Return only the requested JSON object."},
            ], tools=self.tool_schemas))
            self.last_response = repair
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


def _plan_json(plan: Any) -> dict[str, Any]:
    return {
        "version": getattr(plan, "version", None),
        "summary": getattr(plan, "summary", ""),
        "steps": [
            {"id": step.id, "title": step.title, "status": step.status}
            for step in getattr(plan, "steps", ())
        ],
    }
