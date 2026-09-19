from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .goal_programs import OWNER_ID, GoalProgramNotFound, GoalProgramConflict, _hash, _json, _now
from .goal_programs import _timezone


class GoalReviewService:
    def __init__(self, programs, compiler, adjustments, *, queue_delay_seconds: int = 2) -> None:
        self.programs = programs
        self.compiler = compiler
        self.adjustments = adjustments
        self.db = programs.db
        self.queue_delay_seconds = queue_delay_seconds

    def queue_if_day_closed(self, connection, program, local_date: str, *, partial=False) -> str | None:
        # Automatic callers must never turn execution records into paid reviews.
        return None

    def queue_due_reviews(self) -> None:
        return None

    def close_day(self, program_id, local_date, *, idempotency_key, owner_id=OWNER_ID):
        from datetime import date
        if not isinstance(local_date, str) or date.fromisoformat(local_date).isoformat() != local_date:
            raise ValueError("local_date is invalid")
        request_hash = _hash({"program_id": program_id, "local_date": local_date})
        with self.db.transaction() as connection:
            self.programs._lock_command(connection, owner_id, idempotency_key)
            cached = self.programs._receipt(owner_id, idempotency_key, request_hash, identity=("program", program_id, "close_day"), connection=connection)
            if cached is not None:
                return cached
            program = self.programs._program_row(connection, program_id, owner_id, lock=True)
            if program["status"] != "ACTIVE" or program["deleted_at"]:
                raise GoalProgramConflict("only active programs can close a day")
            if local_date > _local_date(program["timezone"]):
                raise ValueError("cannot close a future day")
            existing = self.for_program_date(program_id, local_date, owner_id, connection=connection)
            if existing is None:
                actions = self.actions_for_date(connection, program_id, local_date)
                if not actions:
                    raise ValueError("no actions or execution records for this date")
                if not any(self.programs.latest_feedback(connection, action["id"], local_date)["source_ids"] or action["status"] in {"COMPLETED", "SKIPPED", "DEFERRED"} for action in actions):
                    raise ValueError("record progress or completion before closing this day")
                self._queue(connection, program, local_date, actions)
                self.programs._event(connection, program_id, None, "day.closed", "user", {"local_date": local_date})
            response = self.for_program_date(program_id, local_date, owner_id, connection=connection)
            self.programs._save_receipt(connection, owner_id, "program", program_id, "close_day", idempotency_key, request_hash, response)
            return response

    def actions_for_date(self, connection, program_id, local_date):
        actions = connection.execute("SELECT * FROM goal_actions WHERE program_id=? AND status!='CANCELLED' ORDER BY position,id", (program_id,)).fetchall()
        return [action for action in actions if action["scheduled_date"] == local_date or self.programs.latest_feedback(connection, action["id"], local_date)["source_ids"]]

    def _snapshots(self, connection, actions, local_date=None):
        snapshots = []
        for action in actions:
            feedback = self.programs.latest_feedback(connection, action["id"], local_date)
            progress = json.loads(action["progress_json"])
            if progress.get("source_action_id"):
                feedback["carried_progress"] = {key: progress[key] for key in ("source_action_id", "completed_work", "remaining_work", "output") if key in progress}
            snapshots.append({
                "action_id": action["id"], "action_version": action["version"],
                "status": "PARTIAL" if action["status"] == "SCHEDULED" and progress.get("state") == "PARTIAL" else action["status"],
                "title": action["title"], "estimated_minutes": action["estimated_minutes"],
                "actual_minutes": feedback["actual_minutes"], "difficulty": feedback["difficulty"],
                "feedback": feedback,
            })
        snapshots.sort(key=lambda item: item["action_id"])
        return snapshots

    def _store_snapshots(self, connection, review_id, snapshots):
        connection.execute("DELETE FROM goal_review_action_snapshots WHERE review_id=?", (review_id,))
        for item in snapshots:
            connection.execute(
                "INSERT INTO goal_review_action_snapshots(review_id,action_id,action_version,status,title,estimated_minutes,actual_minutes,difficulty,feedback_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (review_id, item["action_id"], item["action_version"], item["status"], item["title"],
                 item["estimated_minutes"], item["actual_minutes"], item["difficulty"], _json(item["feedback"])),
            )

    def _queue(self, connection, program, local_date: str, actions) -> str:
        snapshots = self._snapshots(connection, actions, local_date)
        signals = _signals(snapshots)
        review_id = f"review_{uuid.uuid4().hex}"
        now = _now()
        connection.execute(
            "INSERT INTO goal_daily_reviews(id,owner_id,program_id,local_date,status,source_hash,signals_json,created_at,updated_at,lease_until) "
            "VALUES (?,?,?,?,'QUEUED',?,?,?,?,?)",
            (
                review_id, program["owner_id"], program["id"], local_date,
                _hash(snapshots), _json(signals), now, now,
                (datetime.now(timezone.utc) + timedelta(seconds=self.queue_delay_seconds)).isoformat(),
            ),
        )
        self._store_snapshots(connection, review_id, snapshots)
        self.programs._event(connection, program["id"], None, "review.queued", "system", {"review_id": review_id, "local_date": local_date})
        return review_id

    def refresh_queued_feedback(self, connection, action_id: str) -> None:
        action = connection.execute("SELECT program_id,scheduled_date FROM goal_actions WHERE id=?", (action_id,)).fetchone()
        if action is not None:
            dates = {action["scheduled_date"]}
            for row in connection.execute("SELECT details_json FROM goal_action_feedback WHERE action_id=?", (action_id,)).fetchall():
                actual_date = json.loads(row["details_json"]).get("actual_date")
                if actual_date:
                    dates.add(actual_date)
            for local_date in sorted(dates):
                self.refresh_date_evidence(connection, action["program_id"], local_date)

    def refresh_date_evidence(self, connection, program_id: str, local_date: str) -> None:
        suffix = " FOR UPDATE OF r" if self.db.backend == "postgresql" else ""
        row = connection.execute(
            "SELECT r.* FROM goal_daily_reviews r WHERE r.program_id=? AND r.local_date=?" + suffix,
            (program_id, local_date),
        ).fetchone()
        if row is None:
            return
        actions = self.actions_for_date(connection, program_id, local_date)
        snapshots = self._snapshots(connection, actions, local_date)
        if _hash(snapshots) == row["source_hash"]:
            return
        if row["status"] != "QUEUED":
            connection.execute("UPDATE goal_daily_reviews SET evidence_stale=1,updated_at=? WHERE id=?", (_now(), row["id"]))
            if row["proposal_id"]:
                connection.execute("UPDATE goal_adjustment_proposals SET status='STALE',version=version+1,updated_at=? WHERE id=? AND status='PENDING'", (_now(), row["proposal_id"]))
            return
        self._store_snapshots(connection, row["id"], snapshots)
        connection.execute(
            "UPDATE goal_daily_reviews SET source_hash=?,signals_json=?,updated_at=? WHERE id=? AND status='QUEUED'",
            (_hash(snapshots), _json(_signals(snapshots)), _now(), row["id"]),
        )

    def claim_next(self, owner: str, lease_seconds: int) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT r.* FROM goal_daily_reviews r JOIN goal_programs p ON p.id=r.program_id "
                "WHERE p.deleted_at IS NULL AND p.status IN ('ACTIVE','COMPLETED') AND "
                "((r.status='QUEUED' AND (r.lease_until IS NULL OR r.lease_until<=?)) OR "
                "(r.status='RUNNING' AND r.lease_until<=?) OR "
                "(r.status='COMPLETED' AND r.evidence_stale=0 AND "
                "((r.adjustment_status='PENDING' AND (r.lease_until IS NULL OR r.lease_until<=?)) OR "
                "(r.adjustment_status='RUNNING' AND r.lease_until<=?)))) ORDER BY r.created_at,r.id LIMIT 1",
                (now.isoformat(),) * 4,
            ).fetchone()
            if row is None:
                return None
            lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
            if row["status"] == "COMPLETED":
                changed = connection.execute(
                    "UPDATE goal_daily_reviews SET adjustment_status='RUNNING',lease_owner=?,lease_until=?,"
                    "adjustment_attempts=adjustment_attempts+1,updated_at=? WHERE id=? AND status='COMPLETED' "
                    "AND evidence_stale=0 AND ((adjustment_status='PENDING' AND (lease_until IS NULL OR lease_until<=?)) "
                    "OR (adjustment_status='RUNNING' AND lease_until<=?))",
                    (owner, lease_until, now.isoformat(), row["id"], now.isoformat(), now.isoformat()),
                ).rowcount
            else:
                changed = connection.execute(
                    "UPDATE goal_daily_reviews SET status='RUNNING',lease_owner=?,lease_until=?,attempts=attempts+1,updated_at=? "
                    "WHERE id=? AND ((status='QUEUED' AND (lease_until IS NULL OR lease_until<=?)) OR (status='RUNNING' AND lease_until<=?))",
                    (owner, lease_until, now.isoformat(), row["id"], now.isoformat(), now.isoformat()),
                ).rowcount
            if changed != 1:
                return None
            return dict(connection.execute("SELECT * FROM goal_daily_reviews WHERE id=?", (row["id"],)).fetchone())

    def renew(self, review_id: str, owner: str, lease_seconds: int) -> bool:
        now = datetime.now(timezone.utc)
        with self.db.transaction() as connection:
            return connection.execute(
                "UPDATE goal_daily_reviews SET lease_until=?,updated_at=? WHERE id=? AND (status='RUNNING' OR (status='COMPLETED' AND adjustment_status='RUNNING')) AND lease_owner=? AND lease_until>? AND evidence_stale=0",
                ((now + timedelta(seconds=lease_seconds)).isoformat(), now.isoformat(), review_id, owner, now.isoformat()),
            ).rowcount == 1

    def release(self, review_id: str, owner: str) -> None:
        with self.db.transaction() as connection:
            connection.execute("UPDATE goal_daily_reviews SET lease_until=?,updated_at=? WHERE id=? AND lease_owner=?", (_now(), _now(), review_id, owner))

    def evidence(self, review_id: str, owner: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            review = connection.execute(
                "SELECT r.*,p.objective_title,p.objective_summary,p.current_program_version_id,p.version program_version,p.daily_minutes,p.schedule_constraints_json "
                "FROM goal_daily_reviews r JOIN goal_programs p ON p.id=r.program_id "
                "WHERE r.id=? AND r.lease_owner=? AND (r.status='RUNNING' OR (r.status='COMPLETED' AND r.adjustment_status='RUNNING')) AND r.lease_until>?",
                (review_id, owner, _now()),
            ).fetchone()
            if review is None:
                raise PermissionError("review lease lost")
            actions = [dict(row) for row in connection.execute(
                "SELECT action_id,status,title,estimated_minutes,actual_minutes,difficulty,feedback_json FROM goal_review_action_snapshots "
                "WHERE review_id=? ORDER BY action_id",
                (review_id,),
            ).fetchall()]
            notes: dict[str, str] = {}
            for action in actions:
                action["feedback"] = json.loads(action.pop("feedback_json"))
                if action["feedback"].get("note"):
                    notes[action["action_id"]] = action["feedback"]["note"]
            coach = None
            if review["current_program_version_id"]:
                version = connection.execute(
                    "SELECT structure_json FROM goal_program_versions WHERE id=?",
                    (review["current_program_version_id"],),
                ).fetchone()
                if version is not None:
                    try: coach = json.loads(version["structure_json"]).get("coach") or None
                    except (TypeError, ValueError): coach = None
        return {
            "review_id": review_id, "program_id": review["program_id"], "local_date": review["local_date"],
            "objective_title": review["objective_title"], "objective_summary": review["objective_summary"],
            "program_version": review["program_version"], "signals": json.loads(review["signals_json"]),
            "daily_minutes": review["daily_minutes"], "schedule_constraints":json.loads(review["schedule_constraints_json"]),
            "day_closed": True, "remaining_minutes": 0,
            "spent_minutes": sum(item["actual_minutes"] or 0 for item in actions),
            "time_complete": all(item["actual_minutes"] is not None for item in actions),
            "evidence_stale": bool(review["evidence_stale"]), "source_hash": review["source_hash"], "revision": review["revision"],
            "coach": coach,
            "completed": sum(item["status"] == "COMPLETED" for item in actions),
            "interrupted": sum(item["status"] in {"SKIPPED", "DEFERRED"} for item in actions),
            "actions": actions,
            "user_notes": [{"title": item["title"], "note": notes[item["action_id"]]} for item in actions if item["action_id"] in notes],
        }

    def complete(self, review_id: str, owner: str, result: dict[str, Any], proposal_id: str | None, *, adjustment_pending: bool = False) -> bool:
        now = _now()
        with self.db.transaction() as connection:
            program = connection.execute("SELECT program_id,owner_id FROM goal_daily_reviews WHERE id=?", (review_id,)).fetchone()
            if program is not None:
                self.programs._program_row(connection,program["program_id"],program["owner_id"],lock=True)
            row = connection.execute(
                "SELECT * FROM goal_daily_reviews WHERE id=? AND status='RUNNING' AND lease_owner=? AND lease_until>?",
                (review_id, owner, now),
            ).fetchone()
            if row is None:
                raise PermissionError("review lease lost")
            if row["evidence_stale"]:
                if proposal_id:
                    connection.execute("UPDATE goal_adjustment_proposals SET status='STALE',version=version+1 WHERE id=? AND status='PENDING'", (proposal_id,))
                connection.execute("UPDATE goal_daily_reviews SET status='FAILED',error_code='REVIEW_EVIDENCE_CHANGED',lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?", (now,review_id))
                return False
            connection.execute(
                "UPDATE goal_daily_reviews SET status='COMPLETED',summary=?,encouragement=?,needs_adjustment=?,adjustment_reason=?,"
                "proposal_id=?,lease_owner=NULL,lease_until=NULL,error_code=NULL,completed_at=?,updated_at=? WHERE id=?",
                (result["summary"], result["encouragement"], int(result["needs_adjustment"]), result["adjustment_reason"], proposal_id, now, now, review_id),
            )
            self.programs._event(connection, row["program_id"], None, "review.completed", "model", {"review_id": review_id, "proposal_id": proposal_id})
            adjustment_status = "RUNNING" if adjustment_pending else "COMPLETED" if proposal_id else "NOT_NEEDED"
            connection.execute(
                "UPDATE goal_daily_reviews SET adjustment_status=?,adjustment_attempts=?,lease_owner=?,lease_until=? WHERE id=?",
                (adjustment_status, int(adjustment_pending), owner if adjustment_pending else None,
                 row["lease_until"] if adjustment_pending else None, review_id),
            )
            return True

    def complete_adjustment(self, review_id: str, owner: str, proposal_id: str | None) -> None:
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM goal_daily_reviews WHERE id=?", (review_id,)).fetchone()
            if row is not None:
                self.programs._program_row(connection, row["program_id"], row["owner_id"], lock=True)
            row = connection.execute(
                "SELECT * FROM goal_daily_reviews WHERE id=? AND status='COMPLETED' AND adjustment_status='RUNNING' AND lease_owner=? AND lease_until>?",
                (review_id, owner, _now()),
            ).fetchone()
            if row is None:
                raise PermissionError("review lease lost")
            if row["evidence_stale"]:
                if proposal_id:
                    connection.execute("UPDATE goal_adjustment_proposals SET status='STALE',version=version+1,updated_at=? WHERE id=? AND status='PENDING'", (_now(), proposal_id))
                status, code = "FAILED", "REVIEW_EVIDENCE_CHANGED"
            else:
                status, code = ("COMPLETED" if proposal_id else "NO_CHANGE"), None
            connection.execute(
                "UPDATE goal_daily_reviews SET adjustment_status=?,adjustment_error_code=?,proposal_id=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (status, code, proposal_id, _now(), review_id),
            )
            self.programs._event(connection, row["program_id"], None, "review.adjustment_finished", "system", {"review_id": review_id, "status": status, "proposal_id": proposal_id})

    def fail(self, review_id: str, owner: str, code: str) -> None:
        with self.db.transaction() as connection:
            now = _now()
            row = connection.execute(
                "SELECT attempts,program_id,status,adjustment_attempts FROM goal_daily_reviews "
                "WHERE id=? AND (status='RUNNING' OR (status='COMPLETED' AND adjustment_status='RUNNING')) AND lease_owner=? AND lease_until>?",
                (review_id, owner, now),
            ).fetchone()
            if row is None:
                raise PermissionError("review lease lost")
            if row["status"] == "COMPLETED":
                status = "PENDING" if row["adjustment_attempts"] < 2 else "FAILED"
                available = (datetime.now(timezone.utc) + timedelta(seconds=self.queue_delay_seconds)).isoformat()
                connection.execute(
                    "UPDATE goal_daily_reviews SET adjustment_status=?,adjustment_error_code=?,lease_owner=NULL,lease_until=?,updated_at=? WHERE id=?",
                    (status, code[:80], available if status == "PENDING" else None, now, review_id),
                )
                return
            status = "QUEUED" if row["attempts"] < 2 else "FAILED"
            connection.execute(
                "UPDATE goal_daily_reviews SET status=?,lease_owner=NULL,lease_until=NULL,error_code=?,updated_at=? WHERE id=?",
                (status, code[:80], now, review_id),
            )

    def retry(self, review_id: str, *, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        request_hash=_hash({"review_id":review_id})
        if not isinstance(idempotency_key,str) or not idempotency_key.strip() or len(idempotency_key)>200:raise ValueError("Idempotency-Key is required")
        with self.db.transaction() as connection:
            self.programs._lock_command(connection,owner_id,idempotency_key)
            receipt=connection.execute("SELECT request_hash,response_json FROM goal_command_receipts WHERE owner_id=? AND idempotency_key=?",(owner_id,idempotency_key)).fetchone()
            if receipt is not None:
                if receipt["request_hash"]!=request_hash:raise ValueError("idempotency key reused with different payload")
                return json.loads(receipt["response_json"])
            row=connection.execute("SELECT r.*,p.owner_id program_owner FROM goal_daily_reviews r JOIN goal_programs p ON p.id=r.program_id WHERE r.id=? AND r.owner_id=?",(review_id,owner_id)).fetchone()
            if row is None:raise GoalProgramNotFound(review_id)
            self.programs._program_row(connection,row["program_id"],owner_id,lock=True)
            lock=" FOR UPDATE" if self.db.backend=="postgresql" else ""
            row=connection.execute("SELECT * FROM goal_daily_reviews WHERE id=? AND owner_id=?"+lock,(review_id,owner_id)).fetchone()
            if row["status"] == "COMPLETED" and not row["evidence_stale"] and row["adjustment_status"] == "FAILED":
                connection.execute("UPDATE goal_daily_reviews SET adjustment_status='PENDING',adjustment_attempts=0,adjustment_error_code=NULL,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?", (_now(), review_id))
                self.programs._event(connection, row["program_id"], None, "review.adjustment_retry_queued", "user", {"review_id": review_id})
                response = self.for_program_date(row["program_id"], row["local_date"], owner_id, connection=connection)
                self.programs._save_receipt(connection, owner_id, "review", review_id, "retry", idempotency_key, request_hash, response)
                return response
            if row["status"]!="FAILED" and not (row["status"]=="COMPLETED" and row["evidence_stale"]):raise ValueError("only failed or outdated reviews can be retried")
            history=json.loads(row["history_json"])
            history.append({"revision":row["revision"],"source_hash":row["source_hash"],"summary":row["summary"],"encouragement":row["encouragement"],"proposal_id":row["proposal_id"],"status":row["status"],"snapshots":[dict(item) for item in connection.execute("SELECT * FROM goal_review_action_snapshots WHERE review_id=?",(review_id,)).fetchall()]})
            history[-1].update({key: row[key] for key in ("needs_adjustment", "adjustment_reason", "adjustment_status", "adjustment_error_code", "adjustment_attempts", "error_code")})
            actions=self.actions_for_date(connection,row["program_id"],row["local_date"])
            snapshots=self._snapshots(connection,actions,row["local_date"])
            self._store_snapshots(connection,review_id,snapshots)
            source_hash = _hash(snapshots)
            revision = row["revision"] + int(source_hash != row["source_hash"])
            now=_now();available=(datetime.now(timezone.utc)+timedelta(seconds=self.queue_delay_seconds)).isoformat()
            connection.execute("UPDATE goal_daily_reviews SET status='QUEUED',attempts=0,lease_owner=NULL,lease_until=?,error_code=NULL,updated_at=?,evidence_stale=0,revision=?,history_json=?,source_hash=?,signals_json=?,proposal_id=NULL WHERE id=?",(available,now,revision,_json(history),source_hash,_json(_signals(snapshots)),review_id))
            connection.execute("UPDATE goal_daily_reviews SET summary=NULL,encouragement=NULL,needs_adjustment=NULL,adjustment_reason=NULL,completed_at=NULL,adjustment_status='NOT_NEEDED',adjustment_error_code=NULL,adjustment_attempts=0 WHERE id=?", (review_id,))
            self.programs._event(connection,row["program_id"],None,"review.retry_queued","user",{"review_id":review_id})
            response=self.for_program_date(row["program_id"],row["local_date"],owner_id,connection=connection)
            self.programs._save_receipt(connection,owner_id,"review",review_id,"retry",idempotency_key,request_hash,response)
        return response

    def for_program_date(self, program_id: str, local_date: str, owner_id: str = OWNER_ID, *, connection=None) -> dict[str, Any] | None:
        if connection is None:
            with self.db.connection() as owned_connection:
                return self.for_program_date(program_id, local_date, owner_id, connection=owned_connection)
        row = connection.execute(
            "SELECT r.* FROM goal_daily_reviews r JOIN goal_programs p ON p.id=r.program_id "
            "WHERE r.program_id=? AND r.local_date=? AND r.owner_id=? AND p.owner_id=?",
            (program_id, local_date, owner_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        proposal = self.adjustments._proposal_json(connection, row["proposal_id"], owner_id) if row["proposal_id"] else None
        return {
            "id": row["id"], "program_id": row["program_id"], "local_date": row["local_date"], "status": row["status"],
            "signals": json.loads(row["signals_json"]), "summary": row["summary"], "encouragement": row["encouragement"],
            "needs_adjustment": bool(row["needs_adjustment"]) if row["needs_adjustment"] is not None else None,
            "adjustment_reason": row["adjustment_reason"], "proposal": proposal, "error_code": row["error_code"],
            "evidence_stale":bool(row["evidence_stale"]), "revision":row["revision"], "history":json.loads(row["history_json"]),
            "adjustment_status": row["adjustment_status"], "adjustment_error_code": row["adjustment_error_code"],
            "adjustment_attempts": row["adjustment_attempts"],
        }


class ManagedGoalReviewWorker:
    def __init__(self, service: GoalReviewService, *, poll_interval: float = .2, lease_seconds: int = 30, expert_advisor=None, shutdown_timeout: float = 2) -> None:
        self.service = service
        self.owner = f"goal-review-worker-{uuid.uuid4().hex}"
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self._task = None
        self._stop = None
        self.expert_advisor = expert_advisor
        self.shutdown_timeout = shutdown_timeout
        self.last_success_at = None
        self.last_error_at = None
        self.last_error_code = None
        self.consecutive_failures = 0

    def health_view(self) -> dict[str, Any]:
        return {"running": self._task is not None and not self._task.done(), "last_success_at": self.last_success_at,
                "last_error_at": self.last_error_at, "last_error_code": self.last_error_code,
                "consecutive_failures": self.consecutive_failures}

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="goal-review-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        task, self._task = self._task, None
        done, _ = await asyncio.wait({task}, timeout=self.shutdown_timeout)
        if not done:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=self.shutdown_timeout)
        if done:
            with contextlib.suppress(asyncio.CancelledError):
                task.result()
        else:
            self.last_error_at = _now()
            self.last_error_code = "WORKER_SHUTDOWN_TIMEOUT"
            task.add_done_callback(_consume_task_result)

    async def run_once(self) -> bool:
        self.service.queue_due_reviews()
        review = self.service.claim_next(self.owner, self.lease_seconds)
        if review is None:
            return False
        work = asyncio.create_task(self._process(review))
        heartbeat = asyncio.create_task(self._heartbeat(review["id"]))
        try:
            done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await heartbeat
            await work
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                self.service.release(review["id"], self.owner)
            raise
        except PermissionError:
            with contextlib.suppress(Exception):
                self.service.release(review["id"], self.owner)
        except Exception as exc:
            with contextlib.suppress(PermissionError):
                self.service.fail(review["id"], self.owner, getattr(exc,"code",type(exc).__name__.upper()))
        finally:
            work.cancel()
            heartbeat.cancel()
            done, pending = await asyncio.wait({work, heartbeat}, timeout=self.shutdown_timeout)
            for task in done:
                _consume_task_result(task)
            for task in pending:
                self.last_error_at = _now()
                self.last_error_code = "MODEL_CANCELLATION_TIMEOUT"
                task.add_done_callback(_consume_task_result)
        return True

    async def _process(self, review) -> None:
        from .goal_adjustments import GoalAdjustmentNoChange

        context = self.service.programs._model_context(
            review["program_id"], "reflector", "daily_review", operation_id=f"{review['id']}:{review['revision']}",
        )
        if review["status"] != "COMPLETED":
            evidence = self.service.evidence(review["id"], self.owner)
            if evidence["evidence_stale"]:
                self.service.complete(review["id"],self.owner,{},None)
                return
            if self.expert_advisor is not None:
                advice = await self.expert_advisor.advise(
                    purpose="review", source_id=review["id"], objective="审阅每日执行证据并提出是否需要调整的建议",
                    context={"daily_evidence": evidence}, roles=("planner", "critic"),
                    thread_id=context.thread_id, runtime_bundle_id=context.runtime_bundle_id,
                    root_budget_id=context.root_budget_id,
                )
                if advice is not None:
                    evidence = {**evidence, "expert_advice": advice}
            result = await self.service.programs._call_model(context, self.service.compiler.review(evidence))
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            pending = bool(evidence["signals"] and result["needs_adjustment"])
            if not self.service.complete(review["id"], self.owner, result, None, adjustment_pending=pending) or not pending:
                return
            reason = result["adjustment_reason"]
        else:
            reason = review["adjustment_reason"]
        try:
            program = self.service.programs.get(review["program_id"])
            if program["status"] == "COMPLETED":
                raise GoalAdjustmentNoChange("program already completed")
            proposal = await self.service.adjustments.propose(
                review["program_id"], reason=reason, expected_version=program["version"],
                idempotency_key=f"auto-review-adjustment-{review['id']}-{review['revision']}", actor="model",
                root_budget_id=context.root_budget_id,
            )
            proposal_id = proposal["id"]
        except GoalAdjustmentNoChange:
            proposal_id = None
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        self.service.complete_adjustment(review["id"], self.owner, proposal_id)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = await self.run_once()
                self.last_success_at = _now()
                self.consecutive_failures = 0
                delay = 0 if worked else self.poll_interval
            except Exception as exc:
                self.last_error_at = _now()
                self.last_error_code = getattr(exc, "code", type(exc).__name__.upper())
                self.consecutive_failures += 1
                logging.getLogger(__name__).warning("Review worker cycle failed: %s", self.last_error_code)
                delay = min(max(self.poll_interval, .05) * 2 ** min(self.consecutive_failures - 1, 8), 5)
            if delay:
                try:
                    await asyncio.wait_for(self._stop.wait(), delay)
                except asyncio.TimeoutError:
                    pass

    async def _heartbeat(self, review_id: str) -> None:
        while True:
            await asyncio.sleep(max(self.lease_seconds / 3, .05))
            if not self.service.renew(review_id, self.owner, self.lease_seconds):
                raise PermissionError("review lease lost")


def _signals(snapshots: list[dict[str, Any]]) -> list[str]:
    signals = []
    if any(item["status"] in {"SCHEDULED", "PARTIAL"} for item in snapshots):
        signals.append("unfinished_actions")
    if any(item["status"] in {"SKIPPED", "DEFERRED"} for item in snapshots):
        signals.append("day_interrupted")
    if any((item["difficulty"] or 0) >= 4 for item in snapshots):
        signals.append("high_difficulty")
    if any(item["actual_minutes"] is not None and item["actual_minutes"] > item["estimated_minutes"] * 1.5 for item in snapshots):
        signals.append("time_overrun")
    return signals


def _local_date(timezone_name: str) -> str:
    return datetime.now(_timezone(timezone_name)).date().isoformat()


def _consume_task_result(task) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()
