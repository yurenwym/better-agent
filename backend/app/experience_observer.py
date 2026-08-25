from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


TERMINAL_RUN_EVENTS = {"run.completed", "run.failed", "run.cancelled", "budget.exhausted"}
TERMINAL_AGENT_EVENTS = {"agent.run.completed", "agent.run.failed", "agent.run.cancelled"}
TERMINAL_THREAD_EVENTS = {"research.completed", "research.failed", "research.cancelled"}
TERMINAL_GOAL_EVENTS = {"program.completed", "program.cancelled", "adjustment.accepted", "adjustment.rejected"}


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
        created = 0
        skipped_non_terminal = sum(item.get("non_terminal_count", 0) for item in sources)
        by_lineage: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for source in sources:
            if source["terminal"]: by_lineage[source["lineage"]].append(source)
        existing = {item["lineage_group_hash"] for item in self.evolution.list_experiences(owner_id=owner_id) if item["source_kind"] != "manual"}
        bundle_id = self.evolution.bundles.active("stable").id
        for lineage, candidates in by_lineage.items():
            if lineage in existing: continue
            source = self._choose(candidates); evidence = self._safe_evidence(candidates)
            try:
                self.evolution.record_experience(
                    owner_id=owner_id, task_type=source["task_type"], outcome=source["outcome"], lineage_group_hash=lineage,
                    source_content_hash=_digest(evidence), runtime_bundle_id=source.get("runtime_bundle_id") or bundle_id, dataset_partition="DISCOVERY",
                    idempotency_key=f"observer:{source['source_kind']}:{source['source_id']}:{source['source_event_id']}:{source['signal_type']}", source_kind=source["source_kind"],
                    source_id=source["source_id"], source_event_id=source["source_event_id"], signal_type=source["signal_type"], severity=source["severity"], evidence=evidence,
                    failure_tags=sorted({tag for item in candidates for tag in item["failure_tags"]}), observed_at=source["occurred_at"],
                )
                created += 1
            except Exception as exc:
                message = str(exc)
                if "UNIQUE constraint failed" not in message and "lineage partition is immutable" not in message: raise
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO evolution_observer_offsets(stream_kind,owner_id,last_row_id,updated_at) VALUES ('all',?,?,?) ON CONFLICT(stream_kind,owner_id) DO UPDATE SET last_row_id=last_row_id+1,updated_at=excluded.updated_at", (owner_id, 0, _now()))
        return {"created": created, "skipped_non_terminal": skipped_non_terminal, "observed": len(sources)}

    def _run_sources(self, owner_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT e.*,g.project_id FROM events e JOIN goals g ON g.id=e.goal_id WHERE COALESCE(g.project_id,'local-user')=? ORDER BY e.run_id,e.seq",
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
        if chosen["type"] == "budget.exhausted": tags.append("budget_exhausted")
        if chosen["type"] == "run.failed": tags.append("run_failed")
        if chosen["type"] == "run.cancelled": tags.append("run_cancelled")
        if any(row["type"] == "run.retrying" for row in rows): tags.append("retry")
        source = self._source("run", run_id, chosen, bool(terminal), "conversation", self._outcome(chosen["type"]), tags, data)
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
            source = self._source("agent_run", run_id, chosen, bool(terminal), "expert", self._outcome(chosen["type"]), ["expert_failed"] if chosen["type"] == "agent.run.failed" else [], json.loads(chosen["data_json"]), runtime_bundle_id=items[0]["runtime_bundle_id"])
            source["non_terminal_count"] = sum(row["type"] not in TERMINAL_AGENT_EVENTS for row in items)
            result.append(source)
        return result

    def _thread_sources(self, owner_id: str) -> list[dict[str, Any]]:
        if self.thread_events is None: return []
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT e.*,t.owner_id FROM thread_events e JOIN threads t ON t.id=e.thread_id WHERE t.owner_id=? ORDER BY e.thread_id,e.seq",
                (owner_id,),
            ).fetchall()
        grouped: dict[str, list[Any]] = defaultdict(list)
        for row in rows: grouped[row["thread_id"]].append(row)
        result = []
        for thread_id, items in grouped.items():
            terminal = [row for row in items if row["type"] in TERMINAL_THREAD_EVENTS]
            for row in terminal:
                data = json.loads(row["data_json"])
                result.append(self._source("thread", thread_id, row, True, "research", self._outcome(row["type"]), ["research_failed"] if row["type"] == "research.failed" else [], data))
        return result

    def _goal_sources(self, owner_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT e.*,p.owner_id,p.id program_id FROM goal_program_events e JOIN goal_programs p ON p.id=e.program_id WHERE p.owner_id=? ORDER BY e.program_id,e.seq",
                (owner_id,),
            ).fetchall()
        return [self._source("goal_program", row["program_id"], row, True, "goal_program", self._outcome(row["type"]), [], json.loads(row["data_json"])) for row in rows if row["type"] in TERMINAL_GOAL_EVENTS]

    @staticmethod
    def _source(kind, source_id, row, terminal, task_type, outcome, tags, data, runtime_bundle_id=None):
        return {"source_kind": kind, "source_id": source_id, "source_event_id": row["event_id"], "signal_type": row["type"].replace(".", "_"), "severity": "error" if outcome == "failure" else "info", "occurred_at": row["occurred_at"], "lineage": _digest(f"{kind}:{source_id}"), "task_type": task_type, "outcome": outcome, "failure_tags": tags, "data": data, "terminal": terminal, "runtime_bundle_id": runtime_bundle_id}

    @staticmethod
    def _choose(items):
        return sorted(items, key=lambda item: (item["outcome"] == "success", item["occurred_at"]))[-1]

    @staticmethod
    def _outcome(event_type: str) -> str:
        return "success" if event_type.endswith("completed") else "cancelled" if event_type.endswith("cancelled") else "failure"

    @staticmethod
    def _safe_evidence(items):
        source = items[-1]
        return {"event_count": len(items), "terminal_event": source["source_event_id"], "retry_count": int(source["data"].get("retry_count", 0)), "failure_tags": sorted({tag for item in items for tag in item["failure_tags"]}), "source_kinds": sorted({item["source_kind"] for item in items})}
