"""Read-only, owner-authorized trace queries over the existing event streams.

A trace is assembled from the durable semantic streams that already exist: the
per-thread events (conversation, model, tool, research and expert lifecycle)
and the per-run events (the goal loop).  Three rules shape this module:

* **Authorize first.**  A caller only ever reads events inside their own owner
  scope.  Thread and task queries resolve the owner from the row itself before
  any event is read; run events are read only through a run whose owner can be
  established - from its source turn's thread, or, for a directly created goal
  run, from the trusted ``agent_loop_context`` it was persisted with.  A deleted
  thread is a tombstone and is never read through.
* **Project, do not resume.**  Events are returned through the authorized read
  projection, so legacy records surface as ``legacy_context_missing`` /
  ``unsupported_version`` and payloads are redacted.  The audit flag is the only
  thing that expands the execution-internal references.
* **Bounded reads.**  A page is a keyset over one stable order,
  ``(occurred_at, stream_kind, stream_id, seq, event_id)``.  The first request
  freezes a ``row_id`` snapshot; later pages of that cursor do not see rows
  inserted afterwards, so an earlier timestamp cannot shift the walk.  Each
  source reads ``limit + 1`` rows after the cursor.  The legacy envelope
  fallback stays capped and a truncated fallback is reported.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from .event_metadata import EVENT_SOURCES
from .event_projection import project_authorized_event
from .events import _redact_value, _row_to_event, _row_to_thread_event


#: Default and maximum page sizes.  A caller may not ask for an unbounded read,
#: and the legacy envelope fallback is capped so a wrong trace id cannot turn
#: into a full-history scan.
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
LEGACY_SCAN_LIMIT = 5_000
#: Extra rows a source may examine past the page while skipping refusals.
#: Hitting it resumes from that source; it does not mean the source ended.
SOURCE_SEEK_BUDGET = 64
_PUBLIC_TRACE_FIELDS = frozenset({
    "event_id", "seq", "type", "occurred_at", "actor", "stream_kind", "stream_id",
    "source", "operation_id", "attempt", "causation_event_id", "phase", "outcome",
    "context", "payload", "compatibility", "payload_version", "schema_version",
})


_SORT_FIELDS = ("occurred_at", "stream_kind", "stream_id", "seq", "event_id")
_SNAPSHOT_TABLES = (("thread", "thread_events"), ("run", "events"), ("agent", "agent_events"))
_STREAM_COLUMN = {"thread": "thread_id", "run": "run_id", "agent": "agent_run_id"}
_SOURCE_NAMES = ("thread", "run", "unlinked", "agent", "legacy_thread", "legacy_run", "legacy_unlinked")


def _event_sort_key(event: Any) -> tuple:
    """The one order used by SQL, the merge and the cursor."""
    kind = getattr(event, "trace_stream_kind", None) or ("thread" if hasattr(event, "thread_id") else "run")
    if kind == "thread":
        stream_id = getattr(event, "thread_id", "") or ""
    elif kind == "agent":
        stream_id = getattr(event, "agent_run_id", "") or ""
    else:
        stream_id = getattr(event, "run_id", "") or ""
    return (event.occurred_at or "", kind, stream_id, int(event.seq or 0), event.event_id or "")


def _blank_positions() -> dict[str, tuple | None]:
    return {name: None for name in _SOURCE_NAMES}


def _parse_sort_key(value: Any) -> tuple:
    if (
        not isinstance(value, list) or len(value) != 5
        or not isinstance(value[0], str) or not isinstance(value[1], str)
        or not isinstance(value[2], str) or type(value[3]) is not int or isinstance(value[3], bool)
        or not isinstance(value[4], str)
    ):
        raise ValueError("cursor must be a page token")
    return tuple(value)


def _encode_cursor(snapshot: dict[str, int], after: tuple | dict[str, tuple | None],
                   seen: bool | None = None) -> str:
    """A broadcast key parks every source. A dict stores one key per source.

    ``seen`` records whether this walk has already returned a row. It is one
    flag, not a list of event ids.
    """
    if isinstance(after, dict):
        payload: dict[str, Any] = {
            "s": [snapshot["thread"], snapshot["run"], snapshot["agent"]],
            "p": [list(after[name]) if after.get(name) is not None else None for name in _SOURCE_NAMES],
        }
    else:
        payload = {"s": [snapshot["thread"], snapshot["run"], snapshot["agent"]], "a": list(after)}
    if seen is not None:
        payload["m"] = 1 if seen else 0
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return "v1." + raw


def _decode_cursor(token: str) -> tuple[dict[str, int], dict[str, tuple | None], bool | None]:
    if not isinstance(token, str) or not token.startswith("v1."):
        raise ValueError("cursor must be a page token")
    body = token[3:]
    padded = body + ("=" * (-len(body) % 4))
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        snapshot_values = payload["s"]
        if (
            not isinstance(snapshot_values, list) or len(snapshot_values) != 3
            or any(type(item) is not int or isinstance(item, bool) or item < 0 for item in snapshot_values)
        ):
            raise ValueError("cursor must be a page token")
        if "p" in payload:
            positions = payload["p"]
            if not isinstance(positions, list) or len(positions) != len(_SOURCE_NAMES):
                raise ValueError("cursor must be a page token")
            decoded = {
                name: None if value is None else _parse_sort_key(value)
                for name, value in zip(_SOURCE_NAMES, positions)
            }
        else:
            shared = _parse_sort_key(payload["a"])
            decoded = {name: shared for name in _SOURCE_NAMES}
        if "m" not in payload:
            seen = None
        elif type(payload["m"]) is not int or payload["m"] not in (0, 1):
            raise ValueError("cursor must be a page token")
        else:
            seen = payload["m"] == 1
    except (TypeError, ValueError, KeyError):
        raise ValueError("cursor must be a page token") from None
    snapshot = {"thread": snapshot_values[0], "run": snapshot_values[1], "agent": snapshot_values[2]}
    return snapshot, decoded, seen


class _ReadScope:
    """Filters that decide which rows belong to this query."""

    def __init__(self, owner_id: str, *, thread_id: str | None = None, trace_id: str | None = None,
                 fields: tuple[str, ...] | None = None, value: str | None = None,
                 run_id: str | None = None, task_id: str | None = None, filter_task: bool = False,
                 kinds: tuple[str, ...] = ()) -> None:
        self.owner_id = owner_id
        self.thread_id = thread_id
        self.trace_id = trace_id
        self.fields = fields
        self.value = value
        self.run_id = run_id
        self.task_id = task_id
        self.filter_task = filter_task
        self.kinds = kinds


class TraceNotFound(KeyError):
    """No event in the requester's owner scope belongs to the requested trace."""


