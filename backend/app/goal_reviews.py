from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .goal_programs import OWNER_ID, GoalProgramNotFound, _hash, _json, _now
from .goal_programs import _timezone


class GoalReviewService:
    def __init__(self, programs, compiler, adjustments, *, queue_delay_seconds: int = 2) -> None:
        self.programs = programs
        self.compiler = compiler
        self.adjustments = adjustments
        self.db = programs.db
        self.queue_delay_seconds = queue_delay_seconds

    def queue_if_day_closed(self, connection, program, local_date: str) -> str | None:
        if program["status"] != "ACTIVE":
            return None
        pending = connection.execute(
            "SELECT 1 FROM goal_actions WHERE program_id=? AND scheduled_date=? AND status='SCHEDULED' LIMIT 1",
            (program["id"], local_date),
        ).fetchone()
        if pending is not None:
            return None
        actions = connection.execute(
            "SELECT * FROM goal_actions WHERE program_id=? AND scheduled_date=? AND status IN ('COMPLETED','SKIPPED','DEFERRED') "
            "ORDER BY position,id",
            (program["id"], local_date),
        ).fetchall()
        if not actions:
            return None
        existing = connection.execute(
            "SELECT id FROM goal_daily_reviews WHERE owner_id=? AND program_id=? AND local_date=?",
            (program["owner_id"], program["id"], local_date),
        ).fetchone()
        if existing is not None:
            return existing["id"]
        return self._queue(connection, program, local_date, actions)

    def queue_due_reviews(self) -> None:
        with self.db.transaction() as connection:
            programs = connection.execute(
                "SELECT * FROM goal_programs WHERE status='ACTIVE' AND deleted_at IS NULL ORDER BY id"
            ).fetchall()
            for program in programs:
                local_today = _local_date(program["timezone"])
                dates = connection.execute(
                    "SELECT DISTINCT scheduled_date FROM goal_actions WHERE program_id=? AND scheduled_date<? ORDER BY scheduled_date",
                    (program["id"], local_today),
                ).fetchall()
                for item in dates:
                    existing = connection.execute(
                        "SELECT 1 FROM goal_daily_reviews WHERE owner_id=? AND program_id=? AND local_date=?",
                        (program["owner_id"], program["id"], item["scheduled_date"]),
                    ).fetchone()
                    if existing is not None:
                        continue
                    actions = connection.execute(
                        "SELECT * FROM goal_actions WHERE program_id=? AND scheduled_date=? AND status!='CANCELLED' ORDER BY position,id",
                        (program["id"], item["scheduled_date"]),
                    ).fetchall()
                    if actions:
                        self._queue(connection, program, item["scheduled_date"], actions)

    def _queue(self, connection, program, local_date: str, actions) -> str:
        snapshots = []
        for action in actions:
            feedback = connection.execute(
                "SELECT actual_minutes,difficulty FROM goal_action_feedback WHERE action_id=? "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (action["id"],),
            ).fetchone()
            snapshots.append({
                "action_id": action["id"], "action_version": action["version"], "status": action["status"],
                "title": action["title"], "estimated_minutes": action["estimated_minutes"],
                "actual_minutes": feedback["actual_minutes"] if feedback else None,
                "difficulty": feedback["difficulty"] if feedback else None,
            })
        snapshots.sort(key=lambda item: item["action_id"])
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
        for item in snapshots:
            connection.execute(
                "INSERT INTO goal_review_action_snapshots(review_id,action_id,action_version,status,title,estimated_minutes,actual_minutes,difficulty) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (review_id, item["action_id"], item["action_version"], item["status"], item["title"],
                 item["estimated_minutes"], item["actual_minutes"], item["difficulty"]),
            )
        self.programs._event(connection, program["id"], None, "review.queued", "system", {"review_id": review_id, "local_date": local_date})
        return review_id

    def refresh_queued_feedback(self, connection, action_id: str) -> None:
        row = connection.execute(
            "SELECT s.review_id,s.estimated_minutes FROM goal_review_action_snapshots s "
            "JOIN goal_daily_reviews r ON r.id=s.review_id WHERE s.action_id=? AND r.status='QUEUED'",
            (action_id,),
        ).fetchone()
        if row is None:
            return
        feedback = connection.execute(
            "SELECT actual_minutes,difficulty FROM goal_action_feedback WHERE action_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (action_id,),
        ).fetchone()
        if feedback is None:
            return
        connection.execute(
            "UPDATE goal_review_action_snapshots SET actual_minutes=?,difficulty=? WHERE review_id=? AND action_id=?",
            (feedback["actual_minutes"], feedback["difficulty"], row["review_id"], action_id),
        )
        snapshots = [dict(item) for item in connection.execute(
            "SELECT action_id,action_version,status,title,estimated_minutes,actual_minutes,difficulty "
            "FROM goal_review_action_snapshots WHERE review_id=? ORDER BY action_id",
            (row["review_id"],),
        ).fetchall()]
        connection.execute(
            "UPDATE goal_daily_reviews SET source_hash=?,signals_json=?,updated_at=? WHERE id=? AND status='QUEUED'",
            (_hash(snapshots), _json(_signals(snapshots)), _now(), row["review_id"]),
        )

    def claim_next(self, owner: str, lease_seconds: int) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT r.* FROM goal_daily_reviews r JOIN goal_programs p ON p.id=r.program_id "
                "WHERE p.deleted_at IS NULL AND p.status IN ('ACTIVE','COMPLETED') AND "
                "((r.status='QUEUED' AND (r.lease_until IS NULL OR r.lease_until<=?)) OR "
                "(r.status='RUNNING' AND r.lease_until<=?)) ORDER BY r.created_at,r.id LIMIT 1",
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
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
                "UPDATE goal_daily_reviews SET lease_until=?,updated_at=? WHERE id=? AND status='RUNNING' AND lease_owner=? AND lease_until>?",
                ((now + timedelta(seconds=lease_seconds)).isoformat(), now.isoformat(), review_id, owner, now.isoformat()),
            ).rowcount == 1

    def evidence(self, review_id: str, owner: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            review = connection.execute(
                "SELECT r.*,p.objective_title,p.objective_summary,p.current_program_version_id,p.version program_version "
                "FROM goal_daily_reviews r JOIN goal_programs p ON p.id=r.program_id "
                "WHERE r.id=? AND r.lease_owner=? AND r.status='RUNNING' AND r.lease_until>?",
                (review_id, owner, _now()),
            ).fetchone()
            if review is None:
                raise PermissionError("review lease lost")
            actions = [dict(row) for row in connection.execute(
                "SELECT action_id,status,title,estimated_minutes,actual_minutes,difficulty FROM goal_review_action_snapshots "
                "WHERE review_id=? ORDER BY action_id",
                (review_id,),
            ).fetchall()]
        return {
            "review_id": review_id, "program_id": review["program_id"], "local_date": review["local_date"],
            "objective_title": review["objective_title"], "objective_summary": review["objective_summary"],
            "program_version": review["program_version"], "signals": json.loads(review["signals_json"]),
            "completed": sum(item["status"] == "COMPLETED" for item in actions),
            "interrupted": sum(item["status"] in {"SKIPPED", "DEFERRED"} for item in actions),
            "actions": actions,
        }

    def complete(self, review_id: str, owner: str, result: dict[str, Any], proposal_id: str | None) -> None:
        now = _now()
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM goal_daily_reviews WHERE id=? AND status='RUNNING' AND lease_owner=? AND lease_until>?",
                (review_id, owner, now),
            ).fetchone()
            if row is None:
                raise PermissionError("review lease lost")
            connection.execute(
                "UPDATE goal_daily_reviews SET status='COMPLETED',summary=?,encouragement=?,needs_adjustment=?,adjustment_reason=?,"
                "proposal_id=?,lease_owner=NULL,lease_until=NULL,error_code=NULL,completed_at=?,updated_at=? WHERE id=?",
                (result["summary"], result["encouragement"], int(result["needs_adjustment"]), result["adjustment_reason"], proposal_id, now, now, review_id),
            )
            self.programs._event(connection, row["program_id"], None, "review.completed", "model", {"review_id": review_id, "proposal_id": proposal_id})

    def fail(self, review_id: str, owner: str, code: str) -> None:
        with self.db.transaction() as connection:
            now = _now()
            row = connection.execute(
                "SELECT attempts,program_id FROM goal_daily_reviews "
                "WHERE id=? AND status='RUNNING' AND lease_owner=? AND lease_until>?",
                (review_id, owner, now),
            ).fetchone()
            if row is None:
                raise PermissionError("review lease lost")
            status = "QUEUED" if row["attempts"] < 2 else "FAILED"
            connection.execute(
                "UPDATE goal_daily_reviews SET status=?,lease_owner=NULL,lease_until=NULL,error_code=?,updated_at=? WHERE id=?",
                (status, code[:80], now, review_id),
            )

    def retry(self, review_id: str, *, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        request_hash=_hash({"review_id":review_id})
        if not isinstance(idempotency_key,str) or not idempotency_key.strip() or len(idempotency_key)>200:raise ValueError("Idempotency-Key is required")
        with self.db.transaction() as connection:
            receipt=connection.execute("SELECT request_hash,response_json FROM goal_command_receipts WHERE owner_id=? AND idempotency_key=?",(owner_id,idempotency_key)).fetchone()
            if receipt is not None:
                if receipt["request_hash"]!=request_hash:raise ValueError("idempotency key reused with different payload")
                return json.loads(receipt["response_json"])
            row=connection.execute("SELECT r.*,p.owner_id program_owner FROM goal_daily_reviews r JOIN goal_programs p ON p.id=r.program_id WHERE r.id=? AND r.owner_id=?",(review_id,owner_id)).fetchone()
            if row is None:raise GoalProgramNotFound(review_id)
            if row["status"]!="FAILED":raise ValueError("only failed reviews can be retried")
            now=_now();available=(datetime.now(timezone.utc)+timedelta(seconds=self.queue_delay_seconds)).isoformat()
            connection.execute("UPDATE goal_daily_reviews SET status='QUEUED',attempts=0,lease_owner=NULL,lease_until=?,error_code=NULL,updated_at=? WHERE id=?",(available,now,review_id))
            self.programs._event(connection,row["program_id"],None,"review.retry_queued","user",{"review_id":review_id})
            response=self.for_program_date(row["program_id"],row["local_date"],owner_id,connection=connection)
            self.programs._save_receipt(connection,owner_id,"review",review_id,"retry",idempotency_key,request_hash,response)
        return response

    def for_program_date(self, program_id: str, local_date: str, owner_id: str = OWNER_ID, *, connection=None) -> dict[str, Any] | None:
        owns_connection = connection is None
        connection = connection or self.db._connect()
        try:
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
            }
        finally:
            if owns_connection:
                connection.close()


