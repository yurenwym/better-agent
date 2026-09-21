from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from .ask import AskValidationError
from .agents import AgentTaskConflict
from .events import export_jsonl
from .evolution import EvolutionConflict, EvolutionGateError
from .goal_program_compiler import GoalCompilationError
from .goal_programs import GoalProgramConflict, GoalProgramNotFound
from .plan_documents import PlanDocumentConflict, PlanDocumentValidationError
from .public_text import public_budget


DEFAULT_PLAN_HISTORY_LIMIT = 50
MAX_PLAN_HISTORY_LIMIT = 100


async def _event_stream(service, run_id: str, request: Request, after_seq: int, follow: bool):
    cursor = after_seq
    terminal_states = {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}
    while True:
        events = service.events.list(run_id, cursor)
        for event in events:
            cursor = event.seq
            yield f"id: {event.seq}\nevent: trajectory\ndata: {json.dumps(_event_json(event), ensure_ascii=False)}\n\n"
        if not follow:
            return
        if await request.is_disconnected():
            return
        if not events and service.get_run(run_id).state.value in terminal_states:
            return
        if not events:
            yield ": keep-alive\n\n"
        await asyncio.sleep(0.05)


async def _thread_event_stream(service, thread_id: str, request: Request, after_seq: int, follow: bool):
    cursor = after_seq
    terminal_states = {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}
    while True:
        events = service.events.list(thread_id, cursor)
        for event in events:
            cursor = event.seq
            yield f"id: {event.seq}\nevent: conversation\ndata: {json.dumps(_thread_event_json(event), ensure_ascii=False)}\n\n"
        if not follow:
            return
        if await request.is_disconnected():
            return
        try:
            thread = service.thread(thread_id)
        except KeyError:
            # A user may delete the thread while its event stream is open.
            return
        with service.db.connection() as connection:
            research_active = connection.execute(
                "SELECT 1 FROM research_jobs WHERE thread_id=? AND status IN ('QUEUED','RUNNING') LIMIT 1",
                (thread_id,),
            ).fetchone() is not None
            expert_active = connection.execute(
                "SELECT 1 FROM agent_runs WHERE thread_id=? AND status IN ('QUEUED','RUNNING','WAITING') LIMIT 1", (thread_id,)
            ).fetchone() is not None
        if not events and thread.active_turn_id:
            if service.turn(thread.active_turn_id).status in terminal_states and not research_active and not expert_active:
                return
        if not events:
            yield ": keep-alive\n\n"
        await asyncio.sleep(0.05)


