from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from typing import Any, Callable

from .ask import ASK_TOOL_SCHEMA, AskRequest, AskValidationError, parse_ask_tool_call
from .conversation import ControlHeadDecoder, RouteProtocolError
from .model_gateway import GatewayError, ModelGateway, ModelRequest
from .runtime import ModelDecision, PlanDraft


class LiveRuntimeModel:
    """Small structured-output adapter around the single V1 ModelGateway."""

    def __init__(self, gateway: ModelGateway, tool_schemas: list[dict[str, Any]] | None = None) -> None:
        self.gateway = gateway
        self.tool_schemas = tool_schemas or []
        self._last_response: ContextVar[Any] = ContextVar("live_model_last_response", default=None)
        self._context_hash: ContextVar[str | None] = ContextVar("live_model_context_hash", default=None)
        self._context_text: ContextVar[str] = ContextVar("live_model_context_text", default="")
        self._text_delta_callback: ContextVar[Callable[[str], None] | None] = ContextVar(
            "live_model_text_delta_callback",
            default=None,
        )
        self._text_reset_callback: ContextVar[Callable[[], None] | None] = ContextVar(
            "live_model_text_reset_callback",
            default=None,
        )
        self._cancel_event: ContextVar[asyncio.Event | None] = ContextVar("live_model_cancel_event", default=None)

    def set_context_snapshot(self, snapshot_hash: str, text: str) -> None:
        self._context_hash.set(snapshot_hash)
        self._context_text.set(text)

    @property
    def last_response(self) -> Any:
        return self._last_response.get()

    @last_response.setter
    def last_response(self, response: Any) -> None:
        self._last_response.set(response)

    def set_text_delta_callback(self, callback: Callable[[str], None] | None):
        return self._text_delta_callback.set(callback)

    def reset_text_delta_callback(self, token) -> None:
        self._text_delta_callback.reset(token)

    def set_text_reset_callback(self, callback: Callable[[], None] | None):
        return self._text_reset_callback.set(callback)

    def reset_text_reset_callback(self, token) -> None:
        self._text_reset_callback.reset(token)

    def set_cancel_event(self, event: asyncio.Event):
        return self._cancel_event.set(event)

    def reset_cancel_event(self, token) -> None:
        self._cancel_event.reset(token)

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
        context_hash = self._context_hash.get()
        if context_hash:
            request_data["context_snapshot"] = {"hash": context_hash, "text": self._context_text.get()}
            messages[1] = {"role": "user", "content": json.dumps(request_data, ensure_ascii=False)}
        response = await self.gateway.complete(
            ModelRequest(messages=messages, tools=self.tool_schemas),
            cancel_event=self._cancel_event.get(),
            on_text_delta=self._text_delta_callback.get(),
            on_text_reset=self._text_reset_callback.get(),
        )
        self.last_response = response
        if allow_tool_calls and response.tool_calls:
            return _tool_call_payload(response.tool_calls)
        try:
            return _parse_json(response.message)
        except (ValueError, json.JSONDecodeError) as exc:
            reset_callback = self._text_reset_callback.get()
            if reset_callback is not None:
                reset_callback()
            repair = await self.gateway.complete(
                ModelRequest(messages=messages + [
                    {"role": "user", "content": "The previous response was not valid JSON. Return only the requested JSON object."},
                ], tools=self.tool_schemas),
                cancel_event=self._cancel_event.get(),
                on_text_delta=self._text_delta_callback.get(),
                on_text_reset=self._text_reset_callback.get(),
            )
            self.last_response = repair
            if allow_tool_calls and repair.tool_calls:
                return _tool_call_payload(repair.tool_calls)
            try:
                return _parse_json(repair.message)
            except (ValueError, json.JSONDecodeError) as repair_exc:
                raise GatewayError("structured model output is invalid", "structure", response.attempts + repair.attempts) from repair_exc