class ManagedGoalReviewWorker:
    def __init__(self, service: GoalReviewService, *, poll_interval: float = .2, lease_seconds: int = 30, expert_advisor=None) -> None:
        self.service = service
        self.owner = f"goal-review-worker-{uuid.uuid4().hex}"
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self._task = None
        self._stop = None
        self.expert_advisor = expert_advisor

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="goal-review-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        task, self._task = self._task, None
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def run_once(self) -> bool:
        self.service.queue_due_reviews()
        review = self.service.claim_next(self.owner, self.lease_seconds)
        if review is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(review["id"]))
        try:
            evidence = self.service.evidence(review["id"], self.owner)
            if self.expert_advisor is not None:
                context = self.service.programs._model_context(evidence["program_id"], "reflector", "daily_review")
                advice = await self.expert_advisor.advise(
                    purpose="review", source_id=review["id"], objective="审阅每日执行证据并提出是否需要调整的建议",
                    context={"daily_evidence": evidence}, roles=("planner", "critic"),
                    thread_id=context.thread_id, runtime_bundle_id=context.runtime_bundle_id,
                )
                if advice is not None:
                    evidence = {**evidence, "expert_advice": advice}
            context = self.service.programs._model_context(evidence["program_id"], "reflector", "daily_review")
            result = await self.service.programs._call_model(context, self.service.compiler.review(evidence))
            proposal_id = None
            if evidence["signals"] and result["needs_adjustment"]:
                program = self.service.programs.get(evidence["program_id"])
                proposal = await self.service.adjustments.propose(
                    evidence["program_id"], reason=result["adjustment_reason"], expected_version=program["version"],
                    idempotency_key=f"auto-review-adjustment-{review['id']}", actor="model",
                )
                proposal_id = proposal["id"]
            self.service.complete(review["id"], self.owner, result, proposal_id)
        except PermissionError:
            pass
        except Exception as exc:
            with contextlib.suppress(PermissionError):
                self.service.fail(review["id"], self.owner, getattr(exc,"code",type(exc).__name__.upper()))
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def _loop(self) -> None:
        while not self._stop.is_set():
            if not await self.run_once():
                try:
                    await asyncio.wait_for(self._stop.wait(), self.poll_interval)
                except asyncio.TimeoutError:
                    pass

    async def _heartbeat(self, review_id: str) -> None:
        while True:
            await asyncio.sleep(max(self.lease_seconds / 3, .05))
            if not self.service.renew(review_id, self.owner, self.lease_seconds):
                return


def _signals(snapshots: list[dict[str, Any]]) -> list[str]:
    signals = []
    if any(item["status"] == "SCHEDULED" for item in snapshots):
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
