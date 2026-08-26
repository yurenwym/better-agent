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


OWNER_ID = "local-user"


class GoalProgramConflict(ValueError):
    def __init__(self, message: str, current: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.current = current


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
        self._recover_interrupted_compilations()

    def _recover_interrupted_compilations(self) -> None:
        now = _now()
        with self.db.transaction() as connection:
            rows = connection.execute("SELECT id FROM goal_programs WHERE status='DRAFT' AND compile_status='COMPILING'").fetchall()
            for row in rows:
                connection.execute("UPDATE goal_programs SET compile_status='FAILED',compile_error_code='COMPILE_INTERRUPTED',version=version+1,updated_at=? WHERE id=?", (now, row["id"]))
                connection.execute("UPDATE goal_command_receipts SET response_json=? WHERE aggregate_type='program' AND aggregate_id=? AND json_extract(response_json,'$._pending_program_id')=?", (_json(self._program_json(connection,row["id"],OWNER_ID)),row["id"],row["id"]))
                self._event(connection,row["id"],None,"program.compile_failed","system",{"reason_code":"COMPILE_INTERRUPTED"})

    async def preview(
        self, plan_document_id: str, *, start_date: str, timezone_name: str,
        daily_minutes: int, requested_end_date: str | None, idempotency_key: str,
        owner_id: str = OWNER_ID,
    ) -> dict[str, Any]:
        start, end, defaulted = _program_dates(start_date, requested_end_date)
        _timezone(timezone_name)
        if isinstance(daily_minutes, bool) or not isinstance(daily_minutes, int) or not 5 <= daily_minutes <= 1440:
            raise ValueError("daily_minutes must be between 5 and 1440")
        request = {"plan_document_id": plan_document_id, "start_date": start.isoformat(), "end_date": end.isoformat(),
                   "timezone": timezone_name, "daily_minutes": daily_minutes, "requested_end_date": requested_end_date}
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
                raise GoalProgramConflict("source plan version is not committed")
            if existing is not None:
                program_id = pending_program_id
            else:
                program_id = f"program_{uuid.uuid4().hex}"
                now = _now()
                connection.execute(
                    "INSERT INTO goal_programs(id,owner_id,source_thread_id,source_plan_document_id,source_plan_document_version_id,"
                    "source_plan_content_hash,status,compile_status,timezone,start_date,end_date,daily_minutes,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,'DRAFT','COMPILING',?,?,?,?,?,?)",
                    (program_id, owner_id, source["thread_id"], plan_document_id, source["version_id"], source["content_hash"],
                     timezone_name, start.isoformat(), end.isoformat(), daily_minutes, now, now),
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
                           "defaulted_end_date": defaulted}
        try:
            structure = await self._compile(source["markdown_content"], compile_request)
            structure = validate_program_structure(structure, start.isoformat(), end.isoformat(), daily_minutes)
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
                   "daily_minutes": row["daily_minutes"], "requested_end_date": row["end_date"], "defaulted_end_date": False}
        try:
            structure = validate_program_structure(await self._compile(source["markdown_content"], request), row["start_date"], row["end_date"], row["daily_minutes"])
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

    async def _compile(self, source_markdown: str, request: dict[str, Any]) -> dict[str, Any]:
        try:
            if self.expert_advisor is not None:
                advice = await self.expert_advisor.advise(
                    purpose="plan", source_id=str(request["source_plan_document_version_id"]),
                    objective="审阅计划并提出可执行性、风险和遗漏建议",
                    context={"request": request, "plan_markdown": source_markdown}, roles=("planner", "critic"),
                )
                if advice is not None:
                    request = {**request, "expert_advice": advice}
            return await asyncio.wait_for(
                self.compiler.compile(source_markdown, request),
                timeout=self.compile_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise GoalCompilationError("COMPILE_TIMEOUT", "goal compilation timed out", temporary=True) from exc

    def activate(self, program_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        return self._command(program_id, owner_id, "activate", idempotency_key, {"expected_version": expected_version},
                             lambda connection, row: self._activate(connection, row, expected_version))

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
                if today or overdue or review:
                    progress = self._progress(connection, row["id"])
                    groups.append({"program": self._program_summary(row), "local_date": local_date,
                                   "day_number": max((date.fromisoformat(local_date)-date.fromisoformat(row["start_date"])).days+1, 1),
                                   "today": today, "overdue": overdue,
                                   "today_estimated_minutes": sum(item["estimated_minutes"] for item in today), "progress": progress,
                                   "review": review})
        return {"date": explicit_date, "programs": groups}

    def complete_action(self, action_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        return self._action_command(action_id, owner_id, "complete", idempotency_key, {"expected_version": expected_version},
                                    lambda c, a, p: self._finish_action(c, a, p, expected_version, "COMPLETED"))

    def skip_action(self, action_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        return self._action_command(action_id, owner_id, "skip", idempotency_key, {"expected_version": expected_version},
                                    lambda c, a, p: self._finish_action(c, a, p, expected_version, "SKIPPED"))

    def defer_action(self, action_id: str, *, expected_version: int, scheduled_date: str, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        return self._action_command(action_id, owner_id, "defer", idempotency_key,
                                    {"expected_version": expected_version, "scheduled_date": scheduled_date},
                                    lambda c, a, p: self._defer_action(c, a, p, expected_version, scheduled_date))

    def feedback(self, action_id: str, payload: dict[str, Any], *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        request_hash = _hash({"action_id": action_id, "expected_version": expected_version, **payload})
        cached = self._receipt(owner_id, idempotency_key, request_hash)
        if cached is not None: return cached
        kind = _short_text(payload.get("kind"), "kind", 40)
        actual = payload.get("actual_minutes"); difficulty = payload.get("difficulty")
        if actual is not None and (isinstance(actual, bool) or not isinstance(actual, int) or not 0 <= actual <= 1440): raise ValueError("actual_minutes is invalid")
        if difficulty is not None and (isinstance(difficulty, bool) or not isinstance(difficulty, int) or not 1 <= difficulty <= 5): raise ValueError("difficulty is invalid")
        note = payload.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 2000): raise ValueError("note is invalid")
        with self.db.transaction() as connection:
            action, program = self._owned_action(connection, action_id, owner_id)
            if action["version"] != expected_version:
                raise GoalProgramConflict("action version conflict", self._action_json(action))
            feedback_id = f"feedback_{uuid.uuid4().hex}"
            connection.execute("INSERT INTO goal_action_feedback(id,owner_id,action_id,kind,actual_minutes,difficulty,reason_code,note,sensitivity,idempotency_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (feedback_id, owner_id, action_id, kind, actual, difficulty, payload.get("reason_code"), note, payload.get("sensitivity", "normal"), idempotency_key, _now()))
            if self.reviews is not None:
                self.reviews.refresh_queued_feedback(connection, action_id)
            self._event(connection, program["id"], action_id, "action.feedback_added", "user", {"feedback_id": feedback_id, "kind": kind, "count": 1})
            response = {"id": feedback_id, "action_id": action_id, "kind": kind, "actual_minutes": actual, "difficulty": difficulty,
                        "reason_code": payload.get("reason_code"), "note": note, "sensitivity": payload.get("sensitivity", "normal")}
            self._save_receipt(connection, owner_id, "action", action_id, "feedback", idempotency_key, request_hash, response)
        return response

    def transition(self, program_id: str, operation: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        return self._command(program_id, owner_id, operation, idempotency_key, {"expected_version": expected_version},
                             lambda c, row: self._transition(c, row, expected_version, operation))

    def tombstone(self, program_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        return self._command(program_id, owner_id, "delete", idempotency_key, {"expected_version": expected_version},
                             lambda c, row: self._tombstone(c, row, expected_version))

    def request_help(self, action_id: str, *, content: str, expected_version: int, idempotency_key: str, client_turn_id: str | None = None, owner_id: str = OWNER_ID) -> dict[str, Any]:
        if self.conversation is None:
            raise RuntimeError("conversation is not configured")
        content = _short_text(content, "content", 4000)
        request_hash = _hash({"action_id": action_id, "content": content, "expected_version": expected_version, "client_turn_id": client_turn_id})
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
        if self.expert_advisor is None:
            turn_key = client_turn_id.strip() if isinstance(client_turn_id,str) and client_turn_id.strip() else f"goal-help-{idempotency_key}"
            submission = self.conversation.accept_turn(thread_id, turn_key, content, [], goal_action_id=action_id)
            response = {"thread_id": submission.thread_id, "turn_id": submission.turn_id, "status": submission.status,
                        "version": submission.version, "event_cursor": submission.event_cursor, "action_id": action_id}
        else:
            turn_key = client_turn_id.strip() if isinstance(client_turn_id,str) and client_turn_id.strip() else f"goal-help-{idempotency_key}"
            submission = self.conversation.accept_turn(
                thread_id, turn_key, content, [], goal_action_id=action_id, deferred_to_expert=True,
            )
            run = self.expert_advisor.start(
                purpose="action-help", source_id=f"{action_id}:{idempotency_key}", objective=content,
                context=context, roles=("planner", "critic"), owner_id=owner_id, thread_id=thread_id,
                append_thread_message=False,
            )
            response = {"thread_id": thread_id, "turn_id": submission.turn_id, "status": "accepted",
                        "version": run["version"], "event_cursor": 0, "action_id": action_id, "agent_run_id": run["id"]}
        with self.db.transaction() as connection:
            self._save_receipt(connection, owner_id, "action", action_id, "request-help", idempotency_key, request_hash, response)
            self._event(connection, program["id"], action_id, "action.help_requested", "user", {"turn_id": response["turn_id"], "agent_run_id": response.get("agent_run_id")})
        return response

    def _activate(self, connection, row, expected_version: int) -> None:
        if row["version"] != expected_version or row["status"] != "DRAFT" or row["compile_status"] != "READY" or not row["current_program_version_id"]:
            raise GoalProgramConflict("program cannot be activated", self._program_json(connection, row["id"], row["owner_id"]))
        self._assert_source(connection, row["id"], row["owner_id"])
        structure = self._structure(connection, row["current_program_version_id"])
        now = _now()
        for item in structure["actions"]:
            connection.execute("INSERT INTO goal_actions(id,program_id,program_version_id,logical_key,scheduled_date,position,title,description,estimated_minutes,completion_criteria,required,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'SCHEDULED',?,?)",
                               (f"action_{uuid.uuid4().hex}", row["id"], row["current_program_version_id"], item["logical_key"], item["scheduled_date"], item["position"], item["title"], item["description"], item["estimated_minutes"], item["completion_criteria"], int(item["required"]), now, now))
        changed = connection.execute("UPDATE goal_programs SET status='ACTIVE',version=version+1,updated_at=? WHERE id=? AND version=? AND status='DRAFT'", (now,row["id"],expected_version)).rowcount
        if changed != 1: raise GoalProgramConflict("program version conflict", self._program_json(connection,row["id"],row["owner_id"]))
        self._event(connection,row["id"],None,"program.activated","user",{"action_count":len(structure["actions"])})

    def _finish_action(self, connection, action, program, expected_version: int, status: str) -> dict[str, Any]:
        if program["status"] != "ACTIVE" or action["status"] != "SCHEDULED" or action["version"] != expected_version:
            raise GoalProgramConflict("action cannot transition", self._action_json(action))
        was_ready = self._progress(connection, program["id"])["completion_ready"]
        field = "completed_at" if status == "COMPLETED" else "skipped_at"
        now = _now()
        changed = connection.execute(f"UPDATE goal_actions SET status=?,version=version+1,{field}=?,updated_at=? WHERE id=? AND version=? AND status='SCHEDULED'", (status,now,now,action["id"],expected_version)).rowcount
        if changed != 1: raise GoalProgramConflict("action version conflict", self._action_json(action))
        self._event(connection,program["id"],action["id"],f"action.{status.lower()}","user",{})
        progress = self._progress(connection, program["id"])
        if progress["completion_ready"] and not was_ready:
            self._event(connection,program["id"],None,"program.completion_ready","system",{})
        if self.reviews is not None:
            self.reviews.queue_if_day_closed(connection, program, action["scheduled_date"])
        return {"action": self._action_json(connection.execute("SELECT * FROM goal_actions WHERE id=?",(action["id"],)).fetchone()), "progress": progress}

    def _defer_action(self, connection, action, program, expected_version: int, scheduled_date: str) -> dict[str, Any]:
        target = date.fromisoformat(scheduled_date)
        if not date.fromisoformat(program["start_date"]) <= target <= date.fromisoformat(program["end_date"]): raise ValueError("scheduled_date is outside the program")
        if program["status"] != "ACTIVE" or action["status"] != "SCHEDULED" or action["version"] != expected_version:
            raise GoalProgramConflict("action cannot be deferred", self._action_json(action))
        now = _now()
        changed=connection.execute("UPDATE goal_actions SET status='DEFERRED',version=version+1,deferred_at=?,updated_at=? WHERE id=? AND version=? AND status='SCHEDULED'",(now,now,action["id"],expected_version)).rowcount
        if changed != 1: raise GoalProgramConflict("action version conflict", self._action_json(action))
        position=int(connection.execute("SELECT COALESCE(MAX(position),0)+1 FROM goal_actions WHERE program_id=? AND program_version_id=? AND scheduled_date=?",(program["id"],action["program_version_id"],scheduled_date)).fetchone()[0])
        replacement_id=f"action_{uuid.uuid4().hex}"
        connection.execute("INSERT INTO goal_actions(id,program_id,program_version_id,logical_key,scheduled_date,position,title,description,estimated_minutes,completion_criteria,required,status,deferred_from_action_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'SCHEDULED',?,?,?)",
                           (replacement_id,program["id"],action["program_version_id"],f"{action['logical_key']}-defer-{replacement_id[-8:]}",scheduled_date,position,action["title"],action["description"],action["estimated_minutes"],action["completion_criteria"],action["required"],action["id"],now,now))
        self._event(connection,program["id"],action["id"],"action.deferred","user",{"replacement_action_id":replacement_id,"scheduled_date":scheduled_date})
        if self.reviews is not None:
            self.reviews.queue_if_day_closed(connection, program, action["scheduled_date"])
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
        connection.execute(
            "INSERT OR IGNORE INTO memory_episodes(id,owner_id,thread_id,project_id,start_message_seq,end_message_seq,source_hash,summary,sensitivity,retrieval_policy,status,created_at) "
            "VALUES (?,?,?,NULL,0,0,?,?,'normal','thread','ACTIVE',?)",
            (episode_id,row["owner_id"],row["source_thread_id"],source_hash,summary,_now()),
        )
        episode=connection.execute("SELECT id FROM memory_episodes WHERE owner_id=? AND thread_id=? AND start_message_seq=0 AND end_message_seq=0 AND source_hash=?",(row["owner_id"],row["source_thread_id"],source_hash)).fetchone()
        connection.execute("UPDATE goal_programs SET completion_summary=?,completion_episode_id=? WHERE id=?",(summary,episode["id"],row["id"]))

    def _tombstone(self, connection, row, expected_version: int) -> None:
        if row["version"]!=expected_version: raise GoalProgramConflict("program version conflict",self._program_json(connection,row["id"],row["owner_id"]))
        changed=connection.execute("UPDATE goal_programs SET deleted_at=?,updated_at=?,version=version+1 WHERE id=? AND version=? AND deleted_at IS NULL",(_now(),_now(),row["id"],expected_version)).rowcount
        if changed!=1: raise GoalProgramConflict("program already deleted",self._program_json(connection,row["id"],row["owner_id"]))
        self._event(connection,row["id"],None,"program.tombstoned","user",{})

    def _command(self, program_id, owner_id, operation, key, payload, mutate):
        request_hash=_hash(payload); cached=self._receipt(owner_id,key,request_hash)
        if cached is not None:return cached
        with self.db.transaction() as connection:
            row=self._program_row(connection,program_id,owner_id); mutate(connection,row)
            response=self._program_json(connection,program_id,owner_id)
            self._save_receipt(connection,owner_id,"program",program_id,operation,key,request_hash,response)
        return response

    def _action_command(self, action_id, owner_id, operation, key, payload, mutate):
        request_hash=_hash(payload); cached=self._receipt(owner_id,key,request_hash)
        if cached is not None:return cached
        with self.db.transaction() as connection:
            action,program=self._owned_action(connection,action_id,owner_id); response=mutate(connection,action,program)
            self._save_receipt(connection,owner_id,"action",action_id,operation,key,request_hash,response)
        return response

    def _receipt(self, owner_id, key, request_hash):
        if not isinstance(key,str) or not key.strip() or len(key)>200: raise ValueError("Idempotency-Key is required")
        with self.db.connection() as connection: row=connection.execute("SELECT request_hash,response_json FROM goal_command_receipts WHERE owner_id=? AND idempotency_key=?",(owner_id,key)).fetchone()
        if row is None:return None
        if row["request_hash"]!=request_hash: raise GoalProgramConflict("idempotency key reused with different payload")
        return json.loads(row["response_json"])

    def _save_receipt(self, connection, owner_id, aggregate_type, aggregate_id, operation, key, request_hash, response):
        connection.execute("INSERT INTO goal_command_receipts(id,owner_id,aggregate_type,aggregate_id,operation,idempotency_key,request_hash,response_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",(f"receipt_{uuid.uuid4().hex}",owner_id,aggregate_type,aggregate_id,operation,key,request_hash,_json(response),_now()))

    def _event(self, connection, program_id, action_id, event_type, actor, data):
        row=connection.execute("SELECT next_event_seq FROM goal_programs WHERE id=?",(program_id,)).fetchone(); seq=int(row["next_event_seq"])
        connection.execute("INSERT INTO goal_program_events(event_id,program_id,seq,action_id,type,actor,schema_version,occurred_at,data_json) VALUES (?,?,?,?,?,?,1,?,?)",(f"goalevt_{uuid.uuid4().hex}",program_id,seq,action_id,event_type,actor,_now(),_json(data)))
        connection.execute("UPDATE goal_programs SET next_event_seq=next_event_seq+1 WHERE id=?",(program_id,))

    def _program_row(self, connection, program_id, owner_id):
        row=connection.execute("SELECT * FROM goal_programs WHERE id=? AND owner_id=?",(program_id,owner_id)).fetchone()
        if row is None:raise GoalProgramNotFound(program_id)
        return row

    def _owned_action(self, connection, action_id, owner_id):
        action=connection.execute("SELECT a.* FROM goal_actions a JOIN goal_programs p ON p.id=a.program_id WHERE a.id=? AND p.owner_id=?",(action_id,owner_id)).fetchone()
        if action is None:raise GoalProgramNotFound(action_id)
        return action,self._program_row(connection,action["program_id"],owner_id)

    def _assert_source(self, connection, program_id, owner_id):
        row=connection.execute("SELECT p.*,v.markdown_content,v.status source_status,v.content_hash current_source_hash,d.current_version_id,t.owner_id thread_owner FROM goal_programs p JOIN plan_documents d ON d.id=p.source_plan_document_id JOIN plan_document_versions v ON v.id=p.source_plan_document_version_id JOIN threads t ON t.id=p.source_thread_id WHERE p.id=? AND p.owner_id=? AND d.thread_id=p.source_thread_id AND v.plan_document_id=d.id",(program_id,owner_id)).fetchone()
        if row is None or row["thread_owner"]!=owner_id or row["source_status"]!="committed" or row["current_source_hash"]!=row["source_plan_content_hash"]: raise GoalProgramConflict("source plan snapshot is invalid")
        return row

    def _structure(self, connection, version_id):
        row=connection.execute("SELECT structure_json FROM goal_program_versions WHERE id=?",(version_id,)).fetchone()
        if row is None:raise GoalProgramConflict("program version is missing")
        return json.loads(row["structure_json"])

    def _program_json(self, connection, program_id, owner_id):
        row=self._program_row(connection,program_id,owner_id); structure=self._structure(connection,row["current_program_version_id"]) if row["current_program_version_id"] else None
        actions=connection.execute("SELECT * FROM goal_actions WHERE program_id=? ORDER BY scheduled_date,position,id",(program_id,)).fetchall()
        result=self._program_summary(row); result.update({"source_thread_id":row["source_thread_id"],"source_plan_document_id":row["source_plan_document_id"],"source_plan_document_version_id":row["source_plan_document_version_id"],"source_plan_content_hash":row["source_plan_content_hash"],"compile_status":row["compile_status"],"compile_error_code":row["compile_error_code"],"daily_minutes":row["daily_minutes"],"current_program_version_id":row["current_program_version_id"],"structure":structure,"actions":[self._action_json(a) for a in actions],"progress":self._progress(connection,program_id),"next_event_seq":row["next_event_seq"],"deleted_at":row["deleted_at"],"completion_summary":row["completion_summary"],"completion_episode_id":row["completion_episode_id"]})
        return result

    @staticmethod
    def _program_summary(row):return {"id":row["id"],"objective_title":row["objective_title"],"objective_summary":row["objective_summary"],"status":row["status"],"timezone":row["timezone"],"start_date":row["start_date"],"end_date":row["end_date"],"version":row["version"]}

    @staticmethod
    def _action_json(row):return {key:row[key] for key in ("id","program_id","program_version_id","logical_key","scheduled_date","position","title","description","estimated_minutes","completion_criteria","status","version","completed_at","skipped_at","deferred_at","cancelled_at","deferred_from_action_id","cancel_reason")} | {"required":bool(row["required"])}

    def _progress(self, connection, program_id):
        rows=connection.execute("SELECT required,status FROM goal_actions WHERE program_id=?",(program_id,)).fetchall()
        eligible=[r for r in rows if r["status"] not in {"DEFERRED","CANCELLED"}]
        required=[r for r in eligible if r["required"]]; completed=sum(r["status"]=="COMPLETED" for r in required)
        return {"required_completed":completed,"required_total":len(required),"completion_rate":completed/len(required) if required else 1.0,"completion_ready":completed==len(required),"optional_completed":sum(r["status"]=="COMPLETED" and not r["required"] for r in eligible)}


def _program_dates(start_value: str, end_value: str | None) -> tuple[date,date,bool]:
    try:start=date.fromisoformat(start_value)
    except (TypeError,ValueError) as exc:raise ValueError("start_date must be YYYY-MM-DD") from exc
    defaulted=end_value is None
    try:end=date.fromisoformat(end_value) if end_value else start+timedelta(days=27)
    except (TypeError,ValueError) as exc:raise ValueError("requested_end_date must be YYYY-MM-DD") from exc
    days=(end-start).days+1
    if not 1<=days<=28:raise ValueError("program duration must be between 1 and 28 days")
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
