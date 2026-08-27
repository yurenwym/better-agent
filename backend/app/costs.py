from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from .db import Database
from .model_gateway import UsageBuckets


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
        return date.today().isoformat()

    def set_budget(self, owner_id: str, period_kind: str, period_key: str, limit_microusd: int) -> None:
        if limit_microusd < 0:
            raise ValueError("budget limit must be non-negative")
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT reserved_microusd,charged_microusd FROM cost_budgets WHERE owner_id=? AND period_kind=? AND period_key=?",
                (owner_id, period_kind, period_key),
            ).fetchone()
            if row and int(row["reserved_microusd"]) + int(row["charged_microusd"]) > limit_microusd:
                raise BudgetExceeded("budget is below current usage")
            connection.execute(
                "INSERT INTO cost_budgets(owner_id,period_kind,period_key,limit_microusd,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(owner_id,period_kind,period_key) DO UPDATE SET limit_microusd=excluded.limit_microusd,version=cost_budgets.version+1,updated_at=excluded.updated_at",
                (owner_id, period_kind, period_key, limit_microusd, _now()),
            )

    def register_price(self, profile_version_id: str, price: PriceSnapshot, effective_at: str = "1970-01-01T00:00:00+00:00") -> None:
        payload = {
            "profile_version_id": profile_version_id,
            "uncached_input_rate": price.uncached_input_rate,
            "cache_read_rate": price.cache_read_rate,
            "cache_write_rate": price.cache_write_rate,
            "output_rate": price.output_rate,
            "reasoning_rate": price.reasoning_rate,
            "effective_at": effective_at,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO model_price_snapshots(id,profile_version_id,uncached_input_rate,cache_read_rate,cache_write_rate,"
                "output_rate,reasoning_rate,effective_at,price_digest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    price.id, profile_version_id, price.uncached_input_rate, price.cache_read_rate, price.cache_write_rate,
                    price.output_rate, price.reasoning_rate, effective_at, digest, _now(),
                ),
            )

    def reserve(
        self, owner_id: str, period_kind: str, period_key: str, invocation_id: str,
        attempt_id: str, amount_microusd: int, idempotency_key: str,
    ) -> dict[str, Any]:
        with self.db.transaction() as connection:
            return self._reserve(connection, owner_id, period_kind, period_key, invocation_id, attempt_id, amount_microusd, idempotency_key)

    def _reserve(self, connection: Any, owner_id: str, period_kind: str, period_key: str, invocation_id: str, attempt_id: str, amount_microusd: int, idempotency_key: str) -> dict[str, Any]:
        cached = connection.execute("SELECT * FROM cost_ledger WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if cached:
            return _ledger(cached)
        budget = connection.execute(
            "SELECT * FROM cost_budgets WHERE owner_id=? AND period_kind=? AND period_key=?",
            (owner_id, period_kind, period_key),
        ).fetchone()
        if budget is None:
            raise KeyError("cost budget is not configured")
        if int(budget["reserved_microusd"]) + int(budget["charged_microusd"]) + amount_microusd > int(budget["limit_microusd"]):
            raise BudgetExceeded("cost budget exhausted")
        connection.execute(
            "UPDATE cost_budgets SET reserved_microusd=reserved_microusd+?,version=version+1,updated_at=? WHERE owner_id=? AND period_kind=? AND period_key=?",
            (amount_microusd, _now(), owner_id, period_kind, period_key),
        )
        entry_id = f"cost_{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO cost_ledger(id,owner_id,period_kind,period_key,invocation_id,attempt_id,entry_type,amount_microusd,cost_status,reason,idempotency_key,created_at) "
            "VALUES (?,?,?,?,?,?,'RESERVE',?,'RESERVED','attempt budget',?,?)",
            (entry_id, owner_id, period_kind, period_key, invocation_id, attempt_id, amount_microusd, idempotency_key, _now()),
        )
        return _ledger(connection.execute("SELECT * FROM cost_ledger WHERE id=?", (entry_id,)).fetchone())

    def settle(self, owner_id: str, period_kind: str, period_key: str, invocation_id: str, attempt_id: str, price_snapshot_id: str, amount_microusd: int, status: str) -> dict[str, Any]:
        with self.db.transaction() as connection:
            return self._settle(connection, owner_id, period_kind, period_key, invocation_id, attempt_id, price_snapshot_id, amount_microusd, status)

    def _settle(self, connection: Any, owner_id: str, period_kind: str, period_key: str, invocation_id: str, attempt_id: str, price_snapshot_id: str, amount_microusd: int, status: str) -> dict[str, Any]:
        cached = connection.execute(
            "SELECT * FROM cost_ledger WHERE attempt_id=? AND entry_type='CHARGE' AND price_snapshot_id=?",
            (attempt_id, price_snapshot_id),
        ).fetchone()
        if cached:
            return _ledger(cached)
        reserved = connection.execute("SELECT amount_microusd FROM cost_ledger WHERE attempt_id=? AND entry_type='RESERVE'", (attempt_id,)).fetchone()
        reserved_amount = int(reserved["amount_microusd"]) if reserved else 0
        charged = min(amount_microusd, reserved_amount) if reserved else amount_microusd
        release = max(reserved_amount - charged, 0)
        connection.execute(
            "UPDATE cost_budgets SET reserved_microusd=MAX(reserved_microusd-?,0),charged_microusd=charged_microusd+?,version=version+1,updated_at=? "
            "WHERE owner_id=? AND period_kind=? AND period_key=?",
            (reserved_amount, charged, _now(), owner_id, period_kind, period_key),
        )
        charge_id = f"cost_{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO cost_ledger(id,owner_id,period_kind,period_key,invocation_id,attempt_id,price_snapshot_id,entry_type,amount_microusd,cost_status,reason,idempotency_key,created_at) "
            "VALUES (?,?,?,?,?,?,?,'CHARGE',?,?, 'attempt settled',?,?)",
            (charge_id, owner_id, period_kind, period_key, invocation_id, attempt_id, price_snapshot_id, charged, status, f"charge:{attempt_id}:{price_snapshot_id}", _now()),
        )
        if release:
            connection.execute(
                "INSERT INTO cost_ledger(id,owner_id,period_kind,period_key,invocation_id,attempt_id,price_snapshot_id,entry_type,amount_microusd,cost_status,reason,idempotency_key,created_at) "
                "VALUES (?,?,?,?,?,?,?,'RELEASE',?,'RELEASED','unused reservation',?,?)",
                (f"cost_{uuid.uuid4().hex}", owner_id, period_kind, period_key, invocation_id, attempt_id, price_snapshot_id, release, f"release:{attempt_id}:{price_snapshot_id}", _now()),
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

    def reserve_attempt(self, connection: Any, handle: Any, attempt_id: str) -> None:
        period = self.today_period()
        budget = connection.execute(
            "SELECT 1 FROM cost_budgets WHERE owner_id=? AND period_kind='DAILY' AND period_key=?",
            (handle.context.owner_id, period),
        ).fetchone()
        if budget is None:
            return
        price = connection.execute(
            "SELECT * FROM model_price_snapshots WHERE profile_version_id=? AND effective_at<=? ORDER BY effective_at DESC,id DESC LIMIT 1",
            (handle.profile_version_id, _now()),
        ).fetchone()
        if price is None:
            raise BudgetExceeded("model price is unavailable")
        profile = connection.execute("SELECT context_window,max_output_tokens FROM model_profile_versions WHERE id=?", (handle.profile_version_id,)).fetchone()
        worst = estimate_cost(
            UsageBuckets(int(profile["context_window"]), 0, 0, int(profile["max_output_tokens"]), 0),
            _price(price),
        )
        if worst.microusd is None:
            raise BudgetExceeded("model cost cannot be reserved")
        self._reserve(connection, handle.context.owner_id, "DAILY", period, handle.invocation_id, attempt_id, worst.microusd, f"reserve:{attempt_id}")

    def settle_attempt(self, connection: Any, handle: Any, attempt_id: str, usage: UsageBuckets | None) -> CostEstimate:
        period = self.today_period()
        reserved = connection.execute(
            "SELECT amount_microusd FROM cost_ledger WHERE attempt_id=? AND entry_type='RESERVE'",
            (attempt_id,),
        ).fetchone()
        if reserved is None:
            return CostEstimate(None, "UNAVAILABLE")
        price_row = connection.execute(
            "SELECT * FROM model_price_snapshots WHERE profile_version_id=? AND effective_at<=? ORDER BY effective_at DESC,id DESC LIMIT 1",
            (handle.profile_version_id, _now()),
        ).fetchone()
        if usage is None:
            result = CostEstimate(int(reserved["amount_microusd"]), "ESTIMATED_PARTIAL")
        else:
            result = estimate_cost(usage, _price(price_row) if price_row else None)
            if result.microusd is None:
                result = CostEstimate(int(reserved["amount_microusd"]), "ESTIMATED_PARTIAL")
        self._settle(
            connection, handle.context.owner_id, "DAILY", period, handle.invocation_id, attempt_id,
            price_row["id"] if price_row else "unavailable", int(result.microusd), result.status,
        )
        return CostEstimateWithPrice(result.microusd, result.status, price_row["id"] if price_row else None)


def _price(row: Any) -> PriceSnapshot:
    return PriceSnapshot(row["id"], row["uncached_input_rate"], row["cache_read_rate"], row["cache_write_rate"], row["output_rate"], row["reasoning_rate"])


@dataclass(frozen=True)
class CostEstimateWithPrice(CostEstimate):
    price_snapshot_id: str | None


def _ledger(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
