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


def _supports_intent_classification(gateway: Any) -> bool:
    return isinstance(gateway, ModelGateway) or getattr(gateway, "supports_intent_classification", False) is True


class LiveRuntimeModel:
    """Small structured-output adapter around the single V1 ModelGateway."""

    def __init__(self, gateway: ModelGateway, tool_schemas: list[dict[str, Any]] | None = None) -> None:
        self.gateway = gateway
        self.tool_schemas = tool_schemas or []
        self.runtime_prompt_policy = None
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
            "只返回 JSON：{\"needs_clarification\": true|false}。仅当目标或交付物确实缺失时才要求澄清。"
            "对于宽泛的内容请求，可以基于明确假设先给出第一版，不要在提供内容前索取个人信息。"
            "例如，用户要求七天饮食计划时，可以声明假设并附安全提示后直接规划，不要先询问个人信息。",
            {"goal": goal, "interactions": interactions},
        )
        return bool(payload.get("needs_clarification", False))

    async def plan(self, goal: dict[str, Any], interactions: list[str]) -> PlanDraft:
        payload = await self._json(
            "只返回包含 summary 和 steps 的 JSON。steps 必须是非空数组，每项包含 id、title、description。"
            "计划必须准确贴合用户目标和交付物，并使用用户的语言。"
            "对于内容或规划请求，应在 summary、步骤标题或描述中写出具体交付内容；"
            "除非用户明确要求工作流，否则不要返回‘澄清目标、逐项执行、总结进度’之类的通用清单。",
            {"goal": goal, "interactions": interactions},
        )
        steps = payload.get("steps")
        if not isinstance(steps, list) or not steps:
            raise GatewayError("模型返回的计划结构无效", "structure")
        normalized = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or not str(step.get("title", "")).strip():
                raise GatewayError("模型返回的计划步骤无效", "structure")
            normalized.append({
                "id": str(step.get("id") or f"step-{index + 1}"),
                "title": str(step["title"]).strip(),
                "description": str(step.get("description", "")),
            })
        return PlanDraft(normalized, str(payload.get("summary", "")))

    async def decide(self, step: dict[str, Any], observation: str, iteration: int) -> ModelDecision:
        payload = await self._json(
            "只返回 JSON。action 只能是 complete_step、continue、await_outcome、tool_call、blocked 之一。"
            "将用户可见结果放在 output 或 summary 中；已有具体结果时不要使用空泛标签。"
            "对于内容交付，只要步骤和观察信息充分，就立即返回可见答案。"
            "不要为了补充假设、获取当前时间或重复 observation 中已有结果而调用工具。"
            "仅当当前步骤明确需要时才调用工具，并且最多调用一个。"
            "tool_call 操作必须包含 tool_call: {id,name,params}。不要返回隐藏推理。",
            {"step": step, "observation": observation, "iteration": iteration},
            allow_tool_calls=True,
        )
        action = str(payload.get("action", "blocked"))
        if action == "complete_step":
            return ModelDecision.complete(_visible_result(payload, "当前步骤已完成"))
        if action == "continue":
            return ModelDecision.continue_(_visible_result(payload, "继续执行当前步骤", "observation"))
        if action == "await_outcome":
            return ModelDecision.await_outcome(_visible_result(payload, "正在等待外部结果", "observation"))
        if action == "blocked":
            return ModelDecision.blocked(_visible_result(payload, "当前任务需要暂停处理"))
        if action == "tool_call":
            tool = payload.get("tool_call")
            if not isinstance(tool, dict) or not isinstance(tool.get("params"), dict) or not tool.get("name"):
                raise GatewayError("模型返回的工具调用结构无效", "structure")
            from .tools import ToolCall

            return ModelDecision.tool(
                ToolCall(str(tool.get("id") or f"model-call-{iteration}"), str(tool["name"]), tool["params"]),
                _visible_result(payload, "模型请求调用工具"),
            )
        raise GatewayError("模型返回了未知的结构化操作", "structure")

    async def reflect(self, goal: dict[str, Any], plan: Any, run_id: str) -> list[dict[str, Any]]:
        payload = await self._json(
            "只返回包含 candidates 的 JSON。candidates 是可选的偏好或习惯对象数组，每项包含 kind、content、scope、confidence、evidence_event_ids。没有证据时不要虚构候选记忆。",
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
        policy = self.runtime_prompt_policy() if self.runtime_prompt_policy is not None else None
        messages = [
            {"role": "system", "content": _with_runtime_policy(instruction, policy)},
            {"role": "user", "content": json.dumps(input_data, ensure_ascii=False)},
        ]
        request_data = dict(input_data)
        context_hash = self._context_hash.get()
        if context_hash:
            request_data["context_snapshot"] = {"hash": context_hash, "text": self._context_text.get()}
            messages[1] = {"role": "user", "content": json.dumps(request_data, ensure_ascii=False)}
        response = await self.gateway.complete(
            ModelRequest(messages=messages, tools=self.tool_schemas, role=None, purpose=None),
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
                    {"role": "user", "content": "上一条响应不是有效 JSON。只返回所要求的 JSON 对象。"},
                ], tools=self.tool_schemas, role=None, purpose=None),
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
        self.runtime_prompt_policy = None

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
                        "只返回 JSON：{\"save_existing_plan\": true|false}。"
                        "判断用户最新消息是否明确要求把助手已经展示的完整 Markdown 计划写入、保存为或生成计划文档。"
                        "‘写进计划页面’、‘保存到计划’或‘生成文档’仅在此前确有完整助手计划时为 true。"
                        "仅询问计划、要求补充细节或个性化但没有明确保存要求时为 false。"
                        "不要仅凭一般规划关键词推断为 true。"
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
            role="conversation",
            purpose="classify_existing_plan_save",
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
                        "只返回 JSON：{\"plan_document_request\": true|false}。"
                        "判断用户最新消息是否明确要求生成、编写、保存完整计划文档或写入计划页面。"
                        "明确要求不经提问直接生成文档时也为 true。一般攻略、推荐、询问计划，"
                        "或没有明确文档/保存指令的计划请求为 false。仅要求七天饮食、旅行或训练计划属于回答请求，"
                        "不是文档保存请求。不要仅凭规划关键词推断为 true。"
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
            role="conversation",
            purpose="classify_plan_document_request",
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
            {"role":"system","content":"只返回 JSON：{\"start_research\":true|false,\"topic\":\"...\"}。仅当用户明确要求启动新的深度研究、调查、带来源对比或有来源报告时为 true。引用已有研究（如‘根据之前的深度研究制定计划’）为 false。普通问题、攻略、计划、推荐以及可直接回答的请求均为 false。简洁保留用户要求的研究主题。"},
            {"role":"user","content":content},
        ],tools=[],temperature=0,max_tokens=200,role="conversation",purpose="classify_research_request")
        try:
            response=await self.gateway.complete(request,cancel_event=cancel_event)
            payload=_parse_json(response.message)
            return payload.get("start_research") is True, str(payload.get("topic") or content).strip()[:2000]
        except GatewayError as exc:
            if exc.kind=="cancelled":raise
            return False,content
        except (TypeError,ValueError,json.JSONDecodeError):return False,content

    async def _classify_explicit_remember(self,content:str,cancel_event):
        if not _supports_intent_classification(self.gateway):return None
        explicit_marker=bool(re.search(r"(?:请|帮我|以后)?\s*(?:记住|记得)|\bremember\b",content,re.I))
        if not explicit_marker:return None
        request=ModelRequest(messages=[{"role":"system","content":"只返回 JSON：{\"remember\":true|false,\"kind\":\"preference|constraint|fact|decision|lesson\",\"scope_type\":\"user|project\",\"scope_id\":\"\",\"content\":\"...\"}。仅当用户明确要求为未来对话记住稳定信息时为 true，例如‘记住我不吃辣’、‘请记住我喜欢简洁明确的回答’、‘以后记得我九点后出发’。没有明确长期记忆指令的普通陈述为 false。content 只保留稳定事实，绝不包含凭据或密钥。"},{"role":"user","content":content}],tools=[],temperature=0,max_tokens=220,role="conversation",purpose="classify_memory_request")
        try:
            payload=_parse_json((await self.gateway.complete(request,cancel_event=cancel_event)).message)
            if payload.get("remember") is not True and explicit_marker:
                repair=ModelRequest(messages=[*request.messages,{"role":"system","content":"应用已经确认这是明确的长期记忆指令。按相同 JSON 结构返回 remember=true 并提取稳定信息，不要再提问。"}],tools=[],temperature=0,max_tokens=220,role="conversation",purpose="repair_memory_request")
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
        owner_id: str = "local-user",
    ) -> Any:
        latest_plan = _latest_assistant_plan(history)
        if latest_plan is not None and _is_plain_existing_plan_save(content):
            response = _build_existing_plan_response(latest_plan)
            if on_text_delta is not None:
                on_text_delta(response.message)
            return response

        human_mode = bool(self.settings and self.settings.get().human_mode)
        if self.memory_store is not None:
            remember=await self._classify_explicit_remember(content,cancel_event)
            if remember:
                try:
                    item=self.memory_store.remember(owner_id,remember["kind"],remember["scope_type"],remember.get("scope_id", ""),remember["content"],f"conversation:{hashlib.sha256((owner_id+":"+content).encode()).hexdigest()}")
                except (ValueError, KeyError, TypeError, AttributeError):
                    item = None
                if item is None:
                    remember = None
            if remember and item is not None:
                header=json.dumps({"v":1,"policy":"answer","content_shape":"text","reason_code":"memory_saved"},ensure_ascii=False)+"\n"
                body=f"已记住：{item.content}\n\n你可以随时在记忆页面编辑、停用或删除。"
                if on_text_delta is not None:on_text_delta(header+body)
                return type("RememberResponse",(),{"message":header+body,"tool_calls":[],"finish_reason":"stop"})()
        explicit_research=_is_explicit_research_command(content)
        start_research,research_topic=(True,content.strip()[:2000]) if explicit_research else ((await self._classify_explicit_research_request(content,cancel_event)) if _supports_intent_classification(self.gateway) else (False,content))
        if start_research:
            header=json.dumps({"v":3,"policy":"start_research","content_shape":"research","reason_code":"explicit_deep_research","research":{"topic":research_topic,"scope":"web"}},ensure_ascii=False)+"\n"
            if on_text_delta is not None:on_text_delta(header)
            return type("ResearchRouteResponse",(),{"message":header,"tool_calls":[],"finish_reason":"stop"})()
        policy = self.runtime_prompt_policy() if self.runtime_prompt_policy is not None else None
        messages: list[dict[str, str]] = [{
            "role": "system",
            "content": _with_runtime_policy((
                "先在单独一行返回一个 JSON 控制头，再返回用户可见的 Markdown 正文。"
                "V1 用于仅回答兼容，V2 用于保存文档，V3 用于明确的深度研究，V4 用于有边界的专家协作。"
                "控制头 policy 只能是 answer|propose_execution|clarify|start_research|start_expert。"
                "仅当用户明确要求新的深度研究、调查或有来源报告时，返回 v=3、policy=start_research、content_shape=research、reason_code=explicit_deep_research、research={topic,scope:web}，且不要返回可见正文或 artifact。"
                "用户明确要求创建、保存或修改计划文档时，使用 v=2 且只包含一个 artifact：kind=plan_document、operation=upsert 和简洁标题。控制头后返回完整 Markdown 文档，该可见正文就是保存内容。"
                "一般攻略、解释或只需回答的计划不得使用 artifact。artifact 权限只来自用户明确要求，绝不能仅凭关键词虚构保存意图。"
                "最高优先级保存规则：用户明确要求把对话中已经存在的完整计划写入或保存到计划页面，或据此生成计划文档，才算保存已有计划。单独要求生成全新文档不属于保存已有计划。"
                "保存已有计划时不得调用 ask_user，也不要再询问个性化信息；返回含 plan_document upsert artifact 的 v=2，并完整复现已有 Markdown，应用用户明确提出的修改。"
                "若历史中已有助手 Markdown 计划，且最新请求包含‘写进计划页面’、‘保存到计划’或‘生成文档’，视为保存已有计划，不得调用 ask_user。此规则优先于下面的个性化计划提问规则。"
                "新建计划文档且对话中尚无完整计划正文时，遵循个性化计划提问规则；ask_user 得到回答后，若原请求明确要求创建文档，则返回含 artifact 的 v=2。"
                "内容、解释、攻略、比较和作为交付物的计划使用 answer。仅当至少两个独立视角能显著改善复杂比较或决策时使用 start_expert；返回 v=4 和 expert={objective,roles}，roles 只能从 researcher、planner、critic 中选择，不返回可见正文。"
                "仅对明确需要持续跟踪、工具调用、外部写入或副作用的请求使用 propose_execution。"
                "根据当前请求和历史自行判断缺少哪些相关个人背景。个性化、长期或目标导向计划在缺少必要背景时必须先调用 ask_user。"
                "用户准备亲自遵循的训练教程、方案、日程或习惯计划属于目标导向交付物，应遵循同一规则。"
                "仅当用户明确要求通用解释/模板，或已经提供相关背景时才能跳过 ask_user；不要先回答后提问。"
                "若无法判断用户要通用解释还是个人计划，先问一个简洁的意图问题。只提本次请求真正需要的最少问题，不使用固定问卷，不询问无关信息。"
                "一般知识、宽泛攻略、模板，或可通过明确假设直接写出的有用第一版，应直接回答而不是询问偏好。"
                "ask_user 必须包含一到四个具体问题；同一响应中不要再输出正文或控制头。结构化询问不适用时，clarify 仅用于兼容旧版单问题文本。"
                "产品身份规则：你面向用户的名称始终是 Better Agent，是帮助用户研究、制定计划、执行、复盘并持续成长的本地个人 Agent。"
                "底层模型和模型供应商只是运行组件；不得自称 Claude、ChatGPT、DeepSeek、Anthropic、OpenAI 或任何其他底层模型、供应商，也不得虚构产品创建者。"
                "用户询问‘你是谁’时，应以 Better Agent 的身份简要说明与当前问题相关的能力。只有用户明确询问技术运行配置、当前模型或供应商时，才可以如实说明底层技术信息，并明确区分产品身份与底层模型。"
                "Markdown 正文中绝不暴露控制头、隐藏推理、工具结构或原始 JSON。使用用户的语言，并在控制头后立即开始有效答案。"
            ), policy),
        }]
        if human_mode:
            messages.append({"role": "system", "content": (
                "拟人对话模式只作用于完整 JSON 控制头之后的用户可见文本。"
                "不得作用于控制 JSON、ask_user、plan_document artifact、研究报告、运行时 JSON、工具或引用。"
                "使用自然口语中文，通常一到两段，最多三段；仅在真实停顿处将 [[next]] 单独放一行。"
                "普通可见正文避免 Markdown 标题、表格和报告式编号列表。"
            )})
        save_existing_plan = await self._classify_existing_plan_save(content, history, cancel_event)
        if save_existing_plan:
            messages.append({
                "role": "system",
                "content": (
                    "意图门禁已确认这是保存已有计划的请求。不要调用 ask_user。"
                    "响应必须包含有效的 v=2 plan_document upsert artifact 和完整 Markdown 计划正文。"
                ),
            })
        if save_existing_plan and latest_plan is not None:
            messages.append({
                "role": "assistant",
                "content": latest_plan,
            })
        else:
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

            bounded_messages = request_messages
            input_limit = getattr(self.gateway, "input_limit", None)
            if callable(input_limit):
                from .token_budget import pack_messages_newest
                try:
                    limit = input_limit(owner_id=owner_id)
                except TypeError:
                    # Keep test doubles and older gateway adapters compatible.
                    limit = input_limit()
                bounded_messages = pack_messages_newest(
                    request_messages,
                    budget=limit,
                    tools=[ASK_TOOL_SCHEMA] if tools is None else tools,
                )
            response = await self.gateway.complete(
                ModelRequest(
                    messages=bounded_messages,
                    tools=[ASK_TOOL_SCHEMA] if tools is None else tools,
                    temperature=0,
                    role="conversation",
                    purpose="route_and_respond",
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
                    request = parse_ask_tool_call(tool_calls[0])
                    if getattr(self.gateway, "supports_role_routing", False):
                        ask_messages = [*bounded_messages, {
                            "role": "system",
                            "content": "已确认当前请求需要用户补充关键信息。调用 ask_user，生成一到四个最少且相关的问题；不要输出正文。",
                        }]
                        ask_input_limit = getattr(self.gateway, "input_limit", None)
                        if callable(ask_input_limit):
                            try:
                                ask_limit = ask_input_limit(owner_id=owner_id)
                            except TypeError:
                                ask_limit = ask_input_limit()
                            from .token_budget import pack_messages_newest
                            ask_messages = pack_messages_newest(ask_messages, budget=ask_limit, tools=[ASK_TOOL_SCHEMA])
                        ask_response = await self.gateway.complete(
                            ModelRequest(
                                messages=ask_messages,
                                tools=[ASK_TOOL_SCHEMA], temperature=0, max_tokens=800,
                                role="ask", purpose="generate_clarification",
                            ),
                            cancel_event=cancel_event,
                        )
                        ask_calls = getattr(ask_response, "tool_calls", []) or []
                        if len(ask_calls) != 1:
                            raise GatewayError("ask role must return one ask_user call", "structure")
                        request = parse_ask_tool_call(ask_calls[0])
                    return request, True
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
                    "对话响应声明了计划文档请求。立即重试且不要调用工具：第一行必须是有效的 v=2 JSON 控制头，"
                    "其中只包含一个 plan_document upsert artifact，随后返回完整 Markdown 正文。"
                )
        if isinstance(response, AskRequest):
            if not await self._classify_explicit_plan_document_request(content, history, cancel_event):
                return response
            return await force_plan_document(
                "意图门禁已确认这是明确的计划文档请求。不要调用 ask_user 或索取更多背景。"
                "返回有效的 v=2 JSON 控制头，其中只包含一个 plan_document upsert artifact，"
                "随后基于合理且明确的假设返回完整 Markdown 计划。"
            )
        if _response_is_plan_shaped(response) and not _response_has_plan_artifact(response):
            if plan_document_intent is None:
                plan_document_intent = await self._classify_explicit_plan_document_request(content, history, cancel_event)
            if plan_document_intent:
                return await force_plan_document(
                    "意图门禁已确认这是明确的计划文档请求。不要调用工具并重试：返回有效的 v=2 JSON 控制头，"
                    "其中只包含一个 plan_document upsert artifact，随后返回完整 Markdown 计划正文。"
                )
        if valid:
            return response

        if plan_document_intent is None:
            plan_document_intent = await self._classify_explicit_plan_document_request(content, history, cancel_event)
        if plan_document_intent:
            return await force_plan_document(
                '意图门禁已确认需要保存计划文档。第一行必须使用以下准确 JSON 结构：'
                '{"v":2,"policy":"answer","content_shape":"plan_document","reason_code":"explicit_plan_save",'
                '"artifact":{"kind":"plan_document","operation":"upsert","title":"PLAN TITLE"}}. '
                '只替换 PLAN TITLE，并从下一行开始输出以 # 开头的完整 Markdown 计划。'
            )

        if on_text_reset is not None:
            on_text_reset()
        repair_messages = messages + [{
            "role": "user",
            "content": (
                "上一条响应违反了对话控制头协议。立即重新处理原始用户请求。第一行必须且只能是一个 JSON 对象，"
                "包含 v=1 或 v=2、policy、content_shape、reason_code；若用户明确要求保存计划，需包含有效的 V2 plan_document upsert artifact。"
                "若用户要求保存已有计划，不得调用 ask_user。若尚无完整计划正文，新计划可在起草前调用 ask_user；得到回答后，明确的计划文档请求必须使用 V2。"
                "控制头之前不要输出正文、Markdown 或代码围栏。"
            ),
        }]
        response, _ = await complete_once(repair_messages)
        return response


def _has_prior_assistant_markdown_plan(history: list[dict[str, Any]]) -> bool:
    return _latest_assistant_plan(history) is not None


def _latest_assistant_plan(history: list[dict[str, Any]]) -> str | None:
    for item in reversed(history):
        if item.get("role") != "assistant":
            continue
        content = item.get("content")
        if not isinstance(content, str) or "\n" not in content:
            continue
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            continue
        if lines[0].startswith("#") or sum(line.startswith(("- ", "* ")) for line in lines) >= 2:
            return content
        if any(line.startswith("|") for line in lines[:5]):
            return content
    return None


def _is_plain_existing_plan_save(content: str) -> bool:
    normalized = re.sub(r"[。！!？?，,；;：:\s]+", "", content.strip().lower())
    normalized = re.sub(r"^(?:请|麻烦|帮我|请帮我)", "", normalized)
    normalized = re.sub(r"(?:一下|吧)$", "", normalized)
    return normalized in {
        "保存成计划",
        "保存为计划",
        "保存到计划",
        "保存到计划页面",
        "写进计划",
        "写进计划页面",
        "存入计划",
        "存入计划页面",
    }


def _plan_title_from_markdown(markdown: str) -> str:
    from .plan_documents import validate_title

    generic_titles = {"专家协作结果", "专家协同结果", "计划", "完整计划", "综合结论", "协作目标", "执行建议"}
    headings = [
        re.sub(r"^#{1,6}\s+", "", line.strip()).strip()
        for line in markdown.splitlines()
        if re.match(r"^#{1,6}\s+\S", line.strip())
    ]
    title = headings[0] if headings and headings[0] not in generic_titles else None
    candidates = [
        line.strip().strip("。.!！")
        for line in markdown.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-", "*", "|", ">"))
    ]
    title = title or next((line for line in candidates if "计划" in line and len(line) <= 120), None)
    title = title or next((heading for heading in headings if "计划" in heading and heading not in generic_titles), None)
    title = title or (headings[0] if headings else None)
    return validate_title((title or "对话计划")[:120])


def _build_existing_plan_response(markdown: str) -> Any:
    header = json.dumps(
        {
            "v": 2,
            "policy": "answer",
            "content_shape": "plan_document",
            "reason_code": "explicit_plan_save",
            "artifact": {
                "kind": "plan_document",
                "operation": "upsert",
                "title": _plan_title_from_markdown(markdown),
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return SimpleNamespace(message=f"{header}\n{markdown}", tool_calls=[], finish_reason="stop")


def _with_runtime_policy(instruction: str, policy: Any) -> str:
    if policy is None or policy == "" or policy == "live-model-v1":
        return instruction
    return "应用以下已经批准的 Better Agent 运行时提示词策略：\n" + json.dumps(policy, ensure_ascii=False) + "\n\n" + instruction


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
