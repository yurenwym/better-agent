from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from .events import export_jsonl


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
        if not events and thread.active_turn_id:
            if service.turn(thread.active_turn_id).status in terminal_states:
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
        return {
            "csrf_token": request.app.state.csrf_token,
            "version": config.version,
            "api_key_env": getattr(config, "api_key_env", "AGENT_MODEL_API_KEY"),
            "api_key_configured": getattr(config, "api_key_configured", False),
        }

    @app.post("/api/threads", status_code=201, dependencies=[Depends(mutate)])
    async def create_thread(payload: dict[str, Any], service=Depends(conversation)) -> dict[str, Any]:
        title = payload.get("title", "新的对话")
        if title is not None and not isinstance(title, str):
            raise HTTPException(status_code=422, detail="title must be a string")
        return _thread_json(service.create_thread(title or "新的对话"))

    @app.get("/api/threads/{thread_id}")
    async def get_thread(thread_id: str, service=Depends(conversation)) -> dict[str, Any]:
        try:
            return _thread_json(service.thread(thread_id))
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
        try:
            expected_version = int(payload["expected_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="expected_version is required") from exc
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

    @app.post("/api/turns/{turn_id}/cancel", dependencies=[Depends(mutate)])
    async def cancel_turn(turn_id: str, service=Depends(conversation)) -> dict[str, Any]:
        try:
            return _turn_json(service.cancel_turn(turn_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="turn not found") from exc

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
        return {"memories": [_memory_json(record) for record in service.memory.all_records()]}

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
        "version": turn.version,
        "materialized_goal_id": turn.materialized_goal_id,
        "materialized_run_id": turn.materialized_run_id,
        "direction_action": turn.direction_action,
        "direction_idempotency_key": turn.direction_idempotency_key,
        "created_at": turn.created_at,
        "updated_at": turn.updated_at,
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
        "created_at": message.created_at,
        "completed_at": message.completed_at,
    }


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
