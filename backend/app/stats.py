from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .events import Event, EventStore
from .model_gateway import normalize_usage


@dataclass
class _Invocation:
    started: float | None = None
    finished: float | None = None
    status: str = "success"


@dataclass
class _Attempt:
    invocation_id: str | None = None
    started: float | None = None
    first_token: float | None = None
    finished: float | None = None
    usage: dict[str, int | None] | None = None
    status: str = "success"
    ttft_override: float | None = None
    decode_override: float | None = None


@dataclass
class _ToolSpan:
    started: float | None = None
    finished: float | None = None


class StatsProjector:
    """Rebuild a run's observable metrics from its complete append-only event log."""

    projection_version = 1

    def __init__(self, db: Database, events: EventStore | None = None) -> None:
        self.db = db
        self.events = events or EventStore(db)

    def project(self, run_id: str, *, rebuild: bool = False) -> dict[str, Any]:
        # Replaying is deliberately cheap for V1 and makes a stale projection impossible
        # when the UI asks for stats after an SSE update.
        del rebuild
        projected = self._calculate(run_id, self.events.list(run_id))
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO run_stats(run_id, projection_version, stats_json, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET projection_version = excluded.projection_version, "
                "stats_json = excluded.stats_json, updated_at = excluded.updated_at",
                (run_id, self.projection_version, json.dumps(projected, ensure_ascii=False, sort_keys=True), _now()),
            )
        return projected

    def rebuild(self, run_id: str) -> dict[str, Any]:
        return self.project(run_id, rebuild=True)

    def _calculate(self, run_id: str, events: list[Event]) -> dict[str, Any]:
        invocations: dict[str, _Invocation] = {}
        attempts: dict[str, _Attempt] = {}
        tools: dict[str, _ToolSpan] = {}
        usage_updates: dict[str, dict[str, int | None]] = {}
        interactions = 0
        current_plan_id: str | None = None
        plan_total: int | None = None
        completed_steps: set[str] = set()
        state: str | None = None

        for event in events:
            data = event.data
            correlation = event.correlation
            if event.type == "interaction.started":
                interactions += 1
            elif event.type == "state.transitioned":
                state = _string(data.get("to")) or state
            elif event.type == "plan.version_created":
                current_plan_id = _string(data.get("plan_version_id")) or current_plan_id
                explicit_total = _integer(data.get("total_steps"))
                if explicit_total is not None:
                    plan_total = explicit_total
                else:
                    plan_total = self._plan_total(current_plan_id, plan_total)
                completed_steps = set()
            elif event.type == "plan.step_finished":
                plan_id = _string(data.get("plan_version_id")) or _string(correlation.get("plan_version_id"))
                if current_plan_id is None or plan_id == current_plan_id:
                    step_id = _string(data.get("plan_step_id")) or _string(correlation.get("plan_step_id"))
                    if step_id:
                        completed_steps.add(step_id)
            elif event.type == "model.invocation_started":
                invocation_id = _event_id(event, "model_invocation_id")
                if invocation_id:
                    invocations.setdefault(invocation_id, _Invocation()).started = _time(event.occurred_at)
            elif event.type == "model.invocation_finished":
                invocation_id = _event_id(event, "model_invocation_id")
                if invocation_id:
                    invocation = invocations.setdefault(invocation_id, _Invocation())
                    invocation.finished = _time(event.occurred_at)
                    invocation.status = _string(data.get("status")) or _string(data.get("outcome")) or "success"
            elif event.type == "model.attempt_started":
                attempt_id = _event_id(event, "model_attempt_id")
                if attempt_id:
                    attempt = attempts.setdefault(attempt_id, _Attempt())
                    attempt.started = _time(event.occurred_at)
                    attempts[attempt_id].invocation_id = _event_id(event, "model_invocation_id")
                    attempts[attempt_id].status = _string(data.get("status")) or "success"
            elif event.type == "model.first_token":
                attempt_id = _event_id(event, "model_attempt_id")
                if attempt_id:
                    attempt = attempts.setdefault(attempt_id, _Attempt())
                    attempt.first_token = _time(event.occurred_at)
                    attempt.ttft_override = _number(data.get("ttft_seconds"))
            elif event.type == "model.usage_updated":
                attempt_id = _event_id(event, "model_attempt_id")
                if attempt_id:
                    usage_updates[attempt_id] = _usage(data.get("usage", data))
            elif event.type == "model.attempt_finished":
                attempt_id = _event_id(event, "model_attempt_id")
                if attempt_id:
                    attempt = attempts.setdefault(attempt_id, _Attempt())
                    attempt.finished = _time(event.occurred_at)
                    attempt.status = _string(data.get("status")) or attempt.status
                    attempt.decode_override = _number(data.get("decode_seconds"))
            elif event.type == "tool.execution.started":
                tool_id = _event_id(event, "tool_call_id")
                if tool_id:
                    tools.setdefault(tool_id, _ToolSpan()).started = _time(event.occurred_at)
            elif event.type == "tool.execution.finished":
                tool_id = _event_id(event, "tool_call_id")
                if tool_id:
                    tools.setdefault(tool_id, _ToolSpan()).finished = _time(event.occurred_at)

        for attempt_id, attempt in attempts.items():
            attempt.usage = usage_updates.get(attempt_id)

        successful_invocations = {
            invocation_id: invocation
            for invocation_id, invocation in invocations.items()
            if invocation.status not in {"failed", "failure", "cancelled", "canceled", "error"}
        }
        successful_attempts = [
            attempt
            for attempt in attempts.values()
            if _attempt_is_successful(attempt, successful_invocations)
        ]
        model_seconds = _sum_durations(successful_invocations.values())
        tool_seconds = _sum_durations(tools.values())
        closed_attempts = [attempt for attempt in successful_attempts if attempt.first_token is not None and attempt.finished is not None]
        ttft = _average((attempt.ttft_override if attempt.ttft_override is not None else attempt.first_token - attempt.started) for attempt in closed_attempts if attempt.started is not None and attempt.first_token is not None)
        decode_seconds = sum(max(attempt.decode_override if attempt.decode_override is not None else attempt.finished - attempt.first_token, 0.0) for attempt in closed_attempts if attempt.finished is not None and attempt.first_token is not None)
        complete_usage = [attempt.usage for attempt in successful_attempts]
        input_tokens = _aggregate_bucket(complete_usage, "input_tokens")
        output_tokens = _aggregate_bucket(complete_usage, "output_tokens")
        uncached = _aggregate_bucket(complete_usage, "uncached_input_tokens")
        cache_read = _aggregate_bucket(complete_usage, "cache_read_tokens")
        cache_write = _aggregate_bucket(complete_usage, "cache_write_tokens")
        cache_denominator = None if None in {uncached, cache_read, cache_write} else (uncached or 0) + (cache_read or 0) + (cache_write or 0)

        if current_plan_id:
            plan_total = self._plan_total(current_plan_id, plan_total)
        if plan_total is None and current_plan_id:
            plan_total = len(completed_steps)

        return {
            "run_id": run_id,
            "projection_version": self.projection_version,
            "state": state or self._run_state(run_id),
            "interactions": interactions,
            "plan_completed": len(completed_steps) if current_plan_id else None,
            "plan_total": plan_total,
            "model_attempts": len(attempts),
            "model_seconds": model_seconds,
            "tool_calls": len([tool for tool in tools.values() if tool.started is not None]),
            "tool_seconds": tool_seconds,
            "ttft_seconds": ttft,
            "tps": (output_tokens / decode_seconds) if output_tokens is not None and decode_seconds > 0 else None,
            "cache_hit_rate": (cache_read / cache_denominator) if cache_denominator else None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "metrics": {
                "failed_model_invocations": sum(invocation.status in {"failed", "failure", "error"} for invocation in invocations.values()),
                "cancelled_model_invocations": sum(invocation.status in {"cancelled", "canceled"} for invocation in invocations.values()),
                "unfinished_attempts": sum(attempt.finished is None for attempt in attempts.values()),
                "unfinished_tool_calls": sum(tool.started is not None and tool.finished is None for tool in tools.values()),
            },
        }

    def _plan_total(self, plan_id: str | None, fallback: int | None) -> int | None:
        if not plan_id:
            return fallback
        with self.db.connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM plan_steps WHERE plan_version_id = ?", (plan_id,)).fetchone()
        return int(row["total"]) if row and row["total"] else fallback

    def _run_state(self, run_id: str) -> str | None:
        with self.db.connection() as connection:
            row = connection.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
        return row["state"] if row else None