class TraceQuery:
    def __init__(self, db) -> None:
        self.db = db
        self._trace_column: bool | None = None

    # ------------------------------------------------------------------ #
    # public queries
    # ------------------------------------------------------------------ #

    def by_trace(self, trace_id: str, *, owner_id: str, audit: bool = False,
                 limit: int | None = None, cursor: int = 0) -> dict[str, Any]:
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")
        size, snapshot, positions, seen = self._position(limit, cursor)
        truncated = False
        scope = _ReadScope(owner_id, trace_id=trace_id, kinds=(
            "thread", "run", "unlinked", "legacy_thread", "legacy_run", "legacy_unlinked",
        ))

        def fetch_indexed(kind, probe, cap):
            if kind == "thread":
                return self._thread_rows(
                    owner_id, thread_id=None, trace_id=trace_id, legacy=False, cap=cap,
                    snapshot=snapshot, after=probe,
                )
            if kind == "run":
                return self._linked_run_rows(
                    owner_id, thread_id=None, trace_id=trace_id, legacy=False, cap=cap,
                    snapshot=snapshot, after=probe,
                )
            return self._unlinked_run_rows(
                owner_id, trace_id=trace_id, legacy=False, cap=cap, snapshot=snapshot, after=probe,
            )

        def fetch_legacy(kind):
            def fetch(probe, cap):
                nonlocal truncated
                events, hit, resume = self._legacy_events(kind, owner_id, trace_id, snapshot, probe)
                fetch.hit = hit
                fetch.resume = resume
                truncated = truncated or hit
                return events
            fetch.hit = False
            fetch.resume = None
            return fetch

        if not self._trace_column_present():
            sources = [(
                "thread",
                lambda probe, cap: [
                    event for event in self._scan(owner_id, cap=cap, snapshot=snapshot, after=probe)
                    if self._trace_of(event) == trace_id
                ],
            )]
        else:
            sources = [
                ("thread", lambda probe, cap: fetch_indexed("thread", probe, cap)),
                ("run", lambda probe, cap: fetch_indexed("run", probe, cap)),
                ("unlinked", lambda probe, cap: fetch_indexed("unlinked", probe, cap)),
                ("legacy_thread", fetch_legacy("legacy_thread")),
                ("legacy_run", fetch_legacy("legacy_run")),
                ("legacy_unlinked", fetch_legacy("legacy_unlinked")),
            ]
        window, next_cursor, saw, budget_hit = self._window(
            sources, owner_id, audit, size, snapshot, positions, scope, seen=seen,
        )
        # An unfinished legacy scan still has a cursor. Absence is only when
        # this walk has finished and no page has produced a row.
        found = bool(window) or saw or seen is True
        if next_cursor is None and not found and seen is not None:
            raise TraceNotFound(trace_id)
        payload = self._describe({"trace_id": trace_id}, window, next_cursor, budget_hit=budget_hit)
        if truncated:
            # The fallback cap was reached; the result is bounded and may be
            # incomplete.  Say so instead of silently claiming completeness.
            payload["legacy_scan_truncated"] = True
            payload["completeness"]["reasons"].append({"code": "truncated"})
            payload["completeness"]["status"] = "partial"
        return payload

    def by_thread(self, thread_id: str, *, owner_id: str, audit: bool = False,
                  limit: int | None = None, cursor: int = 0) -> dict[str, Any]:
        self._authorize_thread(thread_id, owner_id)
        size, snapshot, positions, _seen = self._position(limit, cursor)
        scope = _ReadScope(owner_id, thread_id=thread_id, kinds=("thread", "run", "agent"))
        sources = [
            ("thread", lambda probe, cap: self._thread_rows(
                owner_id, thread_id=thread_id, trace_id=None, legacy=False, cap=cap,
                snapshot=snapshot, after=probe,
            )),
            ("run", lambda probe, cap: self._linked_run_rows(
                owner_id, thread_id=thread_id, trace_id=None, legacy=False, cap=cap,
                snapshot=snapshot, after=probe,
            )),
            ("agent", lambda probe, cap: self._agent_rows(
                owner_id, thread_id, run_id=None, task_id=None, cap=cap, snapshot=snapshot, after=probe,
            )),
        ]
        window, next_cursor, _saw, budget_hit = self._window(
            sources, owner_id, audit, size, snapshot, positions, scope,
        )
        return self._describe({"thread_id": thread_id}, window, next_cursor, budget_hit=budget_hit)

    def by_task(self, kind: str, task_id: str, *, owner_id: str, audit: bool = False,
                limit: int | None = None, cursor: int = 0) -> dict[str, Any]:
        """Return only the events that belong to this one task.

        Authorization resolves the owning thread.  The task key is applied in
        SQL before the page limit, so unrelated history on the same thread is
        not loaded.  ``task_scope`` is ``partial`` when a legacy row cannot be
        attributed to this task; those rows are not shown as task events.
        """
        thread_id = self._task_thread(kind, task_id, owner_id)
        size, snapshot, positions, _seen = self._position(limit, cursor)
        if kind == "expert":
            # ``/tasks/expert`` accepts either the durable run id or one of
            # its agent task ids.  A task id must stay task-scoped; falling
            # back to its run id here would return sibling tasks from the
            # same run and was the source of empty/mis-scoped subtask traces.
            run_id, is_task = self._expert_identity(task_id, owner_id)
            fields = ("task_id", "agent_task_id") if is_task else ("agent_run_id", "run_id")
            match_value = task_id if is_task else run_id
        elif kind == "research":
            run_id, is_task = None, False
            fields = ("job_id", "research_job_id")
            match_value = task_id
        else:
            raise ValueError(f"unsupported task kind: {kind}")

        scope = _ReadScope(
            owner_id, thread_id=thread_id, fields=fields, value=match_value,
            run_id=run_id, task_id=task_id if is_task else None, filter_task=is_task,
            kinds=("thread", "run", "agent") if kind == "expert" else ("thread", "run"),
        )
        sources = [
            ("thread", lambda probe, cap: self._thread_rows(
                owner_id, thread_id=thread_id, trace_id=None, legacy=False, cap=cap,
                fields=fields, value=match_value, snapshot=snapshot, after=probe,
            )),
            ("run", lambda probe, cap: self._linked_run_rows(
                owner_id, thread_id=thread_id, trace_id=None, legacy=False, cap=cap,
                fields=fields, value=match_value, snapshot=snapshot, after=probe,
            )),
        ]
        if kind == "expert":
            # Subtask rows are the authorized run plus this task id.
            # The run view keeps task_id open so its own log is included.
            sources.append(("agent", lambda probe, cap: self._agent_rows(
                owner_id, thread_id, run_id=run_id,
                task_id=task_id if is_task else None,
                cap=cap, snapshot=snapshot, after=probe,
            )))
        window, next_cursor, _saw, budget_hit = self._window(
            sources, owner_id, audit, size, snapshot, positions, scope,
        )
        payload = self._describe(
            {"kind": kind, "id": task_id, "thread_id": thread_id}, window, next_cursor,
            budget_hit=budget_hit,
        )
        reasons = []
        if any(event.get("compatibility") for event in window) or self._has_unattributable_legacy(thread_id, fields):
            reasons.append("historical_unlinked")
        payload["task_scope"] = "partial" if reasons else "exact"
        if reasons:
            payload["task_scope_reasons"] = reasons
        return payload

    # ------------------------------------------------------------------ #
    # pagination
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize(limit: int | None, cursor: Any) -> tuple[int, str | None]:
        """Validate the page.  An empty cursor starts a new snapshot."""
        if limit is None:
            limit = DEFAULT_LIMIT
        if type(limit) is not int or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be an integer between 1 and {MAX_LIMIT}")
        if cursor is None or cursor == 0 or cursor == "0" or cursor == "":
            return limit, None
        if isinstance(cursor, str):
            return limit, cursor
        raise ValueError("cursor must be a page token")

    def _position(self, limit: int | None, cursor: Any,
                  ) -> tuple[int, dict[str, int], dict[str, tuple | None], bool | None]:
        size, token = self._normalize(limit, cursor)
        if token is None:
            return size, self._snapshot(), _blank_positions(), False
        snapshot, positions, seen = _decode_cursor(token)
        return size, snapshot, positions, seen

    def _snapshot(self) -> dict[str, int]:
        """Highest row id currently stored.  Later inserts sort outside this walk."""
        with self.db.connection() as connection:
            snapshot = {}
            for key, table in _SNAPSHOT_TABLES:
                row = connection.execute(f"SELECT COALESCE(MAX(row_id), 0) AS n FROM {table}").fetchone()
                snapshot[key] = int(row["n"])
        return snapshot

    def _window(self, sources, owner_id: str, audit: bool, limit: int, snapshot: dict[str, int],
                positions: dict[str, tuple | None], scope: _ReadScope, seen: bool | None = None,
                ) -> tuple[list[dict[str, Any]], str | None, bool, bool]:
        """Merge one lookahead per source.

        A refused row advances only its own source. The next global event is
        chosen after every source has a readable candidate or is finished.
        The same ``event_id`` is returned only at its earliest in-scope key.
        """
        states = [{
            "name": name, "fetch": fetch, "after": positions.get(name), "pending": [],
            "exhausted": False, "blocked": False, "read": 0, "touched": False, "legacy": name.startswith("legacy"),
        } for name, fetch in sources]
        visible: list[dict[str, Any]] = []
        saw = False
        budget_hit = False

        def fill(state) -> None:
            if state["exhausted"] or state["blocked"]:
                return
            if state["legacy"]:
                cap = limit + 1
            else:
                room = limit + SOURCE_SEEK_BUDGET - state["read"]
                if room <= 0:
                    state["blocked"] = True
                    return
                cap = min(limit + 1, room)
            rows = state["fetch"](state["after"], cap)
            if rows:
                state["touched"] = True
            if not state["legacy"]:
                state["read"] += len(rows)
            state["pending"].extend(rows)
            if getattr(state["fetch"], "hit", False):
                return
            if state["legacy"]:
                if not getattr(state["fetch"], "hit", False):
                    state["exhausted"] = True
                return
            if len(rows) < cap:
                state["exhausted"] = True
            elif state["read"] >= limit + SOURCE_SEEK_BUDGET:
                state["blocked"] = True

        def candidate(state):
            while True:
                while state["pending"]:
                    event = state["pending"][0]
                    key = _event_sort_key(event)
                    if self._has_smaller_copy(event, key, snapshot, scope):
                        state["pending"].pop(0)
                        state["after"] = key
                        continue
                    try:
                        entry = self._entry(event, owner_id, audit)
                    except PermissionError:
                        state["pending"].pop(0)
                        state["after"] = key
                        continue
                    return key, entry
                if state["exhausted"] or state["blocked"]:
                    return None
                if (
                    getattr(state["fetch"], "hit", False)
                    and getattr(state["fetch"], "resume", None) is not None
                    and (state["after"] is None or state["fetch"].resume > state["after"])
                ):
                    state["after"] = state["fetch"].resume
                    state["blocked"] = True
                    return None
                fill(state)
                if state["pending"]:
                    continue
                if getattr(state["fetch"], "hit", False):
                    if getattr(state["fetch"], "resume", None) is not None:
                        state["after"] = state["fetch"].resume
                    state["blocked"] = True
                    return None
                state["exhausted"] = True
                return None

        while len(visible) < limit:
            heads = []
            unknown = False
            for state in states:
                found = candidate(state)
                if state["touched"]:
                    saw = True
                if found is not None:
                    heads.append((found[0], found[1], state))
                elif state["blocked"] and not state["exhausted"]:
                    unknown = True
                    if not state["legacy"]:
                        budget_hit = True
            if unknown:
                break
            if not heads:
                break
            key, entry, state = min(heads, key=lambda item: item[0])
            state["pending"].pop(0)
            state["after"] = key
            visible.append(entry)
        more = budget_hit or any(state["pending"] or not state["exhausted"] for state in states)
        if more:
            outgoing = dict(positions)
            for state in states:
                outgoing[state["name"]] = state["after"]
            progressed = None if seen is None else bool(seen or visible or saw)
            token = _encode_cursor(snapshot, outgoing, seen=progressed)
        else:
            token = None
        return visible, token, saw, budget_hit

    def _has_smaller_copy(self, event: Any, key: tuple, snapshot: dict[str, int], scope: _ReadScope) -> bool:
        """True when an earlier in-scope row already owns this event id."""
        for kind in scope.kinds:
            if self._smaller_in(kind, event.event_id, key, snapshot, scope):
                return True
        return False

    def _smaller_in(self, kind: str, event_id: str, key: tuple, snapshot: dict[str, int],
                    scope: _ReadScope) -> bool:
        if kind in ("thread", "legacy_thread"):
            sql = ("SELECT te.envelope_json FROM thread_events te JOIN threads th ON th.id=te.thread_id "
                   "WHERE th.owner_id=? AND th.deleted_at IS NULL AND te.event_id=? AND te.row_id<=?")
            params: list[Any] = [scope.owner_id, event_id, snapshot["thread"]]
            if scope.thread_id is not None:
                sql += " AND te.thread_id=?"
                params.append(scope.thread_id)
            if scope.fields:
                sql += " AND " + self._equals_sql("te.data_json", scope.fields)
                params.extend([scope.value] * len(scope.fields))
            if kind == "legacy_thread":
                sql += " AND te.trace_id IS NULL"
            elif scope.trace_id is not None:
                sql += " AND te.trace_id=?"
                params.append(scope.trace_id)
            sql += " AND (te.occurred_at, 'thread', te.thread_id, te.seq, te.event_id) < (?,?,?,?,?) LIMIT 1"
            params.extend(key)
            with self.db.connection() as connection:
                row = connection.execute(sql, params).fetchone()
            if row is None:
                return False
            if kind == "legacy_thread":
                return self._trace_of_body(row["envelope_json"]) == scope.trace_id
            return True
        if kind == "agent":
            if scope.thread_id is None:
                return False
            sql = ("SELECT 1 FROM agent_events e JOIN agent_runs r ON r.id=e.agent_run_id "
                   "JOIN threads th ON th.id=r.thread_id "
                   "WHERE th.owner_id=? AND th.deleted_at IS NULL AND r.thread_id=? "
                   "AND e.event_id=? AND e.row_id<=?")
            params = [scope.owner_id, scope.thread_id, event_id, snapshot["agent"]]
            if scope.run_id is not None:
                sql += " AND e.agent_run_id=?"
                params.append(scope.run_id)
            if scope.filter_task:
                sql += " AND e.task_id=?"
                params.append(scope.task_id)
            sql += " AND (e.occurred_at, 'agent', e.agent_run_id, e.seq, e.event_id) < (?,?,?,?,?) LIMIT 1"
            params.extend(key)
            with self.db.connection() as connection:
                return connection.execute(sql, params).fetchone() is not None
        linked = kind in ("run", "legacy_run")
        sql = "SELECT e.envelope_json, r.budget_json FROM events e JOIN runs r ON r.id=e.run_id "
        params = []
        if linked:
            sql += ("JOIN turns t ON t.id=r.source_turn_id JOIN threads th ON th.id=t.thread_id "
                    "WHERE th.owner_id=? AND th.deleted_at IS NULL")
            params.append(scope.owner_id)
            if scope.thread_id is not None:
                sql += " AND t.thread_id=?"
                params.append(scope.thread_id)
        else:
            sql += "WHERE r.source_turn_id IS NULL"
        sql += " AND e.event_id=? AND e.row_id<=?"
        params.extend([event_id, snapshot["run"]])
        if scope.fields and linked:
            sql += " AND " + self._equals_sql("e.data_json", scope.fields)
            params.extend([scope.value] * len(scope.fields))
        if kind.startswith("legacy"):
            sql += " AND e.trace_id IS NULL"
        elif scope.trace_id is not None:
            sql += " AND e.trace_id=?"
            params.append(scope.trace_id)
        sql += " AND (e.occurred_at, 'run', e.run_id, e.seq, e.event_id) < (?,?,?,?,?) LIMIT 1"
        params.extend(key)
        if not linked:
            sql = sql.replace(" LIMIT 1", "")
            with self.db.connection() as connection:
                rows = connection.execute(sql, params).fetchall()
            for row in rows:
                if self._run_owner(row["budget_json"]) != scope.owner_id:
                    continue
                if kind == "legacy_unlinked" and self._trace_of_body(row["envelope_json"]) != scope.trace_id:
                    continue
                return True
            return False
        with self.db.connection() as connection:
            row = connection.execute(sql, params).fetchone()
        if row is None:
            return False
        if kind == "legacy_run":
            return self._trace_of_body(row["envelope_json"]) == scope.trace_id
        return True

    def _describe(self, scope: dict[str, Any], window: list[dict[str, Any]], next_cursor: str | None,
                  *, budget_hit: bool = False) -> dict[str, Any]:
        deferred = [item["domain"] for item in EVENT_SOURCES if item["status"] != "connected"]
        reasons: list[dict[str, Any]] = []
        if deferred:
            reasons.append({"code": "source_not_connected", "sources": deferred})
        if any(item.get("compatibility") for item in window):
            reasons.append({"code": "historical_unlinked"})
        if budget_hit:
            reasons.append({"code": "scan_budget"})
        return {
            "scope": scope, "count": len(window), "next_cursor": next_cursor,
            "order": list(_SORT_FIELDS),
            "sources": {
                "connected": [item["domain"] for item in EVENT_SOURCES if item["status"] == "connected"],
                "deferred": deferred,
            },
            "completeness": {"status": "partial" if reasons else "exact", "reasons": reasons},
            "events": window,
        }

    # ------------------------------------------------------------------ #
    # authorization
    # ------------------------------------------------------------------ #

    def _authorize_thread(self, thread_id: str, owner_id: str) -> None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT owner_id FROM threads WHERE id=? AND deleted_at IS NULL", (thread_id,),
            ).fetchone()
        if row is None or row["owner_id"] != owner_id:
            raise TraceNotFound(thread_id)

    def _task_thread(self, kind: str, task_id: str, owner_id: str) -> str:
        with self.db.connection() as connection:
            if kind == "research":
                row = connection.execute(
                    "SELECT j.thread_id,th.owner_id FROM research_jobs j "
                    "JOIN threads th ON th.id=j.thread_id WHERE j.id=? AND th.deleted_at IS NULL",
                    (task_id,),
                ).fetchone()
            elif kind == "expert":
                # A deleted thread hides its expert runs too, exactly like the
                # research path: authorization never reads through a tombstone.
                row = connection.execute(
                    "SELECT r.thread_id,th.owner_id FROM agent_runs r "
                    "JOIN threads th ON th.id=r.thread_id "
                    "WHERE r.id=? AND th.deleted_at IS NULL", (task_id,),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        "SELECT r.thread_id,th.owner_id FROM agent_tasks t "
                        "JOIN agent_runs r ON r.id=t.agent_run_id "
                        "JOIN threads th ON th.id=r.thread_id "
                        "WHERE t.id=? AND th.deleted_at IS NULL", (task_id,),
                    ).fetchone()
            else:
                raise ValueError(f"unsupported task kind: {kind}")
        if row is None or row["owner_id"] != owner_id or row["thread_id"] is None:
            raise TraceNotFound(task_id)
        return row["thread_id"]

    def _expert_identity(self, task_id: str, owner_id: str) -> tuple[str, bool]:
        """Resolve an expert URL id to ``(run_id, is_task_id)``.

        Agent runs and agent tasks intentionally have different namespaces.
        Keeping that distinction lets a run query include run-level events while
        a subtask query only includes events that name that subtask.
        """
        with self.db.connection() as connection:
            run = connection.execute(
                "SELECT id FROM agent_runs WHERE id=? AND owner_id=?", (task_id, owner_id),
            ).fetchone()
            if run is not None:
                return run["id"], False
            task = connection.execute(
                "SELECT t.id,r.id AS run_id FROM agent_tasks t "
                "JOIN agent_runs r ON r.id=t.agent_run_id "
                "WHERE t.id=? AND r.owner_id=?", (task_id, owner_id),
            ).fetchone()
        if task is None:
            raise TraceNotFound(task_id)
        return task["run_id"], True

    @staticmethod
    def _expert_event_matches(event: Any, task_id: str, run_id: str, is_task: bool) -> bool:
        data = event.data or {}
        if is_task:
            # Agent event payloads use either spelling depending on producer.
            return data.get("task_id") == task_id or data.get("agent_task_id") == task_id
        return data.get("agent_run_id") == run_id or data.get("run_id") == run_id

    @staticmethod
    def _task_key(kind: str, event: Any) -> str | None:
        """The task id an event's own data names, or None if it names none."""
        data = event.data or {}
        if kind == "research":
            return data.get("job_id") or data.get("research_job_id")
        if kind == "expert":
            return data.get("agent_run_id") or data.get("run_id")
        return None

    # ------------------------------------------------------------------ #
    # reading and projecting
    # ------------------------------------------------------------------ #

    def _trace_column_present(self) -> bool:
        """Whether the indexed ``trace_id`` column exists on this database."""
        if self._trace_column is None:
            try:
                with self.db.connection() as connection:
                    connection.execute("SELECT trace_id FROM thread_events LIMIT 0").fetchall()
                self._trace_column = True
            except Exception:
                self._trace_column = False
        return self._trace_column

    def _keyset(self, alias: str, kind: str, snapshot: dict[str, int] | None,
                after: tuple | None) -> tuple[str, list[Any]]:
        """Snapshot upper bound plus the shared sort key.  ``kind`` is a fixed literal."""
        if snapshot is None:
            return "", []
        if kind not in _STREAM_COLUMN:
            raise ValueError("invalid event stream")
        column = _STREAM_COLUMN[kind]
        sql = f" AND {alias}.row_id <= ?"
        params: list[Any] = [snapshot[kind]]
        if after is not None:
            sql += (
                f" AND ({alias}.occurred_at, '{kind}', {alias}.{column}, {alias}.seq, {alias}.event_id)"
                " > (?, ?, ?, ?, ?)"
            )
            params.extend(after)
        sql += f" ORDER BY {alias}.occurred_at, {alias}.{column}, {alias}.seq, {alias}.event_id"
        return sql, params

    def _thread_rows(self, owner_id: str, *, thread_id: str | None, trace_id: str | None,
                     legacy: bool, cap: int | None, fields: tuple[str, ...] | None = None,
                     value: str | None = None, snapshot: dict[str, int] | None = None,
                     after: tuple | None = None) -> list[Any]:
        sql = ("SELECT te.* FROM thread_events te JOIN threads th ON th.id=te.thread_id "
               "WHERE th.owner_id=? AND th.deleted_at IS NULL")
        params: list[Any] = [owner_id]
        if thread_id is not None:
            sql += " AND te.thread_id=?"
            params.append(thread_id)
        if fields:
            sql += " AND " + self._equals_sql("te.data_json", fields)
            params.extend([value] * len(fields))
        if self._trace_column_present() and (trace_id is not None or legacy):
            if legacy:
                sql += " AND te.trace_id IS NULL AND te.envelope_json LIKE ?"
                params.append(f"%{trace_id}%")
            else:
                sql += " AND te.trace_id=?"
                params.append(trace_id)
        bound, bound_params = self._keyset("te", "thread", snapshot, after)
        sql += bound
        params.extend(bound_params)
        if cap is not None:
            sql += " LIMIT ?"
            params.append(cap)
        self._last_thread_sql = sql
        self._last_thread_params = tuple(params)
        with self.db.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_row_to_thread_event(row) for row in rows]

    def _linked_run_rows(self, owner_id: str, *, thread_id: str | None, trace_id: str | None,
                         legacy: bool, cap: int | None, fields: tuple[str, ...] | None = None,
                         value: str | None = None, snapshot: dict[str, int] | None = None,
                         after: tuple | None = None) -> list[Any]:
        sql = ("SELECT e.* FROM events e JOIN runs r ON r.id=e.run_id "
               "JOIN turns t ON t.id=r.source_turn_id JOIN threads th ON th.id=t.thread_id "
               "WHERE th.owner_id=? AND th.deleted_at IS NULL")
        params: list[Any] = [owner_id]
        if thread_id is not None:
            sql += " AND t.thread_id=?"
            params.append(thread_id)
        if fields:
            sql += " AND " + self._equals_sql("e.data_json", fields)
            params.extend([value] * len(fields))
        if self._trace_column_present() and (trace_id is not None or legacy):
            if legacy:
                sql += " AND e.trace_id IS NULL AND e.envelope_json LIKE ?"
                params.append(f"%{trace_id}%")
            else:
                sql += " AND e.trace_id=?"
                params.append(trace_id)
        bound, bound_params = self._keyset("e", "run", snapshot, after)
        sql += bound
        params.extend(bound_params)
        if cap is not None:
            sql += " LIMIT ?"
            params.append(cap)
        with self.db.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_row_to_event(row) for row in rows]

    def _unlinked_run_rows(self, owner_id: str, *, trace_id: str | None, legacy: bool,
                           cap: int | None, snapshot: dict[str, int] | None = None,
                           after: tuple | None = None) -> list[Any]:
        """Events of runs created without a source turn (direct goal runs).

        Such a run has no thread to borrow an owner from, so ownership is read
        from the trusted ``agent_loop_context`` it was persisted with - the same
        identity the runtime executes it under.  A run whose context is absent
        cannot be authorized and is skipped rather than exposed.
        """
        sql = "SELECT e.*,r.budget_json FROM events e JOIN runs r ON r.id=e.run_id WHERE r.source_turn_id IS NULL"
        params: list[Any] = []
        if self._trace_column_present() and (trace_id is not None or legacy):
            if legacy:
                sql += " AND e.trace_id IS NULL AND e.envelope_json LIKE ?"
                params.append(f"%{trace_id}%")
            else:
                sql += " AND e.trace_id=?"
                params.append(trace_id)
        bound, bound_params = self._keyset("e", "run", snapshot, after)
        sql += bound
        params.extend(bound_params)
        if cap is not None:
            sql += " LIMIT ?"
            params.append(cap)
        with self.db.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        events = []
        for row in rows:
            if self._run_owner(row["budget_json"]) != owner_id:
                continue
            events.append(_row_to_event(row))
        return events

    @staticmethod
    def _run_owner(budget_json: Any) -> str | None:
        from .execution_context import deserialize_context
        try:
            saved = json.loads(budget_json or "{}").get("agent_loop_context")
            if not saved:
                return None
            return deserialize_context(saved).owner_id
        except Exception:
            return None

    def _json_text(self, column: str, key: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("invalid event field")
        if self.db.backend == "postgresql":
            return f"({column}::json->>'{key}')"
        return f"json_extract({column}, '$.{key}')"

    def _equals_sql(self, column: str, fields: tuple[str, ...]) -> str:
        return "(" + " OR ".join(f"{self._json_text(column, field)} = ?" for field in fields) + ")"

    def _has_unattributable_legacy(self, thread_id: str, fields: tuple[str, ...]) -> bool:
        """True when a legacy row on this thread names none of the task keys."""
        missing = " AND ".join(f"{self._json_text('data_json', field)} IS NULL" for field in fields)
        sql = ("SELECT 1 FROM thread_events WHERE thread_id=? AND envelope_json IS NULL "
               f"AND {missing} LIMIT 1")
        with self.db.connection() as connection:
            return connection.execute(sql, (thread_id,)).fetchone() is not None

    def _agent_rows(self, owner_id: str, thread_id: str, *, run_id: str | None,
                    task_id: str | None, cap: int | None, snapshot: dict[str, int] | None = None,
                    after: tuple | None = None) -> list[Any]:
        """Expert internal log. These rows have no envelope and are not given one."""
        sql = ("SELECT e.event_id,e.seq,e.type,e.actor,e.data_json,e.occurred_at,e.agent_run_id,e.task_id "
               "FROM agent_events e JOIN agent_runs r ON r.id=e.agent_run_id "
               "JOIN threads th ON th.id=r.thread_id "
               "WHERE th.owner_id=? AND th.deleted_at IS NULL AND r.thread_id=?")
        params: list[Any] = [owner_id, thread_id]
        if run_id is not None:
            sql += " AND e.agent_run_id=?"
            params.append(run_id)
        if task_id is not None:
            sql += " AND e.task_id=?"
            params.append(task_id)
        bound, bound_params = self._keyset("e", "agent", snapshot, after)
        sql += bound
        params.extend(bound_params)
        if cap is not None:
            sql += " LIMIT ?"
            params.append(cap)
        with self.db.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        events = []
        for row in rows:
            try:
                data = json.loads(row["data_json"] or "{}")
            except (TypeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            events.append(_AgentTraceEvent(
                row["event_id"], int(row["seq"]), row["type"], row["occurred_at"], row["actor"], data,
                row["agent_run_id"], row["task_id"],
            ))
        return events

    def _scan(self, owner_id: str, thread_id: str | None = None, cap: int | None = None,
              snapshot: dict[str, int] | None = None, after: tuple | None = None) -> list[Any]:
        """Read the owner's thread and run events, scoped and bounded.

        Only rows whose owning thread belongs to the caller are read, so the
        projection never has to repair a foreign owner after the fact.  Legacy
        rows without an envelope are read too: they project to an explicit
        ``legacy_context_missing`` marker instead of disappearing, so "no event"
        can never be confused with "old event".  Direct goal runs (no source
        turn) are included only by an unscoped scan.
        """
        events = self._thread_rows(
            owner_id, thread_id=thread_id, trace_id=None, legacy=False, cap=cap,
            snapshot=snapshot, after=after,
        )
        events.extend(self._linked_run_rows(
            owner_id, thread_id=thread_id, trace_id=None, legacy=False, cap=cap,
            snapshot=snapshot, after=after,
        ))
        if thread_id is not None:
            events.extend(self._agent_rows(
                owner_id, thread_id, run_id=None, task_id=None, cap=cap,
                snapshot=snapshot, after=after,
            ))
        else:
            events.extend(self._unlinked_run_rows(
                owner_id, trace_id=None, legacy=False, cap=cap, snapshot=snapshot, after=after,
            ))
        return events

    def _scan_trace(self, owner_id: str, trace_id: str, cap: int, snapshot: dict[str, int],
                    after: tuple | None) -> list[Any]:
        """Indexed trace lookup; falls back to a scan when the column is absent."""
        if not self._trace_column_present():
            return [
                event for event in self._scan(owner_id, cap=cap, snapshot=snapshot, after=after)
                if self._trace_of(event) == trace_id
            ]
        events = self._thread_rows(
            owner_id, thread_id=None, trace_id=trace_id, legacy=False, cap=cap,
            snapshot=snapshot, after=after,
        )
        events.extend(self._linked_run_rows(
            owner_id, thread_id=None, trace_id=trace_id, legacy=False, cap=cap,
            snapshot=snapshot, after=after,
        ))
        events.extend(self._unlinked_run_rows(
            owner_id, trace_id=trace_id, legacy=False, cap=cap, snapshot=snapshot, after=after,
        ))
        return events

    def _legacy_events(self, kind: str, owner_id: str, trace_id: str, snapshot: dict[str, int],
                       after: tuple | None) -> tuple[list[Any], bool, tuple | None]:
        """One capped legacy window. The trace test runs after the cap, not before it."""
        if not self._trace_column_present():
            return [], False, None
        bound = LEGACY_SCAN_LIMIT
        if kind == "legacy_thread":
            rows = self._thread_rows(
                owner_id, thread_id=None, trace_id=trace_id, legacy=True, cap=bound,
                snapshot=snapshot, after=after,
            )
        elif kind == "legacy_run":
            rows = self._linked_run_rows(
                owner_id, thread_id=None, trace_id=trace_id, legacy=True, cap=bound,
                snapshot=snapshot, after=after,
            )
        else:
            rows = self._unlinked_run_rows(
                owner_id, trace_id=trace_id, legacy=True, cap=bound, snapshot=snapshot, after=after,
            )
        resume = _event_sort_key(rows[-1]) if rows else None
        matched = [event for event in rows if self._trace_of(event) == trace_id]
        return matched, len(rows) >= bound, resume

    @staticmethod
    def _entry(event: Any, owner_id: str, audit: bool) -> dict[str, Any]:
        projected = project_authorized_event(event, owner_id=owner_id)
        kind = getattr(event, "trace_stream_kind", None) or ("thread" if hasattr(event, "thread_id") else "run")
        projected["stream_kind"] = kind
        if kind == "thread":
            projected["stream_id"] = event.thread_id
        elif kind == "agent":
            projected["stream_id"] = event.agent_run_id
        else:
            projected["stream_id"] = event.run_id
        if projected.get("compatibility") == "legacy_context_missing" and isinstance(getattr(event, "data", None), dict):
            payload = _redact_value(dict(event.data), None, "data")
            if not audit:
                payload.pop("execution_context", None)
            if getattr(event, "agent_run_id", None):
                payload.setdefault("agent_run_id", event.agent_run_id)
            if getattr(event, "agent_task_id", None):
                payload.setdefault("task_id", event.agent_task_id)
            projected["payload"] = payload
        error = (projected.get("outcome") or {}).get("error")
        if isinstance(error, dict) and not audit:
            error.pop("diagnostic_ref", None)
        if not audit:
            projected = {key: value for key, value in projected.items() if key in _PUBLIC_TRACE_FIELDS}
        return projected

    @staticmethod
    def _trace_of_body(envelope_json: Any) -> str | None:
        probe = type("_Envelope", (), {"envelope_json": envelope_json})()
        return TraceQuery._trace_of(probe)

    @staticmethod
    def _trace_of(event: Any) -> str | None:
        if not event.envelope_json:
            return None
        try:
            body = json.loads(event.envelope_json)
        except (TypeError, ValueError):
            return None
        if not isinstance(body, dict):
            return None
        version = body.get("schema_version")
        if type(version) is not int or version != 1:
            return None
        context = body.get("context")
        if not isinstance(context, dict):
            return None
        inner = context.get("context")
        if not isinstance(inner, dict):
            return None
        trace_id = inner.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id.strip():
            return None
        return trace_id


class _AgentTraceEvent:
    """An agent_events row. It has no envelope, and this query does not invent one."""

    trace_stream_kind = "agent"
    envelope_json = None

    def __init__(self, event_id, seq, event_type, occurred_at, actor, data, agent_run_id, agent_task_id):
        self.event_id = event_id
        self.seq = seq
        self.type = event_type
        self.occurred_at = occurred_at
        self.actor = actor
        self.data = data
        self.agent_run_id = agent_run_id
        self.agent_task_id = agent_task_id


def _dedupe(events: list[Any]) -> list[Any]:
    seen: set[str] = set()
    unique: list[Any] = []
    for event in events:
        if event.event_id in seen:
            continue
        seen.add(event.event_id)
        unique.append(event)
    return unique
