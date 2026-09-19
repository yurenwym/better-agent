from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


TERMINAL_RUN_EVENTS = {"run.completed", "run.failed", "run.cancelled"}
TERMINAL_AGENT_EVENTS = {"agent.run.completed", "agent.run.failed", "agent.run.cancelled"}
TERMINAL_THREAD_EVENTS = {
    "turn.completed", "turn.failed", "turn.cancelled",
    "research.completed", "research.partial", "research.failed", "research.cancelled",
}
TERMINAL_GOAL_EVENTS = {
    "program.completed", "program.cancelled", "program.compile_failed", "program.tombstoned",
    "action.completed", "action.skipped", "action.deferred",
    "review.completed", "adjustment.accepted", "adjustment.rejected",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ExperienceObserver:
    """Deterministically projects committed terminal events into quality experiences."""

    def __init__(self, db, events, evolution, *, thread_events=None) -> None:
        self.db = db
        self.events = events
        self.evolution = evolution
        self.thread_events = thread_events

    def observe(self, owner_id: str = "local-user") -> dict[str, int]:
        sources = self._run_sources(owner_id) + self._agent_sources(owner_id) + self._thread_sources(owner_id) + self._goal_sources(owner_id)
        learning_sources = list(sources)
        created = 0
        skipped_non_terminal = sum(item.get("non_terminal_count", 0) for item in sources)
        tables = {"run": "events", "agent": "agent_events", "thread": "thread_events", "goal": "goal_program_events"}
        with self.db.connection() as connection:
            offsets = {
                stream: int((connection.execute(
                    "SELECT last_row_id FROM evolution_observer_offsets WHERE stream_kind=? AND owner_id=?", (stream, owner_id),
                ).fetchone() or {"last_row_id": 0})["last_row_id"])
                for stream in tables
            }
            # Advance only through rows actually read. A later MAX(row_id)
            # can include an event committed after the source query.
            highwaters = {
                stream: max((item["row_id"] for item in sources if item["stream"] == stream), default=offsets[stream])
                for stream in tables
            }
        sources = [item for item in sources if item["row_id"] > offsets[item["stream"]]]
        by_lineage: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for source in sources:
            if source["terminal"]: by_lineage[source["lineage"]].append(source)
        existing = {item["lineage_group_hash"] for item in self.evolution.list_experiences(owner_id=owner_id) if item["source_kind"] != "manual"}
        try:
            for lineage, candidates in by_lineage.items():
                # Lineage groups independent evidence; it is not a permanent
                # deduplication key for later corrections to the same task.
                # The legacy experience index has one row per root. Every new
                # terminal event is separately preserved in learning_jobs below.
                if lineage in existing:
                    continue
                source = self._choose(candidates); evidence = self._safe_evidence(candidates)
                self.evolution.record_experience(
                    owner_id=owner_id, task_type=source["task_type"], outcome=source["outcome"], lineage_group_hash=lineage,
                    source_content_hash=_digest(evidence), runtime_bundle_id=source.get("runtime_bundle_id"), dataset_partition="DISCOVERY",
                    idempotency_key=f"observer:{source['source_kind']}:{source['source_id']}:{source['source_event_id']}:{source['signal_type']}", source_kind=source["source_kind"],
                    source_id=source["source_id"], source_event_id=source["source_event_id"], signal_type=source["signal_type"], severity=source["severity"], evidence=evidence,
                    failure_tags=sorted({tag for item in candidates for tag in item["failure_tags"]}), observed_at=source["occurred_at"],
                    root_task_id=source["root_task_id"], target_role=source.get("target_role", ""),
                    provenance=source.get("provenance", "production"), source_version=source.get("source_version", ""),
                )
                created += 1
            with self.db.transaction() as connection:
                if getattr(self, "learning", None) is not None:
                    # Scan committed source identities, not sequence allocation
                    # order: PostgreSQL transactions may commit out of order.
                    for source in learning_sources:
                        if source["terminal"]:
                            self.learning.enqueue(owner_id, "experience", source["source_event_id"], _digest(source["data"]),
                                                  source["root_task_id"], provenance=source.get("provenance", "production"), connection=connection)
                for stream, highwater in highwaters.items():
                    connection.execute(
                        "INSERT INTO evolution_observer_offsets(stream_kind,owner_id,last_row_id,last_success_row_id,last_error,last_error_at,updated_at) "
                        "VALUES (?,?,?, ?,NULL,NULL,?) ON CONFLICT(stream_kind,owner_id) DO UPDATE SET "
                        "last_row_id=CASE WHEN excluded.last_row_id>evolution_observer_offsets.last_row_id THEN excluded.last_row_id ELSE evolution_observer_offsets.last_row_id END,"
                        "last_success_row_id=CASE WHEN excluded.last_success_row_id>evolution_observer_offsets.last_success_row_id THEN excluded.last_success_row_id ELSE evolution_observer_offsets.last_success_row_id END,last_error=NULL,last_error_at=NULL,updated_at=excluded.updated_at",
                        (stream, owner_id, highwater, highwater, _now()),
                    )
        except Exception as exc:
            with self.db.transaction() as connection:
                for stream in tables:
                    connection.execute(
                        "INSERT INTO evolution_observer_offsets(stream_kind,owner_id,last_row_id,last_success_row_id,last_error,last_error_at,updated_at) "
                        "VALUES (?,?,0,0,?,?,?) ON CONFLICT(stream_kind,owner_id) DO UPDATE SET last_error=excluded.last_error,last_error_at=excluded.last_error_at,updated_at=excluded.updated_at",
                        (stream, owner_id, str(exc)[:500], _now(), _now()),
                    )
            raise
        return {"created": created, "skipped_non_terminal": skipped_non_terminal, "observed": len(sources)}

    def _run_sources(self, owner_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT e.*,g.project_id,r.runtime_bundle_id,r.root_budget_id FROM events e JOIN goals g ON g.id=e.goal_id JOIN runs r ON r.id=e.run_id WHERE COALESCE(g.project_id,'local-user')=? ORDER BY e.run_id,e.seq",
                (owner_id,),
            ).fetchall()
        grouped: dict[str, list[Any]] = defaultdict(list)
        for row in rows: grouped[row["run_id"]].append(row)
        return [self._run_source(run_id, items) for run_id, items in grouped.items()]

    def _run_source(self, run_id: str, rows: list[Any]) -> dict[str, Any]:
        terminal = [row for row in rows if row["type"] in TERMINAL_RUN_EVENTS]
        chosen = terminal[-1] if terminal else rows[-1]
        data = json.loads(chosen["data_json"])
        tags = []
        if any(row["type"] == "budget.exhausted" for row in rows): tags.append("budget_exhausted")
        if chosen["type"] == "run.failed": tags.append("run_failed")
        if chosen["type"] == "run.cancelled": tags.append("run_cancelled")
        if any(row["type"] == "run.retrying" for row in rows): tags.append("retry")
        source = self._source("run", run_id, chosen, bool(terminal), "conversation", self._outcome(chosen["type"]), tags, data, runtime_bundle_id=chosen["runtime_bundle_id"], stream="run", root_task_id=str(data.get("root_task_id") or run_id))
        source["non_terminal_count"] = sum(row["type"] not in TERMINAL_RUN_EVENTS for row in rows)
        return source

    def _agent_sources(self, owner_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT e.*,r.owner_id,r.runtime_bundle_id FROM agent_events e JOIN agent_runs r ON r.id=e.agent_run_id WHERE r.owner_id=? ORDER BY e.agent_run_id,e.seq",
                (owner_id,),
            ).fetchall()
        grouped: dict[str, list[Any]] = defaultdict(list)
        for row in rows: grouped[row["agent_run_id"]].append(row)
        result = []
        for run_id, items in grouped.items():
            terminal = [row for row in items if row["type"] in TERMINAL_AGENT_EVENTS]
            chosen = terminal[-1] if terminal else items[-1]
            data = json.loads(chosen["data_json"])
            source = self._source("agent_run", run_id, chosen, bool(terminal), "expert", self._outcome(chosen["type"]), ["expert_failed"] if chosen["type"] == "agent.run.failed" else [], data, runtime_bundle_id=items[0]["runtime_bundle_id"], stream="agent", root_task_id=str(data.get("root_task_id") or run_id), target_role=str(data.get("role") or ""))
            source["non_terminal_count"] = sum(row["type"] not in TERMINAL_AGENT_EVENTS for row in items)
            result.append(source)
        return result

    def _thread_sources(self, owner_id: str) -> list[dict[str, Any]]:
        if self.thread_events is None: return []
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT e.*,t.owner_id,tr.runtime_bundle_id,tr.root_budget_id FROM thread_events e JOIN threads t ON t.id=e.thread_id LEFT JOIN turns tr ON tr.id=e.turn_id WHERE t.owner_id=? ORDER BY e.thread_id,e.seq",
                (owner_id,),
            ).fetchall()
            research_rows = connection.execute(
                "SELECT j.id,j.retry_of_job_id,j.topic FROM research_jobs j JOIN threads t ON t.id=j.thread_id WHERE t.owner_id=?",
                (owner_id,),
            ).fetchall()
        retry_of = {row["id"]: row["retry_of_job_id"] for row in research_rows}
        research_topics = {row["id"]: str(row["topic"] or "") for row in research_rows}
        result = []
        for row in rows:
            if row["type"] not in TERMINAL_THREAD_EVENTS:
                continue
            data = json.loads(row["data_json"])
            is_research = row["type"].startswith("research.")
            source_id = str(data.get("job_id") or row["turn_id"])
            task_type = "research" if is_research else "conversation"
            tags = ["research_failed" if is_research else "conversation_failed"] if row["type"].endswith("failed") else []
            root_job = source_id
            seen = set()
            while root_job in retry_of and retry_of[root_job] and root_job not in seen:
                seen.add(root_job)
                root_job = str(retry_of[root_job])
            root_task_id = str(data.get("root_task_id") or data.get("root_turn_id") or (root_job if is_research else row["turn_id"]) or source_id)
            provenance = (
                "acceptance" if is_research and research_topics.get(source_id, "").lstrip().upper().startswith("[ACCEPT-")
                else "production"
            )
            result.append(self._source("research" if is_research else "turn", source_id, row, True, task_type, self._outcome(row["type"]), tags, data, runtime_bundle_id=row["runtime_bundle_id"], stream="thread", root_task_id=root_task_id, target_role="researcher" if is_research else "conversation", provenance=provenance))
        return result

    def _goal_sources(self, owner_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT e.*,p.owner_id,p.id program_id FROM goal_program_events e JOIN goal_programs p ON p.id=e.program_id WHERE p.owner_id=? ORDER BY e.program_id,e.seq",
                (owner_id,),
            ).fetchall()
        result = []
        for row in rows:
            if row["type"] not in TERMINAL_GOAL_EVENTS:
                continue
            data = json.loads(row["data_json"])
            prefix = row["type"].split(".", 1)[0]
            aggregate_id = row["program_id"]
            if prefix == "action": aggregate_id = row["action_id"] or row["program_id"]
            elif prefix == "review": aggregate_id = str(data.get("review_id") or row["program_id"])
            elif prefix == "adjustment": aggregate_id = str(data.get("proposal_id") or row["program_id"])
            tags = ["compile_failed"] if row["type"] == "program.compile_failed" else []
            result.append(self._source(prefix, aggregate_id, row, True, prefix, self._outcome(row["type"]), tags, data, stream="goal", root_task_id=str(data.get("root_task_id") or aggregate_id)))
        return result

    @staticmethod
    def _source(kind, source_id, row, terminal, task_type, outcome, tags, data, runtime_bundle_id=None, stream="run", root_task_id="", target_role="", provenance="production"):
        error_kind = str(data.get("error_kind") or data.get("reason_code") or "").lower()
        operational = {
            "payment": "payment_required", "authentication": "authentication", "database": "database",
            "transport": "transport", "source_unavailable": "source_unavailable", "protocol": "protocol_error",
        }
        if error_kind in operational:
            tags = [*tags, operational[error_kind]]
        if str(data.get("http_status") or "") == "402":
            tags = [*tags, "http_402"]
        root = root_task_id or f"{kind}:{source_id}"
        return {"source_kind": kind, "source_id": source_id, "source_event_id": row["event_id"], "signal_type": row["type"].replace(".", "_"), "severity": "error" if outcome in {"failure", "partial"} else "info", "occurred_at": row["occurred_at"], "lineage": _digest(root), "root_task_id": root, "task_type": task_type, "outcome": outcome, "failure_tags": tags, "data": data, "terminal": terminal, "runtime_bundle_id": runtime_bundle_id, "row_id": int(row["row_id"]), "stream": stream, "target_role": target_role, "provenance": provenance}

    @staticmethod
    def _choose(items):
        return sorted(items, key=lambda item: (item["outcome"] == "success", item["occurred_at"]))[-1]

    @staticmethod
    def _outcome(event_type: str) -> str:
        if event_type.endswith(("completed", "accepted")):
            return "success"
        if event_type.endswith("partial"):
            return "partial"
        if event_type.endswith(("cancelled", "skipped", "deferred", "rejected", "tombstoned")):
            return "cancelled"
        return "failure"

    @staticmethod
    def _safe_evidence(items):
        source = items[-1]
        data = source["data"]
        return {
            "event_count": len(items), "terminal_event": source["source_event_id"],
            "retry_count": int(data.get("retry_count", 0)),
            "failure_tags": sorted({tag for item in items for tag in item["failure_tags"]}),
            "source_kinds": sorted({item["source_kind"] for item in items}),
            "root_task_id": source["root_task_id"], "target_role": source.get("target_role", ""),
            "call_id": str(data.get("call_id") or data.get("invocation_id") or ""),
            "finish_reason": str(data.get("finish_reason") or "UNKNOWN"),
            "root_budget_id": str(data.get("root_budget_id") or ""),
            "error_category": str(data.get("error_kind") or data.get("reason_code") or ""),
            "evidence_refs": [str(value) for value in data.get("evidence_refs", []) if isinstance(value, (str, int))][:20],
        }