class LiveConversationModel:
    """Conversation adapter with a bounded ask tool and control-header repair."""

    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway

    async def route_and_respond(
        self,
        *,
        content: str,
        history: list[dict[str, str]],
        skill_names: list[str],
        on_text_delta,
        on_text_reset,
        cancel_event,
    ) -> Any:
        messages: list[dict[str, str]] = [{
            "role": "system",
            "content": (
                "Respond with one JSON control header on a single line, followed by the user-facing Markdown body. "
                "The header must have v=1, policy=answer|propose_execution|clarify, content_shape, and reason_code. "
                "Use answer for content, explanations, guides, comparisons, and plans as deliverables. "
                "Use propose_execution only for explicit ongoing tracking, tool use, external writes, or side effects. "
                "You decide whether the current request needs clarification. "
                "For a personalized, long-term, or goal-driven plan, call ask_user when missing information would materially change the plan. "
                "Treat a request to create a training tutorial, program, routine, or regimen intended to be followed by the user as a goal-driven deliverable, not merely a general explanation. "
                "If it is ambiguous whether the user wants a generic explanation or a personal plan, ask one concise intent question before drafting. "
                "Choose only the minimum questions needed for this specific request; do not use a fixed questionnaire and do not ask for information that is not relevant. "
                "For general knowledge, a broad guide, a template, or a useful first answer that can be written with explicit assumptions, answer directly instead of asking for preferences. "
                "The ask_user call must contain one to four concrete questions; do not emit prose or a control header in the same response. "
                "Use clarify only for legacy one-question text responses when a structured ask is not appropriate. "
                "Never expose the header, hidden reasoning, tool schema, or raw JSON in the Markdown body. "
                "Use the user's language and start the useful answer immediately after the header."
            ),
        }]
        messages.extend(history)
        messages.append({"role": "user", "content": content})
        async def complete_once(request_messages: list[dict[str, str]]):
            decoder = ControlHeadDecoder()
            buffered: list[str] = []
            forwarded = False

            def emit(chunk: str) -> None:
                nonlocal forwarded
                if forwarded:
                    if on_text_delta is not None:
                        on_text_delta(chunk)
                    return
                buffered.append(chunk)
                try:
                    decoder.feed(chunk)
                except RouteProtocolError:
                    return
                if decoder.header is not None:
                    forwarded = True
                    if on_text_delta is not None:
                        on_text_delta("".join(buffered))
                    buffered.clear()

            def reset() -> None:
                nonlocal decoder, buffered, forwarded
                decoder = ControlHeadDecoder()
                buffered = []
                forwarded = False
                if on_text_reset is not None:
                    on_text_reset()

            response = await self.gateway.complete(
                ModelRequest(messages=request_messages, tools=[ASK_TOOL_SCHEMA]),
                cancel_event=cancel_event,
                on_text_delta=emit,
                on_text_reset=reset,
            )
            tool_calls = getattr(response, "tool_calls", []) or []
            if tool_calls:
                if forwarded or buffered:
                    reset()
                if len(tool_calls) != 1:
                    raise GatewayError("conversation supports one ask tool call at a time", "structure")
                try:
                    return parse_ask_tool_call(tool_calls[0]), True
                except AskValidationError as exc:
                    raise GatewayError(str(exc), "structure") from exc
            message = getattr(response, "message", None)
            if not isinstance(message, str):
                return response, True
            try:
                final_decoder = ControlHeadDecoder()
                final_decoder.feed(message)
                final_decoder.finish()
                return response, True
            except RouteProtocolError:
                return response, False

        response, valid = await complete_once(messages)
        if isinstance(response, AskRequest):
            return response
        if valid:
            return response

        if on_text_reset is not None:
            on_text_reset()
        repair_messages = messages + [{
            "role": "user",
            "content": (
                "The previous response violated the conversation control-header protocol. "
                "Retry the original user request now. The first line must be exactly one JSON object "
                "with v=1, policy, content_shape, and reason_code; do not put prose, Markdown, or a code fence before it."
            ),
        }]
        response, _ = await complete_once(repair_messages)
        return response


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