def _event_id(event: Event, key: str) -> str | None:
    return _string(event.data.get(key)) or _string(event.correlation.get(key))


def _usage(value: Any) -> dict[str, int | None]:
    if not isinstance(value, dict):
        return {"uncached_input_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None, "input_tokens": None, "output_tokens": None}
    normalized = normalize_usage(value)
    return {
        "uncached_input_tokens": normalized.uncached_input_tokens,
        "cache_read_tokens": normalized.cache_read_tokens,
        "cache_write_tokens": normalized.cache_write_tokens,
        "input_tokens": normalized.input_tokens,
        "output_tokens": normalized.output_tokens,
    }


def _attempt_is_successful(attempt: _Attempt, invocations: dict[str, _Invocation]) -> bool:
    if attempt.status in {"retry", "failed", "failure", "cancelled", "canceled", "error"}:
        return False
    if attempt.invocation_id is None:
        return True
    return attempt.invocation_id in invocations


def _aggregate_bucket(usages: list[dict[str, int | None] | None], key: str) -> int | None:
    if not usages or any(usage is None or usage.get(key) is None for usage in usages):
        return None
    return sum(int(usage[key] or 0) for usage in usages if usage is not None)


def _sum_durations(values: Any) -> float | None:
    durations = [max(item.finished - item.started, 0.0) for item in values if item.started is not None and item.finished is not None]
    return sum(durations) if durations else None


def _average(values: Any) -> float | None:
    numbers = [float(value) for value in values]
    return sum(numbers) / len(numbers) if numbers else None


def _time(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
