from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from .ask import AskValidationError
from .events import export_jsonl
from .plan_documents import PlanDocumentConflict, PlanDocumentValidationError


DEFAULT_PLAN_HISTORY_LIMIT = 50
MAX_PLAN_HISTORY_LIMIT = 100


async def _event_stream(service, run_id: str, request: Request, after_seq: int, follow: bool):
    cursor = after_seq
    terminal_states = {"COMPLETED", "FAILED", "CANCELLED"}
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
    terminal_states = {"COMPLETED", "FAILED", "CANCELLED"}
    while True:
        events = service.events.list(thread_id, cursor)
        for event in events:
            cursor = event.seq
            yield f"id: {event.seq}\nevent: conversation\ndata: {json.dumps(_thread_event_json(event), ensure_ascii=False)}\n\n"
        if not follow:
            return
        if await request.is_disconnected():
            return
        thread = service.thread(thread_id)
        with service.db.connection() as connection:
            research_active = connection.execute(
                "SELECT 1 FROM research_jobs WHERE thread_id=? AND status IN ('QUEUED','RUNNING') LIMIT 1",
                (thread_id,),
            ).fetchone() is not None
        if not events and thread.active_turn_id:
            if service.turn(thread.active_turn_id).status in terminal_states and not research_active:
                return
        if not events:
            yield ": keep-alive\n\n"
        await asyncio.sleep(0.05)


