from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import Database
from ..events import ThreadEventStore
from .models import Evidence, ResearchPlan, Source


TERMINAL = {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}


class ResearchConflict(ValueError): pass


@dataclass(frozen=True)
class ResearchJob:
    id: str
    thread_id: str
    source_turn_id: str
    schedule_id: str | None
    retry_of_job_id: str | None
    trigger_kind: str
    occurrence_key: str
    topic: str
    source_scopes: tuple[str, ...]
    status: str
    phase: str
    attempts: int
    max_attempts: int
    cancel_requested_at: str | None
    created_at: str
    updated_at: str
    report_title: str | None = None
    report_markdown: str | None = None
    source_count: int = 0
    evidence_count: int = 0
    assistant_message_id: str | None = None
    failure_reason_code: str | None = None
    failure_details: dict[str, Any] | None = None
    traceability: tuple[dict[str, Any], ...] = ()
    missing_requirements: tuple[str, ...] = ()


class ResearchService:
    def __init__(self, db: Database, events: ThreadEventStore, engine=None) -> None:
        self.db = db
        self.events = events
        self.engine = engine
        self.notifications = None

    def create_manual(
        self, thread_id: str, topic: str, client_request_id: str,
        source_scopes: tuple[str, ...], root_budget_id: str | None = None,
        runtime_bundle_id: str | None = None,
    ) -> ResearchJob:
        key = f"manual:{thread_id}:{client_request_id}"
        with self.db.connection() as connection:
            row = connection.execute("SELECT id FROM research_jobs WHERE occurrence_key=?", (key,)).fetchone()
        if row: return self.get(row["id"])
        return self._create_anchor_job(
            thread_id, topic, source_scopes, "manual", key, f"research:{client_request_id}",
            root_budget_id=root_budget_id, runtime_bundle_id=runtime_bundle_id,
        )

    def create_from_turn(
        self, turn_id: str, topic: str, source_scopes: tuple[str, ...], *, connection=None,
    ) -> ResearchJob:
        with (self.db.transaction() if connection is None else nullcontext(connection)) as connection:
            turn = connection.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if not turn: raise KeyError(turn_id)
            prior = connection.execute("SELECT id FROM research_jobs WHERE source_turn_id=?", (turn_id,)).fetchone()
            if prior: return self._job(prior["id"], connection)
            now = _now(); job_id = f"research_{uuid.uuid4().hex}"; message_id = f"message_{uuid.uuid4().hex}"
            next_seq = int(connection.execute("SELECT COALESCE(MAX(message_seq),0)+1 FROM thread_messages WHERE thread_id=?", (turn["thread_id"],)).fetchone()[0])
            connection.execute("INSERT INTO research_jobs(id,thread_id,source_turn_id,trigger_kind,occurrence_key,topic,source_scopes_json,status,phase,available_at,created_at,updated_at,root_budget_id) VALUES (?,?,?,'manual',?, ?,?,'QUEUED','queued',?,?,?,?)", (job_id, turn["thread_id"], turn_id, f"manual:{turn_id}", topic, json.dumps(source_scopes), now, now, now, turn["root_budget_id"]))
            connection.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,presentation,research_job_id,created_at) VALUES (?,?,?,'assistant','','streaming',1,0,?,'standard',?,?)", (message_id, turn["thread_id"], turn_id, next_seq, job_id, now))
            connection.execute("INSERT INTO research_reports(job_id,assistant_message_id,created_at,updated_at) VALUES (?,?,?,?)", (job_id, message_id, now, now))
            connection.execute("UPDATE turns SET status='COMPLETED',policy='start_research',content_shape='research',reason_code='explicit_deep_research',version=version+1,updated_at=? WHERE id=?", (now, turn_id))
            connection.execute("UPDATE turn_jobs SET status='COMPLETED',lease_owner=NULL,lease_until=NULL,finished_at=? WHERE turn_id=?", (now, turn_id))
            self.events.append(turn["thread_id"], turn_id, "research.queued", "research_worker", {"job_id": job_id, "trigger_kind": "manual"}, connection=connection, occurred_at=now)
            self.events.append(turn["thread_id"], turn_id, "message.started", "research_worker", {"message_id": message_id, "generation": 1, "presentation": "standard", "research_job_id": job_id}, connection=connection, occurred_at=now)
            return self._job(job_id, connection)

    def _create_anchor_job(
        self, thread_id: str, topic: str, source_scopes: tuple[str, ...], trigger_kind: str,
        occurrence_key: str, client_turn_id: str, *, schedule_id: str | None = None,
        retry_of_job_id: str | None = None, root_budget_id: str | None = None,
        runtime_bundle_id: str | None = None,
    ) -> ResearchJob:
        topic = topic.strip()
        if not topic or len(topic) > 2000: raise ValueError("research topic must be 1-2000 characters")
        scopes = tuple(dict.fromkeys(source_scopes or ("web",)))
        if not set(scopes) <= {"web", "local_note"}: raise ValueError("invalid research source scope")
        now = _now(); turn_id = f"turn_{uuid.uuid4().hex}"; job_id = f"research_{uuid.uuid4().hex}"
        user_id = f"message_{uuid.uuid4().hex}"; assistant_id = f"message_{uuid.uuid4().hex}"
        with self.db.transaction() as connection:
            existing = connection.execute("SELECT id FROM research_jobs WHERE occurrence_key=?", (occurrence_key,)).fetchone()
            if existing: return self._job(existing["id"], connection)
            thread = connection.execute(
                "SELECT active_turn_id,owner_id FROM threads WHERE id=? AND deleted_at IS NULL", (thread_id,),
            ).fetchone()
            if not thread: raise KeyError(thread_id)
            if thread["active_turn_id"]:
                active = connection.execute("SELECT status FROM turns WHERE id=?", (thread["active_turn_id"],)).fetchone()
                if active and active["status"] not in TERMINAL: raise ResearchConflict("thread has an active turn")
            if self.db.backend == "postgresql":
                from ..costs import CostService
                costs = CostService(self.db)
                if root_budget_id is None:
                    root_budget_id = costs.create_default_root_budget(
                        thread["owner_id"], "research", job_id, connection=connection,
                    )["id"]
                elif connection.execute(
                    "SELECT 1 FROM task_budget_roots WHERE id=? AND owner_id=?",
                    (root_budget_id, thread["owner_id"]),
                ).fetchone() is None:
                    raise ResearchConflict("research root budget is missing or belongs to another owner")
            if runtime_bundle_id is None:
                active_bundle = connection.execute(
                    "SELECT bundle_id FROM runtime_channels WHERE name='stable'",
                ).fetchone()
                runtime_bundle_id = active_bundle["bundle_id"] if active_bundle else None
            if runtime_bundle_id is not None and connection.execute(
                "SELECT 1 FROM runtime_bundles WHERE id=?", (runtime_bundle_id,),
            ).fetchone() is None:
                raise ResearchConflict("research runtime bundle does not exist")
            connection.execute(
                "INSERT INTO turns(id,thread_id,client_turn_id,status,policy,content_shape,reason_code,version,runtime_bundle_id,root_budget_id,created_at,updated_at) "
                "VALUES (?,?,?,'COMPLETED','start_research','research',?,1,?,?,?,?)",
                (
                    turn_id, thread_id, client_turn_id,
                    "scheduled_research" if trigger_kind == "scheduled" else "explicit_deep_research",
                    runtime_bundle_id, root_budget_id, now, now,
                ),
            )
            next_seq = int(connection.execute("SELECT COALESCE(MAX(message_seq),0)+1 FROM thread_messages WHERE thread_id=?", (thread_id,)).fetchone()[0])
            connection.execute(
                "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,presentation,created_at,completed_at) "
                "VALUES (?,?,?,'user',?,'ready',1,?,?, 'standard',?,?)",
                (user_id, thread_id, turn_id, topic, len(topic), next_seq, now, now),
            )
            connection.execute(
                "INSERT INTO research_jobs(id,thread_id,source_turn_id,schedule_id,retry_of_job_id,trigger_kind,occurrence_key,topic,source_scopes_json,status,phase,available_at,created_at,updated_at,root_budget_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,'QUEUED','queued',?,?,?,?)",
                (job_id, thread_id, turn_id, schedule_id, retry_of_job_id, trigger_kind, occurrence_key, topic, json.dumps(scopes), now, now, now, root_budget_id),
            )
            connection.execute(
                "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,presentation,research_job_id,created_at) "
                "VALUES (?,?,?,'assistant','','streaming',1,0,?,'standard',?,?)",
                (assistant_id, thread_id, turn_id, next_seq + 1, job_id, now),
            )
            connection.execute(
                "INSERT INTO research_reports(job_id,assistant_message_id,created_at,updated_at) VALUES (?,?,?,?)",
                (job_id, assistant_id, now, now),
            )
            self.events.append(thread_id, turn_id, "research.queued", "research_worker", {"job_id": job_id, "trigger_kind": trigger_kind, "schedule_id": schedule_id}, connection=connection, occurred_at=now)
            self.events.append(thread_id, turn_id, "message.started", "research_worker", {"message_id": assistant_id, "generation": 1, "presentation": "standard", "research_job_id": job_id}, connection=connection, occurred_at=now)
        return self.get(job_id)

    def claim_next(self, owner: str, lease_seconds: int) -> ResearchJob | None:
        now = _now(); until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self.db.transaction() as connection:
            exhausted=connection.execute("SELECT * FROM research_jobs WHERE status='RUNNING' AND attempts>=max_attempts AND lease_until<=? ORDER BY created_at,id",(now,)).fetchall()
            for stale in exhausted:
                if stale["cancel_requested_at"]:
                    message_id=connection.execute("SELECT assistant_message_id FROM research_reports WHERE job_id=?",(stale["id"],)).fetchone()[0]
                    connection.execute("UPDATE research_jobs SET status='CANCELLED',phase='cancelled',lease_owner=NULL,lease_until=NULL,finished_at=?,updated_at=? WHERE id=?",(now,now,stale["id"]))
                    connection.execute("UPDATE research_job_attempts SET status='CANCELLED',finished_at=? WHERE job_id=? AND status='RUNNING'",(now,stale["id"]))
                    connection.execute("UPDATE thread_messages SET status='cancelled',completed_at=? WHERE research_job_id=? AND status='streaming'",(now,stale["id"]))
                    self.events.append(stale["thread_id"],stale["source_turn_id"],"research.cancelled","research_worker",{"job_id":stale["id"]},connection=connection,occurred_at=now)
                    self.events.append(stale["thread_id"],stale["source_turn_id"],"message.completed","research_worker",{"message_id":message_id,"generation":1,"finish_reason":"cancelled"},connection=connection,occurred_at=now)
                    continue
                connection.execute("UPDATE research_jobs SET status='FAILED',phase='failed',lease_owner=NULL,lease_until=NULL,finished_at=?,updated_at=?,last_error_json=? WHERE id=?",(now,now,json.dumps({"reason_code":"max_attempts_exhausted"}),stale["id"]))
                connection.execute("UPDATE research_job_attempts SET status='LEASE_LOST',finished_at=? WHERE job_id=? AND status='RUNNING'",(now,stale["id"]))
                self.events.append(stale["thread_id"],stale["source_turn_id"],"research.failed","research_worker",{"job_id":stale["id"],"reason_code":"max_attempts_exhausted","retryable":False},connection=connection,occurred_at=now)
                self._fail_message(stale,"max_attempts_exhausted",connection,now)
            row = connection.execute(
                "SELECT * FROM research_jobs WHERE attempts < max_attempts AND available_at<=? AND (status='QUEUED' OR (status='RUNNING' AND lease_until<=?)) ORDER BY created_at,id LIMIT 1",
                (now, now),
            ).fetchone()
            if not row: return None
            if row["status"] == "RUNNING":
                connection.execute("UPDATE research_job_attempts SET status='LEASE_LOST',finished_at=? WHERE job_id=? AND status='RUNNING'", (now, row["id"]))
            attempt = int(row["attempts"]) + 1
            connection.execute(
                "UPDATE research_jobs SET status='RUNNING',phase=CASE WHEN phase='queued' THEN 'planning' ELSE phase END,lease_owner=?,lease_until=?,attempts=?,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",
                (owner, until, attempt, now, now, row["id"]),
            )
            connection.execute(
                "INSERT INTO research_job_attempts(id,job_id,attempt,lease_owner,status,started_at) VALUES (?,?,?,?,'RUNNING',?)",
                (f"research_attempt_{uuid.uuid4().hex}", row["id"], attempt, owner, now),
            )
            self.events.append(row["thread_id"], row["source_turn_id"], "research.started", "research_worker", {"job_id": row["id"], "attempt": attempt}, connection=connection, occurred_at=now)
            return self._job(row["id"], connection)

    def claim(self, job_id: str, owner: str, lease_seconds: int) -> ResearchJob | None:
        """Claim one known queued job without consuming another owner's queue item."""
        now = _now(); until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM research_jobs WHERE id=? AND status='QUEUED' "
                "AND attempts<max_attempts AND available_at<=?", (job_id, now),
            ).fetchone()
            if row is None:
                return None
            attempt = int(row["attempts"]) + 1
            updated = connection.execute(
                "UPDATE research_jobs SET status='RUNNING',phase='planning',lease_owner=?,lease_until=?,"
                "attempts=?,started_at=COALESCE(started_at,?),updated_at=? WHERE id=? AND status='QUEUED'",
                (owner, until, attempt, now, now, job_id),
            )
            if updated.rowcount != 1:
                return None
            connection.execute(
                "INSERT INTO research_job_attempts(id,job_id,attempt,lease_owner,status,started_at) "
                "VALUES (?,?,?,?,'RUNNING',?)",
                (f"research_attempt_{uuid.uuid4().hex}", job_id, attempt, owner, now),
            )
            self.events.append(
                row["thread_id"], row["source_turn_id"], "research.started", "research_worker",
                {"job_id": job_id, "attempt": attempt}, connection=connection, occurred_at=now,
            )
            return self._job(job_id, connection)

    def renew(self, job_id: str, owner: str, lease_seconds: int) -> bool:
        now = _now(); until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self.db.transaction() as connection:
            result = connection.execute("UPDATE research_jobs SET lease_until=?,updated_at=? WHERE id=? AND status='RUNNING' AND lease_owner=? AND lease_until>?", (until, now, job_id, owner, now))
            return result.rowcount == 1

    def set_phase(self, job_id: str, owner: str, phase: str, detail: str = "") -> ResearchJob:
        now = _now()
        with self.db.transaction() as connection:
            row = self._owned(job_id, owner, connection)
            connection.execute("UPDATE research_jobs SET phase=?,updated_at=? WHERE id=?", (phase, now, job_id))
            self.events.append(row["thread_id"], row["source_turn_id"], "research.phase_changed", "research_worker", {"job_id": job_id, "phase": phase, "detail": detail[:300]}, connection=connection, occurred_at=now)
        return self.get(job_id)

    def apply_event(self, job_id: str, owner: str, event) -> None:
        if event.type == "phase": self.set_phase(job_id, owner, event.phase, str(event.data.get("detail", ""))); return
        with self.db.transaction() as connection:
            row = self._owned(job_id, owner, connection); now = _now()
            if event.type == "plan":
                connection.execute("UPDATE research_reports SET title=?,outline_json=?,updated_at=? WHERE job_id=?", (event.data.get("title"), json.dumps(event.data, ensure_ascii=False), now, job_id))
                kind = "research.plan_ready"; data = {"job_id": job_id, "title": event.data.get("title"), "section_count": len(event.data.get("sections", [])), "query_count": len(event.data.get("queries", []))}
            elif event.type == "sources":
                for source in event.data.get("items", []):
                    existing=connection.execute("SELECT ordinal FROM research_sources WHERE id=? AND job_id=?",(source.id,job_id)).fetchone()
                    if existing: continue
                    ordinal=int(connection.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM research_sources WHERE job_id=?",(job_id,)).fetchone()[0])
                    connection.execute(
                        "INSERT INTO research_sources(id,job_id,ordinal,kind,canonical_url,locator,title,content,content_hash,published_at,retrieved_at,quality_score,metadata_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                        (source.id, job_id, ordinal, source.kind, source.canonical_url, source.locator, source.title, source.content,
                         source.content_hash, source.published_at, source.retrieved_at, source.quality_score, json.dumps(source.metadata)),
                    )
                kind = "research.sources_updated"; data = {"job_id": job_id, "source_count": int(event.data.get("count", 0)),"diagnostics":event.data.get("diagnostics",{})}
            elif event.type == "evidence":
                for evidence in event.data.get("items", []):
                    connection.execute(
                        "INSERT OR IGNORE INTO research_evidence(id,job_id,source_id,text,date_hint,relevance,created_at) VALUES (?,?,?,?,?,?,?)",
                        (evidence.id, job_id, evidence.source_id, evidence.text, evidence.date_hint, evidence.relevance, now),
                    )
                return
            elif event.type == "section":
                ordinal = int(event.data["ordinal"]); section_id = f"research_section_{job_id}_{ordinal}"
                connection.execute("INSERT INTO research_sections(id,job_id,ordinal,heading,status,markdown,summary,updated_at,completed_at) VALUES (?,?,?,?,'COMPLETED',?,?,?,?) ON CONFLICT(job_id,ordinal) DO UPDATE SET heading=excluded.heading,status='COMPLETED',markdown=excluded.markdown,summary=excluded.summary,updated_at=excluded.updated_at,completed_at=excluded.completed_at", (section_id, job_id, ordinal, event.data["heading"], event.data.get("markdown", ""), event.data.get("summary", ""), now, now))
                kind = "research.section_completed"; data = {"job_id": job_id, "section_id": section_id, "ordinal": ordinal}
            elif event.type == "report":
                return
            else: return
            self.events.append(row["thread_id"], row["source_turn_id"], kind, "research_worker", data, connection=connection, occurred_at=now)

    def complete(
        self, job_id: str, owner: str, title: str, markdown: str,
        source_count: int, evidence_count: int, traceability=(),
    ) -> ResearchJob:
        return self._complete_delivery(
            job_id, owner, title, markdown, source_count, evidence_count,
            "COMPLETED", "completed", traceability, (),
        )

    def complete_partial(
        self, job_id: str, owner: str, title: str, markdown: str,
        source_count: int, evidence_count: int, traceability, missing_requirements,
    ) -> ResearchJob:
        missing = tuple(str(item).strip()[:300] for item in missing_requirements if str(item).strip())[:12]
        if not missing:
            raise ValueError("partial research requires missing requirements")
        return self._complete_delivery(
            job_id, owner, title, markdown, source_count, evidence_count,
            "PARTIAL", "partial", traceability, missing,
        )

    def _complete_delivery(
        self, job_id: str, owner: str, title: str, markdown: str,
        source_count: int, evidence_count: int, status: str, phase: str,
        traceability, missing_requirements,
    ) -> ResearchJob:
        now = _now()
        with self.db.transaction() as connection:
            row = self._owned(job_id, owner, connection)
            if row["cancel_requested_at"]:raise ResearchConflict("research was cancelled before completion")
            report = connection.execute("SELECT assistant_message_id FROM research_reports WHERE job_id=?", (job_id,)).fetchone()
            message_id = report["assistant_message_id"]
            summary = json.dumps({
                "traceability": list(traceability),
                "missing_requirements": list(missing_requirements),
                "completion_status": status,
            }, ensure_ascii=False)
            connection.execute("UPDATE research_reports SET title=?,summary_json=?,markdown=?,partial_markdown=?,source_count=?,evidence_count=?,updated_at=?,completed_at=? WHERE job_id=?", (title, summary, markdown, markdown, source_count, evidence_count, now, now, job_id))
            connection.execute("UPDATE thread_messages SET content=?,content_length=?,status='ready',completed_at=? WHERE id=?", (markdown, len(markdown), now, message_id))
            connection.execute("UPDATE research_jobs SET status=?,phase=?,lease_owner=NULL,lease_until=NULL,finished_at=?,updated_at=? WHERE id=?", (status, phase, now, now, job_id))
            connection.execute("UPDATE research_job_attempts SET status='COMPLETED',finished_at=? WHERE job_id=? AND status='RUNNING'", (now, job_id))
            self.events.append(row["thread_id"], row["source_turn_id"], "message.snapshot", "research_worker", {"message_id": message_id, "generation": 1, "content": markdown, "offset": len(markdown)}, connection=connection, occurred_at=now)
            self.events.append(row["thread_id"], row["source_turn_id"], "message.completed", "research_worker", {"message_id": message_id, "generation": 1, "finish_reason": phase}, connection=connection, occurred_at=now)
            event_type = "research.completed" if status == "COMPLETED" else "research.partial"
            self.events.append(row["thread_id"], row["source_turn_id"], event_type, "research_worker", {"job_id": job_id, "report_url": f"/api/research/jobs/{job_id}/report", "message_id": message_id, "source_count": source_count, "missing_requirements": list(missing_requirements)}, connection=connection, occurred_at=now)
        return self.get(job_id)

    def fail(self, job_id: str, owner: str, reason: str, retryable: bool = False,
             diagnostics: dict[str, Any] | None = None) -> ResearchJob:
        now = _now()
        error = {"reason_code": reason, **_safe_failure_details(diagnostics)}
        with self.db.transaction() as connection:
            row = self._owned(job_id, owner, connection)
            if retryable and int(row["attempts"]) < int(row["max_attempts"]):
                available=(datetime.now(timezone.utc)+timedelta(seconds=2**int(row["attempts"]))).isoformat()
                connection.execute("UPDATE research_jobs SET status='QUEUED',last_error_json=?,lease_owner=NULL,lease_until=NULL,available_at=?,updated_at=? WHERE id=?",(json.dumps(error,ensure_ascii=False),available,now,job_id))
                connection.execute("UPDATE research_job_attempts SET status='FAILED',finished_at=?,error_json=? WHERE job_id=? AND status='RUNNING'",(now,json.dumps(error,ensure_ascii=False),job_id))
                self.events.append(row["thread_id"],row["source_turn_id"],"research.retry_scheduled","research_worker",{"job_id":job_id,"attempt":row["attempts"],"available_at":available,"reason_code":reason},connection=connection,occurred_at=now)
                return self._job(job_id,connection)
            connection.execute("UPDATE research_jobs SET status='FAILED',phase='failed',last_error_json=?,lease_owner=NULL,lease_until=NULL,finished_at=?,updated_at=? WHERE id=?", (json.dumps(error,ensure_ascii=False), now, now, job_id))
            connection.execute("UPDATE research_job_attempts SET status='FAILED',finished_at=?,error_json=? WHERE job_id=? AND status='RUNNING'", (now, json.dumps(error,ensure_ascii=False), job_id))
            self.events.append(row["thread_id"], row["source_turn_id"], "research.failed", "research_worker", {"job_id": job_id, "reason_code": reason, "retryable": retryable}, connection=connection, occurred_at=now)
            self._fail_message(row,reason,connection,now)
        return self.get(job_id)

    def completed_sections(self,job_id:str)->dict[int,dict[str,str]]:
        with self.db.connection() as connection:
            rows=connection.execute("SELECT ordinal,heading,markdown,summary FROM research_sections WHERE job_id=? AND status='COMPLETED' ORDER BY ordinal",(job_id,)).fetchall()
        return {int(row["ordinal"]):dict(row) for row in rows}

    def recovery_context(self,job_id:str)->tuple[dict[int,dict[str,str]],tuple[Source,...],tuple[Evidence,...],ResearchPlan|None]:
        sections=self.completed_sections(job_id)
        if not sections:return {},(),(),None
        with self.db.connection() as connection:
            source_rows=connection.execute("SELECT * FROM research_sources WHERE job_id=? ORDER BY ordinal",(job_id,)).fetchall()
            evidence_rows=connection.execute("SELECT * FROM research_evidence WHERE job_id=? ORDER BY created_at,id",(job_id,)).fetchall()
            report=connection.execute("SELECT title,outline_json FROM research_reports WHERE job_id=?",(job_id,)).fetchone()
        import re
        cited={item for section in sections.values() for item in re.findall(r"\[\[source:([^\]]+)\]\]",section["markdown"])}
        sources={row["id"]:Source(row["id"],int(row["ordinal"]),row["kind"],row["canonical_url"],row["locator"],row["title"],row["content"],row["published_at"],row["retrieved_at"],float(row["quality_score"]),row["content_hash"],json.loads(row["metadata_json"] or "{}")) for row in source_rows}
        evidence=tuple(Evidence(row["id"],row["source_id"],row["text"],row["date_hint"],float(row["relevance"])) for row in evidence_rows)
        aliases={source_id.removeprefix("source_"):source_id for source_id in sources}
        normalized_cited={aliases.get(item,item) for item in cited}
        if not normalized_cited or not normalized_cited<=set(sources) or not normalized_cited<={item.source_id for item in evidence}:return {},(),(),None
        outline=json.loads(report["outline_json"] or "{}") if report else {}
        plan=ResearchPlan(str(outline.get("title") or report["title"]),tuple(outline.get("sections",())),tuple(outline.get("queries",()))) if outline.get("sections") else None
        return sections,tuple(sources.values()),evidence,plan

    def recovery_sections(self,job_id:str)->dict[int,dict[str,str]]:
        return self.recovery_context(job_id)[0]

    def _fail_message(self,row,reason,connection,now)->None:
        report=connection.execute("SELECT assistant_message_id FROM research_reports WHERE job_id=?",(row["id"],)).fetchone()
        if not report or not report["assistant_message_id"]:return
        message_id=report["assistant_message_id"]
        connection.execute("UPDATE thread_messages SET status='failed',completed_at=? WHERE id=? AND status='streaming'",(now,message_id))
        self.events.append(row["thread_id"],row["source_turn_id"],"message.completed","research_worker",{"message_id":message_id,"generation":1,"finish_reason":"failed","reason_code":reason},connection=connection,occurred_at=now)

    def finish_cancelled(self, job_id: str, owner: str) -> ResearchJob:
        now = _now()
        with self.db.transaction() as connection:
            row = self._owned(job_id, owner, connection)
            connection.execute("UPDATE research_jobs SET status='CANCELLED',phase='cancelled',lease_owner=NULL,lease_until=NULL,finished_at=?,updated_at=? WHERE id=?", (now, now, job_id))
            connection.execute("UPDATE research_job_attempts SET status='CANCELLED',finished_at=? WHERE job_id=? AND status='RUNNING'", (now, job_id))
            connection.execute("UPDATE thread_messages SET status='cancelled',completed_at=? WHERE research_job_id=?", (now, job_id))
            self.events.append(row["thread_id"], row["source_turn_id"], "research.cancelled", "research_worker", {"job_id": job_id}, connection=connection, occurred_at=now)
        return self.get(job_id)

    def cancel(self, job_id: str) -> ResearchJob:
        now = _now()
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM research_jobs WHERE id=?", (job_id,)).fetchone()
            if not row: raise KeyError(job_id)
            if row["status"] in TERMINAL: return self._job(job_id, connection)
            if row["status"] == "RUNNING":
                connection.execute("UPDATE research_jobs SET cancel_requested_at=?,updated_at=? WHERE id=?", (now, now, job_id))
            else:
                connection.execute("UPDATE research_jobs SET status='CANCELLED',phase='cancelled',cancel_requested_at=?,finished_at=?,updated_at=? WHERE id=?", (now, now, now, job_id))
                connection.execute("UPDATE thread_messages SET status='cancelled',completed_at=? WHERE research_job_id=?", (now, job_id))
                self.events.append(row["thread_id"], row["source_turn_id"], "research.cancelled", "research_worker", {"job_id": job_id}, connection=connection, occurred_at=now)
        return self.get(job_id)

    def retry(self, job_id: str, topic: str | None, client_key: str) -> ResearchJob:
        old = self.get(job_id); key = f"retry:{job_id}:{client_key}"
        with self.db.connection() as connection:
            prior = connection.execute("SELECT id FROM research_jobs WHERE occurrence_key=?", (key,)).fetchone()
            source = connection.execute("SELECT root_budget_id FROM research_jobs WHERE id=?", (job_id,)).fetchone()
        if prior: return self.get(prior["id"])
        return self._create_anchor_job(
            old.thread_id, topic or old.topic, old.source_scopes, "retry", key,
            f"research-retry:{client_key}", retry_of_job_id=job_id,
            root_budget_id=source["root_budget_id"] if source else None,
            runtime_bundle_id=self._runtime_bundle_id(old.source_turn_id),
        )

    def _runtime_bundle_id(self, turn_id: str) -> str | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT runtime_bundle_id FROM turns WHERE id=?", (turn_id,),
            ).fetchone()
        return row["runtime_bundle_id"] if row else None

    def delete(self, job_id: str) -> None:
        with self.db.transaction() as connection:
            row = connection.execute("SELECT status FROM research_jobs WHERE id=?", (job_id,)).fetchone()
            if not row: raise KeyError(job_id)
            if row["status"] not in TERMINAL:
                raise ResearchConflict("active research must be cancelled before deletion")
            connection.execute("UPDATE research_jobs SET retry_of_job_id=NULL WHERE retry_of_job_id=?", (job_id,))
            connection.execute("UPDATE research_schedules SET last_job_id=NULL WHERE last_job_id=?", (job_id,))
            replacement = "该深度研究记录已删除。"
            connection.execute(
                "UPDATE thread_messages SET content=?,content_length=?,status='ready',research_job_id=NULL WHERE research_job_id=?",
                (replacement, len(replacement), job_id),
            )
            connection.execute("DELETE FROM research_jobs WHERE id=?", (job_id,))

    def get(self, job_id: str) -> ResearchJob:
        with self.db.connection() as connection: return self._job(job_id, connection)

    def list(self, *, thread_id: str | None = None, schedule_id: str | None = None, status: str | None = None, limit: int = 50, offset: int = 0) -> list[ResearchJob]:
        clauses, args = [], []
        for field, value in (("thread_id", thread_id), ("schedule_id", schedule_id), ("status", status)):
            if value: clauses.append(f"j.{field}=?"); args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db.connection() as connection:
            rows = connection.execute(f"SELECT j.id FROM research_jobs j{where} ORDER BY j.created_at DESC,j.id LIMIT ? OFFSET ?", (*args, min(max(limit, 1), 100), max(offset, 0))).fetchall()
            return [self._job(row["id"], connection) for row in rows]

    @staticmethod
    def _owned(job_id: str, owner: str, connection):
        row = connection.execute("SELECT * FROM research_jobs WHERE id=? AND status='RUNNING' AND lease_owner=? AND lease_until>?", (job_id, owner, _now())).fetchone()
        if not row: raise PermissionError("research job lease lost")
        return row

    def _job(self, job_id: str, connection) -> ResearchJob:
        greatest = "GREATEST" if self.db.backend == "postgresql" else "MAX"
        row = connection.execute(
            "SELECT j.*,r.title report_title,r.markdown report_markdown,r.summary_json,"
            f"{greatest}(r.source_count,(SELECT COUNT(*) FROM research_sources s WHERE s.job_id=j.id)) source_count,"
            f"{greatest}(r.evidence_count,(SELECT COUNT(*) FROM research_evidence e WHERE e.job_id=j.id)) evidence_count,"
            "r.assistant_message_id FROM research_jobs j LEFT JOIN research_reports r ON r.job_id=j.id WHERE j.id=?",
            (job_id,),
        ).fetchone()
        if not row: raise KeyError(job_id)
        error = json.loads(row["last_error_json"] or "{}")
        summary = json.loads(row["summary_json"] or "{}")
        details = {key:value for key,value in error.items() if key != "reason_code"}
        return ResearchJob(row["id"], row["thread_id"], row["source_turn_id"], row["schedule_id"], row["retry_of_job_id"], row["trigger_kind"], row["occurrence_key"], row["topic"], tuple(json.loads(row["source_scopes_json"])), row["status"], row["phase"], int(row["attempts"]), int(row["max_attempts"]), row["cancel_requested_at"], row["created_at"], row["updated_at"], row["report_title"], row["report_markdown"], int(row["source_count"] or 0), int(row["evidence_count"] or 0), row["assistant_message_id"], error.get("reason_code"), details or None, tuple(summary.get("traceability", ())), tuple(summary.get("missing_requirements", ())))


def _now() -> str: return datetime.now(timezone.utc).isoformat()


def _safe_failure_details(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    missing = value.get("missing_requirements")
    if not isinstance(missing, (list, tuple)):
        return {}
    items = [str(item).strip()[:300] for item in missing if str(item).strip()][:12]
    result = {"missing_requirements": items} if items else {}
    if value.get("repair_error") in {"timeout", "unknowncitation", "failed"}:
        result["repair_error"] = value["repair_error"]
    return result
