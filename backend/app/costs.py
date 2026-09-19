from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database
from .model_gateway import UsageBuckets
from .config import monetary_limits_enabled


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class PriceSnapshot:
    id: str
    uncached_input_rate: int
    cache_read_rate: int
    cache_write_rate: int
    output_rate: int
    reasoning_rate: int


@dataclass(frozen=True)
class CostEstimate:
    microusd: int | None
    status: str


def estimate_cost(usage: UsageBuckets, price: PriceSnapshot | None) -> CostEstimate:
    if price is None:
        return CostEstimate(None, "UNAVAILABLE")
    buckets = (
        (usage.uncached_input_tokens, price.uncached_input_rate),
        (usage.cache_read_tokens, price.cache_read_rate),
        (usage.cache_write_tokens, price.cache_write_rate),
        (usage.output_tokens, price.output_rate),
        (usage.reasoning_tokens, price.reasoning_rate),
    )
    if any(tokens is None and rate != 0 for tokens, rate in buckets):
        return CostEstimate(None, "ESTIMATED_PARTIAL")
    total = sum(int(tokens or 0) * rate for tokens, rate in buckets)
    return CostEstimate(math.ceil(total / 1_000_000), "ESTIMATED_COMPLETE")


class CostService:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def today_period() -> str:
        # Ledger timestamps use UTC. Using the host's local date here makes a
        # new local day fail to match same-moment UTC reservations near midnight.
        return datetime.now(timezone.utc).date().isoformat()

    def create_root_budget(self, owner_id: str, root_kind: str, root_object_id: str, *,
                           max_attempts: int, deadline_at: str, limit_microusd: int,
                           connection=None) -> dict[str, Any]:
        if self.db.backend != "postgresql":
            raise RuntimeError("root task budgets require PostgreSQL")
        if not owner_id or not root_object_id or root_kind not in {"turn", "run", "agent", "research", "evaluation", "learning", "goal_operation"}:
            raise ValueError("invalid root budget identity")
        if any(type(value) is not int or value < 0 for value in (max_attempts, limit_microusd)):
            raise ValueError("root budget limits must be nonnegative integers")
        deadline = datetime.fromisoformat(deadline_at)
        if deadline.tzinfo is None:
            raise ValueError("root deadline must include a timezone")
        deadline = deadline.astimezone(timezone.utc)
        identity = hashlib.sha256(json.dumps([owner_id, root_kind, root_object_id]).encode()).hexdigest()
        root_id = f"task_budget_{identity}"
        with (self.db.transaction() if connection is None else nullcontext(connection)) as connection:
            connection.execute(
                "INSERT INTO task_budget_roots(id,owner_id,root_kind,root_object_id,max_attempts,deadline_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(owner_id,root_kind,root_object_id) DO NOTHING",
                (root_id, owner_id, root_kind, root_object_id, max_attempts, deadline.isoformat()),
            )
            root = connection.execute("SELECT * FROM task_budget_roots WHERE id=? FOR UPDATE", (root_id,)).fetchone()
            existing_deadline = datetime.fromisoformat(str(root["deadline_at"]))
            if root["max_attempts"] != max_attempts or existing_deadline != deadline:
                raise ValueError("root budget identity is already bound to different limits")
            connection.execute(
                "INSERT INTO cost_budgets(owner_id,period_kind,period_key,limit_microusd,updated_at) "
                "VALUES (?,'ROOT',?,?,?) ON CONFLICT(owner_id,period_kind,period_key) DO NOTHING",
                (owner_id, root_id, limit_microusd, _now()),
            )
            money = connection.execute(
                "SELECT limit_microusd FROM cost_budgets WHERE owner_id=? AND period_kind='ROOT' AND period_key=?",
                (owner_id, root_id),
            ).fetchone()
            if money["limit_microusd"] != limit_microusd:
                raise ValueError("root budget identity is already bound to a different cost limit")
            return dict(root)

    def create_default_root_budget(
        self, owner_id: str, root_kind: str, root_object_id: str, *, connection=None,
    ) -> dict[str, Any]:
        return self.create_root_budget(
            owner_id, root_kind, root_object_id,
            max_attempts=int(os.getenv("ROOT_TASK_MAX_ATTEMPTS", "30")),
            deadline_at=(datetime.now(timezone.utc) + timedelta(
                minutes=int(os.getenv("ROOT_TASK_DEADLINE_MINUTES", "30")),
            )).isoformat(),
            limit_microusd=int(os.getenv("ROOT_TASK_MAX_COST_MICROUSD", "1000000")) if monetary_limits_enabled() else 0,
            connection=connection,
        )

    def root_seconds_remaining(self, owner_id: str, root_budget_id: str) -> float:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT EXTRACT(EPOCH FROM (deadline_at-clock_timestamp())) AS seconds "
                "FROM task_budget_roots WHERE id=? AND owner_id=?",
                (root_budget_id, owner_id),
            ).fetchone()
        if row is None:
            raise BudgetExceeded("root task budget is missing or belongs to another owner")
        return max(float(row["seconds"]), 0.0)

    def ensure_default_root_budget(
        self, owner_id: str, root_kind: str, root_object_id: str, *, connection=None,
    ) -> dict[str, Any]:
        if self.db.backend != "postgresql":
            raise RuntimeError("root task budgets require PostgreSQL")
        identity = json.dumps([owner_id, root_kind, root_object_id])
        with (self.db.transaction() if connection is None else nullcontext(connection)) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(?,0))", (identity,))
            root = connection.execute(
                "SELECT * FROM task_budget_roots WHERE owner_id=? AND root_kind=? AND root_object_id=?",
                (owner_id, root_kind, root_object_id),
            ).fetchone()
            if root is not None:
                return dict(root)
            return self.create_default_root_budget(owner_id, root_kind, root_object_id, connection=connection)

    def resume_root_after_ask(
        self, owner_id: str, root_budget_id: str, waiting_since: str, *, resumed_at: str, connection,
    ) -> float:
        root = connection.execute(
            "SELECT deadline_at FROM task_budget_roots WHERE id=? AND owner_id=? FOR UPDATE",
            (root_budget_id, owner_id),
        ).fetchone()
        if root is None:
            raise BudgetExceeded("root task budget is missing or belongs to another owner")
        deadline = datetime.fromisoformat(str(root["deadline_at"]))
        waiting = datetime.fromisoformat(waiting_since)
        resumed = datetime.fromisoformat(resumed_at)
        # The persisted Ask interval is human waiting, not agent execution.
        # An already exhausted execution deadline must never be revived.
        if deadline <= waiting:
            return 0.0
        delay = max((resumed - waiting).total_seconds(), 0.0)
        if delay:
            connection.execute(
                "UPDATE task_budget_roots SET deadline_at=?,version=version+1 WHERE id=? AND owner_id=?",
                ((deadline + timedelta(seconds=delay)).isoformat(), root_budget_id, owner_id),
            )
        return delay

    def _reserve_root_attempt(self, connection, handle, attempt_id):
        root_id = handle.context.root_budget_id
        if root_id is None:
            return
        root = connection.execute(
            "SELECT *,deadline_at<=clock_timestamp() AS expired FROM task_budget_roots "
            "WHERE id=? AND owner_id=? FOR UPDATE", (root_id, handle.context.owner_id),
        ).fetchone()
        if root is None:
            raise BudgetExceeded("root task budget is missing or belongs to another owner")
        # A repeated reservation is the same attempt, even if the task has since expired.
        if connection.execute(
            "SELECT 1 FROM cost_ledger WHERE attempt_id=? AND period_kind='ROOT' AND period_key=? AND entry_type='RESERVE'",
            (attempt_id, root_id),
        ).fetchone():
            return
        if root["expired"]:
            raise BudgetExceeded("root task deadline exceeded")
        if root["attempts_started"] >= root["max_attempts"]:
            raise BudgetExceeded("root task attempt limit exhausted")
        connection.execute("UPDATE task_budget_roots SET attempts_started=attempts_started+1,version=version+1 WHERE id=?", (root_id,))

    def set_budget(self, owner_id: str, period_kind: str, period_key: str, limit_microusd: int) -> None:
        if limit_microusd < 0:
            raise ValueError("budget limit must be non-negative")
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT reserved_microusd,charged_microusd FROM cost_budgets WHERE owner_id=? AND period_kind=? AND period_key=?",
                (owner_id, period_kind, period_key),
            ).fetchone()
            if monetary_limits_enabled() and row and int(row["reserved_microusd"]) + int(row["charged_microusd"]) > limit_microusd:
                raise BudgetExceeded("budget is below current usage")
            connection.execute(
                "INSERT INTO cost_budgets(owner_id,period_kind,period_key,limit_microusd,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(owner_id,period_kind,period_key) DO UPDATE SET limit_microusd=excluded.limit_microusd,version=cost_budgets.version+1,updated_at=excluded.updated_at",
                (owner_id, period_kind, period_key, limit_microusd, _now()),
            )

    def register_price(
        self,
        profile_version_id: str,
        price: PriceSnapshot,
        effective_at: str = "1970-01-01T00:00:00+00:00",
        source_url: str = "legacy:unspecified",
    ) -> None:
        payload = {
            "profile_version_id": profile_version_id,
            "uncached_input_rate": price.uncached_input_rate,
            "cache_read_rate": price.cache_read_rate,
            "cache_write_rate": price.cache_write_rate,
            "output_rate": price.output_rate,
            "reasoning_rate": price.reasoning_rate,
            "effective_at": effective_at,
            "source_url": source_url,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO model_price_snapshots(id,profile_version_id,uncached_input_rate,cache_read_rate,cache_write_rate,"
                "output_rate,reasoning_rate,effective_at,price_digest,created_at,source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    price.id, profile_version_id, price.uncached_input_rate, price.cache_read_rate, price.cache_write_rate,
                    price.output_rate, price.reasoning_rate, effective_at, digest, _now(), source_url,
                ),
            )

    def reserve(
        self, owner_id: str, period_kind: str, period_key: str, invocation_id: str,
        attempt_id: str, amount_microusd: int, idempotency_key: str,
    ) -> dict[str, Any]:
        with self.db.transaction() as connection:
            return self._reserve(connection, owner_id, period_kind, period_key, invocation_id, attempt_id, amount_microusd, idempotency_key)

    def _reserve(
        self, connection: Any, owner_id: str, period_kind: str, period_key: str, invocation_id: str,
        attempt_id: str, amount_microusd: int, idempotency_key: str, price_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        cached = connection.execute("SELECT * FROM cost_ledger WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if cached:
            return _ledger(cached)
        lock = " FOR UPDATE" if self.db.backend == "postgresql" else ""
        budget = connection.execute(
            "SELECT * FROM cost_budgets WHERE owner_id=? AND period_kind=? AND period_key=?" + lock,
            (owner_id, period_kind, period_key),
        ).fetchone()
        if budget is None:
            raise KeyError("cost budget is not configured")
        if monetary_limits_enabled() and int(budget["reserved_microusd"]) + int(budget["charged_microusd"]) + amount_microusd > int(budget["limit_microusd"]):
            raise BudgetExceeded("cost budget exhausted")
        connection.execute(
            "UPDATE cost_budgets SET reserved_microusd=reserved_microusd+?,version=version+1,updated_at=? WHERE owner_id=? AND period_kind=? AND period_key=?",
            (amount_microusd, _now(), owner_id, period_kind, period_key),
        )
        entry_id = f"cost_{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO cost_ledger(id,owner_id,period_kind,period_key,invocation_id,attempt_id,price_snapshot_id,entry_type,amount_microusd,cost_status,reason,idempotency_key,created_at) "
            "VALUES (?,?,?,?,?,?,?,'RESERVE',?,'RESERVED','attempt budget',?,?)",
            (entry_id, owner_id, period_kind, period_key, invocation_id, attempt_id, price_snapshot_id, amount_microusd, idempotency_key, _now()),
        )
        return _ledger(connection.execute("SELECT * FROM cost_ledger WHERE id=?", (entry_id,)).fetchone())

    def settle(self, owner_id: str, period_kind: str, period_key: str, invocation_id: str, attempt_id: str, price_snapshot_id: str, amount_microusd: int, status: str) -> dict[str, Any]:
        with self.db.transaction() as connection:
            return self._settle(connection, owner_id, period_kind, period_key, invocation_id, attempt_id, price_snapshot_id, amount_microusd, status)

    def _settle(self, connection: Any, owner_id: str, period_kind: str, period_key: str, invocation_id: str, attempt_id: str, price_snapshot_id: str, amount_microusd: int, status: str) -> dict[str, Any]:
        if self.db.backend == "postgresql":
            connection.execute(
                "SELECT 1 FROM cost_budgets WHERE owner_id=? AND period_kind=? AND period_key=? FOR UPDATE",
                (owner_id, period_kind, period_key),
            ).fetchone()
        cached = connection.execute(
            "SELECT * FROM cost_ledger WHERE attempt_id=? AND period_kind=? AND period_key=? "
            "AND entry_type='CHARGE' AND price_snapshot_id=?",
            (attempt_id, period_kind, period_key, price_snapshot_id),
        ).fetchone()
        if cached:
            return _ledger(cached)
        reserved = connection.execute(
            "SELECT amount_microusd FROM cost_ledger WHERE attempt_id=? AND period_kind=? AND period_key=? AND entry_type='RESERVE'",
            (attempt_id, period_kind, period_key),
        ).fetchone()
        reserved_amount = int(reserved["amount_microusd"]) if reserved else 0
        charged = min(amount_microusd, reserved_amount) if reserved and monetary_limits_enabled() else amount_microusd
        release = max(reserved_amount - charged, 0)
        connection.execute(
            "UPDATE cost_budgets SET reserved_microusd=reserved_microusd-?,charged_microusd=charged_microusd+?,version=version+1,updated_at=? "
            "WHERE owner_id=? AND period_kind=? AND period_key=?",
            (reserved_amount, charged, _now(), owner_id, period_kind, period_key),
        )
        charge_id = f"cost_{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO cost_ledger(id,owner_id,period_kind,period_key,invocation_id,attempt_id,price_snapshot_id,entry_type,amount_microusd,cost_status,reason,idempotency_key,created_at) "
            "VALUES (?,?,?,?,?,?,?,'CHARGE',?,?, 'attempt settled',?,?)",
            (
                charge_id, owner_id, period_kind, period_key, invocation_id, attempt_id,
                price_snapshot_id, charged, status,
                f"charge:{attempt_id}:{period_kind}:{period_key}:{price_snapshot_id}", _now(),
            ),
        )
        if release:
            connection.execute(
                "INSERT INTO cost_ledger(id,owner_id,period_kind,period_key,invocation_id,attempt_id,price_snapshot_id,entry_type,amount_microusd,cost_status,reason,idempotency_key,created_at) "
                "VALUES (?,?,?,?,?,?,?,'RELEASE',?,'RELEASED','unused reservation',?,?)",
                (
                    f"cost_{uuid.uuid4().hex}", owner_id, period_kind, period_key,
                    invocation_id, attempt_id, price_snapshot_id, release,
                    f"release:{attempt_id}:{period_kind}:{period_key}:{price_snapshot_id}", _now(),
                ),
            )
        return _ledger(connection.execute("SELECT * FROM cost_ledger WHERE id=?", (charge_id,)).fetchone())

    def summary(self, owner_id: str, period_kind: str, period_key: str) -> dict[str, int]:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT limit_microusd,reserved_microusd,charged_microusd FROM cost_budgets WHERE owner_id=? AND period_kind=? AND period_key=?",
                (owner_id, period_kind, period_key),
            ).fetchone()
        if row is None:
            raise KeyError("cost budget is not configured")
        return {key: int(row[key]) for key in ("limit_microusd", "reserved_microusd", "charged_microusd")}

    def usage_summary(self, owner_id: str) -> dict[str, Any]:
        """Authoritative observability aggregate; unknown cost and timing stay explicit."""
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT i.role,v.provider_name,a.profile_version_id,a.reason,a.status,a.cost_status,a.cost_microusd,"
                "a.started_at,a.first_token_at,a.finished_at,a.output_tokens FROM model_attempts a "
                "JOIN model_invocations i ON i.id=a.invocation_id "
                "JOIN model_profile_versions v ON v.id=a.profile_version_id WHERE i.owner_id=? ORDER BY a.started_at,a.id",
                (owner_id,),
            ).fetchall()
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row["role"], row["provider_name"], row["profile_version_id"])
            item = groups.setdefault(key, {
                "role": key[0], "provider": key[1], "profile_version_id": key[2], "attempts": 0,
                "succeeded": 0, "fallbacks": 0, "cost_microusd": 0, "unknown_cost_attempts": 0,
                "ttft_seconds": [], "latency_seconds": [], "tps": [],
            })
            item["attempts"] += 1
            item["succeeded"] += row["status"] == "SUCCEEDED"
            item["fallbacks"] += row["reason"] == "fallback"
            if row["cost_microusd"] is None:
                item["unknown_cost_attempts"] += 1
            else:
                item["cost_microusd"] += int(row["cost_microusd"])
            started, first, finished = (_parse_time(row[name]) for name in ("started_at", "first_token_at", "finished_at"))
            if started is not None and finished is not None:
                item["latency_seconds"].append(max(finished - started, 0.0))
            if started is not None and first is not None:
                item["ttft_seconds"].append(max(first - started, 0.0))
            if first is not None and finished is not None and row["output_tokens"] is not None and finished > first:
                item["tps"].append(int(row["output_tokens"]) / (finished - first))
        result = []
        for item in groups.values():
            attempts = item["attempts"]
            result.append({
                **{key: item[key] for key in ("role","provider","profile_version_id","attempts","succeeded","fallbacks","cost_microusd","unknown_cost_attempts")},
                "success_rate": item["succeeded"] / attempts,
                "fallback_rate": item["fallbacks"] / attempts,
                "ttft_seconds": _average(item["ttft_seconds"]),
                "tps": _average(item["tps"]),
                "p95_latency_seconds": _percentile(item["latency_seconds"], .95),
            })
        return {"cost_mode": "enforce" if monetary_limits_enabled() else "observe", "groups": sorted(result, key=lambda item: (item["role"], item["provider"], item["profile_version_id"]))}

    def reserve_attempt(self, connection: Any, handle: Any, attempt_id: str) -> None:
        self._reserve_root_attempt(connection, handle, attempt_id)
        periods = self._configured_attempt_periods(connection, handle)
        canary = connection.execute(
            "SELECT 1 FROM canary_exposures e JOIN canary_deployments d ON d.id=e.deployment_id WHERE e.run_id=? AND d.release_contract_version<>''",
            (handle.context.run_id,),
        ).fetchone()
        if not periods and not canary and monetary_limits_enabled():
            if self.db.backend == "postgresql":
                raise BudgetExceeded("model call has no configured budget")
            return
        price = self._attempt_price(connection, handle)
        if price is None and monetary_limits_enabled():
            raise BudgetExceeded("model price is unavailable")
        profile = connection.execute("SELECT context_window,max_output_tokens FROM model_profile_versions WHERE id=?", (handle.profile_version_id,)).fetchone()
        worst = estimate_cost(
            UsageBuckets(int(profile["context_window"]), 0, 0, int(profile["max_output_tokens"]), 0),
            _price(price) if price else None,
        )
        if worst.microusd is None and monetary_limits_enabled():
            raise BudgetExceeded("model cost cannot be reserved")
        if canary:
            from .canary_budget import reserve_canary_attempt
            reserve_canary_attempt(connection, handle, attempt_id, worst.microusd)
        for period_kind, period_key in periods:
            self._reserve(
                connection, handle.context.owner_id, period_kind, period_key,
                handle.invocation_id, attempt_id, worst.microusd if monetary_limits_enabled() else 0,
                f"reserve:{attempt_id}:{period_kind}:{period_key}", price["id"] if price else None,
            )

    def settle_attempt(self, connection: Any, handle: Any, attempt_id: str, usage: UsageBuckets | None) -> CostEstimate:
        reservations = connection.execute(
            "SELECT period_kind,period_key,amount_microusd,price_snapshot_id FROM cost_ledger "
            "WHERE attempt_id=? AND entry_type='RESERVE' ORDER BY period_kind,period_key",
            (attempt_id,),
        ).fetchall()
        if not reservations:
            return CostEstimate(None, "UNAVAILABLE")
        price_snapshot_ids = {row["price_snapshot_id"] for row in reservations}
        if price_snapshot_ids == {None} and not monetary_limits_enabled():
            return CostEstimate(None, "UNAVAILABLE")
        if len(price_snapshot_ids) != 1 or None in price_snapshot_ids:
            raise BudgetExceeded("attempt price reservation is invalid")
        price_row = connection.execute(
            "SELECT * FROM model_price_snapshots WHERE id=? AND profile_version_id=?",
            (price_snapshot_ids.pop(), handle.profile_version_id),
        ).fetchone()
        if usage is None and not monetary_limits_enabled():
            return CostEstimateWithPrice(None, "UNAVAILABLE", price_row["id"] if price_row else None)
        if usage is None:
            result = CostEstimate(max(int(row["amount_microusd"]) for row in reservations), "ESTIMATED_PARTIAL")
        else:
            result = estimate_cost(usage, _price(price_row) if price_row else None)
            if result.microusd is None and not monetary_limits_enabled():
                return CostEstimateWithPrice(None, result.status, price_row["id"] if price_row else None)
            if result.microusd is None:
                result = CostEstimate(max(int(row["amount_microusd"]) for row in reservations), "ESTIMATED_PARTIAL")
        for reservation in reservations:
            self._settle(
                connection, handle.context.owner_id, reservation["period_kind"], reservation["period_key"],
                handle.invocation_id, attempt_id, price_row["id"] if price_row else "unavailable",
                int(result.microusd), result.status,
            )
        return CostEstimateWithPrice(result.microusd, result.status, price_row["id"] if price_row else None)

    def _attempt_price(self, connection: Any, handle: Any):
        if handle.context.price_snapshot_id is not None:
            return connection.execute(
                "SELECT * FROM model_price_snapshots WHERE id=? AND profile_version_id=?",
                (handle.context.price_snapshot_id, handle.profile_version_id),
            ).fetchone()
        return connection.execute(
            "SELECT * FROM model_price_snapshots WHERE profile_version_id=? AND effective_at<=? "
            "ORDER BY effective_at DESC,id DESC LIMIT 1",
            (handle.profile_version_id, _now()),
        ).fetchone()

    def _configured_attempt_periods(self, connection: Any, handle: Any) -> list[tuple[str, str]]:
        today = self.today_period()
        if not monetary_limits_enabled():
            for kind, key in (("INVOCATION",handle.invocation_id),("DAILY",today),("MONTHLY",today[:7])):
                connection.execute(
                    "INSERT INTO cost_budgets(owner_id,period_kind,period_key,limit_microusd,updated_at) VALUES (?,?,?,0,?) "
                    "ON CONFLICT(owner_id,period_kind,period_key) DO NOTHING",
                    (handle.context.owner_id,kind,key,_now()),
                )
        invocation = connection.execute(
            "SELECT 1 FROM cost_budgets WHERE owner_id=? AND period_kind='INVOCATION' AND period_key=?",
            (handle.context.owner_id, handle.invocation_id),
        ).fetchone()
        if invocation is None:
            template = connection.execute(
                "SELECT limit_microusd FROM cost_budgets WHERE owner_id=? AND period_kind='INVOCATION' AND period_key='default'",
                (handle.context.owner_id,),
            ).fetchone()
            if template is not None:
                connection.execute(
                    "INSERT INTO cost_budgets(owner_id,period_kind,period_key,limit_microusd,updated_at) VALUES (?,'INVOCATION',?,?,?)",
                    (handle.context.owner_id, handle.invocation_id, int(template["limit_microusd"]), _now()),
                )
        candidates = (
            ("INVOCATION", handle.invocation_id),
            ("DAILY", today),
            ("MONTHLY", today[:7]),
        )
        if handle.context.root_budget_id is not None:
            root_key = handle.context.root_budget_id
            if connection.execute(
                "SELECT 1 FROM cost_budgets WHERE owner_id=? AND period_kind='ROOT' AND period_key=?",
                (handle.context.owner_id, root_key),
            ).fetchone() is None:
                raise BudgetExceeded("root task cost budget is missing")
            candidates = (("ROOT", root_key), *candidates)
        return [
            (kind, key) for kind, key in candidates
            if connection.execute(
                "SELECT 1 FROM cost_budgets WHERE owner_id=? AND period_kind=? AND period_key=?",
                (handle.context.owner_id, kind, key),
            ).fetchone() is not None
        ]


def _price(row: Any) -> PriceSnapshot:
    return PriceSnapshot(row["id"], row["uncached_input_rate"], row["cache_read_rate"], row["cache_write_rate"], row["output_rate"], row["reasoning_rate"])


@dataclass(frozen=True)
class CostEstimateWithPrice(CostEstimate):
    price_snapshot_id: str | None


def _ledger(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(math.ceil(len(ordered) * quantile) - 1, len(ordered) - 1)]