def register_routes(app) -> None:
    async def mutate(request: Request) -> None:
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("application/json"):
            raise HTTPException(status_code=415, detail="JSON content type required")
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in {"127.0.0.1", "localhost"}:
                raise HTTPException(status_code=403, detail="local origin required")
        if request.headers.get("x-csrf-token") != request.app.state.csrf_token:
            raise HTTPException(status_code=403, detail="CSRF token required")

    def runtime(request: Request):
        value = getattr(request.app.state, "runtime", None)
        if value is None:
            raise HTTPException(status_code=503, detail="runtime is not configured")
        return value

    def conversation(request: Request):
        service = runtime(request)
        value = getattr(service, "conversation", None)
        if value is None:
            raise HTTPException(status_code=503, detail="conversation runtime is not configured")
        return value

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request) -> dict[str, Any]:
        config = request.app.state.config
        settings = getattr(request.app.state.runtime, "settings", None)
        return {
            "csrf_token": request.app.state.csrf_token,
            "version": config.version,
            "api_key_env": getattr(config, "api_key_env", "AGENT_MODEL_API_KEY"),
            "api_key_configured": getattr(config, "api_key_configured", False),
            "human_mode": settings.get().human_mode if settings else False,
        }

    @app.get("/api/settings")
    async def get_settings(service=Depends(runtime)) -> dict[str, Any]:
        settings = getattr(service, "settings", None)
        return {"human_mode": settings.get().human_mode if settings else False}

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

    @app.get("/api/threads/{thread_id}")
    async def get_thread(thread_id: str, service=Depends(conversation)) -> dict[str, Any]:
        try:
            payload = _thread_json(service.thread(thread_id))
            payload["turns"] = [_turn_json(turn) for turn in service.turns(thread_id)]
            return payload
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="thread not found") from exc

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
    async def list_skills(request: Request) -> dict[str, Any]:
        service = runtime(request)
        return {"skills": [skill.public_view() for skill in service.skills.list()]}

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

    @app.get("/api/runs/{run_id}/export")
    async def export_run(run_id: str, request: Request, mode: str = "redacted") -> StreamingResponse:
        service = runtime(request)
        if mode not in {"redacted", "full"}:
            raise HTTPException(status_code=400, detail="invalid export mode")
        body = export_jsonl(service.events.list(run_id), mode=mode, workspace=service.db.workspace)
        return StreamingResponse(iter([body]), media_type="application/x-ndjson")

    @app.get("/api/memories")
    async def list_memories(request: Request) -> dict[str, Any]:
        service = runtime(request)
        store = getattr(service, "memory_store", None)
        if store is not None:
            return {"entries": [_memory_entry_json(item) for item in store.list_entries()], "proposals": [_memory_proposal_json(item) for item in store.list_proposals()], "episodes": [_memory_episode_json(item) for item in store.list_episodes()]}
        return {"memories": [_memory_json(record) for record in service.memory.all_records()]}

    @app.post("/api/memory/entries", status_code=201, dependencies=[Depends(mutate)])
    async def create_memory_entry(payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        try:
            item=service.memory_store.remember("local-user",payload.get("kind","fact"),payload.get("scope_type","user"),payload.get("scope_id",""),payload.get("content",""),payload.get("idempotency_key") or f"api:{uuid.uuid4().hex}",payload.get("source_refs",[]),pinned=payload.get("pinned",False),importance=float(payload.get("importance",.5)))
            return _memory_entry_json(item)
        except (ValueError,KeyError) as exc:raise HTTPException(status_code=422,detail=str(exc)) from exc

    @app.patch("/api/memory/entries/{entry_id}", dependencies=[Depends(mutate)])
    async def edit_memory_entry(entry_id:str,payload:dict[str,Any],service=Depends(runtime))->dict[str,Any]:
        try:return _memory_entry_json(service.memory_store.edit(entry_id,"local-user",payload.get("content",""),payload.get("base_revision_id","")))
        except KeyError as exc:raise HTTPException(status_code=404,detail="memory not found") from exc
        except ValueError as exc:raise HTTPException(status_code=409,detail=str(exc)) from exc

    @app.post("/api/memory/entries/{entry_id}/archive", dependencies=[Depends(mutate)])
    async def archive_memory_entry(entry_id:str,service=Depends(runtime))->dict[str,Any]:
        try:return _memory_entry_json(service.memory_store.set_status(entry_id,"local-user","ARCHIVED"))
        except KeyError as exc:raise HTTPException(status_code=404,detail="memory not found") from exc

    @app.delete("/api/memory/entries/{entry_id}",status_code=204,dependencies=[Depends(mutate)])
    async def purge_memory_entry(entry_id:str,service=Depends(runtime))->Response:
        try:service.memory_store.purge(entry_id,"local-user");return Response(status_code=204)
        except KeyError as exc:raise HTTPException(status_code=404,detail="memory not found") from exc

    @app.post("/api/memory/proposals/{proposal_id}/decision",dependencies=[Depends(mutate)])
    async def decide_memory_proposal(proposal_id:str,payload:dict[str,Any],service=Depends(runtime))->dict[str,Any]:
        try:return _memory_proposal_json(service.memory_store.decide_proposal(proposal_id,"local-user",payload.get("accept") is True,payload.get("idempotency_key") or f"decision:{uuid.uuid4().hex}"))
        except KeyError as exc:raise HTTPException(status_code=404,detail="proposal not found") from exc
        except ValueError as exc:raise HTTPException(status_code=409,detail=str(exc)) from exc

    @app.patch("/api/memory/episodes/{episode_id}",dependencies=[Depends(mutate)])
    async def edit_memory_episode(episode_id:str,payload:dict[str,Any],service=Depends(runtime))->dict[str,Any]:
        try:return _memory_episode_json(service.memory_store.edit_episode(episode_id,"local-user",payload.get("summary",""),payload.get("retrieval_policy")))
        except KeyError as exc:raise HTTPException(status_code=404,detail="episode not found") from exc
        except ValueError as exc:raise HTTPException(status_code=422,detail=str(exc)) from exc

    @app.delete("/api/memory/episodes/{episode_id}",status_code=204,dependencies=[Depends(mutate)])
    async def delete_memory_episode(episode_id:str,service=Depends(runtime))->Response:
        try:service.memory_store.delete_episode(episode_id,"local-user");return Response(status_code=204)
        except KeyError as exc:raise HTTPException(status_code=404,detail="episode not found") from exc

    @app.get("/api/memories/{memory_id}/versions")
    async def list_memory_versions(memory_id: str, request: Request) -> dict[str, Any]:
        service = runtime(request)
        return {"versions": [_memory_version_json(version) for version in service.memory.versions(memory_id)]}

    @app.post("/api/memories", dependencies=[Depends(mutate)])
    async def create_memory(payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
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
    async def edit_memory(memory_id: str, payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        return _memory_json(service.memory.edit(memory_id, payload["content"]))

    @app.post("/api/memories/{memory_id}/confirm", dependencies=[Depends(mutate)])
    async def confirm_memory(memory_id: str, payload: dict[str, Any] | None = None, service=Depends(runtime)) -> dict[str, Any]:
        return _memory_json(service.memory.confirm(memory_id, (payload or {}).get("content")))

    @app.post("/api/memories/{memory_id}/reject", dependencies=[Depends(mutate)])
    async def reject_memory(memory_id: str, service=Depends(runtime)) -> dict[str, Any]:
        return _memory_json(service.memory.reject(memory_id))

    @app.post("/api/memories/{memory_id}/disable", dependencies=[Depends(mutate)])
    async def disable_memory(memory_id: str, service=Depends(runtime)) -> dict[str, Any]:
        return _memory_json(service.memory.disable(memory_id))

    @app.post("/api/memories/{memory_id}/rollback", dependencies=[Depends(mutate)])
    async def rollback_memory(memory_id: str, payload: dict[str, Any], service=Depends(runtime)) -> dict[str, Any]:
        return _memory_json(service.memory.rollback(memory_id, int(payload["version"])))


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
        "skill_names": list(run.skill_names),
        "source_plan_document_id": run.source_plan_document_id,
        "source_plan_document_version_id": run.source_plan_document_version_id,
        "source_plan_content_hash": run.source_plan_content_hash,
    }


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
        "version": turn.version,
        "skill_names": list(turn.skill_names),
        "materialized_goal_id": turn.materialized_goal_id,
        "materialized_run_id": turn.materialized_run_id,
        "direction_action": turn.direction_action,
        "direction_idempotency_key": turn.direction_idempotency_key,
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
    }


def _research_job_json(job) -> dict[str, Any]:
    return {
        "id": job.id, "thread_id": job.thread_id, "source_turn_id": job.source_turn_id,
        "schedule_id": job.schedule_id, "retry_of_job_id": job.retry_of_job_id,
        "trigger_kind": job.trigger_kind, "topic": job.topic, "source_scopes": list(job.source_scopes),
        "status": job.status, "phase": job.phase, "attempts": job.attempts,
        "cancel_requested_at": job.cancel_requested_at, "created_at": job.created_at, "updated_at": job.updated_at,
        "title": job.report_title, "source_count": job.source_count, "evidence_count": job.evidence_count,
        "assistant_message_id": job.assistant_message_id,
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
    return {"id":item.id,"kind":item.kind,"scope_type":item.scope_type,"scope_id":item.scope_id,"status":item.status,"content":item.content,"revision_id":item.revision_id,"revision_no":item.revision_no,"pinned":item.pinned,"importance":item.importance,"sensitivity":item.sensitivity,"created_at":item.created_at,"updated_at":item.updated_at}


def _memory_proposal_json(item) -> dict[str, Any]:
    return {"id":item.id,"operation":item.operation,"target_entry_id":item.target_entry_id,"base_revision_id":item.base_revision_id,"kind":item.kind,"scope_type":item.scope_type,"scope_id":item.scope_id,"content":item.content,"confidence":item.confidence,"status":item.status,"accepted_revision_id":item.accepted_revision_id,"reason":item.reason,"created_at":item.created_at}


def _memory_episode_json(item) -> dict[str, Any]:
    return {"id":item.id,"thread_id":item.thread_id,"project_id":item.project_id,"start_message_seq":item.start_message_seq,"end_message_seq":item.end_message_seq,"summary":item.summary,"sensitivity":item.sensitivity,"retrieval_policy":item.retrieval_policy,"status":item.status,"created_at":item.created_at}


def _memory_version_json(version) -> dict[str, Any]:
    return {
        "path": version.path,
        "version": version.version,
        "content": version.content,
        "content_hash": version.content_hash,
    }


def _public_budget(budget: dict[str, Any]) -> dict[str, Any]:
    visible = {key: value for key, value in budget.items() if key != "identical_actions"}
    if "identical_actions" in budget:
        visible["identical_action_count"] = len(budget["identical_actions"])
    return visible


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
