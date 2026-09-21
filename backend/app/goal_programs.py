from __future__ import annotations

import hashlib
import json
import uuid
import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database
from .goal_program_compiler import GoalCompilationError, GoalCompiler, validate_program_structure
from .goal_calendar import MAX_PROGRAM_DAYS, calendar_days, schedule_constraints
from .token_budget import DEFAULT_TOKEN_COUNTER


OWNER_ID = "local-user"


class GoalProgramConflict(ValueError):
    """A business conflict with a stable, machine-readable reason code.

    ``code`` is what tool adapters map to their public error codes; the message
    stays user/operator readable and may change without breaking callers.
    """

    def __init__(
        self,
        message: str,
        current: dict[str, Any] | None = None,
        *,
        code: str = "GOAL_PROGRAM_CONFLICT",
    ) -> None:
        super().__init__(message)
        self.current = current
        self.code = code


class GoalProgramNotFound(KeyError):
    pass


class GoalProgramService:
    def __init__(self, db: Database, compiler: GoalCompiler, *, plan_documents=None, conversation=None,
                 compile_timeout_seconds: float = 120) -> None:
        self.db = db
        self.compiler = compiler
        self.plan_documents = plan_documents
        self.conversation = conversation
        self.compile_timeout_seconds = compile_timeout_seconds
        self.reviews = None
        self.expert_advisor = None
        self.automatic_expert_advice = True
        self._recover_interrupted_compilations()

    def _recover_interrupted_compilations(self) -> None:
        now = _now()
        with self.db.transaction() as connection:
            rows = connection.execute("SELECT id FROM goal_programs WHERE status='DRAFT' AND compile_status='COMPILING'").fetchall()
            for row in rows:
                connection.execute("UPDATE goal_programs SET compile_status='FAILED',compile_error_code='COMPILE_INTERRUPTED',version=version+1,updated_at=? WHERE id=?", (now, row["id"]))
                receipts = connection.execute(
                    "SELECT id,response_json FROM goal_command_receipts "
                    "WHERE aggregate_type='program' AND aggregate_id=?",
                    (row["id"],),
                ).fetchall()
                completed = _json(self._program_json(connection, row["id"], OWNER_ID))
                for receipt in receipts:
                    if json.loads(receipt["response_json"]).get("_pending_program_id") == row["id"]:
                        connection.execute(
                            "UPDATE goal_command_receipts SET response_json=? WHERE id=?",
                            (completed, receipt["id"]),
                        )
                self._event(connection,row["id"],None,"program.compile_failed","system",{"reason_code":"COMPILE_INTERRUPTED"})

    async def preview(
        self, plan_document_id: str, *, start_date: str, timezone_name: str,
        daily_minutes: int, requested_end_date: str | None, idempotency_key: str,
        owner_id: str = OWNER_ID, constraints: dict[str, Any] | None = None,
        expected_source_version_id: str | None = None,
        root_budget_id: str | None = None,
        runtime_bundle_id: str | None = None,
    ) -> dict[str, Any]:
        start, end, defaulted = _program_dates(start_date, requested_end_date)
        _timezone(timezone_name)
        constraints = schedule_constraints(constraints)
        calendar = calendar_days(start.isoformat(), end.isoformat(), constraints)
        if not calendar["available_dates"]:
            raise ValueError("执行周期内没有可用学习日，请调整日期或休息日")
        if isinstance(daily_minutes, bool) or not isinstance(daily_minutes, int) or not 5 <= daily_minutes <= 1440:
            raise ValueError("daily_minutes must be between 5 and 1440")
        request = {"plan_document_id": plan_document_id, "start_date": start.isoformat(), "end_date": end.isoformat(),
                   "timezone": timezone_name, "daily_minutes": daily_minutes, "requested_end_date": requested_end_date,
                   "schedule_constraints": constraints, "calendar": calendar,
                   "expected_source_version_id": expected_source_version_id}
        request_hash = _hash(request)
        cached = self._receipt(owner_id, idempotency_key, request_hash)
        if cached is not None:
            if cached.get("_pending_program_id"):
                return await self._wait_for_preview(owner_id, idempotency_key, request_hash)
            if cached.get("compile_status") == "FAILED":
                raise GoalCompilationError(cached.get("compile_error_code") or "COMPILE_FAILED", "goal compilation failed")
            return cached
        pending_program_id = None
        completed_receipt = None
        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT request_hash,response_json FROM goal_command_receipts WHERE owner_id=? AND idempotency_key=?",
                (owner_id,idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"]!=request_hash:
                    raise GoalProgramConflict("idempotency key reused with different payload")
                completed_receipt=json.loads(existing["response_json"])
                pending_program_id=completed_receipt.get("_pending_program_id")
            if existing is not None:
                source = None
            else:
                source = connection.execute(
                    "SELECT d.id document_id,d.thread_id,d.current_version_id,v.id version_id,v.content_hash,v.markdown_content,v.status,"
                    "t.owner_id FROM plan_documents d JOIN threads t ON t.id=d.thread_id "
                    "JOIN plan_document_versions v ON v.id=d.current_version_id "
                    "WHERE d.id=? AND d.deleted_at IS NULL AND t.owner_id=?",
                    (plan_document_id, owner_id),
                ).fetchone()
            if source is None:
                if existing is not None:
                    pass
                else:
                    raise GoalProgramNotFound(plan_document_id)
            elif source["status"] != "committed":
                raise GoalProgramConflict("source plan version is not committed", code="VERSION_CONFLICT")
            # Compare the expected source version inside the same transaction
            # that read it, so a user reviewing v1 cannot have the compile bind
            # a v2 that landed in between.
            if source is not None and expected_source_version_id is not None and source["version_id"] != expected_source_version_id:
                raise GoalProgramConflict(
                    "source plan version changed since the user reviewed it",
                    code="VERSION_CONFLICT",
                )
            if existing is not None:
                program_id = pending_program_id
            else:
                program_id = f"program_{uuid.uuid4().hex}"
                now = _now()
                connection.execute(
                    "INSERT INTO goal_programs(id,owner_id,source_thread_id,source_plan_document_id,source_plan_document_version_id,"
                    "source_plan_content_hash,status,compile_status,timezone,start_date,end_date,daily_minutes,created_at,updated_at,schedule_constraints_json) "
                    "VALUES (?,?,?,?,?,?,'DRAFT','COMPILING',?,?,?,?,?,?,?)",
                    (program_id, owner_id, source["thread_id"], plan_document_id, source["version_id"], source["content_hash"],
                     timezone_name, start.isoformat(), end.isoformat(), daily_minutes, now, now, _json(constraints)),
                )
                self._save_receipt(connection,owner_id,"program",program_id,"preview",idempotency_key,request_hash,{"_pending_program_id":program_id})
        if completed_receipt is not None and pending_program_id is None:
            if completed_receipt.get("compile_status")=="FAILED":
                raise GoalCompilationError(completed_receipt.get("compile_error_code") or "COMPILE_FAILED","goal compilation failed")
            return completed_receipt
        if pending_program_id is not None:
            return await self._wait_for_preview(owner_id,idempotency_key,request_hash)
        compile_request = {**request, "owner_id": owner_id, "source_thread_id": source["thread_id"],
                           "source_plan_document_version_id": source["version_id"], "source_plan_content_hash": source["content_hash"],
                           "defaulted_end_date": defaulted, "root_budget_id": root_budget_id,
                           "runtime_bundle_id": runtime_bundle_id}
        try:
            structure = await self._compile(program_id, source["markdown_content"], compile_request)
            structure = validate_program_structure(structure, start.isoformat(), end.isoformat(), daily_minutes, constraints=constraints)
        except asyncio.CancelledError:
            with self.db.transaction() as connection:
                connection.execute("UPDATE goal_programs SET compile_status='FAILED',compile_error_code='COMPILE_CANCELLED',version=version+1,updated_at=? WHERE id=?", (_now(), program_id))
                self._event(connection, program_id, None, "program.compile_failed", "compiler", {"reason_code": "COMPILE_CANCELLED"})
                response = self._program_json(connection, program_id, owner_id)
                self._complete_reserved_receipt(connection, owner_id, idempotency_key, request_hash, response)
            raise
        except GoalCompilationError as exc:
            with self.db.transaction() as connection:
                connection.execute("UPDATE goal_programs SET compile_status='FAILED',compile_error_code=?,version=version+1,updated_at=? WHERE id=?",
                                   (exc.code, _now(), program_id))
                self._event(connection, program_id, None, "program.compile_failed", "compiler", {"reason_code": exc.code})
                response = self._program_json(connection, program_id, owner_id)
                self._complete_reserved_receipt(connection, owner_id, idempotency_key, request_hash, response)
            raise
        with self.db.transaction() as connection:
            self._assert_source(connection, program_id, owner_id)
            version_id = f"programv_{uuid.uuid4().hex}"
            now = _now()
            connection.execute(
                "INSERT INTO goal_program_versions(id,program_id,version,base_version_id,source_plan_document_version_id,structure_json,change_summary,actor,created_at) "
                "VALUES (?,?,1,NULL,?,?,?,'compiler',?)",
                (version_id, program_id, source["version_id"], _json(structure), "Initial compiled execution", now),
            )
            connection.execute(
                "UPDATE goal_programs SET objective_title=?,objective_summary=?,compile_status='READY',compile_error_code=NULL,"
                "current_program_version_id=?,version=version+1,updated_at=? WHERE id=? AND compile_status='COMPILING'",
                (structure["objective_title"], structure["objective_summary"], version_id, now, program_id),
            )
            self._event(connection, program_id, None, "program.compiled", "compiler", {"program_version_id": version_id})
            response = self._program_json(connection, program_id, owner_id)
            self._complete_reserved_receipt(connection, owner_id, idempotency_key, request_hash, response)
        return response

    async def _wait_for_preview(self, owner_id: str, key: str, request_hash: str) -> dict[str, Any]:
        for _ in range(1500):
            await asyncio.sleep(.02)
            cached=self._receipt(owner_id,key,request_hash)
            if cached is not None and not cached.get("_pending_program_id"):
                if cached.get("compile_status")=="FAILED":
                    raise GoalCompilationError(cached.get("compile_error_code") or "COMPILE_FAILED","goal compilation failed")
                return cached
        raise GoalProgramConflict("preview is still compiling")

    def _complete_reserved_receipt(self,connection,owner_id,key,request_hash,response):
        changed=connection.execute(
            "UPDATE goal_command_receipts SET response_json=? WHERE owner_id=? AND idempotency_key=? AND request_hash=?",
            (_json(response),owner_id,key,request_hash),
        ).rowcount
        if changed!=1:raise GoalProgramConflict("preview idempotency reservation was lost")

    async def retry_compile(self, program_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        request_hash = _hash({"program_id": program_id, "expected_version": expected_version})
        cached = self._receipt(owner_id, idempotency_key, request_hash)
        if cached is not None:
            if cached.get("compile_status") == "FAILED":
                raise GoalCompilationError(cached.get("compile_error_code") or "COMPILE_FAILED", "goal compilation failed")
            return cached
        with self.db.transaction() as connection:
            row = self._program_row(connection, program_id, owner_id)
            if row["status"] != "DRAFT" or row["compile_status"] != "FAILED" or row["version"] != expected_version:
                raise GoalProgramConflict("program cannot be recompiled", self._program_json(connection, program_id, owner_id))
            source = self._assert_source(connection, program_id, owner_id)
            changed = connection.execute("UPDATE goal_programs SET compile_status='COMPILING',compile_error_code=NULL,version=version+1,updated_at=? WHERE id=? AND version=?",
                                         (_now(), program_id, expected_version)).rowcount
            if changed != 1:
                raise GoalProgramConflict("program version conflict", self._program_json(connection, program_id, owner_id))
            compiling_version = expected_version + 1
        request = {"owner_id": owner_id, "source_thread_id": row["source_thread_id"], "plan_document_id": row["source_plan_document_id"],
                   "source_plan_document_version_id": row["source_plan_document_version_id"], "source_plan_content_hash": row["source_plan_content_hash"],
                   "start_date": row["start_date"], "end_date": row["end_date"], "timezone": row["timezone"],
                   "daily_minutes": row["daily_minutes"], "requested_end_date": row["end_date"], "defaulted_end_date": False,
                   "schedule_constraints": json.loads(row["schedule_constraints_json"])}
        request["calendar"] = calendar_days(row["start_date"], row["end_date"], request["schedule_constraints"])
        try:
            structure = validate_program_structure(await self._compile(program_id, source["markdown_content"], request), row["start_date"], row["end_date"], row["daily_minutes"], constraints=request["schedule_constraints"])
        except asyncio.CancelledError:
            with self.db.transaction() as connection:
                connection.execute("UPDATE goal_programs SET compile_status='FAILED',compile_error_code='COMPILE_CANCELLED',version=version+1,updated_at=? WHERE id=? AND version=?",
                                   (_now(), program_id, compiling_version))
                self._event(connection, program_id, None, "program.compile_failed", "compiler", {"reason_code": "COMPILE_CANCELLED"})
                response = self._program_json(connection, program_id, owner_id)
                self._save_receipt(connection, owner_id, "program", program_id, "compile-retry", idempotency_key, request_hash, response)
            raise
        except GoalCompilationError as exc:
            with self.db.transaction() as connection:
                connection.execute("UPDATE goal_programs SET compile_status='FAILED',compile_error_code=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                                   (exc.code, _now(), program_id, compiling_version))
                self._event(connection, program_id, None, "program.compile_failed", "compiler", {"reason_code": exc.code})
                response = self._program_json(connection, program_id, owner_id)
                self._save_receipt(connection, owner_id, "program", program_id, "compile-retry", idempotency_key, request_hash, response)
            raise
        with self.db.transaction() as connection:
            row = self._program_row(connection, program_id, owner_id)
            if row["version"] != compiling_version or row["compile_status"] != "COMPILING":
                raise GoalProgramConflict("program version conflict", self._program_json(connection, program_id, owner_id))
            existing = connection.execute("SELECT id FROM goal_program_versions WHERE program_id=? AND version=1", (program_id,)).fetchone()
            version_id = existing["id"] if existing else f"programv_{uuid.uuid4().hex}"
            if existing is None:
                connection.execute("INSERT INTO goal_program_versions(id,program_id,version,source_plan_document_version_id,structure_json,change_summary,actor,created_at) VALUES (?,?,1,?,?,?,'compiler',?)",
                                   (version_id, program_id, row["source_plan_document_version_id"], _json(structure), "Initial compiled execution", _now()))
            connection.execute("UPDATE goal_programs SET objective_title=?,objective_summary=?,compile_status='READY',current_program_version_id=?,version=version+1,updated_at=? WHERE id=?",
                               (structure["objective_title"], structure["objective_summary"], version_id, _now(), program_id))
            self._event(connection, program_id, None, "program.compiled", "compiler", {"program_version_id": version_id})
            response = self._program_json(connection, program_id, owner_id)
            self._save_receipt(connection, owner_id, "program", program_id, "compile-retry", idempotency_key, request_hash, response)
        return response

    async def _compile(self, program_id: str, source_markdown: str, request: dict[str, Any]) -> dict[str, Any]:
        try:
            context = self._model_context(
                program_id, "planner", "compile_goal_program",
                root_budget_id=request.get("root_budget_id"),
                runtime_bundle_id=request.get("runtime_bundle_id"),
            )
            memory_constraint = None
            if getattr(self, "learning", None) is not None:
                with self.db.connection() as connection:
                    scope = connection.execute("SELECT owner_id,project_id FROM threads WHERE id=?", (context.thread_id,)).fetchone()
                if scope is not None:
                    memory_constraint = self.learning.planning_constraints(scope["owner_id"], scope["project_id"], program_id=program_id, turn_id=context.turn_id)
                    from dataclasses import replace
                    base_bundle_id = context.runtime_bundle_id
                    if base_bundle_id is None:
                        from .behavior import BehaviorBundleService
                        base_bundle_id = BehaviorBundleService(self.db).active("stable").id
                    context = replace(context, runtime_bundle_id=self.learning.resolve_task_policy(scope["owner_id"], scope["project_id"], base_bundle_id))
            if context.runtime_bundle_id:
                from .behavior import BehaviorBundleService
                from .task_policy import planning_request
                policy = BehaviorBundleService(self.db).get(context.runtime_bundle_id).manifest.get("task_policy")
                if policy is not None:
                    request = planning_request(request, policy)
            if memory_constraint is not None:
                from .task_policy import planning_request
                policy = {**request.get("task_policy", {"schema_version": 1})}
                policy["action_max_minutes"] = min(policy.get("action_max_minutes", 180), memory_constraint["value"])
                request = planning_request(request, policy)
                request["memory_revision_ids"] = [memory_constraint["revision_id"]]
            if self.expert_advisor is not None and self.automatic_expert_advice:
                advice = await self.expert_advisor.advise(
                    purpose="plan", source_id=str(request["source_plan_document_version_id"]),
                    objective="审阅计划并提出可执行性、风险和遗漏建议",
                    context={"request": request, "plan_markdown": source_markdown}, roles=("planner", "critic"),
                    thread_id=context.thread_id, runtime_bundle_id=context.runtime_bundle_id,
                    root_budget_id=context.root_budget_id,
                )
                if advice is not None:
                    request = {**request, "expert_advice": advice}
            # Revalidate after advisory awaits, immediately before dispatch.
            if getattr(self, "learning", None) is not None and scope is not None:
                resolved = self.learning.resolve_task_policy(scope["owner_id"], scope["project_id"], base_bundle_id)
                if resolved != context.runtime_bundle_id:
                    raise GoalCompilationError("TASK_POLICY_REVOKED", "task policy changed before dispatch")
            if memory_constraint is not None:
                latest = self.learning.planning_constraints(scope["owner_id"], scope["project_id"], program_id=program_id, turn_id=context.turn_id)
                if latest != memory_constraint:
                    raise GoalCompilationError("MEMORY_REVOKED", "planning constraint changed before dispatch")
                with self.db.transaction() as connection:
                    self._event(connection, program_id, None, "memory.context_included", "compiler", {
                        "revision_ids": request["memory_revision_ids"], "consumer": "compile_goal_program",
                        "action_max_minutes": request["action_max_minutes"],
                    })
            result = await asyncio.wait_for(
                self._call_model(context, self.compiler.compile(source_markdown, request)),
                timeout=self.compile_timeout_seconds,
            )
            if "task_policy" in request:
                from .task_policy import validate_planned_actions
                nominal_minutes = {item["logical_key"]: item["estimated_minutes"] for item in result["actions"]}
                result = validate_planned_actions(result, request)
                with self.db.transaction() as connection:
                    self._event(connection, program_id, None, "task_policy.executed", "compiler", {
                        "bundle_id": context.runtime_bundle_id, "policy": request["task_policy"],
                        "action_minutes": [item["estimated_minutes"] for item in result["actions"]],
                        "nominal_minutes": nominal_minutes,
                        "confounded": memory_constraint is not None,
                    })
            if memory_constraint is not None and self.learning.planning_constraints(scope["owner_id"], scope["project_id"], program_id=program_id, turn_id=context.turn_id) != memory_constraint:
                raise GoalCompilationError("MEMORY_REVOKED", "planning constraint changed during generation; result discarded")
            if getattr(self, "learning", None) is not None and scope is not None and self.learning.resolve_task_policy(scope["owner_id"], scope["project_id"], base_bundle_id) != context.runtime_bundle_id:
                raise GoalCompilationError("TASK_POLICY_REVOKED", "task policy changed during generation; result discarded")
            return result
        except asyncio.TimeoutError as exc:
            raise GoalCompilationError("COMPILE_TIMEOUT", "goal compilation timed out", temporary=True) from exc

    def activate(self, program_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID,
                 expected_snapshot_hash: str | None = None) -> dict[str, Any]:
        def activate_checked(connection, row):
            if expected_snapshot_hash is not None:
                current = self._program_json(connection, program_id, owner_id)
                if self.activation_snapshot_hash(current) != expected_snapshot_hash:
                    raise GoalProgramConflict("execution preview changed", code="VERSION_CONFLICT")
            self._activate(connection, row, expected_version)
        return self._command(program_id, owner_id, "activate", idempotency_key, {"expected_version": expected_version},
                             activate_checked)

    @staticmethod
    def activation_snapshot_hash(program: dict[str, Any]) -> str:
        keys = ("id", "version", "current_program_version_id", "source_plan_document_version_id",
                "source_plan_content_hash", "structure", "start_date", "end_date", "timezone", "daily_minutes", "schedule_constraints")
        return _hash({key: program.get(key) for key in keys})

    def activation_receipt(self, program_id: str, *, expected_version: int, idempotency_key: str,
                           owner_id: str) -> dict[str, Any] | None:
        return self._receipt(owner_id, idempotency_key, _hash({"expected_version": expected_version}),
                             identity=("program", program_id, "activate"))

    # Preview generation is not user consent. Runtime approval binds this
    # displayed snapshot; activate verifies its hash inside the transaction.
    PREVIEW_SNAPSHOT_EVENT = "goal_tool.preview_generated"

    def record_preview_snapshot(
        self, program_id: str, snapshot_hash: str, *,
        source_version_id: str, source_content_hash: str, owner_id: str = OWNER_ID,
    ) -> None:
        with self.db.transaction() as connection:
            row = self._program_row(connection, program_id, owner_id)
            program = self._program_json(connection, program_id, owner_id)
            self._event(connection, program_id, None, self.PREVIEW_SNAPSHOT_EVENT, "runtime", {
                "snapshot_hash": snapshot_hash,
                "program_version": int(row["version"]),
                "source_version_id": source_version_id,
                "source_content_hash": source_content_hash,
                "preview": {key: program.get(key) for key in (
                    "objective_title", "start_date", "end_date", "timezone", "daily_minutes", "structure")},
            })

    def preview_snapshot(self, program_id: str, owner_id: str = OWNER_ID) -> dict[str, Any] | None:
        with self.db.connection() as connection:
            self._program_row(connection, program_id, owner_id)
            event = connection.execute(
                "SELECT data_json,occurred_at FROM goal_program_events WHERE program_id=? AND type=? ORDER BY seq DESC LIMIT 1",
                (program_id, self.PREVIEW_SNAPSHOT_EVENT),
            ).fetchone()
        if event is None:
            return None
        data = json.loads(event["data_json"])
        return {
            "snapshot_hash": data.get("snapshot_hash"),
            "program_version": data.get("program_version"),
            "source_version_id": data.get("source_version_id"),
            "source_content_hash": data.get("source_content_hash"),
            "occurred_at": event["occurred_at"],
            "preview": data.get("preview"),
        }

    def get(self, program_id: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        with self.db.connection() as connection:
            return self._program_json(connection, program_id, owner_id)

    def get_action_context(self, action_id: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        with self.db.connection() as connection:
            action, program = self._owned_action(connection, action_id, owner_id)
            return {"action": self._action_json(action), "program": self._program_summary(program)}

    def list(self, owner_id: str = OWNER_ID, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute("SELECT id FROM goal_programs WHERE owner_id=? " + ("" if include_deleted else "AND deleted_at IS NULL ") + "ORDER BY updated_at DESC,id", (owner_id,)).fetchall()
            return [self._program_json(connection, row["id"], owner_id) for row in rows]

    def list_summaries(
        self, owner_id: str = OWNER_ID, *, offset: int = 0, limit: int = 10,
        include_deleted: bool = False,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Lightweight, pageable goal list for tool consumption.

        Unlike :meth:`list` this does not expand every program's structure and
        actions; progress is aggregated for the page's programs only.
        """
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        with self.db.connection() as connection:
            clause = "" if include_deleted else "AND deleted_at IS NULL "
            scope_params = ()
            if project_id is not None:
                clause += "AND source_thread_id IN (SELECT id FROM threads WHERE project_id=? AND owner_id=? AND deleted_at IS NULL) "
                scope_params = (project_id, owner_id)
            rows = connection.execute(
                "SELECT * FROM goal_programs WHERE owner_id=? " + clause
                + "ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
                (owner_id, *scope_params, limit + 1, offset),
            ).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            ids = [row["id"] for row in page]
            progress: dict[str, dict[str, Any]] = {}
            if ids:
                placeholders = ",".join("?" for _ in ids)
                aggregates = connection.execute(
                    f"SELECT program_id,status,required FROM goal_actions WHERE program_id IN ({placeholders})",
                    tuple(ids),
                ).fetchall()
                grouped: dict[str, list[Any]] = {}
                for item in aggregates:
                    grouped.setdefault(item["program_id"], []).append(item)
                for program_id, items in grouped.items():
                    eligible = [item for item in items if item["status"] not in {"DEFERRED", "CANCELLED"}]
                    required = [item for item in eligible if item["required"]]
                    completed = sum(1 for item in required if item["status"] == "COMPLETED")
                    progress[program_id] = {
                        "required_completed": completed,
                        "required_total": len(required),
                        "completion_rate": completed / len(required) if required else 1.0,
                        "completion_ready": completed == len(required),
                        "optional_completed": sum(1 for item in eligible if not item["required"] and item["status"] == "COMPLETED"),
                    }
            items = [
                {
                    **self._program_summary(row),
                    "compile_status": row["compile_status"],
                    "progress": progress.get(row["id"], {
                        "required_completed": 0, "required_total": 0, "completion_rate": 1.0,
                        "completion_ready": True, "optional_completed": 0,
                    }),
                }
                for row in page
            ]
        next_offset = offset + limit
        return {
            "items": items,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
        }

    def today(self, owner_id: str = OWNER_ID, explicit_date: str | None = None) -> dict[str, Any]:
        if explicit_date is not None:
            date.fromisoformat(explicit_date)
        with self.db.connection() as connection:
            rows = connection.execute("SELECT * FROM goal_programs WHERE owner_id=? AND status='ACTIVE' AND deleted_at IS NULL ORDER BY created_at,id", (owner_id,)).fetchall()
            groups = []
            for row in rows:
                local_date = explicit_date or datetime.now(_timezone(row["timezone"])).date().isoformat()
                actions = connection.execute("SELECT * FROM goal_actions WHERE program_id=? AND status='SCHEDULED' AND scheduled_date<=? ORDER BY scheduled_date,position,id", (row["id"], local_date)).fetchall()
                today = [self._action_json(item) for item in actions if item["scheduled_date"] == local_date]
                overdue = [self._action_json(item) for item in actions if item["scheduled_date"] < local_date]
                review = self.reviews.for_program_date(row["id"], local_date, owner_id, connection=connection) if self.reviews else None
                completed = [self._action_json(item) for item in connection.execute("SELECT * FROM goal_actions WHERE program_id=? AND status='COMPLETED' AND scheduled_date=? ORDER BY position,id", (row["id"], local_date)).fetchall()]
                other_completed = connection.execute("SELECT * FROM goal_actions WHERE program_id=? AND status='COMPLETED' AND scheduled_date!=? ORDER BY position,id", (row["id"], local_date)).fetchall()
                completed.extend(self._action_json(item) for item in other_completed if self.latest_feedback(connection, item["id"], local_date)["source_ids"])
                for item in today + overdue + completed:
                    feedback = self.latest_feedback(connection, item["id"], local_date)
                    item["time_entry"] = {"actual_date": local_date, "actual_minutes": feedback["actual_minutes"]}
                timing = self.day_time(row["id"], local_date, owner_id, connection=connection)
                pending_review_dates = [item["scheduled_date"] for item in connection.execute(
                    "SELECT DISTINCT a.scheduled_date FROM goal_actions a WHERE a.program_id=? AND a.scheduled_date<? AND a.status!='CANCELLED' "
                    "AND NOT EXISTS (SELECT 1 FROM goal_daily_reviews r WHERE r.program_id=a.program_id AND r.local_date=a.scheduled_date) ORDER BY a.scheduled_date",
                    (row["id"], local_date),
                ).fetchall()]
                terminal_today = connection.execute("SELECT 1 FROM goal_actions WHERE program_id=? AND scheduled_date=? AND status IN ('SKIPPED','DEFERRED') LIMIT 1", (row["id"], local_date)).fetchone() is not None
                if today or overdue or review or completed or timing["has_execution_record"] or terminal_today or pending_review_dates:
                    progress = self._progress(connection, row["id"])
                    groups.append({"program": self._program_summary(row), "local_date": local_date,
                                   "day_number": max((date.fromisoformat(local_date)-date.fromisoformat(row["start_date"])).days+1, 1),
                                   "today": today, "overdue": overdue, "completed": completed,
                                   "day_time": timing,
                                   "has_execution_record": timing["has_execution_record"] or terminal_today,
                                   "pending_review_dates": pending_review_dates,
                                   "needs_review": (review is None and (timing["has_execution_record"] or terminal_today)) or bool(review and review.get("evidence_stale")),
                                   "today_estimated_minutes": sum(item["estimated_minutes"] for item in today), "progress": progress,
                                   "review": review})
        return {"date": explicit_date, "programs": groups}

    def period_summary_status(self, program_id: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        with self.db.connection() as connection:
            program = self._program_row(connection, program_id, owner_id)
            key = f"period-summary:{program_id}:{program['current_program_version_id']}"
            request_hash = _hash({"program_id": program_id, "version_id": program["current_program_version_id"]})
            receipt = self._receipt(owner_id, key, request_hash, connection=connection)
        status = (receipt or {}).get("status", "NOT_STARTED")
        if status == "PENDING" and (not receipt.get("lease_until") or datetime.fromisoformat(receipt["lease_until"]) <= datetime.now(timezone.utc)):
            status = "UNKNOWN"
        return {"status": status, "summary": (receipt or {}).get("summary"),
                "error_code": (receipt or {}).get("error_code"),
                "retryable": program["status"] == "COMPLETED" and status in {"NOT_STARTED", "FAILED", "UNKNOWN"}}

    async def period_summary(self, program_id: str, owner_id: str = OWNER_ID, *, retry_key: str | None = None) -> str | None:
        """执行周期结束时用模型生成真正的周期复盘。

        模型不可用或不可解析时返回 None，调用方保留 _save_completion_episode 写下的模板总结作为兜底。
        """
        review = getattr(self.compiler, "period_review", None)
        if review is None:
            return None
        with self.db.transaction() as connection:
            program = self._program_row(connection,program_id,owner_id,lock=True)
            if program["status"] != "COMPLETED":
                return None
            summary_key=f"period-summary:{program_id}:{program['current_program_version_id']}"
            summary_hash=_hash({"program_id":program_id,"version_id":program["current_program_version_id"]})
            cached=self._receipt(owner_id,summary_key,summary_hash,identity=("program",program_id,"period-summary"),connection=connection)
            if retry_key is not None and (not isinstance(retry_key, str) or not retry_key.strip() or len(retry_key) > 200):
                raise ValueError("Idempotency-Key is required")
            retry_keys = (cached or {}).get("retry_keys", [])
            if cached is not None:
                if cached.get("status") == "COMPLETED":
                    return cached.get("summary")
                if retry_key is not None and retry_key in retry_keys:
                    return None
                if cached.get("status") == "UNKNOWN" and not retry_key:
                    return None
                if cached.get("status") == "PENDING":
                    # Expired/legacy reservations have unknown provider outcomes.
                    # Only an explicit retry may replace them; fence late responses.
                    if not retry_key or (cached.get("lease_until") and datetime.fromisoformat(cached["lease_until"]) > datetime.now(timezone.utc)):
                        return None
            attempt_id = uuid.uuid4().hex
            reservation = {"status": "PENDING", "summary": None, "attempt_id": attempt_id,
                           "lease_until": (datetime.now(timezone.utc) + timedelta(seconds=self.compile_timeout_seconds + 30)).isoformat(),
                           "retry_keys": [*retry_keys, retry_key] if retry_key is not None else retry_keys}
            if cached is None:
                self._save_receipt(connection,owner_id,"program",program_id,"period-summary",summary_key,summary_hash,reservation)
            else:
                self._complete_reserved_receipt(connection,owner_id,summary_key,summary_hash,reservation)
            self._event(connection, program_id, None, "period_review.started", "user" if retry_key else "runtime", {"attempt_id": attempt_id})
        with self.db.connection() as connection:
            program = self._program_row(connection, program_id, owner_id)
            actions = [dict(row) for row in connection.execute(
                "SELECT id,title,scheduled_date,status,estimated_minutes "
                "FROM goal_actions a WHERE a.program_id=? ORDER BY scheduled_date,position",
                (program_id,),
            ).fetchall()]
            for action in actions:
                effective = self.latest_feedback(connection, action["id"])
                entries = [json.loads(row["details_json"]) for row in connection.execute("SELECT details_json FROM goal_action_feedback WHERE action_id=?", (action["id"],)).fetchall()]
                dates = {entry["actual_date"] for entry in entries if entry.get("actual_date")}
                durations = [self.latest_feedback(connection, action["id"], day)["actual_minutes"] for day in dates]
                complete = bool(durations) and all(value is not None for value in durations) and all(entry.get("actual_date") for entry in entries)
                action.update(note=effective["note"], difficulty=effective["difficulty"], actual_minutes=sum(durations) if complete else None)
            reviews = [dict(row) for row in connection.execute(
                "SELECT local_date,summary,encouragement FROM goal_daily_reviews WHERE program_id=? AND status='COMPLETED' AND evidence_stale=0 ORDER BY local_date",
                (program_id,),
            ).fetchall()]
            coach = None
            if program["current_program_version_id"]:
                version = connection.execute(
                    "SELECT structure_json FROM goal_program_versions WHERE id=?",
                    (program["current_program_version_id"],),
                ).fetchone()
                if version is not None:
                    try: coach = json.loads(version["structure_json"]).get("coach") or None
                    except (TypeError, ValueError): coach = None
        evidence = {
            "objective_title": program["objective_title"], "objective_summary": program["objective_summary"],
            "start_date": program["start_date"], "end_date": program["end_date"], "coach": coach,
            "actions": actions,
            "daily_reviews": [{"local_date": item["local_date"], "summary": item["summary"]} for item in reviews],
        }
        try:
            context = self._model_context(program_id,"reflector","period_review",operation_id=f"{program_id}:{program['current_program_version_id']}")
            result = await asyncio.wait_for(self._call_model(context,review(evidence)), timeout=self.compile_timeout_seconds)
            from .goal_program_compiler import validate_period_review
            summary = validate_period_review(result)["summary"]
        except asyncio.CancelledError:
            self._finish_period_summary(program_id, owner_id, summary_key, summary_hash, reservation, "UNKNOWN", error_code="CANCELLED")
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, GoalCompilationError) else "MODEL_TIMEOUT" if isinstance(exc, TimeoutError) else "MODEL_ERROR"
            self._finish_period_summary(program_id, owner_id, summary_key, summary_hash, reservation, "FAILED", error_code=code)
            return None
        if not self._finish_period_summary(program_id, owner_id, summary_key, summary_hash, reservation, "COMPLETED", summary=summary):
            return None
        return summary

    def _finish_period_summary(self, program_id, owner_id, key, request_hash, reservation, status, *, summary=None, error_code=None):
        with self.db.transaction() as connection:
            self._program_row(connection, program_id, owner_id, lock=True)
            current = self._receipt(owner_id, key, request_hash, connection=connection)
            if current is None or current.get("attempt_id") != reservation["attempt_id"] or current.get("status") != "PENDING":
                return False
            self._complete_reserved_receipt(connection, owner_id, key, request_hash,
                                            {**reservation, "status": status, "summary": summary, "error_code": error_code})
            if status == "COMPLETED":
                self._set_completion_summary(connection, program_id, summary, owner_id)
            self._event(connection, program_id, None, "period_review.finished", "runtime",
                        {"attempt_id": reservation["attempt_id"], "status": status, "error_code": error_code})
        return True

    def set_completion_summary(self, program_id: str, summary: str, owner_id: str = OWNER_ID) -> None:
        """把 agent 生成的周期复盘写回 program 与它对应的记忆 episode，替换模板总结。"""
        with self.db.transaction() as connection:
            self._set_completion_summary(connection, program_id, summary, owner_id)

    def _set_completion_summary(self, connection, program_id, summary, owner_id):
        row = self._program_row(connection, program_id, owner_id)
        connection.execute("UPDATE goal_programs SET completion_summary=?,updated_at=? WHERE id=?", (summary, _now(), program_id))
        if row["completion_episode_id"]:
            connection.execute(
                "UPDATE memory_episodes SET summary=?,synopsis_json=? WHERE id=?",
                (summary, _json([{"text": summary, "source_message_ids": []}]), row["completion_episode_id"]),
            )

    def complete_action(self, action_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID,
                        feedback: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"expected_version": expected_version}
        if feedback:
            payload["feedback"] = feedback
        def finish(connection, action, program):
            self._write_feedback(connection, action, program, {**(feedback or {}), "kind": "completion"}, idempotency_key)
            return self._finish_action(connection, action, program, expected_version, "COMPLETED")
        return self._action_command(action_id, owner_id, "complete", idempotency_key, payload, finish)

    def reopen_action(self, action_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        def reopen(connection, action, program):
            if program["status"] != "ACTIVE" or action["status"] != "COMPLETED" or action["version"] != expected_version:
                raise GoalProgramConflict("only completed actions in an active program can reopen")
            connection.execute("UPDATE goal_actions SET status='SCHEDULED',completed_at=NULL,version=version+1,updated_at=? WHERE id=?", (_now(), action_id))
            self._event(connection, program["id"], action_id, "action.reopened", "user", {})
            if self.reviews:
                self.reviews.refresh_queued_feedback(connection, action_id)
            return {"action": self._action_json(connection.execute("SELECT * FROM goal_actions WHERE id=?", (action_id,)).fetchone()), "progress": self._progress(connection, program["id"])}
        return self._action_command(action_id, owner_id, "reopen", idempotency_key, {"expected_version": expected_version}, reopen)

    def close_day(self, program_id: str, local_date: str, *, idempotency_key: str, owner_id: str = OWNER_ID):
        if self.reviews is None:
            raise RuntimeError("reviews are not configured")
        return self.reviews.close_day(program_id, local_date, idempotency_key=idempotency_key, owner_id=owner_id)

    def day_time(self, program_id: str, local_date: str, owner_id: str = OWNER_ID, *, connection=None):
        if connection is None:
            with self.db.connection() as owned:
                return self.day_time(program_id, local_date, owner_id, connection=owned)
        program = self._program_row(connection, program_id, owner_id)
        return program_day_time(connection, program, local_date)

    def skip_action(self, action_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        return self._action_command(action_id, owner_id, "skip", idempotency_key, {"expected_version": expected_version},
                                    lambda c, a, p: self._finish_action(c, a, p, expected_version, "SKIPPED"))

    def defer_action(self, action_id: str, *, expected_version: int, scheduled_date: str, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        return self._action_command(action_id, owner_id, "defer", idempotency_key,
                                    {"expected_version": expected_version, "scheduled_date": scheduled_date},
                                    lambda c, a, p: self._defer_action(c, a, p, expected_version, scheduled_date))

    def feedback(self, action_id: str, payload: dict[str, Any], *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        request_hash = _hash({"action_id": action_id, "expected_version": expected_version, **payload})
        cached = self._receipt(owner_id, idempotency_key, request_hash, identity=("action", action_id, "feedback"))
        if cached is not None: return cached
        with self.db.transaction() as connection:
            self._lock_command(connection, owner_id, idempotency_key)
            cached = self._receipt(owner_id, idempotency_key, request_hash, identity=("action", action_id, "feedback"), connection=connection)
            if cached is not None: return cached
            action, program = self._owned_action(connection, action_id, owner_id, lock=True)
            if action["version"] != expected_version:
                raise GoalProgramConflict("action version conflict", self._action_json(action), code="VERSION_CONFLICT")
            if program["status"] != "ACTIVE" or action["status"] in {"CANCELLED", "DEFERRED"}:
                raise GoalProgramConflict("only active actions can be corrected", code="ACTION_NOT_ELIGIBLE")
            response = self._write_feedback(connection, action, program, payload, idempotency_key)
            if payload.get("kind") != "partial":
                connection.execute("UPDATE goal_actions SET version=version+1,updated_at=? WHERE id=?", (_now(), action_id))
            if self.reviews is not None:
                self.reviews.refresh_queued_feedback(connection, action_id)
            self._save_receipt(connection, owner_id, "action", action_id, "feedback", idempotency_key, request_hash, response)
        return response

    def _write_feedback(self, connection, action, program, payload, key):
        allowed = {"kind", "actual_minutes", "actual_date", "cleared_fields", "difficulty", "reason_code", "note", "sensitivity", "completed_work", "remaining_work", "output"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError("unknown feedback fields")
        kind = _short_text(payload.get("kind"), "kind", 40)
        actual = payload.get("actual_minutes"); difficulty = payload.get("difficulty")
        if actual is not None and (isinstance(actual, bool) or not isinstance(actual, int) or not 0 <= actual <= 1440): raise ValueError("actual_minutes is invalid")
        if difficulty is not None and (isinstance(difficulty, bool) or not isinstance(difficulty, int) or not 1 <= difficulty <= 5): raise ValueError("difficulty is invalid")
        note = payload.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 2000): raise ValueError("note is invalid")
        details = {name: payload[name] for name in ("completed_work", "remaining_work", "output") if name in payload}
        for name, value in details.items():
            if not isinstance(value, str) or len(value) > 2000:
                raise ValueError(f"{name} is invalid")
        actual_date = payload.get("actual_date") or _execution_date(program["timezone"])
        if not isinstance(actual_date, str) or date.fromisoformat(actual_date).isoformat() != actual_date:
            raise ValueError("actual_date is invalid")
        if actual_date > _execution_date(program["timezone"]):
            raise ValueError("actual_date cannot be in the future")
        cleared = payload.get("cleared_fields", [])
        clearable = {"actual_minutes", "difficulty", "reason_code", "note", "completed_work", "remaining_work", "output"}
        if not isinstance(cleared, list) or any(not isinstance(item, str) or item not in clearable for item in cleared) or (cleared and kind != "correction"):
            raise ValueError("cleared_fields is invalid")
        if any(payload.get(item) is not None for item in cleared):
            raise ValueError("cannot set and clear the same field")
        details.update(actual_date=actual_date, cleared_fields=cleared)
        if kind == "partial" and (program["status"] != "ACTIVE" or action["status"] != "SCHEDULED"):
            raise GoalProgramConflict("only pending actions can record partial progress", code="ACTION_NOT_ELIGIBLE")
        feedback_id = f"feedback_{uuid.uuid4().hex}"
        connection.execute("INSERT INTO goal_action_feedback(id,owner_id,action_id,kind,actual_minutes,difficulty,reason_code,note,sensitivity,idempotency_key,created_at,details_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (feedback_id, program["owner_id"], action["id"], kind, actual, difficulty, payload.get("reason_code"), note, payload.get("sensitivity", "normal"), key, _now(), _json(details)))
        if kind in {"partial", "correction"}:
            progress = {**json.loads(action["progress_json"]), **details, "state": "PARTIAL", "feedback_id": feedback_id}
            progress.update({name: value for name, value in {"actual_minutes": actual, "difficulty": difficulty, "note": note}.items() if value is not None})
            for field in cleared:
                progress.pop(field, None)
            connection.execute("UPDATE goal_actions SET progress_json=?,version=version+?,updated_at=? WHERE id=?", (_json(progress), int(kind == "partial"), _now(), action["id"]))
        self._event(connection, program["id"], action["id"], "action.feedback_added", "user", {"feedback_id": feedback_id, "kind": kind, "count": 1})
        return {"id": feedback_id, "action_id": action["id"], "kind": kind, "actual_minutes": actual, "difficulty": difficulty,
                "reason_code": payload.get("reason_code"), "note": note, "sensitivity": payload.get("sensitivity", "normal"), **details}

    @staticmethod
    def latest_feedback(connection, action_id, actual_date=None):
        rows = connection.execute("SELECT * FROM goal_action_feedback WHERE action_id=? ORDER BY created_at DESC,id DESC", (action_id,)).fetchall()
        result = {"actual_minutes": None, "difficulty": None, "note": None, "reason_code": None, "source_ids": []}
        seen = set()
        for row in rows:
            details = json.loads(row["details_json"])
            if actual_date is not None and details.get("actual_date") != actual_date:
                continue
            used = False
            for field in details.get("cleared_fields", []):
                if field not in seen:
                    result[field] = None
                    seen.add(field)
                    used = True
            for field in ("actual_minutes", "difficulty", "note", "reason_code"):
                if field not in seen and row[field] is not None:
                    result[field] = row[field]
                    seen.add(field)
                    used = True
            for field, value in details.items():
                if field not in seen and field != "cleared_fields":
                    result[field] = value
                    seen.add(field)
                    used = True
            if used:
                result["source_ids"].append(row["id"])
        return result

    def transition(self, program_id: str, operation: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        return self._command(program_id, owner_id, operation, idempotency_key, {"expected_version": expected_version},
                             lambda c, row: self._transition(c, row, expected_version, operation))

    def tombstone(self, program_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        return self._command(program_id, owner_id, "delete", idempotency_key, {"expected_version": expected_version},
                             lambda c, row: self._tombstone(c, row, expected_version))

    def request_help(self, action_id: str, *, content: str, expected_version: int, idempotency_key: str, client_turn_id: str | None = None, owner_id: str = OWNER_ID, expert: bool = False) -> dict[str, Any]:
        if self.conversation is None:
            raise RuntimeError("conversation is not configured")
        content = _short_text(content, "content", 4000)
        request_hash = _hash({"action_id": action_id, "content": content, "expected_version": expected_version, "client_turn_id": client_turn_id, "expert": expert})
        cached = self._receipt(owner_id, idempotency_key, request_hash)
        if cached is not None: return cached
        with self.db.connection() as connection:
            action, program = self._owned_action(connection, action_id, owner_id)
            if action["version"] != expected_version:
                raise GoalProgramConflict("action version conflict", self._action_json(action))
            thread = connection.execute("SELECT id,owner_id FROM threads WHERE id=?", (program["source_thread_id"],)).fetchone()
            if thread is None or thread["owner_id"] != owner_id: raise GoalProgramNotFound(action_id)
            context = {"program": self._program_summary(program), "action": self._action_json(action), "user_question": content}
            thread_id = program["source_thread_id"]
        if not expert or self.expert_advisor is None:
            turn_key = client_turn_id.strip() if isinstance(client_turn_id,str) and client_turn_id.strip() else f"goal-help-{idempotency_key}"
            submission = self.conversation.accept_turn(thread_id, turn_key, content, [], goal_action_id=action_id)
            response = {"thread_id": submission.thread_id, "turn_id": submission.turn_id, "status": submission.status,
                        "version": submission.version, "event_cursor": submission.event_cursor, "action_id": action_id}
        else:
            turn_key = client_turn_id.strip() if isinstance(client_turn_id,str) and client_turn_id.strip() else f"goal-help-{idempotency_key}"
            submission = self.conversation.accept_turn(
                thread_id, turn_key, content, [], goal_action_id=action_id, deferred_to_expert=True,
            )
            expert_turn = self.conversation.turn(submission.turn_id, owner_id)
            run = self.expert_advisor.start(
                purpose="action-help", source_id=f"{action_id}:{idempotency_key}", objective=content,
                context=context, roles=("planner", "critic"), owner_id=owner_id, thread_id=thread_id,
                append_thread_message=False, runtime_bundle_id=expert_turn.runtime_bundle_id,
                root_budget_id=expert_turn.root_budget_id,
            )
            response = {"thread_id": thread_id, "turn_id": submission.turn_id, "status": "accepted",
                        "version": run["version"], "event_cursor": 0, "action_id": action_id, "agent_run_id": run["id"]}
        with self.db.transaction() as connection:
            self._save_receipt(connection, owner_id, "action", action_id, "request-help", idempotency_key, request_hash, response)
            self._event(connection, program["id"], action_id, "action.help_requested", "user", {"turn_id": response["turn_id"], "agent_run_id": response.get("agent_run_id")})
        return response

    def _activate(self, connection, row, expected_version: int) -> None:
        if row["version"] != expected_version or row["status"] != "DRAFT" or row["compile_status"] != "READY" or not row["current_program_version_id"]:
            raise GoalProgramConflict("program cannot be activated", self._program_json(connection, row["id"], row["owner_id"]), code="ACTION_NOT_ELIGIBLE")
        self._assert_source(connection, row["id"], row["owner_id"])
        structure = self._structure(connection, row["current_program_version_id"])
        validate_program_structure(structure, row["start_date"], row["end_date"], row["daily_minutes"], constraints=json.loads(row["schedule_constraints_json"]))
        now = _now()
        for item in structure["actions"]:
            connection.execute("INSERT INTO goal_actions(id,program_id,program_version_id,logical_key,scheduled_date,position,title,description,estimated_minutes,completion_criteria,required,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'SCHEDULED',?,?)",
                               (f"action_{uuid.uuid4().hex}", row["id"], row["current_program_version_id"], item["logical_key"], item["scheduled_date"], item["position"], item["title"], item["description"], item["estimated_minutes"], item["completion_criteria"], int(item["required"]), now, now))
        changed = connection.execute("UPDATE goal_programs SET status='ACTIVE',version=version+1,updated_at=? WHERE id=? AND version=? AND status='DRAFT'", (now,row["id"],expected_version)).rowcount
        if changed != 1: raise GoalProgramConflict("program version conflict", self._program_json(connection,row["id"],row["owner_id"]), code="VERSION_CONFLICT")
        self._event(connection,row["id"],None,"program.activated","user",{"action_count":len(structure["actions"])})

    def _finish_action(self, connection, action, program, expected_version: int, status: str) -> dict[str, Any]:
        if program["status"] != "ACTIVE" or action["status"] != "SCHEDULED" or action["version"] != expected_version:
            raise GoalProgramConflict("action cannot transition", self._action_json(action), code="ACTION_NOT_ELIGIBLE")
        was_ready = self._progress(connection, program["id"])["completion_ready"]
        field = "completed_at" if status == "COMPLETED" else "skipped_at"
        now = _now()
        changed = connection.execute(f"UPDATE goal_actions SET status=?,version=version+1,{field}=?,updated_at=? WHERE id=? AND version=? AND status='SCHEDULED'", (status,now,now,action["id"],expected_version)).rowcount
        if changed != 1: raise GoalProgramConflict("action version conflict", self._action_json(action), code="VERSION_CONFLICT")
        self._event(connection,program["id"],action["id"],f"action.{status.lower()}","user",{})
        progress = self._progress(connection, program["id"])
        if progress["completion_ready"] and not was_ready:
            self._event(connection,program["id"],None,"program.completion_ready","system",{})
        if self.reviews is not None:
            self.reviews.refresh_queued_feedback(connection, action["id"])
        return {"action": self._action_json(connection.execute("SELECT * FROM goal_actions WHERE id=?",(action["id"],)).fetchone()), "progress": progress}

    def _defer_action(self, connection, action, program, expected_version: int, scheduled_date: str) -> dict[str, Any]:
        target = date.fromisoformat(scheduled_date)
        if not date.fromisoformat(program["start_date"]) <= target <= date.fromisoformat(program["end_date"]): raise ValueError("scheduled_date is outside the program")
        if program["status"] != "ACTIVE" or action["status"] != "SCHEDULED" or action["version"] != expected_version:
            raise GoalProgramConflict("action cannot be deferred", self._action_json(action), code="ACTION_NOT_ELIGIBLE" if program["status"] != "ACTIVE" or action["status"] != "SCHEDULED" else "VERSION_CONFLICT")
        if target <= date.fromisoformat(action["scheduled_date"]):
            raise ValueError("延期日期必须晚于原定日期")
        calendar = calendar_days(program["start_date"], program["end_date"], json.loads(program["schedule_constraints_json"]))
        if scheduled_date not in calendar["available_dates"]:
            raise ValueError("所选日期为休息日，请选择可用日期")
        workload = connection.execute(
            "SELECT COUNT(*) count,COALESCE(SUM(estimated_minutes),0) minutes FROM goal_actions "
            "WHERE program_id=? AND scheduled_date=? AND status IN ('SCHEDULED','COMPLETED')",
            (program["id"], scheduled_date),
        ).fetchone()
        if workload["count"] >= 6 or workload["minutes"] + action["estimated_minutes"] > program["daily_minutes"]:
            raise GoalProgramConflict(
                "所选日期的行动已超出每日预算，请选择其他日期或先调整计划",
                code="DAILY_CAPACITY_EXCEEDED",
            )
        now = _now()
        changed=connection.execute("UPDATE goal_actions SET status='DEFERRED',version=version+1,deferred_at=?,updated_at=? WHERE id=? AND version=? AND status='SCHEDULED'",(now,now,action["id"],expected_version)).rowcount
        if changed != 1: raise GoalProgramConflict("action version conflict", self._action_json(action), code="VERSION_CONFLICT")
        position=int(connection.execute("SELECT COALESCE(MAX(position),0)+1 FROM goal_actions WHERE program_id=? AND scheduled_date=?",(program["id"],scheduled_date)).fetchone()[0])
        replacement_id=f"action_{uuid.uuid4().hex}"
        prior_progress=json.loads(action["progress_json"])
        carry={name:prior_progress[name] for name in ("completed_work","remaining_work","output","note") if prior_progress.get(name)}
        if prior_progress:
            carry.update({"state":"CARRIED_OVER","source_action_id":action["id"]})
        connection.execute("INSERT INTO goal_actions(id,program_id,program_version_id,logical_key,scheduled_date,position,title,description,estimated_minutes,completion_criteria,required,status,deferred_from_action_id,created_at,updated_at,progress_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,'SCHEDULED',?,?,?,?)",
                           (replacement_id,program["id"],action["program_version_id"],f"{action['logical_key']}-defer-{replacement_id[-8:]}",scheduled_date,position,action["title"],action["description"],action["estimated_minutes"],action["completion_criteria"],action["required"],action["id"],now,now,_json(carry)))
        self._event(connection,program["id"],action["id"],"action.deferred","user",{"replacement_action_id":replacement_id,"scheduled_date":scheduled_date})
        if self.reviews is not None:
            self.reviews.refresh_queued_feedback(connection,action["id"])
            self.reviews.refresh_queued_feedback(connection,replacement_id)
        return {"action":self._action_json(connection.execute("SELECT * FROM goal_actions WHERE id=?",(action["id"],)).fetchone()),"replacement":self._action_json(connection.execute("SELECT * FROM goal_actions WHERE id=?",(replacement_id,)).fetchone()),"progress":self._progress(connection,program["id"])}

    def _transition(self, connection, row, expected_version: int, operation: str) -> None:
        targets={"pause":({"ACTIVE"},"PAUSED"),"resume":({"PAUSED"},"ACTIVE"),"complete":({"ACTIVE","PAUSED"},"COMPLETED"),"cancel":({"DRAFT","ACTIVE","PAUSED"},"CANCELLED")}
        if operation not in targets: raise ValueError("unknown program operation")
        sources,target=targets[operation]
        if row["version"] != expected_version or row["status"] not in sources: raise GoalProgramConflict("illegal program transition",self._program_json(connection,row["id"],row["owner_id"]))
        if operation=="complete" and not self._progress(connection,row["id"])["completion_ready"]: raise GoalProgramConflict("required actions are not complete",self._program_json(connection,row["id"],row["owner_id"]))
        now=_now(); terminal_field=",completed_at=?" if target=="COMPLETED" else ",cancelled_at=?" if target=="CANCELLED" else ""
        params:list[Any]=[target,now]
        if terminal_field: params.append(now)
        params.extend([row["id"],expected_version,row["status"]])
        changed=connection.execute(f"UPDATE goal_programs SET status=?,updated_at=?{terminal_field},version=version+1 WHERE id=? AND version=? AND status=?",params).rowcount
        if changed!=1: raise GoalProgramConflict("program version conflict",self._program_json(connection,row["id"],row["owner_id"]))
        if target=="CANCELLED": connection.execute("UPDATE goal_actions SET status='CANCELLED',cancel_reason='PROGRAM_CANCELLED',cancelled_at=?,updated_at=?,version=version+1 WHERE program_id=? AND status='SCHEDULED'",(now,now,row["id"]))
        if target=="COMPLETED": self._save_completion_episode(connection,row)
        event_type={"pause":"program.paused","resume":"program.resumed","complete":"program.completed","cancel":"program.cancelled"}[operation]
        self._event(connection,row["id"],None,event_type,"user",{})

    def _save_completion_episode(self, connection, row) -> None:
        actions=connection.execute("SELECT required,status FROM goal_actions WHERE program_id=?",(row["id"],)).fetchall()
        required=sum(bool(item["required"]) and item["status"] not in {"DEFERRED","CANCELLED"} for item in actions)
        completed=sum(bool(item["required"]) and item["status"]=="COMPLETED" for item in actions)
        optional=sum(not item["required"] and item["status"]=="COMPLETED" for item in actions)
        skipped=sum(item["status"]=="SKIPPED" for item in actions)
        summary=f"已完成目标“{row['objective_title']}”。执行周期 {row['start_date']} 至 {row['end_date']}；必做行动完成 {completed}/{required}，选做完成 {optional}，跳过 {skipped}。"
        source_hash=_hash({"program_id":row["id"],"program_version_id":row["current_program_version_id"],"summary":summary})
        episode_id=f"episode_{uuid.uuid4().hex}"
        structured = _json([{"text": summary, "source_message_ids": []}])
        connection.execute(
            "INSERT OR IGNORE INTO memory_episodes("
            "id,owner_id,thread_id,project_id,start_message_seq,end_message_seq,source_hash,summary,"
            "sensitivity,retrieval_policy,status,created_at,schema_version,synopsis_json,outcomes_json,"
            "source_message_ids_json,source_token_count,summary_token_count,tokenizer_version) "
            "VALUES (?,?,?,NULL,0,0,?,?,'normal','thread','ACTIVE',?,'episode-v2',?,?,?,0,?,?)",
            (
                episode_id, row["owner_id"], row["source_thread_id"], source_hash, summary, _now(),
                structured, structured, "[]", DEFAULT_TOKEN_COUNTER.count_text(summary),
                DEFAULT_TOKEN_COUNTER.version,
            ),
        )
        episode=connection.execute("SELECT id FROM memory_episodes WHERE owner_id=? AND thread_id=? AND start_message_seq=0 AND end_message_seq=0 AND source_hash=?",(row["owner_id"],row["source_thread_id"],source_hash)).fetchone()
        connection.execute("UPDATE goal_programs SET completion_summary=?,completion_episode_id=? WHERE id=?",(summary,episode["id"],row["id"]))

    def _tombstone(self, connection, row, expected_version: int) -> None:
        if row["version"]!=expected_version: raise GoalProgramConflict("program version conflict",self._program_json(connection,row["id"],row["owner_id"]))
        changed=connection.execute("UPDATE goal_programs SET deleted_at=?,updated_at=?,version=version+1 WHERE id=? AND version=? AND deleted_at IS NULL",(_now(),_now(),row["id"],expected_version)).rowcount
        if changed!=1: raise GoalProgramConflict("program already deleted",self._program_json(connection,row["id"],row["owner_id"]))
        self._event(connection,row["id"],None,"program.tombstoned","user",{})

    def _command(self, program_id, owner_id, operation, key, payload, mutate):
        request_hash=_hash(payload)
        with self.db.transaction() as connection:
            self._lock_command(connection, owner_id, key)
            cached=self._receipt(owner_id,key,request_hash,identity=("program",program_id,operation),connection=connection)
            if cached is not None:return cached
            row=self._program_row(connection,program_id,owner_id,lock=True); mutate(connection,row)
            response=self._program_json(connection,program_id,owner_id)
            self._save_receipt(connection,owner_id,"program",program_id,operation,key,request_hash,response)
        return response

    def _action_command(self, action_id, owner_id, operation, key, payload, mutate):
        request_hash=_hash(payload)
        with self.db.transaction() as connection:
            self._lock_command(connection, owner_id, key)
            cached=self._receipt(owner_id,key,request_hash,identity=("action",action_id,operation),connection=connection)
            if cached is not None:return cached
            action,program=self._owned_action(connection,action_id,owner_id,lock=True); response=mutate(connection,action,program)
            self._save_receipt(connection,owner_id,"action",action_id,operation,key,request_hash,response)
        return response

    def _lock_command(self, connection, owner_id, key):
        if not isinstance(key,str) or not key.strip() or len(key)>200: raise ValueError("Idempotency-Key is required")
        if self.db.backend == "postgresql":
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(?,0))", (f"goal-command:{owner_id}:{key}",))

    def _receipt(self, owner_id, key, request_hash, *, identity=None, connection=None):
        if not isinstance(key,str) or not key.strip() or len(key)>200: raise ValueError("Idempotency-Key is required")
        if connection is None:
            with self.db.connection() as owned:
                return self._receipt(owner_id, key, request_hash, identity=identity, connection=owned)
        row=connection.execute("SELECT * FROM goal_command_receipts WHERE owner_id=? AND idempotency_key=?",(owner_id,key)).fetchone()
        if row is None:return None
        if identity is not None and tuple(row[name] for name in ("aggregate_type","aggregate_id","operation")) != identity:
            raise GoalProgramConflict("idempotency key reused for another resource or operation")
        if row["request_hash"]!=request_hash: raise GoalProgramConflict("idempotency key reused with different payload")
        return json.loads(row["response_json"])

    def _save_receipt(self, connection, owner_id, aggregate_type, aggregate_id, operation, key, request_hash, response):
        connection.execute("INSERT INTO goal_command_receipts(id,owner_id,aggregate_type,aggregate_id,operation,idempotency_key,request_hash,response_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",(f"receipt_{uuid.uuid4().hex}",owner_id,aggregate_type,aggregate_id,operation,key,request_hash,_json(response),_now()))

    def _event(self, connection, program_id, action_id, event_type, actor, data):
        row=connection.execute("SELECT next_event_seq FROM goal_programs WHERE id=?",(program_id,)).fetchone(); seq=int(row["next_event_seq"])
        connection.execute("INSERT INTO goal_program_events(event_id,program_id,seq,action_id,type,actor,schema_version,occurred_at,data_json) VALUES (?,?,?,?,?,?,1,?,?)",(f"goalevt_{uuid.uuid4().hex}",program_id,seq,action_id,event_type,actor,_now(),_json(data)))
        connection.execute("UPDATE goal_programs SET next_event_seq=next_event_seq+1 WHERE id=?",(program_id,))

    def _program_row(self, connection, program_id, owner_id, *, lock=False):
        suffix = " FOR UPDATE" if lock and self.db.backend == "postgresql" else ""
        row=connection.execute("SELECT * FROM goal_programs WHERE id=? AND owner_id=?"+suffix,(program_id,owner_id)).fetchone()
        if row is None:raise GoalProgramNotFound(program_id)
        return row

    def _model_context(
        self, program_id: str, role: str, purpose: str, *,
        operation_id: str | None = None, root_budget_id: str | None = None,
        runtime_bundle_id: str | None = None,
    ):
        from .model_control import ModelCallContext

        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT p.owner_id,p.source_thread_id,v.source_turn_id,t.runtime_bundle_id FROM goal_programs p "
                "JOIN plan_document_versions v ON v.id=p.source_plan_document_version_id "
                "LEFT JOIN turns t ON t.id=v.source_turn_id WHERE p.id=?",
                (program_id,),
            ).fetchone()
        if row is None:
            raise GoalProgramNotFound(program_id)
        if self.db.backend=="postgresql" and root_budget_id is None:
            from .costs import CostService
            identity=_hash({"program_id":program_id,"purpose":purpose,"operation_id":operation_id or program_id})
            root=CostService(self.db).ensure_default_root_budget(row["owner_id"],"goal_operation",identity)
            root_budget_id=root["id"]
        return ModelCallContext(
            role=role, purpose=purpose, owner_id=row["owner_id"], thread_id=row["source_thread_id"], turn_id=row["source_turn_id"],
            runtime_bundle_id=runtime_bundle_id if runtime_bundle_id is not None else row["runtime_bundle_id"],
            root_budget_id=root_budget_id,
        )

    async def _call_model(self, context, invocation):
        gateway = getattr(self.compiler, "gateway", None)
        if getattr(gateway, "control_store", None) is None:
            return await invocation
        token = gateway.set_call_context(context)
        try:
            return await invocation
        finally:
            gateway.reset_call_context(token)

    def _owned_action(self, connection, action_id, owner_id, *, lock=False):
        action=connection.execute("SELECT a.* FROM goal_actions a JOIN goal_programs p ON p.id=a.program_id WHERE a.id=? AND p.owner_id=?",(action_id,owner_id)).fetchone()
        if action is None:raise GoalProgramNotFound(action_id)
        program = self._program_row(connection,action["program_id"],owner_id,lock=lock)
        if lock:
            action = connection.execute("SELECT * FROM goal_actions WHERE id=?", (action_id,)).fetchone()
        return action,program

    def _assert_source(self, connection, program_id, owner_id):
        row=connection.execute("SELECT p.*,v.markdown_content,v.status source_status,v.content_hash current_source_hash,d.current_version_id,t.owner_id thread_owner FROM goal_programs p JOIN plan_documents d ON d.id=p.source_plan_document_id JOIN plan_document_versions v ON v.id=p.source_plan_document_version_id JOIN threads t ON t.id=p.source_thread_id WHERE p.id=? AND p.owner_id=? AND d.thread_id=p.source_thread_id AND v.plan_document_id=d.id",(program_id,owner_id)).fetchone()
        if row is None or row["thread_owner"]!=owner_id or row["source_status"]!="committed" or row["current_source_hash"]!=row["source_plan_content_hash"]: raise GoalProgramConflict("source plan snapshot is invalid", code="VERSION_CONFLICT")
        return row

    def _structure(self, connection, version_id):
        row=connection.execute("SELECT structure_json FROM goal_program_versions WHERE id=?",(version_id,)).fetchone()
        if row is None:raise GoalProgramConflict("program version is missing")
        return json.loads(row["structure_json"])

    def _program_json(self, connection, program_id, owner_id):
        row=self._program_row(connection,program_id,owner_id); structure=self._structure(connection,row["current_program_version_id"]) if row["current_program_version_id"] else None
        actions=connection.execute("SELECT * FROM goal_actions WHERE program_id=? ORDER BY scheduled_date,position,id",(program_id,)).fetchall()
        result=self._program_summary(row); result.update({"source_thread_id":row["source_thread_id"],"source_plan_document_id":row["source_plan_document_id"],"source_plan_document_version_id":row["source_plan_document_version_id"],"source_plan_content_hash":row["source_plan_content_hash"],"compile_status":row["compile_status"],"compile_error_code":row["compile_error_code"],"daily_minutes":row["daily_minutes"],"current_program_version_id":row["current_program_version_id"],"structure":structure,"actions":[self._action_json(a) for a in actions],"progress":self._progress(connection,program_id),"next_event_seq":row["next_event_seq"],"deleted_at":row["deleted_at"],"completion_summary":row["completion_summary"],"completion_episode_id":row["completion_episode_id"]})
        result["schedule_constraints"] = json.loads(row["schedule_constraints_json"])
        result["calendar"] = calendar_days(row["start_date"],row["end_date"],result["schedule_constraints"])
        return result

    @staticmethod
    def _program_summary(row):return {"id":row["id"],"objective_title":row["objective_title"],"objective_summary":row["objective_summary"],"status":row["status"],"timezone":row["timezone"],"start_date":row["start_date"],"end_date":row["end_date"],"version":row["version"]}

    @staticmethod
    def _action_json(row):return {key:row[key] for key in ("id","program_id","program_version_id","logical_key","scheduled_date","position","title","description","estimated_minutes","completion_criteria","status","version","completed_at","skipped_at","deferred_at","cancelled_at","deferred_from_action_id","cancel_reason")} | {"required":bool(row["required"]), "progress":json.loads(row["progress_json"])}

    def _progress(self, connection, program_id):
        rows=connection.execute("SELECT required,status FROM goal_actions WHERE program_id=?",(program_id,)).fetchall()
        eligible=[r for r in rows if r["status"] not in {"DEFERRED","CANCELLED"}]
        required=[r for r in eligible if r["required"]]; completed=sum(r["status"]=="COMPLETED" for r in required)
        return {"required_completed":completed,"required_total":len(required),"completion_rate":completed/len(required) if required else 1.0,"completion_ready":completed==len(required),"optional_completed":sum(r["status"]=="COMPLETED" and not r["required"] for r in eligible)}


def program_day_time(connection, program, local_date):
    date.fromisoformat(local_date)
    actions = connection.execute("SELECT * FROM goal_actions WHERE program_id=? AND status!='CANCELLED'", (program["id"],)).fetchall()
    spent, unknown, has_record = 0, [], False
    for action in actions:
        feedback = GoalProgramService.latest_feedback(connection, action["id"], local_date)
        has_record = has_record or bool(feedback["source_ids"])
        if feedback["actual_minutes"] is not None:
            spent += feedback["actual_minutes"]
        elif feedback["source_ids"] or action["scheduled_date"] == local_date:
            unknown.append(action["id"])
    return {"spent_minutes": spent, "remaining_minutes": None if unknown else max(0, program["daily_minutes"]-spent),
            "time_complete": not unknown, "unknown_action_ids": unknown, "has_execution_record": has_record}


def _execution_date(timezone_name):
    return datetime.now(_timezone(timezone_name)).date().isoformat()


def _program_dates(start_value: str, end_value: str | None) -> tuple[date,date,bool]:
    try:start=date.fromisoformat(start_value)
    except (TypeError,ValueError) as exc:raise ValueError("start_date must be YYYY-MM-DD") from exc
    defaulted=end_value is None
    try:end=date.fromisoformat(end_value) if end_value else start+timedelta(days=27)
    except (TypeError,ValueError) as exc:raise ValueError("requested_end_date must be YYYY-MM-DD") from exc
    days=(end-start).days+1
    if not 1<=days<=MAX_PROGRAM_DAYS:raise ValueError(f"program duration must be between 1 and {MAX_PROGRAM_DAYS} days")
    return start,end,defaulted


def _timezone(name: str) -> ZoneInfo:
    if not isinstance(name,str) or not name.strip():raise ValueError("timezone is required")
    try:return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:raise ValueError("timezone must be a valid IANA timezone") from exc


def _short_text(value: Any,label: str,limit: int)->str:
    if not isinstance(value,str) or not value.strip() or len(value)>limit:raise ValueError(f"{label} is invalid")
    return value.strip()


def _json(value: Any)->str:return json.dumps(value,ensure_ascii=False,separators=(",",":"),sort_keys=True)
def _hash(value: Any)->str:return "sha256:"+hashlib.sha256(_json(value).encode()).hexdigest()
def _now()->str:return datetime.now(timezone.utc).isoformat()