def register_routes(app) -> None:
    async def mutate(request: Request) -> None:
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("application/json"):
            raise HTTPException(status_code=415, detail="请求必须使用 JSON 内容类型")
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in {"127.0.0.1", "localhost"}:
                raise HTTPException(status_code=403, detail="仅允许从本机页面发起请求")
        if request.headers.get("x-csrf-token") != request.app.state.csrf_token:
            raise HTTPException(status_code=403, detail="缺少有效的安全校验令牌")

    @app.get("/api/learning/policy")
    async def learning_policy(request: Request):
        return runtime(request).learning.policy(owner_id(request))

    @app.put("/api/learning/policy", dependencies=[Depends(mutate)])
    async def configure_learning_policy(request: Request):
        try:
            return runtime(request).learning.configure(owner_id(request), **(await request.json()))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=409 if exc.__class__.__name__ == "LearningConflict" else 422, detail=str(exc)) from exc

    @app.get("/api/learning/history")
    async def learning_history(request: Request):
        return {"items": runtime(request).learning.history(owner_id(request))}

    @app.post("/api/learning/prompt-suites", dependencies=[Depends(mutate)])
    async def register_learning_suite(request: Request):
        try:
            payload = await request.json()
            identity = runtime(request).learning.prompt_learning.register_suite(owner_id(request), payload["cases"])
            return {"digest": identity}
        except (ValueError, KeyError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/learning/constraints", dependencies=[Depends(mutate)])
    async def bind_learning_constraint(request: Request):
        try:
            payload = await request.json()
            job_id = runtime(request).learning.bind_constraint_scope(owner_id(request), payload["message_id"], payload["applicability"])
            return {"job_id": job_id}
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/learning/prompt-suites/{suite_digest}", dependencies=[Depends(mutate)])
    async def forget_learning_suite(suite_digest: str, request: Request):
        try:
            runtime(request).learning.prompt_learning.forget_suite(owner_id(request), suite_digest)
            return {"status": "FORGOTTEN"}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/learning/jobs/{job_id}/suspend", dependencies=[Depends(mutate)])
    async def suspend_learning(job_id: str, request: Request):
        payload = await request.json()
        try:
            runtime(request).learning.suspend(job_id, owner_id(request), expected_version=payload["expected_version"], reason="user_requested")
            return {"status": "SUSPENDED"}
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def runtime(request: Request):
        value = getattr(request.app.state, "runtime", None)
        if value is None:
            raise HTTPException(status_code=503, detail="Agent 运行时尚未配置")
        return value

    def owner_id(request: Request) -> str:
        # The current local deployment has no external auth provider yet. Keep
        # the compatibility identity explicit and injectable for multi-user
        # deployments/tests instead of scattering the literal through routes.
        value = request.headers.get("x-owner-id", "local-user").strip()
        if not value or len(value) > 160:
            raise HTTPException(status_code=400, detail="invalid owner id")
        return value

    def conversation(request: Request):
        service = runtime(request)
        value = getattr(service, "conversation", None)
        if value is None:
            raise HTTPException(status_code=503, detail="对话运行时尚未配置")
        return value

    def goal_programs(request: Request):
        service = runtime(request)
        value = getattr(service, "goal_programs", None)
        if value is None:
            raise HTTPException(status_code=503, detail="目标执行服务尚未配置")
        return value

    def goal_adjustments(request: Request):
        service = runtime(request)
        value = getattr(service, "goal_adjustments", None)
        if value is None: raise HTTPException(status_code=503, detail="目标调整服务尚未配置")
        return value

    def goal_reviews(request: Request):
        service = runtime(request)
        value = getattr(service, "goal_reviews", None)
        if value is None:
            raise HTTPException(status_code=503, detail="目标复盘服务尚未配置")
        return value

    def agent_tasks(request: Request):
        service = runtime(request)
        value = getattr(service, "agent_tasks", None)
        if value is None: raise HTTPException(status_code=503, detail="专家协作运行时尚未配置")
        return value

    def idempotency_key(request: Request) -> str:
        key = request.headers.get("idempotency-key", "")
        if not key.strip():
            raise HTTPException(status_code=422, detail="缺少 Idempotency-Key 请求头")
        return key

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request) -> dict[str, Any]:
        config = request.app.state.config
        settings = getattr(request.app.state.runtime, "settings", None)
        return {
            "csrf_token": request.app.state.csrf_token,
            "version": config.version,
            "api_key_env": getattr(
                request.app.state.runtime,
                "model_api_key_env",
                getattr(config, "api_key_env", "AGENT_MODEL_API_KEY"),
            ),
            "api_key_configured": getattr(config, "api_key_configured", False),
            "human_mode": settings.get().human_mode if settings else False,
        }

    def model_admin(request: Request):
        value = getattr(runtime(request), "model_admin", None)
        if value is None:
            from .model_admin import ModelAdminService
            value = ModelAdminService(runtime(request).db)
            runtime(request).model_admin = value
        return value

    @app.get("/api/model-profiles")
    async def list_model_profiles(service=Depends(model_admin)):
        return {"profiles": service.list_profiles()}

    @app.get("/api/model-capacity")
    async def resolve_model_capacity(
        base_url: str, protocol: str = "openai_compatible", model: str = "",
    ):
        """Resolve one endpoint/model capacity so the console never guesses it."""
        from .model_capacity import CapacityContractError, resolve_working_window

        try:
            resolution = resolve_working_window(
                base_url=base_url, protocol=protocol, model_id=model,
            )
        except (ValueError, CapacityContractError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "capacity": resolution.public_view(),
            "evidence": resolution.capacity_record(),
            "entry": resolution.entry.public_view() if resolution.entry is not None else None,
        }

    @app.get("/api/model-readiness")
    async def get_model_readiness(request: Request):
        service = getattr(runtime(request), "model_readiness", None)
        if service is None:
            raise HTTPException(status_code=503, detail="模型就绪检查尚未配置")
        return service.check()

    @app.post("/api/model-profiles", status_code=201, dependencies=[Depends(mutate)])
    async def create_model_profile(payload: dict[str, Any], service=Depends(model_admin)):
        from .model_admin import ModelAdminError
        from .model_capacity import loads_capacity_record
        payload["working_window_mode"] = payload.get("working_window_mode") or (loads_capacity_record(payload.get("capacity_evidence")) or {}).get("mode") or "manual"
        try: return service.create_profile(payload)
        except ModelAdminError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/model-profiles/{profile_id}")
    async def get_model_profile(profile_id: str, service=Depends(model_admin)):
        try: return service.get_profile(profile_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="model profile not found") from exc

    @app.post("/api/model-profiles/{profile_id}/versions", status_code=201, dependencies=[Depends(mutate)])
    async def create_model_profile_version(profile_id: str, payload: dict[str, Any], service=Depends(model_admin)):
        from .model_admin import ModelAdminError
        from .model_capacity import loads_capacity_record
        payload["working_window_mode"] = payload.get("working_window_mode") or (loads_capacity_record(payload.get("capacity_evidence")) or {}).get("mode") or "manual"
        try: return service.add_version(profile_id, payload)
        except KeyError as exc: raise HTTPException(status_code=404, detail="model profile not found") from exc
        except ModelAdminError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/model-profile-versions/{version_id}/verify", dependencies=[Depends(mutate)])
    async def verify_model_profile_version(version_id: str, service=Depends(model_admin)):
        from .model_admin import ExternalCredentialMissing, ModelAdminError
        try: return await service.verify(version_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="model profile version not found") from exc
        except ExternalCredentialMissing as exc:
            raise HTTPException(status_code=422, detail={"code": "EXTERNAL_CREDENTIAL_MISSING", "credential_env_ref": str(exc)}) from exc
        except ModelAdminError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/model-profile-versions/{version_id}/disable", dependencies=[Depends(mutate)])
    async def disable_model_profile_version(version_id: str, service=Depends(model_admin)):
        try: return service.disable(version_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="model profile version not found") from exc

    @app.get("/api/model-routing-policies")
    async def list_model_routing_policies(service=Depends(model_admin)):
        return {"policies": service.list_policies()}

    @app.post("/api/model-routing-policies", status_code=201, dependencies=[Depends(mutate)])
    async def create_model_routing_policy(payload: dict[str, Any], service=Depends(model_admin)):
        from .model_admin import ModelAdminError
        try: return service.create_policy(_required_text(payload, "name"), payload.get("roles"))
        except (ModelAdminError, KeyError) as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/model-routing-policies/{policy_id}")
    async def get_model_routing_policy(policy_id: str, service=Depends(model_admin)):
        try: return service.policy(policy_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="routing policy not found") from exc

    @app.post("/api/model-routing-policies/{policy_id}/candidate", status_code=201, dependencies=[Depends(mutate)])
    async def create_routing_policy_candidate(policy_id: str, payload: dict[str, Any], request: Request, service=Depends(runtime)):
        try:
            policy = service.model_admin.policy(policy_id)
            base = service.behavior.active("stable")
            routing = {"policy_id": policy["id"], "digest": policy["policy_digest"]}
            bindings = {role: route["primary"] for role, route in policy["roles"].items()}
            target = service.behavior.ensure({**base.manifest, "model_routing": routing, "model_role_bindings": bindings})
            return _evolution_call(lambda: service.evolution.propose_candidate(
                candidate_type="policy", experience_ids=payload.get("experience_ids", []),
                base_bundle_id=base.id, target_bundle_id=target.id,
                proposed_content={"model_routing": routing, "model_role_bindings": bindings},
                permission_diff={"added": []}, reason=_required_text(payload, "reason"),
                idempotency_key=idempotency_key(request),
            ))
        except KeyError as exc: raise HTTPException(status_code=404, detail="routing policy not found") from exc

    @app.get("/api/workspaces/{resource_id}")
    async def get_goal_workspace(resource_id: str, request: Request):
        from .goal_workspace import GoalWorkspaceService
        try:
            return GoalWorkspaceService(runtime(request)).get(resource_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="workspace not found") from exc

    @app.get("/api/settings")
    async def get_settings(service=Depends(runtime)) -> dict[str, Any]:
        settings = getattr(service, "settings", None)
        return {"human_mode": settings.get().human_mode if settings else False}

    @app.get("/api/growth/profile")
    async def get_growth_profile(request: Request):
        from .growth import GrowthProfileService
        return GrowthProfileService(runtime(request)).profile()

    @app.get("/api/growth/programs/{program_id}")
    async def get_growth_program(program_id: str, request: Request):
        from .growth import GrowthProfileService
        try:
            return GrowthProfileService(runtime(request)).program(program_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="growth program not found") from exc

    @app.post("/api/plans/{plan_document_id}/program-preview", dependencies=[Depends(mutate)], response_model=None)
    async def preview_goal_program(
        plan_document_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_programs)
    ) -> dict[str, Any] | JSONResponse:
        try:
            return await service.preview(
                plan_document_id,
                start_date=_required_text(payload, "start_date"),
                timezone_name=_required_text(payload, "timezone"),
                daily_minutes=_required_int(payload, "daily_minutes"),
                requested_end_date=payload.get("requested_end_date"),
                constraints=payload.get("schedule_constraints"),
                idempotency_key=idempotency_key(request),
            )
        except GoalProgramNotFound as exc:
            raise HTTPException(status_code=404, detail="plan not found") from exc
        except GoalProgramConflict as exc:
            return _goal_conflict(exc)
        except GoalCompilationError as exc:
            raise HTTPException(status_code=503 if exc.temporary else 422, detail={"reason_code": exc.code}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/programs/{program_id}/compile-retry", dependencies=[Depends(mutate)], response_model=None)
    async def retry_goal_compile(program_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_programs)):
        try:
            return await service.retry_compile(program_id, expected_version=_required_int(payload, "expected_version"), idempotency_key=idempotency_key(request))
        except GoalProgramNotFound as exc: raise HTTPException(status_code=404, detail="program not found") from exc
        except GoalProgramConflict as exc: return _goal_conflict(exc)
        except GoalCompilationError as exc: raise HTTPException(status_code=503 if exc.temporary else 422, detail={"reason_code": exc.code}) from exc
        except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/programs/{program_id}/activate", dependencies=[Depends(mutate)], response_model=None)
    async def activate_goal_program(program_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_programs)):
        return _goal_call(lambda: service.activate(program_id, expected_version=_required_int(payload, "expected_version"), idempotency_key=idempotency_key(request)))

    @app.get("/api/programs")
    async def list_goal_programs(service=Depends(goal_programs)) -> dict[str, Any]:
        return {"programs": service.list()}

    @app.get("/api/programs/{program_id}")
    async def get_goal_program(program_id: str, service=Depends(goal_programs)) -> dict[str, Any]:
        try: return service.get(program_id)
        except GoalProgramNotFound as exc: raise HTTPException(status_code=404, detail="program not found") from exc

    @app.get("/api/today")
    async def get_today(date: str | None = None, service=Depends(goal_programs)) -> dict[str, Any]:
        try: return service.today(explicit_date=date)
        except ValueError as exc: raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD") from exc

    @app.get("/api/programs/{program_id}/reviews/{local_date}")
    async def get_goal_review(program_id: str, local_date: str, service=Depends(goal_reviews)):
        try:
            return service.for_program_date(program_id, local_date) or Response(status_code=204)
        except GoalProgramNotFound as exc:
            raise HTTPException(status_code=404, detail="program not found") from exc

    @app.post("/api/reviews/{review_id}/retry", dependencies=[Depends(mutate)], response_model=None)
    async def retry_goal_review(review_id: str, request: Request, service=Depends(goal_reviews)):
        try:return service.retry(review_id, idempotency_key=idempotency_key(request))
        except GoalProgramNotFound as exc:raise HTTPException(status_code=404,detail="review not found") from exc
        except ValueError as exc:raise HTTPException(status_code=409,detail=str(exc)) from exc

    @app.get("/api/actions/{action_id}")
    async def get_goal_action(action_id: str, service=Depends(goal_programs)):
        try: return service.get_action_context(action_id)
        except GoalProgramNotFound as exc: raise HTTPException(status_code=404, detail="action not found") from exc

    @app.post("/api/threads/{thread_id}/messages/{message_id}/save-plan", dependencies=[Depends(mutate)], response_model=None)
    async def save_message_plan(thread_id: str, message_id: str, payload: dict[str, Any], request: Request, service=Depends(runtime), current_owner: str = Depends(owner_id)):
        try:
            if payload.get("confirmed") is not True or set(payload) != {"title", "confirmed"}:
                raise ValueError("confirm this answer as a plan before saving")
            title = _required_text(payload, "title")
            from .plan_documents import validate_title
            title = validate_title(title)
            idempotency_key(request)
            with service.db.connection() as connection:
                row = connection.execute(
                    "SELECT m.*,t.status turn_status FROM thread_messages m JOIN threads h ON h.id=m.thread_id "
                    "JOIN turns t ON t.id=m.turn_id WHERE m.id=? AND m.thread_id=? AND h.owner_id=?",
                    (message_id, thread_id, current_owner),
                ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="message not found")
            if row["role"] != "assistant" or row["status"] != "ready" or row["turn_status"] != "COMPLETED" or not row["content"].strip():
                raise ValueError("only a completed assistant answer can be saved")
            # The source turn is the durable deduplication identity, including after a lost HTTP response.
            revision = service.plan_documents.save_model_revision(
                thread_id=thread_id, title=title, markdown_content=row["content"],
                source_turn_id=row["turn_id"], source_message_id=message_id, actor="user",
            )
            with service.db.transaction() as connection:
                connection.execute("UPDATE thread_messages SET plan_document_version_id=? WHERE id=? AND thread_id=?",
                                   (revision.id, message_id, thread_id))
            return {"plan_document_id": revision.plan_document_id, "plan_document_version_id": revision.id}
        except PlanDocumentConflict as exc:
            return JSONResponse(status_code=409, content={"detail": str(exc)})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/programs/{program_id}/days/{local_date}/close", dependencies=[Depends(mutate)], response_model=None)
    async def close_goal_day(program_id: str, local_date: str, request: Request, service=Depends(goal_programs), current_owner: str = Depends(owner_id)):
        return _goal_call(lambda: service.close_day(program_id, local_date, idempotency_key=idempotency_key(request), owner_id=current_owner))

    @app.post("/api/actions/{action_id}/reopen", dependencies=[Depends(mutate)], response_model=None)
    async def reopen_goal_action(action_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_programs), current_owner: str = Depends(owner_id)):
        return _goal_call(lambda: service.reopen_action(action_id, expected_version=_required_int(payload,"expected_version"), idempotency_key=idempotency_key(request), owner_id=current_owner))

    @app.post("/api/actions/{action_id}/complete", dependencies=[Depends(mutate)], response_model=None)
    async def complete_goal_action(action_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_programs)):
        return _goal_call(lambda: service.complete_action(action_id, expected_version=_required_int(payload,"expected_version"), idempotency_key=idempotency_key(request),feedback={key:value for key,value in payload.items() if key!="expected_version"}))

    @app.post("/api/actions/{action_id}/skip", dependencies=[Depends(mutate)], response_model=None)
    async def skip_goal_action(action_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_programs)):
        return _goal_call(lambda: service.skip_action(action_id, expected_version=_required_int(payload,"expected_version"), idempotency_key=idempotency_key(request)))

    @app.post("/api/actions/{action_id}/defer", dependencies=[Depends(mutate)], response_model=None)
    async def defer_goal_action(action_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_programs)):
        return _goal_call(lambda: service.defer_action(action_id, expected_version=_required_int(payload,"expected_version"), scheduled_date=_required_text(payload,"scheduled_date"), idempotency_key=idempotency_key(request)))

    @app.post("/api/actions/{action_id}/feedback", dependencies=[Depends(mutate)], response_model=None)
    async def add_goal_feedback(action_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_programs)):
        expected_version = _required_int(payload, "expected_version")
        return _goal_call(lambda: service.feedback(action_id, {key:value for key,value in payload.items() if key != "expected_version"}, expected_version=expected_version, idempotency_key=idempotency_key(request)))

    @app.post("/api/actions/{action_id}/request-help", status_code=202, dependencies=[Depends(mutate)], response_model=None)
    async def request_goal_help(action_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_programs)):
        try:
            if "expert" in payload and not isinstance(payload["expert"], bool):
                raise ValueError("expert must be boolean")
            return service.request_help(
                action_id, content=_required_text(payload, "content"),
                expected_version=_required_int(payload, "expected_version"),
                idempotency_key=idempotency_key(request), client_turn_id=payload.get("client_turn_id"), expert=payload.get("expert", False),
            )
        except GoalProgramNotFound as exc: raise HTTPException(status_code=404, detail="action not found") from exc
        except GoalProgramConflict as exc: return _goal_conflict(exc)
        except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    for operation in ("pause", "resume", "cancel"):
        async def lifecycle(program_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_programs), operation=operation):
            return _goal_call(lambda: service.transition(program_id, operation, expected_version=_required_int(payload,"expected_version"), idempotency_key=idempotency_key(request)))
        app.add_api_route(f"/api/programs/{{program_id}}/{operation}", lifecycle, methods=["POST"], dependencies=[Depends(mutate)], response_model=None)

    @app.post("/api/programs/{program_id}/complete", dependencies=[Depends(mutate)], response_model=None)
    async def complete_goal_program(program_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_programs)):
        response = _goal_call(lambda: service.transition(
            program_id, "complete",
            expected_version=_required_int(payload, "expected_version"),
            idempotency_key=idempotency_key(request),
        ))
        if isinstance(response, JSONResponse):
            return response
        # 状态切换与模板总结已经落库；周期复盘是加成，模型不可用时保留模板总结。
        try:
            summary = await service.period_summary(program_id)
            if summary:
                return service.get(program_id)
        except Exception:
            pass
        return response

    @app.get("/api/programs/{program_id}/period-review")
    async def period_review_status(program_id: str, service=Depends(goal_programs), current_owner: str = Depends(owner_id)):
        try:
            return service.period_summary_status(program_id, current_owner)
        except GoalProgramNotFound as exc:
            raise HTTPException(status_code=404, detail="program not found") from exc

    @app.post("/api/programs/{program_id}/period-review/retry", dependencies=[Depends(mutate)], response_model=None)
    async def retry_period_review(program_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_programs), current_owner: str = Depends(owner_id)):
        try:
            await service.period_summary(program_id, current_owner, retry_key=idempotency_key(request))
            return service.period_summary_status(program_id, current_owner)
        except GoalProgramNotFound as exc:
            raise HTTPException(status_code=404, detail="program not found") from exc
        except GoalProgramConflict as exc:
            return _goal_conflict(exc)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/programs/{program_id}", dependencies=[Depends(mutate)], response_model=None)
    async def tombstone_goal_program(program_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_programs)):
        return _goal_call(lambda: service.tombstone(program_id, expected_version=_required_int(payload,"expected_version"), idempotency_key=idempotency_key(request)))

    @app.post("/api/programs/{program_id}/adjustments", dependencies=[Depends(mutate)], response_model=None)
    async def propose_goal_adjustment(program_id: str, payload: dict[str, Any], request: Request, service=Depends(goal_adjustments)):
        try:return await service.propose(program_id,reason=_required_text(payload,"reason"),expected_version=_required_int(payload,"expected_version"),idempotency_key=idempotency_key(request))
        except GoalProgramNotFound as exc:raise HTTPException(status_code=404,detail="program not found") from exc
        except GoalProgramConflict as exc:return _goal_conflict(exc)
        except GoalCompilationError as exc:raise HTTPException(status_code=503 if exc.temporary else 422,detail={"reason_code":exc.code}) from exc
        except ValueError as exc:raise HTTPException(status_code=422,detail=str(exc)) from exc

    @app.get("/api/adjustments/{proposal_id}")
    async def get_goal_adjustment(proposal_id: str, service=Depends(goal_adjustments)):
        try:return service.get(proposal_id)
        except GoalProgramNotFound as exc:raise HTTPException(status_code=404,detail="adjustment not found") from exc

    for decision in ("accept","reject"):
        async def decide_adjustment(proposal_id: str,payload:dict[str,Any],request:Request,service=Depends(goal_adjustments),decision=decision):
            return _goal_call(lambda:getattr(service,decision)(proposal_id,expected_version=_required_int(payload,"expected_version"),idempotency_key=idempotency_key(request)))
        app.add_api_route(f"/api/adjustments/{{proposal_id}}/{decision}",decide_adjustment,methods=["POST"],dependencies=[Depends(mutate)],response_model=None)

    @app.post("/api/adjustments/{proposal_id}/sync-plan-document",dependencies=[Depends(mutate)],response_model=None)
    async def sync_goal_adjustment(proposal_id:str,payload:dict[str,Any],request:Request,service=Depends(goal_adjustments)):
        return _goal_call(lambda:service.sync_plan_document(proposal_id,expected_version=_required_int(payload,"expected_version"),rebase_to_current=payload.get("rebase_to_current",False) is True,idempotency_key=idempotency_key(request)))

    @app.put("/api/settings/human-mode", dependencies=[Depends(mutate)])
    async def set_human_mode(payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        settings = getattr(service, "settings", None)
        if settings is None: raise HTTPException(status_code=503, detail="settings are not configured")
        try:
            result = settings.set_human_mode(payload.get("enabled"))
            return {"human_mode": result.human_mode, "updated_at": result.updated_at}
        except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/threads", status_code=201, dependencies=[Depends(mutate)])
    async def create_thread(payload: dict[str, Any], service=Depends(conversation)) -> dict[str, Any]:
        title = payload.get("title", "新的对话")
        if title is not None and not isinstance(title, str):
            raise HTTPException(status_code=422, detail="title must be a string")
        return _thread_json(service.create_thread(title or "新的对话"))

    @app.get("/api/threads")
    async def list_threads(service=Depends(conversation)) -> dict[str, Any]:
        return {"threads": [_thread_json(thread) for thread in service.threads()]}

    @app.get("/api/threads/{thread_id}")
    async def get_thread(thread_id: str, service=Depends(conversation)) -> dict[str, Any]:
        try:
            payload = _thread_json(service.thread(thread_id))
            payload["turns"] = [_turn_json(turn) for turn in service.turns(thread_id)]
            return payload
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="thread not found") from exc

    @app.delete("/api/threads/{thread_id}", status_code=204, dependencies=[Depends(mutate)])
    async def delete_thread(thread_id: str, service=Depends(conversation)) -> Response:
        try:
            service.delete_thread(thread_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="thread not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=204)

    @app.get("/api/threads/{thread_id}/messages")
    async def get_thread_messages(thread_id: str, service=Depends(conversation)) -> dict[str, Any]:
        try:
            service.thread(thread_id)
            return {"messages": [_thread_message_json(message) for message in service.messages(thread_id)]}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="thread not found") from exc

    @app.get("/api/threads/{thread_id}/events")
    async def get_thread_events(thread_id: str, request: Request, after_seq: int = 0, service=Depends(conversation)) -> dict[str, Any]:
        try:
            service.thread(thread_id)
            return {"events": [_thread_event_json(event) for event in service.events.list(thread_id, after_seq)]}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="thread not found") from exc

    @app.get("/api/threads/{thread_id}/events/stream")
    async def thread_event_stream(thread_id: str, request: Request, follow: bool = True, service=Depends(conversation)) -> StreamingResponse:
        try:
            service.thread(thread_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="thread not found") from exc
        last_event_id = request.headers.get("last-event-id") or request.query_params.get("after_seq", "0")
        try:
            after_seq = int(last_event_id or 0)
        except ValueError:
            after_seq = 0
        return StreamingResponse(
            _thread_event_stream(service, thread_id, request, after_seq, follow),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/threads/{thread_id}/archive")
    async def archive_status(thread_id: str, service=Depends(runtime), current_owner: str = Depends(owner_id)):
        try:
            return service.archiver.status(thread_id, current_owner)
        except (KeyError, PermissionError) as exc:
            raise HTTPException(status_code=404, detail="thread not found") from exc

    @app.post("/api/threads/{thread_id}/archive/{job_id}/retry", dependencies=[Depends(mutate)])
    async def retry_archive(thread_id: str, job_id: str, payload: dict[str, Any], service=Depends(runtime), current_owner: str = Depends(owner_id)):
        from .memory_archive import ArchiveError
        try:
            return service.archiver.retry(thread_id, current_owner, job_id, _required_text(payload, "expected_updated_at"))
        except (KeyError, PermissionError) as exc:
            raise HTTPException(status_code=404, detail="archive job not found") from exc
        except ArchiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/threads/{thread_id}/plan")
    async def get_thread_plan(
        thread_id: str,
        limit: int = DEFAULT_PLAN_HISTORY_LIMIT,
        offset: int = 0,
        service=Depends(conversation),
    ) -> dict[str, Any]:
        limit, offset = _plan_history_pagination(limit, offset)
        try:
            service.thread(thread_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="thread not found") from exc
        try:
            document = service.plan_documents.get_by_thread(thread_id)
        except KeyError:
            return {"plan": None}
        return {"plan": _plan_document_json(document, service.plan_documents, limit=limit, offset=offset)}

    @app.post("/api/threads/{thread_id}/expert-runs", status_code=202, dependencies=[Depends(mutate)])
    async def create_expert_run(thread_id: str, payload: dict[str, Any], request: Request, root=Depends(runtime), service=Depends(agent_tasks), current_owner: str = Depends(owner_id)):
        try: root.conversation.thread(thread_id, current_owner)
        except KeyError as exc: raise HTTPException(status_code=404, detail="thread not found") from exc
        objective = payload.get("objective"); key = payload.get("idempotency_key")
        if not isinstance(objective, str) or not objective.strip() or not isinstance(key, str) or not key.strip():
            raise HTTPException(status_code=422, detail="objective and idempotency_key are required")
        bundle = root.behavior.active("stable")
        run = service.create_run(current_owner, objective, {"objective":objective,"thread_id":thread_id,"task_mode":"user_task"}, bundle.id, thread_id=thread_id, idempotency_key=key)
        return _agent_run_json(run)

    @app.get("/api/threads/{thread_id}/expert-runs/latest")
    async def latest_expert_run(thread_id: str, root=Depends(runtime), service=Depends(agent_tasks), current_owner: str = Depends(owner_id)):
        try:
            root.conversation.thread(thread_id, current_owner)
            return _agent_run_json(service.latest_run_for_thread(thread_id, current_owner))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="expert run not found") from exc

    @app.get("/api/agent-runs/{run_id}")
    async def get_agent_run(run_id: str, service=Depends(agent_tasks), current_owner: str = Depends(owner_id)):
        try: return _agent_run_json(service.get_run(run_id, current_owner))
        except KeyError as exc: raise HTTPException(status_code=404, detail="expert run not found") from exc

    @app.get("/api/agent-runs/{run_id}/tasks")
    async def get_agent_tasks(run_id: str, service=Depends(agent_tasks), current_owner: str = Depends(owner_id)):
        try: service.get_run(run_id, current_owner)
        except KeyError as exc: raise HTTPException(status_code=404, detail="expert run not found") from exc
        return {"tasks": [_agent_task_json(item) for item in service.tasks(run_id, current_owner)]}

    @app.get("/api/agent-runs/{run_id}/artifacts")
    async def get_agent_artifacts(run_id: str, service=Depends(agent_tasks), current_owner: str = Depends(owner_id)):
        try:
            service.get_run(run_id, current_owner)
            tasks = service.tasks(run_id, current_owner)
        except KeyError as exc: raise HTTPException(status_code=404, detail="expert run not found") from exc
        return {"artifacts":[service.artifact(item["result_artifact_id"]) for item in tasks if item.get("result_artifact_id")]}

    @app.post("/api/agent-runs/{run_id}/cancel", dependencies=[Depends(mutate)])
    async def cancel_agent_run(run_id: str, payload: dict[str, Any], service=Depends(agent_tasks), current_owner: str = Depends(owner_id)):
        try:
            service.get_run(run_id, current_owner)
            return _agent_run_json(service.cancel_run(run_id, str(payload.get("reason") or "user cancelled")))
        except KeyError as exc: raise HTTPException(status_code=404, detail="expert run not found") from exc
        except AgentTaskConflict as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/agent-runs/{run_id}/events")
    async def get_agent_events(run_id: str, after_seq: int = 0, service=Depends(agent_tasks), current_owner: str = Depends(owner_id)):
        try: service.get_run(run_id, current_owner)
        except KeyError as exc: raise HTTPException(status_code=404, detail="expert run not found") from exc
        return {"events": service.events(run_id, after_seq, current_owner)}

    @app.get("/api/agent-runs/{run_id}/events/stream")
    async def agent_event_stream(run_id: str, request: Request, follow: bool = True, service=Depends(agent_tasks), current_owner: str = Depends(owner_id)):
        try: service.get_run(run_id, current_owner)
        except KeyError as exc: raise HTTPException(status_code=404, detail="expert run not found") from exc
        raw = request.headers.get("last-event-id") or request.query_params.get("after_seq", "0")
        try: after_seq = int(raw or 0)
        except ValueError: after_seq = 0
        async def stream():
            cursor = after_seq
            while True:
                events = service.events(run_id, cursor, current_owner)
                for event in events:
                    cursor = event["seq"]
                    yield f"id: {cursor}\nevent: expert\ndata: {json.dumps(event,ensure_ascii=False)}\n\n"
                if not follow or (not events and service.get_run(run_id, current_owner)["status"] in {"SUCCEEDED","FAILED","CANCELLED"}): return
                if await request.is_disconnected(): return
                if not events: yield ": keep-alive\n\n"
                await asyncio.sleep(.05)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    @app.get("/api/plans")
    async def list_plan_documents(service=Depends(runtime)) -> dict[str, Any]:
        return {
            "plans": [
                _plan_document_summary_json(document, service.plan_documents)
                for document in service.plan_documents.list_documents()
            ]
        }

    @app.get("/api/plans/{plan_document_id}")
    async def get_plan_document(
        plan_document_id: str,
        limit: int = DEFAULT_PLAN_HISTORY_LIMIT,
        offset: int = 0,
        service=Depends(runtime),
    ) -> dict[str, Any]:
        limit, offset = _plan_history_pagination(limit, offset)
        try:
            document = service.plan_documents.get_document(plan_document_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="plan not found") from exc
        return _plan_document_json(document, service.plan_documents, limit=limit, offset=offset)

    @app.get("/api/plans/{plan_document_id}/versions")
    async def get_plan_versions(
        plan_document_id: str,
        limit: int = DEFAULT_PLAN_HISTORY_LIMIT,
        offset: int = 0,
        service=Depends(runtime),
    ) -> dict[str, Any]:
        limit, offset = _plan_history_pagination(limit, offset)
        try:
            service.plan_documents.get_document(plan_document_id)
            versions = service.plan_documents.list_versions(plan_document_id, limit=limit, offset=offset)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="plan not found") from exc
        total = service.plan_documents.count_versions(plan_document_id)
        return {
            "versions": [_plan_document_version_json(version, include_markdown=False) for version in versions],
            "versions_total": total,
            "versions_offset": offset,
            "versions_limit": limit,
            "versions_has_more": offset + len(versions) < total,
        }

    @app.get("/api/plans/{plan_document_id}/versions/{version}")
    async def get_plan_version(plan_document_id: str, version: int, service=Depends(runtime)) -> dict[str, Any]:
        try:
            service.plan_documents.get_document(plan_document_id)
            selected = next(
                item for item in service.plan_documents.list_versions(plan_document_id)
                if item.version == version
            )
        except (KeyError, StopIteration) as exc:
            raise HTTPException(status_code=404, detail="plan version not found") from exc
        return _plan_document_version_json(selected)

    @app.get("/api/plans/{plan_document_id}/file")
    async def get_plan_file(plan_document_id: str, service=Depends(runtime)) -> PlainTextResponse:
        try:
            document = service.plan_documents.get_document(plan_document_id)
            content = service.plan_documents.projector.read_text_stable(document.id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="plan not found") from exc
        except (OSError, UnicodeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="plan file unavailable") from exc
        return PlainTextResponse(content, media_type="text/markdown; charset=utf-8")

    @app.delete("/api/plans/{plan_document_id}", dependencies=[Depends(mutate)], status_code=204)
    async def delete_plan_document(
        plan_document_id: str,
        payload: dict[str, Any],
        service=Depends(runtime),
    ) -> Response:
        expected_hash = payload.get("expected_content_hash", payload.get("expected_file_hash"))
        if not isinstance(expected_hash, str) or not expected_hash:
            raise HTTPException(status_code=422, detail="expected_content_hash is required")
        try:
            service.plan_documents.delete_document(
                plan_document_id,
                expected_version=_required_int(payload, "expected_version"),
                expected_file_hash=expected_hash,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="plan not found") from exc
        except PlanDocumentConflict as exc:
            return JSONResponse(
                status_code=409,
                content={"detail": str(exc), "current": _current_plan_conflict(service, plan_document_id)},
            )
        except OSError:
            return _plan_projection_failure(service, plan_document_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return Response(status_code=204)

    @app.put("/api/plans/{plan_document_id}", dependencies=[Depends(mutate)], response_model=None)
    async def update_plan_document(
        plan_document_id: str,
        payload: dict[str, Any],
        service=Depends(runtime),
    ) -> dict[str, Any] | JSONResponse:
        try:
            document = service.plan_documents.get_document(plan_document_id)
            current = service.plan_documents.current_version(plan_document_id)
            expected_version = _required_int(payload, "expected_version")
            expected_hash = payload.get("expected_content_hash", payload.get("expected_file_hash"))
            if not isinstance(expected_hash, str) or not expected_hash:
                raise ValueError("expected_content_hash is required")
            markdown = payload.get("markdown")
            title = payload.get("title")
            if not isinstance(markdown, str) or not isinstance(title, str):
                raise ValueError("title and markdown are required")
            if expected_version != current.version:
                raise PlanDocumentConflict("plan document head conflict")
            revision = service.plan_documents.save_model_revision(
                thread_id=document.thread_id,
                title=title,
                markdown_content=markdown,
                source_turn_id=None,
                source_message_id=None,
                actor="user",
                expected_version_id=current.id,
                expected_file_hash=expected_hash,
                change_summary=str(payload.get("change_summary", "")),
            )
        except (PlanDocumentConflict, KeyError) as exc:
            return JSONResponse(
                status_code=409,
                content={"detail": str(exc), "current": _current_plan_conflict(service, plan_document_id)},
            )
        except PlanDocumentValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (OSError, UnicodeError):
            return _plan_projection_failure(service, plan_document_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _plan_document_version_json(revision)

    @app.post("/api/plans/{plan_document_id}/restore", dependencies=[Depends(mutate)], response_model=None)
    async def restore_plan_document(
        plan_document_id: str,
        payload: dict[str, Any],
        service=Depends(runtime),
    ) -> dict[str, Any] | JSONResponse:
        expected_hash = payload.get("expected_content_hash", payload.get("expected_file_hash"))
        if not isinstance(expected_hash, str) or not expected_hash:
            raise HTTPException(status_code=422, detail="expected_content_hash is required")
        try:
            version = _required_int(payload, "version")
            restored = service.plan_documents.restore_version(
                plan_document_id,
                version,
                expected_version=_required_int(payload, "expected_version"),
                expected_file_hash=expected_hash,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="plan not found") from exc
        except (PlanDocumentConflict, StopIteration, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (OSError, UnicodeError):
            return _plan_projection_failure(service, plan_document_id)
        return _plan_document_version_json(restored)

    @app.post("/api/plans/{plan_document_id}/sync-file", dependencies=[Depends(mutate)], response_model=None)
    async def sync_plan_file(plan_document_id: str, service=Depends(runtime)) -> dict[str, Any] | JSONResponse:
        try:
            revision = service.plan_documents.sync_file(plan_document_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="plan not found") from exc
        except PlanDocumentValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PlanDocumentConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (OSError, UnicodeError):
            return _plan_projection_failure(service, plan_document_id)
        return _plan_document_version_json(revision)

    @app.post("/api/plans/{plan_document_id}/retry-projection", dependencies=[Depends(mutate)])
    async def retry_plan_projection(plan_document_id: str, service=Depends(runtime)) -> dict[str, Any]:
        try:
            document = service.plan_documents.retry_projection(plan_document_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="plan not found") from exc
        return _plan_document_json(document, service.plan_documents)

    @app.post("/api/threads/{thread_id}/turns", status_code=202, dependencies=[Depends(mutate)])
    async def post_turn(
        thread_id: str,
        payload: dict[str, Any],
        request: Request,
        service=Depends(conversation),
    ) -> dict[str, Any]:
        content = payload.get("content")
        client_turn_id = payload.get("client_turn_id")
        skill_names = payload.get("skill_names", [])
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(status_code=422, detail="content is required")
        if not isinstance(client_turn_id, str) or not client_turn_id.strip():
            raise HTTPException(status_code=422, detail="client_turn_id is required")
        if not isinstance(skill_names, list) or not all(isinstance(name, str) for name in skill_names):
            raise HTTPException(status_code=422, detail="skill_names must be an array of strings")
        readiness_service = getattr(request.app.state.runtime, "model_readiness", None)
        from .conversation import UnavailableConversationModel
        from .live_model import LiveConversationModel
        enforce_readiness = isinstance(
            getattr(request.app.state.runtime, "conversation_model", None),
            (LiveConversationModel, UnavailableConversationModel),
        )
        if readiness_service is not None and enforce_readiness:
            readiness = readiness_service.check()
            if not readiness["ready"]:
                raise HTTPException(status_code=503, detail=readiness)
        try:
            accepted = service.accept_turn(thread_id, client_turn_id, content, skill_names)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="thread not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "thread_id": accepted.thread_id,
            "turn_id": accepted.turn_id,
            "status": accepted.status,
            "version": accepted.version,
            "event_cursor": accepted.event_cursor,
        }

    @app.post("/api/turns/{turn_id}/direction", dependencies=[Depends(mutate)])
    async def select_turn_direction(
        turn_id: str,
        payload: dict[str, Any],
        service=Depends(conversation),
    ) -> dict[str, Any]:
        action = payload.get("action")
        idempotency_key = payload.get("idempotency_key")
        expected_version = payload.get("expected_version")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise HTTPException(status_code=422, detail="expected_version must be an integer")
        if not isinstance(action, str) or not isinstance(idempotency_key, str):
            raise HTTPException(status_code=422, detail="action and idempotency_key are required")
        try:
            turn = await service.select_direction(turn_id, action, expected_version, idempotency_key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="turn not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        result: dict[str, Any] = {"turn": _turn_json(turn)}
        if turn.materialized_run_id and service.agent_runtime is not None:
            result["run"] = _run_json(
                service.agent_runtime.get_run(turn.materialized_run_id), service.agent_runtime
            )
        return result

    @app.get("/api/turns/{turn_id}/ask")
    async def get_turn_ask(turn_id: str, service=Depends(conversation)) -> dict[str, Any]:
        try:
            ask = service.pending_ask(turn_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="turn not found") from exc
        if ask is None:
            raise HTTPException(status_code=404, detail="no pending ask")
        return _ask_json(ask)

    @app.post("/api/turns/{turn_id}/ask/answer", dependencies=[Depends(mutate)])
    async def answer_turn_ask(
        turn_id: str,
        payload: dict[str, Any],
        service=Depends(conversation),
    ) -> dict[str, Any]:
        expected_version = payload.get("expected_version")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise HTTPException(status_code=422, detail="expected_version must be an integer")
        idempotency_key = payload.get("idempotency_key")
        answers = payload.get("answers")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise HTTPException(status_code=422, detail="idempotency_key is required")
        if not isinstance(answers, list):
            raise HTTPException(status_code=422, detail="answers must be an array")
        try:
            result = service.answer_ask(turn_id, expected_version, idempotency_key, answers)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="turn not found") from exc
        except AskValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ask_id": result.ask_id, "turn": _turn_json(result.turn)}

    @app.post("/api/turns/{turn_id}/cancel", dependencies=[Depends(mutate)])
    async def cancel_turn(turn_id: str, service=Depends(conversation)) -> dict[str, Any]:
        try:
            return _turn_json(service.cancel_turn(turn_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="turn not found") from exc

    @app.get("/api/turns/{turn_id}/tool-call")
    async def get_turn_tool_call(turn_id: str, service=Depends(conversation)) -> dict[str, Any]:
        try:
            call = service.pending_tool_call(turn_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="turn not found") from exc
        if call is None:
            raise HTTPException(status_code=404, detail="no pending tool call")
        return {"tool_call": call.as_dict()}

    @app.post("/api/turns/{turn_id}/tool-call/decision", dependencies=[Depends(mutate)])
    async def decide_turn_tool_call(
        turn_id: str,
        payload: dict[str, Any],
        service=Depends(conversation),
    ) -> dict[str, Any]:
        action = payload.get("action")
        idempotency_key = payload.get("idempotency_key")
        expected_version = payload.get("expected_version")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise HTTPException(status_code=422, detail="expected_version must be an integer")
        if not isinstance(action, str) or not isinstance(idempotency_key, str):
            raise HTTPException(status_code=422, detail="action and idempotency_key are required")
        try:
            turn = service.decide_tool_call(turn_id, action, expected_version, idempotency_key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="turn not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"turn": _turn_json(turn)}

    @app.post("/api/threads/{thread_id}/research", status_code=202, dependencies=[Depends(mutate)])
    async def create_research(thread_id: str, payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        research = getattr(service, "research", None)
        if research is None:
            raise HTTPException(status_code=503, detail="research is not configured")
        try:
            scopes = payload.get("source_scopes", ["web"])
            if not isinstance(scopes, list): raise ValueError("source_scopes must be an array")
            job = research.create_manual(thread_id, str(payload.get("topic", "")), str(payload.get("client_request_id", "")), tuple(scopes))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="thread not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409 if "active turn" in str(exc) else 422, detail=str(exc)) from exc
        return {"job_id": job.id, "status": job.status, "event_cursor": service.conversation.thread(thread_id).next_event_seq - 1}

    @app.get("/api/research/jobs")
    async def list_research_jobs(thread_id: str | None = None, schedule_id: str | None = None, status: str | None = None, limit: int = 50, offset: int = 0, service=Depends(runtime)) -> dict[str, Any]:
        research = getattr(service, "research", None)
        return {"jobs": [] if research is None else [_research_job_json(job) for job in research.list(thread_id=thread_id, schedule_id=schedule_id, status=status, limit=limit, offset=offset)]}

    @app.get("/api/research/jobs/{job_id}")
    async def get_research_job(job_id: str, service=Depends(runtime)) -> dict[str, Any]:
        try: return _research_job_json(service.research.get(job_id))
        except (AttributeError, KeyError) as exc: raise HTTPException(status_code=404, detail="research job not found") from exc

    @app.get("/api/research/jobs/{job_id}/report")
    async def get_research_report(job_id: str, service=Depends(runtime)) -> dict[str, Any]:
        try:
            job = service.research.get(job_id)
            if not job.report_markdown: raise HTTPException(status_code=409, detail="research report is not ready")
            return {"job_id": job.id, "title": job.report_title, "markdown": job.report_markdown}
        except KeyError as exc: raise HTTPException(status_code=404, detail="research job not found") from exc

    @app.get("/api/research/jobs/{job_id}/sources")
    async def get_research_sources(job_id: str, service=Depends(runtime)) -> dict[str, Any]:
        try: service.research.get(job_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="research job not found") from exc
        with service.db.connection() as connection:
            rows = connection.execute("SELECT id,ordinal,kind,canonical_url,locator,title,published_at,retrieved_at,quality_score FROM research_sources WHERE job_id=? ORDER BY ordinal", (job_id,)).fetchall()
        return {"sources": [dict(row) for row in rows]}

    @app.post("/api/research/jobs/{job_id}/cancel", dependencies=[Depends(mutate)])
    async def cancel_research(job_id: str, service=Depends(runtime)) -> dict[str, Any]:
        try: return _research_job_json(service.research.cancel(job_id))
        except KeyError as exc: raise HTTPException(status_code=404, detail="research job not found") from exc

    @app.delete("/api/research/jobs/{job_id}", status_code=204, dependencies=[Depends(mutate)])
    async def delete_research(job_id: str, service=Depends(runtime)) -> Response:
        from .research.service import ResearchConflict
        try: service.research.delete(job_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="research job not found") from exc
        except ResearchConflict as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=204)

    @app.post("/api/research/jobs/{job_id}/retry", status_code=202, dependencies=[Depends(mutate)])
    async def retry_research(job_id: str, payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        key = payload.get("client_request_id")
        if not isinstance(key, str) or not key: raise HTTPException(status_code=422, detail="client_request_id is required")
        try:
            job = service.research.retry(job_id, payload.get("topic"), key)
            return {"job_id": job.id, "status": job.status}
        except KeyError as exc: raise HTTPException(status_code=404, detail="research job not found") from exc

    @app.post("/api/research/schedules", status_code=201, dependencies=[Depends(mutate)])
    async def create_schedule(payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        try:
            result = service.schedules.create(name=payload.get("name", ""), topic=payload.get("topic", ""), source_scopes=tuple(payload.get("source_scopes", ["web"])), trigger_type=payload.get("trigger_type", "daily"), trigger_time=payload.get("trigger_time"), trigger_weekday=payload.get("trigger_weekday"), interval_hours=payload.get("interval_hours"), timezone_name=payload.get("timezone", "Asia/Shanghai"), enabled=payload.get("enabled", True), notify_enabled=payload.get("notify_enabled", True))
            return _schedule_json(result)
        except (ValueError, TypeError) as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/research/schedules")
    async def list_schedules(service=Depends(runtime)) -> dict[str, Any]:
        return {"schedules": [_schedule_json(item) for item in service.schedules.list()]}

    @app.get("/api/research/schedules/{schedule_id}")
    async def get_schedule(schedule_id: str, service=Depends(runtime)) -> dict[str, Any]:
        try: return _schedule_json(service.schedules.get(schedule_id))
        except KeyError as exc: raise HTTPException(status_code=404, detail="schedule not found") from exc

    @app.put("/api/research/schedules/{schedule_id}", dependencies=[Depends(mutate)])
    async def update_schedule(schedule_id: str, payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        try: return _schedule_json(service.schedules.update(schedule_id, **payload))
        except KeyError as exc: raise HTTPException(status_code=404, detail="schedule not found") from exc
        except (ValueError, TypeError) as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/research/schedules/{schedule_id}", status_code=204, dependencies=[Depends(mutate)])
    async def delete_schedule(schedule_id: str, service=Depends(runtime)) -> Response:
        try: service.schedules.delete(schedule_id); return Response(status_code=204)
        except KeyError as exc: raise HTTPException(status_code=404, detail="schedule not found") from exc

    @app.post("/api/research/schedules/{schedule_id}/run", status_code=202, dependencies=[Depends(mutate)])
    async def run_schedule(schedule_id: str, payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        key=payload.get("client_request_id")
        if not isinstance(key,str) or not key: raise HTTPException(status_code=422,detail="client_request_id is required")
        try:
            job=service.schedules.run_now(schedule_id,key); return {"job_id":job.id,"status":job.status}
        except KeyError as exc: raise HTTPException(status_code=404,detail="schedule not found") from exc

    @app.get("/api/research/schedules/{schedule_id}/jobs")
    async def schedule_jobs(schedule_id: str, service=Depends(runtime)) -> dict[str, Any]:
        try: service.schedules.get(schedule_id)
        except KeyError as exc: raise HTTPException(status_code=404,detail="schedule not found") from exc
        return {"jobs":[_research_job_json(job) for job in service.research.list(schedule_id=schedule_id)]}

    @app.post("/api/notification/channels", status_code=201, dependencies=[Depends(mutate)])
    async def create_channel(payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        try:return _channel_json(service.notifications.create(payload.get("name",""),payload.get("channel_type",""),payload.get("secret_env_name",""),payload.get("enabled",True)))
        except ValueError as exc:raise HTTPException(status_code=422,detail=str(exc)) from exc

    @app.get("/api/notification/channels")
    async def list_channels(service=Depends(runtime)) -> dict[str, Any]:return {"channels":[_channel_json(x) for x in service.notifications.list()]}

    @app.put("/api/notification/channels/{channel_id}",dependencies=[Depends(mutate)])
    async def update_channel(channel_id:str,payload:dict[str,Any],service=Depends(runtime))->dict[str,Any]:
        try:return _channel_json(service.notifications.update(channel_id,name=payload.get("name"),enabled=payload.get("enabled"),secret_env_name=payload.get("secret_env_name")))
        except KeyError as exc:raise HTTPException(status_code=404,detail="channel not found") from exc
        except ValueError as exc:raise HTTPException(status_code=422,detail=str(exc)) from exc

    @app.delete("/api/notification/channels/{channel_id}",status_code=204,dependencies=[Depends(mutate)])
    async def delete_channel(channel_id:str,service=Depends(runtime))->Response:
        try:service.notifications.delete(channel_id);return Response(status_code=204)
        except KeyError as exc:raise HTTPException(status_code=404,detail="channel not found") from exc

    @app.post("/api/notification/channels/{channel_id}/test", dependencies=[Depends(mutate)])
    async def test_channel(channel_id:str,service=Depends(runtime))->dict[str,Any]:
        try:return vars(await service.notifications.test(channel_id))
        except KeyError as exc:raise HTTPException(status_code=404,detail="channel not found") from exc

    @app.post("/api/research/jobs/{job_id}/notifications/{channel_id}/retry",dependencies=[Depends(mutate)])
    async def retry_notification(job_id:str,channel_id:str,service=Depends(runtime))->dict[str,Any]:
        try:return vars(await service.notifications.retry(job_id,channel_id))
        except KeyError as exc:raise HTTPException(status_code=404,detail="report or channel not found") from exc

    @app.get("/api/skills")
    async def list_skills(request: Request, include_disabled: bool = False) -> dict[str, Any]:
        service = runtime(request)
        items = service.skill_platform.installed_versions() if include_disabled else service.skill_platform.enabled_versions()
        return {"skills": [{**item, "enabled": item["status"] == "ENABLED"} for item in items if item["status"] != "UNINSTALLED"]}

    async def skill_zip_mutate(request: Request) -> None:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/zip":
            raise HTTPException(status_code=415, detail="请求必须使用 ZIP 内容类型")
        origin = request.headers.get("origin")
        if origin and urlparse(origin).hostname not in {"127.0.0.1", "localhost"}:
            raise HTTPException(status_code=403, detail="仅允许从本机页面发起请求")
        if request.headers.get("x-csrf-token") != request.app.state.csrf_token:
            raise HTTPException(status_code=403, detail="缺少有效的安全校验令牌")

    @app.post("/api/skills/install", dependencies=[Depends(skill_zip_mutate)])
    async def preview_skill_install(request: Request, service=Depends(runtime)):
        from .skill_platform import SkillValidationError
        try: return service.skill_platform.preview_install(await request.body())
        except SkillValidationError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def _confirm_skill_install(install_token: str, payload: dict[str, Any], request: Request, service):
        from .skill_platform import SkillValidationError
        try:
            return service.skill_platform.confirm_install(
                install_token, granted_tools=payload.get("granted_tools", []),
                idempotency_key=idempotency_key(request),
            )
        except SkillValidationError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/skills/{install_token}/confirm-install", dependencies=[Depends(mutate)])
    async def confirm_skill_install(install_token: str, payload: dict[str, Any], request: Request, service=Depends(runtime)):
        return await _confirm_skill_install(install_token, payload, request, service)

    @app.post("/api/skills/confirm-install", dependencies=[Depends(mutate)], include_in_schema=False)
    async def confirm_skill_install_legacy(payload: dict[str, Any], request: Request, service=Depends(runtime)):
        return await _confirm_skill_install(_required_text(payload, "install_token"), payload, request, service)

    @app.put("/api/threads/{thread_id}/skills", dependencies=[Depends(mutate)])
    async def bind_thread_skills(thread_id: str, payload: dict[str, Any], request: Request, service=Depends(runtime)):
        from .skill_platform import SkillValidationError
        service.conversation.thread(thread_id)
        try:
            return service.skill_platform.bind(
                "THREAD", thread_id, payload.get("version_ids", []), idempotency_key=idempotency_key(request),
            )
        except (SkillValidationError, KeyError) as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/skills/{skill_id}", status_code=204, dependencies=[Depends(mutate)])
    async def uninstall_skill(skill_id: str, request: Request, service=Depends(runtime)):
        try: service.skill_platform.uninstall(skill_id, idempotency_key=idempotency_key(request))
        except KeyError as exc: raise HTTPException(status_code=404, detail="skill not found") from exc
        return Response(status_code=204)

    @app.get("/api/skills/{skill_id}/versions")
    async def list_skill_versions(skill_id: str, service=Depends(runtime)):
        try: return {"versions": service.skill_platform.versions(skill_id)}
        except KeyError as exc: raise HTTPException(status_code=404, detail="skill not found") from exc

    @app.put("/api/skill-versions/{version_id}/grant", dependencies=[Depends(mutate)])
    async def update_skill_grant(version_id: str, payload: dict[str, Any], request: Request, service=Depends(runtime)):
        from .skill_platform import SkillValidationError
        try: return service.skill_platform.grant(version_id, payload.get("granted_tools", []), idempotency_key=idempotency_key(request))
        except KeyError as exc: raise HTTPException(status_code=404, detail="skill version not found") from exc
        except SkillValidationError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def set_skill_enabled(version_id: str, enabled: bool, request: Request, service):
        from .skill_platform import SkillValidationError
        try: return service.skill_platform.set_enabled(version_id, enabled, idempotency_key=idempotency_key(request))
        except KeyError as exc: raise HTTPException(status_code=404, detail="skill version not found") from exc
        except SkillValidationError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/skill-versions/{version_id}/enable", dependencies=[Depends(mutate)])
    async def enable_skill_version(version_id: str, request: Request, service=Depends(runtime)):
        return await set_skill_enabled(version_id, True, request, service)

    @app.post("/api/skill-versions/{version_id}/disable", dependencies=[Depends(mutate)])
    async def disable_skill_version(version_id: str, request: Request, service=Depends(runtime)):
        return await set_skill_enabled(version_id, False, request, service)

    @app.get("/api/trusted-connectors")
    async def list_trusted_connectors(service=Depends(runtime)):
        return {"connectors": service.connectors.list()}

    @app.post("/api/trusted-connectors", status_code=201, dependencies=[Depends(mutate)])
    async def create_trusted_connector(payload: dict[str, Any], request: Request, service=Depends(runtime)):
        from .trusted_connectors import ConnectorSecurityError
        try:
            return service.connectors.register(
                _required_text(payload, "name"), _required_text(payload, "base_url"), payload.get("methods", []),
                payload.get("paths", []), payload.get("credential_env_ref"), idempotency_key=idempotency_key(request),
                timeout_seconds=float(payload.get("timeout_seconds", 30)),
                max_response_bytes=int(payload.get("max_response_bytes", 2 * 1024 * 1024)),
                request_schema=payload.get("request_schema", {}),
            )
        except (ConnectorSecurityError, ValueError, TypeError) as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/trusted-connector-versions/{version_id}/verify", dependencies=[Depends(mutate)])
    async def verify_trusted_connector(version_id: str, service=Depends(runtime)):
        from .trusted_connectors import ConnectorSecurityError
        try: return service.connectors.verify(version_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="connector version not found") from exc
        except ConnectorSecurityError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/goals", status_code=201, dependencies=[Depends(mutate)])
    async def create_goal(payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        if not payload.get("title"):
            raise HTTPException(status_code=422, detail="title is required")
        run = await service.create_goal(payload["title"], payload.get("description", ""), payload.get("project_id"))
        return {"id": run.goal_id, "run_id": run.id, "state": run.state.value}

    @app.get("/api/goals/{goal_id}")
    async def get_goal(goal_id: str, request: Request) -> dict[str, Any]:
        service = runtime(request)
        with service.db.connection() as connection:
            goal = connection.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
            runs = connection.execute("SELECT id FROM runs WHERE goal_id = ? ORDER BY created_at", (goal_id,)).fetchall()
        if goal is None:
            raise HTTPException(status_code=404, detail="goal not found")
        return {"id": goal["id"], "title": goal["title"], "description": goal["description"], "runs": [row["id"] for row in runs]}

    @app.post("/api/goals/{goal_id}/messages", dependencies=[Depends(mutate)])
    async def post_message(goal_id: str, payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        if not payload.get("content"):
            raise HTTPException(status_code=422, detail="content is required")
        skill_names = payload.get("skill_names")
        if skill_names is not None and (
            not isinstance(skill_names, list)
            or not all(isinstance(name, str) for name in skill_names)
        ):
            raise HTTPException(status_code=422, detail="skill_names must be an array of strings")
        with service.db.connection() as connection:
            row = connection.execute(
                "SELECT id FROM runs WHERE goal_id = ? ORDER BY created_at DESC LIMIT 1", (goal_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="goal run not found")
        try:
            run = await service.handle_message(row["id"], payload["content"], skill_names)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _run_json(run, service)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> dict[str, Any]:
        service = runtime(request)
        return _run_json(service.get_run(run_id), service)

    @app.post("/api/runs/{run_id}/resume", dependencies=[Depends(mutate)])
    async def resume_run(run_id: str, service=Depends(runtime)) -> dict[str, Any]:
        return _run_json(await service.resume(run_id), service)

    @app.post("/api/runs/{run_id}/outcome", dependencies=[Depends(mutate)])
    async def continue_outcome(run_id: str, payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        return _run_json(await service.continue_outcome(run_id, bool(payload.get("finished", False))), service)

    @app.post("/api/runs/{run_id}/cancel", dependencies=[Depends(mutate)])
    async def cancel_run(run_id: str, service=Depends(runtime)) -> dict[str, Any]:
        return _run_json(await service.cancel(run_id), service)

    @app.post("/api/runs/{run_id}/budget", dependencies=[Depends(mutate)])
    async def add_budget(run_id: str, payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        try:
            return _run_json(await service.add_budget(run_id, int(payload.get("amount", 0))), service)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/plans")
    async def list_plans(run_id: str, request: Request) -> dict[str, Any]:
        service = runtime(request)
        with service.db.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM plan_versions WHERE run_id = ? ORDER BY version", (run_id,)
            ).fetchall()
        plans = [_plan_json(service.plans.get(row["id"])) for row in rows]
        return {"current": plans[-1] if plans else None, "history": plans}

    @app.post("/api/runs/{run_id}/plans/{version}/approve", dependencies=[Depends(mutate)])
    async def approve_plan(run_id: str, version: int, service=Depends(runtime)) -> dict[str, Any]:
        try:
            return _run_json(await service.approve_plan(run_id, version), service)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/plans/revise", dependencies=[Depends(mutate)])
    async def revise_plan(run_id: str, payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        try:
            plan = await service.revise_plan(
                run_id,
                int(payload["expected_version"]),
                payload.get("steps", []),
                payload.get("summary", ""),
            )
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _plan_json(plan)

    @app.post("/api/runs/{run_id}/steps/{step_id}/cancel", dependencies=[Depends(mutate)])
    async def cancel_step(run_id: str, step_id: str, service=Depends(runtime)) -> dict[str, Any]:
        return _run_json(await service.cancel_step(run_id, step_id), service)

    @app.post("/api/approvals/{approval_id}/grant", dependencies=[Depends(mutate)])
    async def grant_approval(approval_id: str, service=Depends(runtime)) -> dict[str, Any]:
        return _run_json(await service.grant_approval(approval_id), service)

    @app.post("/api/approvals/{approval_id}/reject", dependencies=[Depends(mutate)])
    async def reject_approval(approval_id: str, service=Depends(runtime)) -> dict[str, Any]:
        return _run_json(await service.reject_approval(approval_id), service)

    @app.get("/api/runs/{run_id}/events")
    async def get_events(run_id: str, request: Request, after_seq: int = 0) -> dict[str, Any]:
        service = runtime(request)
        return {"events": [_event_json(event) for event in service.events.list(run_id, after_seq)]}

    @app.get("/api/runs/{run_id}/events/stream")
    async def event_stream(run_id: str, request: Request, follow: bool = True) -> StreamingResponse:
        service = runtime(request)
        last_event_id = request.headers.get("last-event-id") or request.query_params.get("after_seq", "0")
        try:
            after_seq = int(last_event_id or 0)
        except ValueError:
            after_seq = 0

        return StreamingResponse(
            _event_stream(service, run_id, request, after_seq, follow),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/runs/{run_id}/messages")
    async def get_messages(run_id: str, request: Request) -> dict[str, Any]:
        service = runtime(request)
        service.get_run(run_id)
        with service.db.connection() as connection:
            rows = connection.execute(
                "SELECT id, run_id, interaction_id, role, content, created_at FROM messages "
                "WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
        return {
            "messages": [
                {
                    "id": row["id"],
                    "run_id": row["run_id"],
                    "interaction_id": row["interaction_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": row["created_at"],
                    "streaming": row["role"] == "assistant" and not row["content"],
                }
                for row in rows
            ]
        }

    @app.get("/api/runs/{run_id}/stats")
    async def get_stats(run_id: str, request: Request) -> dict[str, Any]:
        service = runtime(request)
        projector = getattr(service, "stats", None)
        return projector.project(run_id) if projector else {"run_id": run_id, "state": service.get_run(run_id).state.value}

    @app.put("/api/cost/budgets", dependencies=[Depends(mutate)])
    async def put_cost_budget(payload: dict[str, Any], request: Request, service=Depends(runtime), current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        period_kind = str(payload.get("period_kind", ""))
        period_key = str(payload.get("period_key", ""))
        limit = payload.get("limit_microusd")
        if period_kind not in {"INVOCATION", "DAILY", "MONTHLY"} or not period_key or not isinstance(limit, int) or limit < 0:
            raise HTTPException(status_code=422, detail="invalid cost budget")
        idempotency_key(request)
        try:
            service.costs.set_budget(current_owner, period_kind, period_key, limit)
        except Exception as exc:
            from .costs import BudgetExceeded
            if isinstance(exc, BudgetExceeded):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise
        return service.costs.summary(current_owner, period_kind, period_key)

    @app.get("/api/cost/summary")
    async def get_cost_summary(period_kind: str, period_key: str, service=Depends(runtime), current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        if period_kind not in {"INVOCATION", "DAILY", "MONTHLY"} or not period_key:
            raise HTTPException(status_code=422, detail="invalid cost period")
        try:
            return service.costs.summary(current_owner, period_kind, period_key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="cost budget not found") from exc

    @app.get("/api/usage/summary")
    async def get_usage_summary(service=Depends(runtime), current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        return service.costs.usage_summary(current_owner)

    @app.get("/api/cost/export")
    async def export_cost_ledger(service=Depends(runtime)):
        from .control_exports import cost_export
        return PlainTextResponse(cost_export(service.db), media_type="application/x-ndjson")

    @app.get("/api/model-invocations/export")
    async def export_model_invocations(service=Depends(runtime)):
        from .control_exports import invocation_export
        return PlainTextResponse(invocation_export(service.db), media_type="application/x-ndjson")

    @app.get("/api/skills/audit/export")
    async def export_skill_audit(service=Depends(runtime)):
        from .control_exports import skill_audit_export
        return PlainTextResponse(skill_audit_export(service.db), media_type="application/x-ndjson")

    @app.get("/api/model-invocations/{invocation_id}")
    async def get_model_invocation(invocation_id: str, service=Depends(runtime), current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        with service.db.connection() as connection:
            row = connection.execute("SELECT * FROM model_invocations WHERE id=? AND owner_id=?", (invocation_id, current_owner)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="model invocation not found")
        return {key: row[key] for key in row.keys() if key not in {"route_snapshot_json"}}

    @app.get("/api/model-invocations/{invocation_id}/attempts")
    async def get_model_attempts(invocation_id: str, service=Depends(runtime), current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        with service.db.connection() as connection:
            owner = connection.execute("SELECT 1 FROM model_invocations WHERE id=? AND owner_id=?", (invocation_id, current_owner)).fetchone()
            rows = connection.execute("SELECT * FROM model_attempts WHERE invocation_id=? ORDER BY ordinal", (invocation_id,)).fetchall() if owner else []
        if owner is None:
            raise HTTPException(status_code=404, detail="model invocation not found")
        return {"attempts": [{key: row[key] for key in row.keys()} for row in rows]}

    @app.get("/api/runs/{run_id}/export")
    async def export_run(run_id: str, request: Request, mode: str = "redacted") -> StreamingResponse:
        service = runtime(request)
        if mode not in {"redacted", "full"}:
            raise HTTPException(status_code=400, detail="invalid export mode")
        body = export_jsonl(service.events.list(run_id), mode=mode, workspace=service.db.workspace)
        return StreamingResponse(iter([body]), media_type="application/x-ndjson")

    @app.get("/api/memories")
    async def list_memories(request: Request, current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        service = runtime(request)
        store = getattr(service, "memory_store", None)
        if store is not None:
            return {"entries": [_memory_entry_json(item) for item in store.list_entries(current_owner)], "proposals": [_memory_proposal_json(item) for item in store.list_proposals(current_owner)], "episodes": [_memory_episode_json(item) for item in store.list_episodes(current_owner)]}
        if current_owner != "local-user":
            return {"memories": []}
        return {"memories": [_memory_json(record) for record in service.memory.all_records()]}

    @app.post("/api/memory/entries", status_code=201, dependencies=[Depends(mutate)])
    async def create_memory_entry(payload: dict[str, Any], request: Request, service=Depends(runtime), current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        from .memory_v2 import MemoryConflict
        try:
            item=service.memory_store.remember(current_owner,payload.get("kind","fact"),payload.get("scope_type","user"),payload.get("scope_id",""),payload.get("content",""),idempotency_key(request),payload.get("source_refs",[]),pinned=payload.get("pinned",False),importance=payload.get("importance",.5),sensitivity=payload.get("sensitivity","normal"),source_thread_id=payload.get("source_thread_id"),source_run_id=payload.get("source_run_id"))
            return _memory_entry_json(item)
        except MemoryConflict as exc:raise HTTPException(status_code=409,detail={"reason_code":exc.reason_code,"message":str(exc)}) from exc
        except (ValueError,TypeError) as exc:raise HTTPException(status_code=422,detail={"reason_code":"INVALID_MEMORY_REQUEST","message":str(exc)}) from exc

    @app.patch("/api/memory/entries/{entry_id}", dependencies=[Depends(mutate)])
    async def edit_memory_entry(entry_id:str,payload:dict[str,Any],request:Request,service=Depends(runtime),current_owner: str = Depends(owner_id))->dict[str,Any]:
        from .memory_v2 import MemoryConflict
        try:return _memory_entry_json(service.memory_store.edit(entry_id,current_owner,payload.get("content",""),payload.get("base_revision_id",""),idempotency_key=idempotency_key(request)))
        except KeyError as exc:raise HTTPException(status_code=404,detail="memory not found") from exc
        except MemoryConflict as exc:raise HTTPException(status_code=409,detail={"reason_code":exc.reason_code,"message":str(exc)}) from exc
        except ValueError as exc:raise HTTPException(status_code=422,detail=str(exc)) from exc

    @app.post("/api/memory/entries/{entry_id}/archive", dependencies=[Depends(mutate)])
    async def archive_memory_entry(entry_id:str,request:Request,service=Depends(runtime),current_owner: str = Depends(owner_id))->dict[str,Any]:
        from .memory_v2 import MemoryConflict
        try:return _memory_entry_json(service.memory_store.set_status(entry_id,current_owner,"ARCHIVED",idempotency_key=idempotency_key(request)))
        except KeyError as exc:raise HTTPException(status_code=404,detail="memory not found") from exc
        except MemoryConflict as exc:raise HTTPException(status_code=409,detail={"reason_code":exc.reason_code,"message":str(exc)}) from exc

    @app.post("/api/memory/entries/{entry_id}/restore", dependencies=[Depends(mutate)])
    async def restore_memory_entry(entry_id:str,request:Request,service=Depends(runtime),current_owner: str = Depends(owner_id))->dict[str,Any]:
        from .memory_v2 import MemoryConflict
        try:return _memory_entry_json(service.memory_store.restore(entry_id,current_owner,idempotency_key(request)))
        except KeyError as exc:raise HTTPException(status_code=404,detail="memory not found") from exc
        except MemoryConflict as exc:raise HTTPException(status_code=409,detail={"reason_code":exc.reason_code,"message":str(exc)}) from exc

    @app.delete("/api/memory/entries/{entry_id}",status_code=204,dependencies=[Depends(mutate)])
    async def purge_memory_entry(entry_id:str,request:Request,service=Depends(runtime),current_owner: str = Depends(owner_id))->Response:
        from .memory_v2 import MemoryConflict
        try:service.memory_store.purge(entry_id,current_owner,idempotency_key=idempotency_key(request));return Response(status_code=204)
        except KeyError as exc:raise HTTPException(status_code=404,detail="memory not found") from exc
        except MemoryConflict as exc:raise HTTPException(status_code=409,detail={"reason_code":exc.reason_code,"message":str(exc)}) from exc

    @app.post("/api/memory/proposals/{proposal_id}/decision",dependencies=[Depends(mutate)])
    async def decide_memory_proposal(proposal_id:str,payload:dict[str,Any],request:Request,service=Depends(runtime),current_owner: str = Depends(owner_id))->dict[str,Any]:
        from .memory_v2 import MemoryConflict
        if type(payload.get("accept")) is not bool:
            raise HTTPException(status_code=422,detail={"reason_code":"INVALID_APPROVAL_DECISION","message":"accept must be a boolean"})
        if "expected_version" not in payload:
            raise HTTPException(status_code=422,detail={"reason_code":"INVALID_APPROVAL_DECISION","message":"expected_version is required"})
        try:return _memory_proposal_json(service.memory_store.decide_proposal(proposal_id,current_owner,payload["accept"],idempotency_key(request),accepted_content=payload.get("accepted_content"),expected_version=payload["expected_version"]))
        except KeyError as exc:raise HTTPException(status_code=404,detail="proposal not found") from exc
        except MemoryConflict as exc:raise HTTPException(status_code=409,detail={"reason_code":exc.reason_code,"message":str(exc)}) from exc
        except (ValueError,TypeError) as exc:raise HTTPException(status_code=422,detail={"reason_code":"INVALID_APPROVAL_DECISION","message":str(exc)}) from exc

    @app.patch("/api/memory/episodes/{episode_id}",dependencies=[Depends(mutate)])
    async def edit_memory_episode(episode_id:str,payload:dict[str,Any],request:Request,service=Depends(runtime),current_owner: str = Depends(owner_id))->dict[str,Any]:
        from .memory_v2 import MemoryConflict
        if "expected_version" not in payload:raise HTTPException(status_code=422,detail={"reason_code":"INVALID_EPISODE_REQUEST","message":"expected_version is required"})
        try:return _memory_episode_json(service.memory_store.edit_episode(episode_id,current_owner,payload.get("summary",""),payload.get("retrieval_policy"),payload["expected_version"],idempotency_key(request)))
        except KeyError as exc:raise HTTPException(status_code=404,detail="episode not found") from exc
        except MemoryConflict as exc:raise HTTPException(status_code=409,detail={"reason_code":exc.reason_code,"message":str(exc)}) from exc
        except (ValueError,TypeError) as exc:raise HTTPException(status_code=422,detail={"reason_code":"INVALID_EPISODE_REQUEST","message":str(exc)}) from exc

    @app.delete("/api/memory/episodes/{episode_id}",status_code=204,dependencies=[Depends(mutate)])
    async def delete_memory_episode(episode_id:str,payload:dict[str,Any],request:Request,service=Depends(runtime),current_owner: str = Depends(owner_id))->Response:
        from .memory_v2 import MemoryConflict
        if "expected_version" not in payload:raise HTTPException(status_code=422,detail={"reason_code":"INVALID_EPISODE_REQUEST","message":"expected_version is required"})
        try:service.memory_store.delete_episode(episode_id,current_owner,payload["expected_version"],idempotency_key(request));return Response(status_code=204)
        except KeyError as exc:raise HTTPException(status_code=404,detail="episode not found") from exc
        except MemoryConflict as exc:raise HTTPException(status_code=409,detail={"reason_code":exc.reason_code,"message":str(exc)}) from exc
        except (ValueError,TypeError) as exc:raise HTTPException(status_code=422,detail={"reason_code":"INVALID_EPISODE_REQUEST","message":str(exc)}) from exc

    @app.get("/api/memories/{memory_id}/versions")
    async def list_memory_versions(memory_id: str, request: Request, current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        service = runtime(request)
        store = getattr(service, "memory_store", None)
        if store is not None:
            try:
                return {"versions": [_memory_revision_json(version) for version in store.revisions(memory_id, current_owner)]}
            except KeyError:
                raise HTTPException(status_code=404, detail="memory not found")
        if current_owner != "local-user":
            raise HTTPException(status_code=404, detail="memory not found")
        return {"versions": [_memory_version_json(version) for version in service.memory.versions(memory_id)]}

    @app.post("/api/memories", dependencies=[Depends(mutate)])
    async def create_memory(payload: dict[str, Any], service=Depends(runtime), current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        if getattr(service, "memory_store", None) is not None:
            raise HTTPException(status_code=410, detail="legacy memory API is disabled; use /api/memory/entries")
        if current_owner != "local-user":
            raise HTTPException(status_code=404, detail="memory API is not available for this owner")
        try:
            record = service.memory.create_candidate(
                payload.get("run_id", "memory"),
                payload.get("goal_id", "memory"),
                payload["kind"],
                payload["content"],
                payload.get("scope", "global"),
                float(payload.get("confidence", 1.0)),
                payload.get("evidence_event_ids", []),
                payload.get("project_id"),
                payload.get("skill_name"),
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _memory_json(record)

    @app.patch("/api/memories/{memory_id}", dependencies=[Depends(mutate)])
    async def edit_memory(memory_id: str, payload: dict[str, Any], service=Depends(runtime), current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        if getattr(service, "memory_store", None) is not None:
            raise HTTPException(status_code=410, detail="legacy memory API is disabled; use /api/memory/entries")
        if current_owner != "local-user":
            raise HTTPException(status_code=404, detail="memory not found")
        return _memory_json(service.memory.edit(memory_id, payload["content"]))

    @app.post("/api/memories/{memory_id}/confirm", dependencies=[Depends(mutate)])
    async def confirm_memory(memory_id: str, payload: dict[str, Any] | None = None, service=Depends(runtime), current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        if getattr(service, "memory_store", None) is not None:
            raise HTTPException(status_code=410, detail="legacy memory API is disabled; use /api/memory/proposals/{proposal_id}/decision")
        if current_owner != "local-user":
            raise HTTPException(status_code=404, detail="memory not found")
        return _memory_json(service.memory.confirm(memory_id, (payload or {}).get("content")))

    @app.post("/api/memories/{memory_id}/reject", dependencies=[Depends(mutate)])
    async def reject_memory(memory_id: str, service=Depends(runtime), current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        if getattr(service, "memory_store", None) is not None:
            raise HTTPException(status_code=410, detail="legacy memory API is disabled; use /api/memory/proposals/{proposal_id}/decision")
        if current_owner != "local-user":
            raise HTTPException(status_code=404, detail="memory not found")
        return _memory_json(service.memory.reject(memory_id))

    @app.post("/api/memories/{memory_id}/disable", dependencies=[Depends(mutate)])
    async def disable_memory(memory_id: str, service=Depends(runtime), current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        if getattr(service, "memory_store", None) is not None:
            raise HTTPException(status_code=410, detail="legacy memory API is disabled; use /api/memory/entries/{entry_id}/archive")
        if current_owner != "local-user":
            raise HTTPException(status_code=404, detail="memory not found")
        return _memory_json(service.memory.disable(memory_id))

    @app.post("/api/memories/{memory_id}/rollback", dependencies=[Depends(mutate)])
    async def rollback_memory(memory_id: str, payload: dict[str, Any], service=Depends(runtime), current_owner: str = Depends(owner_id)) -> dict[str, Any]:
        if getattr(service, "memory_store", None) is not None:
            raise HTTPException(status_code=410, detail="legacy memory API is disabled; use /api/memory/entries/{entry_id}")
        if current_owner != "local-user":
            raise HTTPException(status_code=404, detail="memory not found")
        return _memory_json(service.memory.rollback(memory_id, int(payload["version"])))

    def evolution(request: Request):
        service = getattr(runtime(request), "evolution", None)
        if service is None:
            raise HTTPException(status_code=503, detail="evolution service is not configured")
        return service

    @app.post("/api/evolution/experiences", status_code=201, dependencies=[Depends(mutate)])
    async def record_evolution_experience(payload: dict[str, Any], request: Request, service=Depends(evolution)):
        return _evolution_call(lambda: service.record_experience(
            task_type=_required_text(payload, "task_type"), outcome=_required_text(payload, "outcome"),
            lineage_group_hash=_required_text(payload, "lineage_group_hash"), source_content_hash=_required_text(payload, "source_content_hash"),
            runtime_bundle_id=_required_text(payload, "runtime_bundle_id"), dataset_partition=_required_text(payload, "dataset_partition"),
            idempotency_key=idempotency_key(request), root_task_id=str(payload.get("root_task_id") or ""),
            target_role=str(payload.get("target_role") or ""), provenance=str(payload.get("provenance") or "production"),
            source_version=str(payload.get("source_version") or ""),
        ))

    @app.post("/api/evolution/candidates", status_code=201, dependencies=[Depends(mutate)])
    async def create_evolution_candidate(payload: dict[str, Any], request: Request, service=Depends(evolution)):
        return _evolution_call(lambda: service.propose_candidate(
            candidate_type=_required_text(payload, "candidate_type"), experience_ids=payload.get("experience_ids"),
            base_bundle_id=_required_text(payload, "base_bundle_id"), target_bundle_id=_required_text(payload, "target_bundle_id"),
            proposed_content=payload.get("proposed_content"), permission_diff=payload.get("permission_diff"),
            reason=_required_text(payload, "reason"), idempotency_key=idempotency_key(request),
            problem_fingerprint=str(payload.get("problem_fingerprint") or ""),
            root_cause_hypothesis=str(payload.get("root_cause_hypothesis") or ""),
            confidence_limitations=str(payload.get("confidence_limitations") or ""),
            target_role=str(payload.get("target_role") or ""), allowed_path=str(payload.get("allowed_path") or ""),
            expected_metrics=payload.get("expected_metrics"), risks=payload.get("risks"), replay_case_ids=payload.get("replay_case_ids"),
        ))

    @app.post("/api/evolution/content-authorizations", status_code=201, dependencies=[Depends(mutate)])
    async def grant_evolution_content_authorization(payload: dict[str, Any], request: Request, service=Depends(evolution)):
        return _evolution_call(lambda: service.grant_content_authorization(
            subject_id=_required_text(payload, "subject_id"), source_scope=payload.get("source_scope") or [],
            purpose=_required_text(payload, "purpose"), expires_at=_required_text(payload, "expires_at"),
            idempotency_key=idempotency_key(request),
        ))

    @app.post("/api/evolution/content-authorizations/{authorization_id}/revoke", dependencies=[Depends(mutate)])
    async def revoke_evolution_content_authorization(authorization_id: str, service=Depends(evolution)):
        return _evolution_call(lambda: service.revoke_content_authorization(authorization_id))

    @app.post("/api/evolution/generation-batches", status_code=201, dependencies=[Depends(mutate)])
    async def authorize_evolution_generation(payload: dict[str, Any], request: Request, service=Depends(evolution)):
        return _evolution_call(lambda: service.authorize_generation_batch(
            experience_ids=payload.get("experience_ids") or [], base_bundle_id=_required_text(payload, "base_bundle_id"),
            problem_fingerprint=_required_text(payload, "problem_fingerprint"), root_budget_id=_required_text(payload, "root_budget_id"),
            max_calls=_required_int(payload, "max_calls"), budget_microusd=_required_int(payload, "budget_microusd"),
            deadline_at=_required_text(payload, "deadline_at"), generation_config=payload.get("generation_config") or {},
            content_authorization_id=payload.get("content_authorization_id"), idempotency_key=idempotency_key(request),
        ))

    @app.get("/api/evolution/generation-batches/{batch_id}")
    async def get_evolution_generation(batch_id: str, service=Depends(evolution)):
        return _evolution_call(lambda: service.get_generation_batch(batch_id))

    @app.post("/api/evolution/generation-batches/{batch_id}/run", dependencies=[Depends(mutate)])
    async def run_evolution_generation(batch_id: str, request: Request):
        generator = getattr(runtime(request), "candidate_generator", None)
        if generator is None:
            raise HTTPException(status_code=503, detail="candidate generator is not configured")
        try:
            return await asyncio.to_thread(generator.run_batch, batch_id)
        except (EvolutionConflict, EvolutionGateError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/evolution/candidates")
    async def list_evolution_candidates(service=Depends(evolution)):
        return {"candidates": service.list_candidates()}

    def real_evaluator(request: Request):
        value = getattr(runtime(request), "real_evaluator", None)
        if value is None:
            raise HTTPException(status_code=503, detail="real evaluator is not configured")
        return value

    @app.get("/api/evaluation-suites")
    async def list_evaluation_suites(service=Depends(real_evaluator)):
        return {"suites": service.list_suites()}

    @app.post("/api/evaluation-runs", status_code=202, dependencies=[Depends(mutate)])
    async def create_evaluation_run(payload: dict[str, Any], request: Request, service=Depends(real_evaluator)):
        from .real_evaluation import EvaluationAccessError
        try: return service.enqueue(service.authoritative_config(payload), idempotency_key=idempotency_key(request), root_budget_id=payload.get("root_budget_id"))
        except KeyError as exc: raise HTTPException(status_code=404, detail="evaluation suite not found") from exc
        except (EvaluationAccessError, ValueError) as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/evaluation-runs/{evaluation_run_id}")
    async def get_evaluation_run(evaluation_run_id: str, service=Depends(real_evaluator)):
        try: return service.run(evaluation_run_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="evaluation run not found") from exc

    @app.get("/api/evaluation-runs/{evaluation_run_id}/report")
    async def get_evaluation_report(evaluation_run_id: str, service=Depends(real_evaluator)):
        try: return service.report(evaluation_run_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="evaluation run not found") from exc

    @app.get("/api/evaluation-runs/{evaluation_run_id}/events")
    async def get_evaluation_events(evaluation_run_id: str, after_seq: int = 0, service=Depends(real_evaluator)):
        try: return service.progress(evaluation_run_id, after_seq=max(after_seq, 0))
        except KeyError as exc: raise HTTPException(status_code=404, detail="evaluation run not found") from exc

    @app.get("/api/evaluation-runs/{evaluation_run_id}/events/stream")
    async def stream_evaluation_events(evaluation_run_id: str, request: Request, after_seq: int = 0, service=Depends(real_evaluator)):
        async def generate():
            header_cursor = request.headers.get("last-event-id", "")
            cursor = max(after_seq, int(header_cursor) if header_cursor.isdigit() else 0, 0)
            while True:
                try: batch = service.progress(evaluation_run_id, cursor)["events"]
                except KeyError: return
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\nevent: evaluation\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                with service.db.connection() as connection:
                    row = connection.execute("SELECT status FROM evaluation_runs WHERE id=?", (evaluation_run_id,)).fetchone()
                if row is None or row["status"] in {"COMPLETED", "FAILED", "CANCELLED", "BUDGET_BLOCKED"}: return
                if await request.is_disconnected(): return
                if not batch: yield ": keep-alive\n\n"
                await asyncio.sleep(.05)
        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/api/evaluation-runs/{evaluation_run_id}/cancel", dependencies=[Depends(mutate)])
    async def cancel_evaluation_run(evaluation_run_id: str, service=Depends(real_evaluator)):
        try: return service.cancel(evaluation_run_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="evaluation run not found") from exc
        except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/evaluation-runs/{evaluation_run_id}/resume", dependencies=[Depends(mutate)])
    async def resume_evaluation_run(evaluation_run_id: str, payload: dict[str, Any], request: Request, service=Depends(real_evaluator)):
        from .real_evaluation import EvaluationAccessError
        try: return service.resume_with_budget(evaluation_run_id, payload.get("budget_microusd"), idempotency_key=idempotency_key(request))
        except KeyError as exc: raise HTTPException(status_code=404, detail="evaluation run not found") from exc
        except (ValueError, EvaluationAccessError) as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/evolution/experiences")
    async def list_evolution_experiences(service=Depends(evolution)):
        return {"experiences": service.list_experiences()}

    @app.post("/api/evolution/experiences/observe", dependencies=[Depends(mutate)])
    async def observe_evolution_experiences(request: Request):
        observer = getattr(runtime(request), "observer", None)
        if observer is None:
            raise HTTPException(status_code=503, detail="experience observer is not configured")
        return observer.observe()

    @app.get("/api/evolution/candidates/{candidate_id}")
    async def get_evolution_candidate(candidate_id: str, service=Depends(evolution)):
        return _evolution_call(lambda: service.get_candidate(candidate_id))

    @app.post("/api/evolution/candidates/{candidate_id}/evaluate", dependencies=[Depends(mutate)])
    async def evaluate_evolution_candidate(candidate_id: str, payload: dict[str, Any], request: Request, service=Depends(evolution)):
        try:
            return await asyncio.to_thread(service.evaluate_builtin, candidate_id, expected_version=_required_int(payload,"expected_version"), idempotency_key=idempotency_key(request))
        except (EvolutionConflict, EvolutionGateError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/evolution/candidates/{candidate_id}/approve", dependencies=[Depends(mutate)])
    async def approve_evolution_candidate(candidate_id: str, payload: dict[str, Any], request: Request, service=Depends(evolution)):
        return _evolution_call(lambda: service.approve_builtin(candidate_id, expected_version=_required_int(payload,"expected_version"), idempotency_key=idempotency_key(request)))

    @app.post("/api/evolution/candidates/{candidate_id}/reject", dependencies=[Depends(mutate)])
    async def reject_evolution_candidate(candidate_id: str, payload: dict[str, Any], request: Request, service=Depends(evolution)):
        return _evolution_call(lambda: service.reject(
            candidate_id, expected_version=_required_int(payload, "expected_version"), reason=str(payload.get("reason") or "user rejected"),
            actor="user", idempotency_key=idempotency_key(request),
        ))

    @app.post("/api/evolution/candidates/{candidate_id}/start-canary", dependencies=[Depends(mutate)])
    async def start_evolution_canary(candidate_id: str, payload: dict[str, Any], request: Request, service=Depends(evolution)):
        return _evolution_call(lambda: service.start_canary_builtin(
            candidate_id, expected_version=_required_int(payload,"expected_version"), idempotency_key=idempotency_key(request),
            target_role=str(payload.get("target_role") or ""), target_purpose=str(payload.get("target_purpose") or ""),
            budget_microusd=payload.get("budget_microusd"), deadline_at=payload.get("deadline_at"), max_calls=payload.get("max_calls"),
        ))

    @app.post("/api/evolution/canaries/{deployment_id}/quality", dependencies=[Depends(mutate)])
    async def assess_evolution_quality(deployment_id: str, payload: dict[str, Any], service=Depends(evolution)):
        _evolution_call(lambda: service.assess_canary_quality(
            deployment_id, _required_text(payload, "run_id"), passed=payload.get("passed"),
            prompt_digest=_required_text(payload, "prompt_digest"), reason=_required_text(payload, "reason"),
        ))
        return {"recorded": True}

    @app.post("/api/evolution/candidates/{candidate_id}/promote", dependencies=[Depends(mutate)])
    async def promote_evolution_candidate(candidate_id: str, payload: dict[str, Any], request: Request, service=Depends(evolution)):
        return _evolution_call(lambda: service.promote(
            candidate_id, expected_version=_required_int(payload, "expected_version"), idempotency_key=idempotency_key(request),
        ))

    @app.post("/api/evolution/candidates/{candidate_id}/rollback", dependencies=[Depends(mutate)])
    async def rollback_evolution_candidate(candidate_id: str, payload: dict[str, Any], request: Request, service=Depends(evolution)):
        return _evolution_call(lambda: service.rollback(
            candidate_id, expected_version=_required_int(payload, "expected_version"), reason=str(payload.get("reason") or "user rollback"),
            actor=str(payload.get("actor") or "user"), idempotency_key=idempotency_key(request),
        ))

    @app.get("/api/evolution/bundles")
    async def list_evolution_bundles(service=Depends(evolution)):
        return {"bundles": service.list_bundles()}

    @app.get("/api/evolution/history")
    async def list_evolution_history(after_id: int = 0, service=Depends(evolution)):
        return {"events": service.history(after_id)}


def _run_json(run, service) -> dict[str, Any]:
    return {
        "id": run.id,
        "goal_id": run.goal_id,
        "session_id": run.session_id,
        "state": run.state.value,
        "resume_state": run.resume_state.value if run.resume_state else None,
        "current_plan_version_id": run.current_plan_version_id,
        "current_step_id": run.current_step_id,
        "version": run.version,
        "budget": _public_budget(run.budget),
        "pending_approvals": [approval.id for approval in service.pending_approvals(run.id)],
        "approval_details": service.approval_details(run.id),
        "skill_names": list(run.skill_names),
        "source_plan_document_id": run.source_plan_document_id,
        "source_plan_document_version_id": run.source_plan_document_version_id,
        "source_plan_content_hash": run.source_plan_content_hash,
    }


def _agent_run_json(run: dict[str, Any]) -> dict[str, Any]:
    return {key: run.get(key) for key in (
        "id","owner_id","thread_id","objective","mode","status","context_snapshot_id","runtime_bundle_id",
        "coordinator_task_id","budget_units","reserved_budget_units","version","cancel_requested_at","created_at","updated_at","finished_at",
    )}


def _agent_task_json(task: dict[str, Any]) -> dict[str, Any]:
    return {key: task.get(key) for key in (
        "id","agent_run_id","root_task_id","parent_task_id","child_key","role","objective","output_schema","status","priority",
        "join_policy","attempts","max_attempts","lease_epoch","budget_units","result_artifact_id","error_code","cancel_requested_at",
        "cancel_reason","version","created_at","updated_at","finished_at",
    )}


def _goal_call(callback):
    try:
        return callback()
    except GoalProgramNotFound as exc:
        raise HTTPException(status_code=404, detail="goal resource not found") from exc
    except GoalProgramConflict as exc:
        return _goal_conflict(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _goal_conflict(exc: GoalProgramConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc), "current": exc.current})


def _thread_json(thread) -> dict[str, Any]:
    return {
        "id": thread.id,
        "title": thread.title,
        "version": thread.version,
        "active_turn_id": thread.active_turn_id,
        "next_event_seq": thread.next_event_seq,
        "created_at": thread.created_at,
        "updated_at": thread.updated_at,
    }


def _turn_json(turn) -> dict[str, Any]:
    return {
        "id": turn.id,
        "thread_id": turn.thread_id,
        "client_turn_id": turn.client_turn_id,
        "parent_turn_id": turn.parent_turn_id,
        "status": turn.status,
        "policy": turn.policy,
        "content_shape": turn.content_shape,
        "reason_code": turn.reason_code,
        "artifact_kind": turn.artifact_kind,
        "artifact_operation": turn.artifact_operation,
        "artifact_title": turn.artifact_title,
        "goal_action_id": turn.goal_action_id,
        "version": turn.version,
        "skill_names": list(turn.skill_names),
        "materialized_goal_id": turn.materialized_goal_id,
        "materialized_run_id": turn.materialized_run_id,
        "direction_action": turn.direction_action,
        "direction_idempotency_key": turn.direction_idempotency_key,
        "metrics": {
            "queue_wait_ms": turn.queue_wait_ms,
            "context_ms": turn.context_ms,
            "model_ttft_ms": turn.model_ttft_ms,
            "stream_ms": turn.stream_ms,
            "answer_wait_ms": turn.answer_wait_ms,
            "total_ms": turn.total_ms,
            "model_attempt_count": turn.model_attempt_count,
        },
        "created_at": turn.created_at,
        "updated_at": turn.updated_at,
    }


def _ask_json(ask) -> dict[str, Any]:
    return {
        "id": ask.id,
        "turn_id": ask.turn_id,
        "questions": [question.as_dict() for question in ask.questions],
        "status": ask.status,
        "continuation_turn_id": ask.continuation_turn_id,
        "created_at": ask.created_at,
        "answered_at": ask.answered_at,
    }


def _thread_message_json(message) -> dict[str, Any]:
    return {
        "id": message.id,
        "thread_id": message.thread_id,
        "turn_id": message.turn_id,
        "role": message.role,
        "content": message.content,
        "status": message.status,
        "generation": message.generation,
        "content_length": message.content_length,
        "plan_document_version_id": message.plan_document_version_id,
        "presentation": getattr(message, "presentation", "standard"),
        "research_job_id": getattr(message, "research_job_id", None),
        "created_at": message.created_at,
        "completed_at": message.completed_at,
        "total_ms": getattr(message, "total_ms", None),
    }


def _research_job_json(job) -> dict[str, Any]:
    return {
        "id": job.id, "thread_id": job.thread_id, "source_turn_id": job.source_turn_id,
        "schedule_id": job.schedule_id, "retry_of_job_id": job.retry_of_job_id,
        "trigger_kind": job.trigger_kind, "topic": job.topic, "source_scopes": list(job.source_scopes),
        "status": job.status, "phase": job.phase, "attempts": job.attempts,
        "cancel_requested_at": job.cancel_requested_at, "created_at": job.created_at, "updated_at": job.updated_at,
        "title": job.report_title, "source_count": job.source_count, "evidence_count": job.evidence_count,
        "assistant_message_id": job.assistant_message_id, "failure_reason_code": job.failure_reason_code,
        "failure_details": job.failure_details, "traceability": list(job.traceability),
        "missing_requirements": list(job.missing_requirements),
    }


def _schedule_json(item) -> dict[str, Any]:
    return {"id":item.id,"name":item.name,"thread_id":item.thread_id,"topic":item.topic,"source_scopes":list(item.source_scopes),"trigger_type":item.trigger_type,"trigger_time":item.trigger_time,"trigger_weekday":item.trigger_weekday,"interval_hours":item.interval_hours,"timezone":item.timezone,"enabled":item.enabled,"notify_enabled":item.notify_enabled,"next_run_at":item.next_run_at,"last_run_at":item.last_run_at,"last_job_id":item.last_job_id,"created_at":item.created_at,"updated_at":item.updated_at}


def _channel_json(item) -> dict[str, Any]:
    return {"id":item.id,"name":item.name,"channel_type":item.channel_type,"secret_env_name":item.secret_env_name,"enabled":item.enabled,"configured":item.configured}


def _plan_document_json(
    document,
    service,
    *,
    limit: int = DEFAULT_PLAN_HISTORY_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    current = None
    if document.current_version_id:
        try:
            current = _plan_document_version_json(service.get_version(document.current_version_id))
        except KeyError:
            current = None
    versions = service.list_versions(document.id, limit=limit, offset=offset)
    total = service.count_versions(document.id)
    return {
        "id": document.id,
        "thread_id": document.thread_id,
        "title": document.title,
        "current_version_id": document.current_version_id,
        "projected_version_id": document.projected_version_id,
        "file_status": document.file_status,
        "file_path": f"plans/{document.id}/plan.md",
        "created_at": document.created_at,
        "updated_at": document.updated_at,
        "deleted_at": document.deleted_at,
        "current": current,
        "versions": [_plan_document_version_json(item, include_markdown=False) for item in versions],
        "versions_total": total,
        "versions_offset": offset,
        "versions_limit": limit,
        "versions_has_more": offset + len(versions) < total,
    }


def _plan_document_summary_json(document, service) -> dict[str, Any]:
    version = None
    if document.current_version_id:
        try:
            version = service.get_version(document.current_version_id).version
        except KeyError:
            pass
    return {
        "id": document.id,
        "thread_id": document.thread_id,
        "title": document.title,
        "version": version,
        "file_status": document.file_status,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }


def _plan_document_version_json(version, *, include_markdown: bool = True) -> dict[str, Any]:
    payload = {
        "id": version.id,
        "plan_document_id": version.plan_document_id,
        "version": version.version,
        "base_version_id": version.base_version_id,
        "title": version.title,
        "content_hash": version.content_hash,
        "source_turn_id": version.source_turn_id,
        "source_message_id": version.source_message_id,
        "actor": version.actor,
        "change_summary": version.change_summary,
        "status": version.status,
        "created_at": version.created_at,
        "committed_at": version.committed_at,
    }
    if include_markdown:
        payload["markdown"] = version.markdown_content
    return payload


def _plan_history_pagination(limit: int, offset: int) -> tuple[int, int]:
    if isinstance(limit, bool) or limit < 1 or limit > MAX_PLAN_HISTORY_LIMIT:
        raise HTTPException(status_code=422, detail=f"limit must be between 1 and {MAX_PLAN_HISTORY_LIMIT}")
    if isinstance(offset, bool) or offset < 0:
        raise HTTPException(status_code=422, detail="offset must be non-negative")
    return limit, offset


def _current_plan_conflict(service, document_id: str) -> dict[str, Any] | None:
    try:
        document = service.plan_documents.get_document(document_id)
        current = service.plan_documents.current_version(document_id)
    except KeyError:
        return None
    return {
        "id": document.id,
        "version": current.version,
        "version_id": current.id,
        "title": current.title,
        "markdown": current.markdown_content,
        "content_hash": current.content_hash,
        "file_status": document.file_status,
    }


def _plan_projection_failure(service, document_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": "plan file projection failed; retry projection",
            "retry": True,
            "current": _current_plan_conflict(service, document_id),
        },
    )


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{key} is required")
    return value.strip()


def _thread_event_json(event) -> dict[str, Any]:
    return {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "seq": event.seq,
        "thread_id": event.thread_id,
        "turn_id": event.turn_id,
        "type": event.type,
        "occurred_at": event.occurred_at,
        "actor": event.actor,
        "data": event.data,
    }


def _plan_json(plan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "run_id": plan.run_id,
        "goal_id": plan.goal_id,
        "version": plan.version,
        "status": plan.status,
        "summary": plan.summary,
        "source_document_version_id": plan.source_document_version_id,
        "steps": [
            {
                "id": step.id,
                "title": step.title,
                "description": step.description,
                "status": step.status,
                "position": step.position,
            }
            for step in plan.steps
        ],
    }


def _memory_json(record) -> dict[str, Any]:
    return {
        "id": record.id,
        "run_id": record.run_id,
        "kind": record.kind,
        "content": record.content,
        "scope": record.scope,
        "project_id": record.project_id,
        "skill_name": record.skill_name,
        "confidence": record.confidence,
        "status": record.status,
        "version": record.version,
        "evidence_event_ids": list(record.evidence_event_ids),
        "path": record.path,
    }


def _memory_entry_json(item) -> dict[str, Any]:
    return {"id":item.id,"kind":item.kind,"scope_type":item.scope_type,"scope_id":item.scope_id,"status":item.status,"content":item.content,"revision_id":item.revision_id,"revision_no":item.revision_no,"pinned":item.pinned,"importance":item.importance,"sensitivity":item.sensitivity,"evidence_state":item.evidence_state,"evidence_label":_evidence_label(item.evidence_state,len(item.evidence)),"evidence_count":len(item.evidence),"evidence":[_memory_evidence_json(value) for value in item.evidence],"created_at":item.created_at,"updated_at":item.updated_at}


def _memory_proposal_json(item) -> dict[str, Any]:
    return {"id":item.id,"operation":item.operation,"target_entry_id":item.target_entry_id,"base_revision_id":item.base_revision_id,"kind":item.kind,"scope_type":item.scope_type,"scope_id":item.scope_id,"content":item.content,"original_content":item.original_content,"accepted_content":item.accepted_content,"model_confidence":item.confidence,"status":item.status,"accepted_revision_id":item.accepted_revision_id,"reason":item.reason,"evidence_state":item.evidence_state,"evidence_label":_evidence_label(item.evidence_state,len(item.evidence)),"evidence_count":len(item.evidence),"evidence":[_memory_evidence_json(value) for value in item.evidence],"version":item.version,"created_at":item.created_at}


def _memory_evidence_json(item) -> dict[str, Any]:
    return {"source_type":item.source_type,"source_id":item.source_id,"source_label":item.source_label,"excerpt":item.excerpt}


def _evidence_label(state: str, count: int) -> str:
    if state == "VERIFIED": return f"已验证用户依据（{count} 条）" if count else "用户直接确认"
    if state == "INVALID": return "依据已失效"
    return "历史记录，依据未验证"


def _memory_episode_json(item) -> dict[str, Any]:
    return {"id":item.id,"thread_id":item.thread_id,"project_id":item.project_id,"start_message_seq":item.start_message_seq,"end_message_seq":item.end_message_seq,"summary":item.summary,"sensitivity":item.sensitivity,"retrieval_policy":item.retrieval_policy,"status":item.status,"version":item.version,"created_at":item.created_at}


def _memory_version_json(version) -> dict[str, Any]:
    return {
        "path": version.path,
        "version": version.version,
        "content": version.content,
        "content_hash": version.content_hash,
    }


def _public_budget(budget: dict[str, Any]) -> dict[str, Any]:
    return public_budget(budget)


def _event_json(event) -> dict[str, Any]:
    return {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "seq": event.seq,
        "run_id": event.run_id,
        "goal_id": event.goal_id,
        "type": event.type,
        "occurred_at": event.occurred_at,
        "actor": event.actor,
        "correlation": event.correlation,
        "data": event.data,
    }


def _memory_revision_json(version) -> dict[str, Any]:
    return {
        "id": version.id,
        "entry_id": version.entry_id,
        "revision_no": version.revision_no,
        "operation": version.operation,
        "content": version.content,
        "base_revision_id": version.base_revision_id,
        "actor": version.actor,
        "source_refs": list(version.source_refs),
        "reason": version.reason,
        "created_at": version.created_at,
    }


def _evolution_call(callback):
    from .evolution import EvolutionConflict

    try:
        return callback()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="evolution resource not found") from exc
    except EvolutionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