class ManagedExperienceObserver:
    """Runs the idempotent observer as part of the application lifecycle."""

    def __init__(self, observer: ExperienceObserver, *, poll_interval: float = .5, owner_id: str = "local-user", candidate_generator=None) -> None:
        self.observer = observer
        self.poll_interval = poll_interval
        self.owner_id = owner_id
        self.candidate_generator = candidate_generator
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="experience-observer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> dict[str, int]:
        result = await asyncio.to_thread(self.observer.observe, self.owner_id)
        if getattr(self.observer, "learning", None) is not None:
            result["learning_processed"] = int(await asyncio.to_thread(self.observer.learning.run_once, self.owner_id))
            with self.observer.db.connection() as connection:
                owners = [row[0] for row in connection.execute("SELECT owner_id FROM learning_policies WHERE paused=0 AND owner_id<>?", (self.owner_id,)).fetchall()]
            for owner_id in owners:
                await asyncio.to_thread(self.observer.observe, owner_id)
                result["learning_processed"] += int(await asyncio.to_thread(self.observer.learning.run_once, owner_id))
        result["canaries_stopped"] = await asyncio.to_thread(self.observer.evolution.maintain_canaries)
        if self.candidate_generator is not None:
            generated = await asyncio.to_thread(self.candidate_generator.generate, self.owner_id)
            result["candidates_created"] = len(generated)
        return result

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                # Terminal-event producers must never fail because observation is unavailable.
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), self.poll_interval)
            except asyncio.TimeoutError:
                pass
