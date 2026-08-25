from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any, Callable

from .ask import ASK_TOOL_SCHEMA, AskRequest, AskValidationError, parse_ask_tool_call
from .conversation import ControlHeadDecoder, RouteProtocolError
from .model_gateway import GatewayError, ModelGateway, ModelRequest
from .runtime import ModelDecision, PlanDraft


def _is_explicit_research_command(content: str) -> bool:
    if re.search(r"(?:基于|参考|根据|结合).{0,12}(?:深度|深入)(?:研究|调研)|(?:刚才|之前|已有|上述).{0,8}(?:深度|深入)(?:研究|调研)", content, re.I):
        return False
    return bool(re.search(r"(?:请|帮我|开始|进行|开展|启动|做一份?)?\s*(?:深度|深入)(?:研究|调研)|\b(?:start|do|conduct)\s+(?:a\s+)?deep\s+research\b", content, re.I))


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

    def __init__(self, gateway: ModelGateway, settings=None) -> None:
        self.gateway = gateway
        self.settings = settings
        self.memory_store = None

    async def _classify_existing_plan_save(
        self,
        content: str,
        history: list[dict[str, Any]],
        cancel_event,
    ) -> bool:
        if not _has_prior_assistant_markdown_plan(history):
            return False
        request = ModelRequest(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Return JSON only: {\"save_existing_plan\": true|false}. "
                        "Use the LLM to decide whether the latest user message explicitly asks to write, save, or generate a document from a complete Markdown plan already shown by the assistant. "
                        "A request such as 写进计划页面, 保存到计划, or 生成文档 means true only when that prior assistant plan exists. "
                        "Questions about the plan, requests for more detail, or personalization without an explicit save request are false. "
                        "Do not infer true from generic planning keywords."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "latest_user_message": content,
                            "has_prior_assistant_markdown_plan": True,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            tools=[],
            temperature=0,
        )
        try:
            response = await self.gateway.complete(request, cancel_event=cancel_event)
            if getattr(response, "tool_calls", []) or []:
                raise GatewayError("conversation returned a tool call while tools are disabled", "structure")
            payload = _parse_json(response.message)
        except GatewayError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return payload.get("save_existing_plan") is True

    async def _classify_explicit_plan_document_request(
        self,
        content: str,
        history: list[dict[str, Any]],
        cancel_event,
    ) -> bool:
        request = ModelRequest(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Return JSON only: {\"plan_document_request\": true|false}. "
                        "Use the LLM to decide whether the latest user message explicitly asks for a complete plan document "
                        "to be generated, written, saved, or put into the plan page. True includes a direct request to proceed "
                        "without asking questions. False includes a generic guide or recommendation, a question about a plan, "
                        "or a plan request without an explicit document/save instruction. Merely asking to make a 7-day diet, "
                        "travel, or training plan is an answer request, not a document save request. Do not infer true from "
                        "planning keywords alone."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "latest_user_message": content,
                            "has_prior_assistant_markdown_plan": _has_prior_assistant_markdown_plan(history),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            tools=[],
            temperature=0,
        )
        try:
            response = await self.gateway.complete(request, cancel_event=cancel_event)
            if getattr(response, "tool_calls", []) or []:
                return False
            payload = _parse_json(response.message)
        except GatewayError as exc:
            if exc.kind == "cancelled":
                raise
            return False
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return payload.get("plan_document_request") is True

    async def _classify_explicit_research_request(self, content: str, cancel_event) -> tuple[bool, str]:
        request = ModelRequest(messages=[
            {"role":"system","content":"Return JSON only: {\"start_research\":true|false,\"topic\":\"...\"}. True only when the user explicitly requests starting a new deep research, investigation, comparison with sources, or a sourced report. References to existing research, such as 'based on the previous deep research, create a plan', are false. Ordinary questions, guides, plans, recommendations, and requests that can be answered directly are false. Preserve the requested topic concisely."},
            {"role":"user","content":content},
        ],tools=[],temperature=0,max_tokens=200)
        try:
            response=await self.gateway.complete(request,cancel_event=cancel_event)
            payload=_parse_json(response.message)
            return payload.get("start_research") is True, str(payload.get("topic") or content).strip()[:2000]
        except GatewayError as exc:
            if exc.kind=="cancelled":raise
            return False,content
        except (TypeError,ValueError,json.JSONDecodeError):return False,content

    async def _classify_explicit_remember(self,content:str,cancel_event):
        if not isinstance(self.gateway,ModelGateway):return None
        explicit_marker=bool(re.search(r"(?:请|帮我|以后)?\s*(?:记住|记得)|\bremember\b",content,re.I))
        if not explicit_marker:return None
        request=ModelRequest(messages=[{"role":"system","content":"Return JSON only: {\"remember\":true|false,\"kind\":\"preference|constraint|fact|decision|lesson\",\"scope_type\":\"user|project\",\"scope_id\":\"\",\"content\":\"...\"}. True only when the user explicitly commands you to remember stable information for future conversations. Examples that MUST be true: 'Remember that I dislike spicy food', '请记住我喜欢简洁明确的回答', '以后记得我九点后出发'. Ordinary statements without an explicit remember-for-future command are false. Preserve only the stable fact in content. Never include credentials or secrets."},{"role":"user","content":content}],tools=[],temperature=0,max_tokens=220)
        try:
            payload=_parse_json((await self.gateway.complete(request,cancel_event=cancel_event)).message)
            if payload.get("remember") is not True and explicit_marker:
                repair=ModelRequest(messages=[*request.messages,{"role":"system","content":"The application already verified an explicit remember-for-future command. Return the same JSON schema with remember=true and extract the stable information; do not ask a question."}],tools=[],temperature=0,max_tokens=220)
                payload=_parse_json((await self.gateway.complete(repair,cancel_event=cancel_event)).message)
            if payload.get("remember") is not True:return None
            if payload.get("kind") not in {"preference","constraint","fact","decision","lesson"} or payload.get("scope_type") not in {"user","project"}:return None
            return payload
        except GatewayError as exc:
            if exc.kind=="cancelled":raise
            return None
        except Exception:return None

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
        human_mode = bool(self.settings and self.settings.get().human_mode)
        if self.memory_store is not None:
            remember=await self._classify_explicit_remember(content,cancel_event)
            if remember:
                item=self.memory_store.remember("local-user",remember["kind"],remember["scope_type"],remember.get("scope_id", ""),remember["content"],f"conversation:{hashlib.sha256(content.encode()).hexdigest()}")
                header=json.dumps({"v":1,"policy":"answer","content_shape":"text","reason_code":"memory_saved"},ensure_ascii=False)+"\n"
                body=f"已记住：{item.content}\n\n你可以随时在记忆页面编辑、停用或删除。"
                if on_text_delta is not None:on_text_delta(header+body)
                return type("RememberResponse",(),{"message":header+body,"tool_calls":[],"finish_reason":"stop"})()
        explicit_research=_is_explicit_research_command(content)
        start_research,research_topic=(True,content.strip()[:2000]) if explicit_research else ((await self._classify_explicit_research_request(content,cancel_event)) if isinstance(self.gateway, ModelGateway) else (False,content))
        if start_research:
            header=json.dumps({"v":3,"policy":"start_research","content_shape":"research","reason_code":"explicit_deep_research","research":{"topic":research_topic,"scope":"web"}},ensure_ascii=False)+"\n"
            if on_text_delta is not None:on_text_delta(header)
            return type("ResearchRouteResponse",(),{"message":header,"tool_calls":[],"finish_reason":"stop"})()
        messages: list[dict[str, str]] = [{
            "role": "system",
            "content": (
                "Respond with one JSON control header on a single line, followed by the user-facing Markdown body. "
                "Use V1 for answer-only compatibility, V2 for saved documents, V3 for explicit deep research, and V4 for bounded expert collaboration. "
                "The header policy is answer|propose_execution|clarify|start_research|start_expert. "
                "Only when the user explicitly asks for deep research, investigation, or a sourced report, return exactly "
                "v=3, policy=start_research, content_shape=research, reason_code=explicit_deep_research, and research={topic,scope:web}; no visible body or artifact. "
                "For an explicit request to create, save, or modify a plan document, use v=2 with exactly one artifact "
                "object: kind=plan_document, operation=upsert, and a concise title. Return the complete Markdown document "
                "after the header; that exact visible body is the saved document. "
                "Do not use an artifact for a generic guide, explanation, or answer-only plan. "
                "Artifact authority comes only from the user's explicit request; never invent a save request from keywords alone. "
                "Highest-priority save rule: saving an existing plan means the user explicitly asks to put, write, or save a complete plan already present in the conversation into the plan page or generate a plan document from that plan. "
                "This includes requests such as 'write the plan already shown into the plan page', 'save that plan as a plan document', or 'generate the document from that plan'; a standalone request to generate a new document does not count as saving an existing plan. "
                "For saving an existing plan, do not call ask_user and do not ask for personalization; return v=2 with the plan_document upsert artifact and reproduce the complete existing Markdown body, applying any explicit edits. "
                "If history contains a prior assistant Markdown plan and the latest request includes Chinese phrases such as '\u5199\u8fdb\u8ba1\u5212\u9875\u9762', '\u4fdd\u5b58\u5230\u8ba1\u5212', or '\u751f\u6210\u6587\u6863', treat it as saving an existing plan and do not call ask_user. "
                "This saving rule takes precedence over the personalized-plan question rule below. "
                "For a new plan document, when no complete plan body exists in the conversation, follow the personalized-plan question rule; after ask_user answers, if the original request explicitly asks to create the plan document, return v=2 with the artifact. "
                "Use answer for content, explanations, guides, comparisons, and plans as deliverables. "
                "Use start_expert only when at least two independent perspectives materially improve a complex comparison or decision; return exactly v=4 with expert={objective,roles}, roles chosen only from researcher,planner,critic, and no visible body. "
                "Use propose_execution only for explicit ongoing tracking, tool use, external writes, or side effects. "
                "You decide which relevant personal context is missing from the current request and history. "
                "For a personalized, long-term, or goal-driven plan, you must call ask_user before drafting when the relevant personal context is not already provided. "
                "Treat a request to create a training tutorial, program, routine, or regimen intended to be followed by the user as a goal-driven deliverable, not merely a general explanation, and apply the same rule. "
                "Only skip ask_user when the user explicitly asks for a generic explanation or template, or has already provided the relevant personal context; do not answer first and ask later. "
                "If it is ambiguous whether the user wants a generic explanation or a personal plan, ask one concise intent question before drafting. "
                "Choose only the minimum questions needed for this specific request; do not use a fixed questionnaire and do not ask for information that is not relevant. "
                "For general knowledge, a broad guide, a template, or a useful first answer that can be written with explicit assumptions, answer directly instead of asking for preferences. "
                "The ask_user call must contain one to four concrete questions; do not emit prose or a control header in the same response. "
                "Use clarify only for legacy one-question text responses when a structured ask is not appropriate. "
                "Never expose the header, hidden reasoning, tool schema, or raw JSON in the Markdown body. "
                "Use the user's language and start the useful answer immediately after the header."
            ),
        }]
        if human_mode:
            messages.append({"role": "system", "content": (
                "Human conversation mode applies only to user-facing text after the exact JSON control header. "
                "Never apply it to control JSON, ask_user, plan_document artifacts, research reports, runtime JSON, tools, or citations. "
                "Use natural spoken Chinese in usually 1-2 and at most 3 bubbles. Put [[next]] on its own line only at a real pause. "
                "Avoid Markdown headings, tables, and report-style numbered lists in the ordinary visible body."
            )})
        save_existing_plan = await self._classify_existing_plan_save(content, history, cancel_event)
        if save_existing_plan:
            messages.append({
                "role": "system",
                "content": (
                    "The intent gate classified this request as saving an existing plan. "
                    "Do not call ask_user. The response must contain a valid v=2 plan_document upsert artifact "
                    "and the complete Markdown plan body."
                ),
            })
        messages.extend(history)
        messages.append({"role": "user", "content": content})
        async def complete_once(request_messages: list[dict[str, str]], *, tools=None):
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
                ModelRequest(
                    messages=request_messages,
                    tools=[ASK_TOOL_SCHEMA] if tools is None else tools,
                    temperature=0,
                ),
                cancel_event=cancel_event,
                on_text_delta=emit,
                on_text_reset=reset,
            )
            tool_calls = getattr(response, "tool_calls", []) or []
            if tool_calls:
                if tools is not None and not tools:
                    raise GatewayError("conversation returned a tool call while tools are disabled", "structure")
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

        async def force_plan_document(instruction: str):
            if on_text_reset is not None:
                on_text_reset()
            force_messages = messages + [{"role": "user", "content": instruction}]
            forced_response, _ = await complete_once(force_messages, tools=[])
            if not _response_has_plan_artifact(forced_response):
                forced_response = _wrap_confirmed_plan_markdown(forced_response)
                if on_text_reset is not None: on_text_reset()
                if on_text_delta is not None: on_text_delta(forced_response.message)
            return forced_response

        response, valid = await complete_once(messages, tools=[] if save_existing_plan else None)
        declared_plan_document = _response_declares_plan_document_intent(response)
        plan_document_intent: bool | None = True if save_existing_plan else None
        if _response_has_plan_artifact(response) and not save_existing_plan:
            plan_document_intent = await self._classify_explicit_plan_document_request(content, history, cancel_event)
            if not plan_document_intent:
                response = _downgrade_unconfirmed_plan_document(response)
                if on_text_reset is not None: on_text_reset()
                if on_text_delta is not None: on_text_delta(response.message)
                return response
        if (save_existing_plan or declared_plan_document) and not _response_has_plan_artifact(response):
            if plan_document_intent is None:
                plan_document_intent = await self._classify_explicit_plan_document_request(content, history, cancel_event)
            if plan_document_intent:
                return await force_plan_document(
                    "The conversation response declared a plan-document request. "
                    "Retry now with no tool call: the first line must be a valid v=2 JSON control header "
                    "containing exactly one plan_document upsert artifact, followed by the complete Markdown body."
                )
        if isinstance(response, AskRequest):
            if not await self._classify_explicit_plan_document_request(content, history, cancel_event):
                return response
            return await force_plan_document(
                "The intent gate confirmed an explicit plan-document request. "
                "Do not call ask_user or request more context. Return a valid v=2 JSON control header "
                "with exactly one plan_document upsert artifact, followed by a complete Markdown plan using "
                "reasonable explicit assumptions."
            )
        if _response_is_plan_shaped(response) and not _response_has_plan_artifact(response):
            if plan_document_intent is None:
                plan_document_intent = await self._classify_explicit_plan_document_request(content, history, cancel_event)
            if plan_document_intent:
                return await force_plan_document(
                    "The intent gate confirmed an explicit plan-document request. "
                    "Retry with no tool call and return a valid v=2 JSON control header with exactly one "
                    "plan_document upsert artifact, followed by the complete Markdown plan body."
                )
        if valid:
            return response

        if plan_document_intent is None:
            plan_document_intent = await self._classify_explicit_plan_document_request(content, history, cancel_event)
        if plan_document_intent:
            return await force_plan_document(
                'The intent gate confirmed a saved plan document. Begin with this exact JSON shape on one line: '
                '{"v":2,"policy":"answer","content_shape":"plan_document","reason_code":"explicit_plan_save",'
                '"artifact":{"kind":"plan_document","operation":"upsert","title":"PLAN TITLE"}}. '
                'Replace only PLAN TITLE, then put the complete Markdown plan beginning with # on the next line.'
            )

        if on_text_reset is not None:
            on_text_reset()
        repair_messages = messages + [{
            "role": "user",
            "content": (
                "The previous response violated the conversation control-header protocol. "
                "Retry the original user request now. The first line must be exactly one JSON object "
                "with v=1 or v=2, policy, content_shape, and reason_code; if the user explicitly requested a saved plan, "
                "include the valid V2 plan_document upsert artifact. If the user asked to save an existing plan, do not call ask_user. "
                "If no complete plan body exists yet, a new plan may use ask_user before drafting; after ask_user answers, an explicit plan-document request must use V2. "
                "Do not put prose, Markdown, or a code fence before it."
            ),
        }]
        response, _ = await complete_once(repair_messages)
        return response


def _has_prior_assistant_markdown_plan(history: list[dict[str, Any]]) -> bool:
    for item in history:
        if item.get("role") != "assistant":
            continue
        content = item.get("content")
        if not isinstance(content, str) or "\n" not in content:
            continue
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            continue
        if lines[0].startswith("#") or sum(line.startswith(("- ", "* ")) for line in lines) >= 2:
            return True
        if any(line.startswith("|") for line in lines[:5]):
            return True
    return False


def _response_has_plan_artifact(response: Any) -> bool:
    if isinstance(response, AskRequest):
        return False
    message = getattr(response, "message", None)
    if not isinstance(message, str):
        return False
    decoder = ControlHeadDecoder()
    try:
        decoder.feed(message)
        return decoder.finish().artifact is not None
    except RouteProtocolError:
        return False


def _wrap_confirmed_plan_markdown(response: Any) -> Any:
    from .plan_documents import validate_title

    message=getattr(response,"message",None)
    if not isinstance(message,str):raise GatewayError("model did not return required plan document artifact","structure")
    lines=message.splitlines();declaration=lines[0].lower() if lines else ""
    if not re.search(r"content[_ -]?shape\s*[=:]\s*plan[_ -]?document|artifact.{0,40}plan[_ -]?document",declaration):
        raise GatewayError("model did not return required plan document artifact","structure")
    start=next((index for index,line in enumerate(lines[1:],1) if re.match(r"^#\s+\S",line.strip())),None)
    if start is None:raise GatewayError("model did not return required plan document artifact","structure")
    body="\n".join(lines[start:]).strip()+"\n";title=validate_title(re.sub(r"^#\s+","",lines[start].strip()).strip())
    header=json.dumps({"v":2,"policy":"answer","content_shape":"plan_document","reason_code":"explicit_plan_save","artifact":{"kind":"plan_document","operation":"upsert","title":title}},ensure_ascii=False,separators=(",",":"))
    values=dict(vars(response)) if hasattr(response,"__dict__") else {}
    values.update(message=header+"\n"+body,tool_calls=getattr(response,"tool_calls",[]) or [])
    return SimpleNamespace(**values)


def _downgrade_unconfirmed_plan_document(response: Any) -> Any:
    message=getattr(response,"message",None)
    if not isinstance(message,str):return response
    _,separator,body=message.partition("\n")
    if not separator:return response
    header=json.dumps({"v":1,"policy":"answer","content_shape":"guide","reason_code":"content_only"},ensure_ascii=False,separators=(",",":"))
    values=dict(vars(response)) if hasattr(response,"__dict__") else {}
    values.update(message=header+"\n"+body,tool_calls=getattr(response,"tool_calls",[]) or [])
    return SimpleNamespace(**values)


def _response_declares_plan_document_intent(response: Any) -> bool:
    if isinstance(response, AskRequest):
        return False
    message = getattr(response, "message", None)
    if not isinstance(message, str):
        return False
    decoder = ControlHeadDecoder()
    try:
        decoder.feed(message)
        return decoder.finish().content_shape == "plan_document"
    except RouteProtocolError:
        return False


def _response_is_plan_shaped(response: Any) -> bool:
    if isinstance(response, AskRequest):
        return False
    message = getattr(response, "message", None)
    if not isinstance(message, str):
        return False
    decoder = ControlHeadDecoder()
    try:
        decoder.feed(message)
        decision = decoder.finish()
        shape = decision.content_shape.lower().replace("-", "_")
        return "plan" in shape or "document" in shape
    except RouteProtocolError:
        return False


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
