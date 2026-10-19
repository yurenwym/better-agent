"""Execution rights on existing task rows; domain services own business state."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
from typing import Protocol, runtime_checkable


class TaskKind(StrEnum):
    RESEARCH = "research"
    EXPERT = "expert"
    TURN = "turn"
    PROJECTION = "projection"
    GOAL = "goal"


@dataclass(frozen=True)
class TaskRef:
    kind: TaskKind
    id: str


@dataclass(frozen=True)
class LeaseToken:
    task: TaskRef
    owner: str
    epoch: int


@dataclass(frozen=True)
class TaskAttempt:
    token: LeaseToken
    number: int


class LeaseLost(PermissionError):
    pass


class TaskCancelled(LeaseLost):
    pass


@runtime_checkable
class LeaseRuntime(Protocol):
    """Names shared by task rows that already have this execution-right shape.

    The names are not a checklist for every background worker. Archive,
    Embedding, Learning and Notification keep their own columns. A missing
    verb stays missing; this protocol does not add a stub, and possessing the
    name is not evidence that the behavior holds.
    """

    def claim(self, *args, **kwargs): ...

    def heartbeat(self, *args, **kwargs): ...

    def require_lease(self, *args, **kwargs): ...

    def finish(self, *args, **kwargs): ...

    def recover_expired(self, *args, **kwargs): ...

    def request_cancel(self, *args, **kwargs): ...


_TABLES = {
    TaskKind.RESEARCH: ("research_jobs", "id"),
    TaskKind.EXPERT: ("agent_tasks", "id"),
    TaskKind.TURN: ("turn_jobs", "turn_id"),
    TaskKind.PROJECTION: ("turns", "id"),
    TaskKind.GOAL: ("runs", "id"),
}


class TaskRuntime:
    def __init__(self, db):
        self.db = db

    def now(self, connection):
        if self.db.backend == "postgresql":
            return connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        return datetime.now(timezone.utc)

    def _row(self, connection, task: TaskRef):
        table, key = _TABLES[TaskKind(task.kind)]
        if task.kind == TaskKind.EXPERT and self.db.backend == "postgresql":
            # Preserve the domain's global lock ordering for fan-out/join.
            connection.execute(
                "SELECT id FROM agent_runs WHERE id=(SELECT agent_run_id FROM agent_tasks WHERE id=?) FOR UPDATE",
                (task.id,),
            ).fetchone()
        lock = " FOR UPDATE" if self.db.backend == "postgresql" else ""
        row = connection.execute(f"SELECT * FROM {table} WHERE {key}=?" + lock, (task.id,)).fetchone()
        if row is not None and task.kind == TaskKind.PROJECTION:
            row = dict(row)
            row.update(status="RUNNING" if row["direction_projection_status"] == "COMPILING" else "QUEUED",
                       lease_owner=row["direction_projection_claim_owner"],
                       lease_until=row["direction_projection_lease_until"],
                       lease_epoch=row["direction_projection_epoch"],
                       attempts=row["direction_projection_attempts"],
                       cancel_requested_at=None if row["status"] == "AWAITING_DIRECTION" else "closed")
        if row is not None and task.kind == TaskKind.GOAL:
            row = dict(row)
            row.update(status=row["execution_status"], lease_owner=row["execution_owner"],
                       lease_until=row["execution_until"], lease_epoch=row["execution_epoch"],
                       attempts=row["execution_attempts"],
                       cancel_requested_at="cancelled" if row["state"] == "CANCELLED" else None)
        return row

    def _update(self, connection, task, **fields):
        table, key = _TABLES[task.kind]
        columns = {}
        if task.kind == TaskKind.PROJECTION:
            columns = {"status": "direction_projection_status", "lease_owner": "direction_projection_claim_owner",
                       "lease_until": "direction_projection_lease_until", "lease_epoch": "direction_projection_epoch",
                       "attempts": "direction_projection_attempts"}
            if fields.get("status") == "RUNNING":
                fields["status"] = "COMPILING"
        if task.kind == TaskKind.GOAL:
            columns = {"status": "execution_status", "lease_owner": "execution_owner",
                       "lease_until": "execution_until", "lease_epoch": "execution_epoch", "attempts": "execution_attempts"}
        assignments = ",".join(f"{columns.get(name, name)}=?" for name in fields)
        connection.execute(f"UPDATE {table} SET {assignments} WHERE {key}=?", (*fields.values(), task.id))

    @staticmethod
    def _date(value):
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @classmethod
    def _live(cls, row, now):
        until = cls._date(row["lease_until"]) if row else None
        return until is not None and until > now

    def _column(self, row, name: str):
        if row is None:
            return None
        return row[name] if name in row.keys() else None

    def _business_owner(self, connection, task: TaskRef, row):
        """The task's owner from its durable row, never the worker lease owner."""
        kind = TaskKind(task.kind)
        if kind == TaskKind.RESEARCH:
            found = connection.execute(
                "SELECT owner_id FROM threads WHERE id=? AND deleted_at IS NULL",
                (row["thread_id"],),
            ).fetchone()
        elif kind == TaskKind.EXPERT:
            found = connection.execute(
                "SELECT owner_id, thread_id FROM agent_runs WHERE id=?",
                (row["agent_run_id"],),
            ).fetchone()
            if found is None:
                return None
            if found["thread_id"] is None:
                return found["owner_id"]
            active = connection.execute(
                "SELECT owner_id FROM threads WHERE id=? AND deleted_at IS NULL",
                (found["thread_id"],),
            ).fetchone()
            if active is None or active["owner_id"] != found["owner_id"]:
                return None
            return found["owner_id"]
        elif kind in {TaskKind.TURN, TaskKind.PROJECTION}:
            found = connection.execute(
                "SELECT h.owner_id FROM turns t JOIN threads h ON h.id=t.thread_id "
                "AND h.deleted_at IS NULL WHERE t.id=?",
                (task.id,),
            ).fetchone()
        elif kind == TaskKind.GOAL and self._column(row, "source_turn_id"):
            found = connection.execute(
                "SELECT h.owner_id FROM turns t JOIN threads h ON h.id=t.thread_id "
                "AND h.deleted_at IS NULL WHERE t.id=?",
                (row["source_turn_id"],),
            ).fetchone()
        elif kind == TaskKind.GOAL:
            from .execution_context import deserialize_context
            try:
                budget = json.loads(row["budget_json"])
                if not isinstance(budget, dict):
                    return None
                context = deserialize_context(budget.get("agent_loop_context"))
            except (TypeError, ValueError):
                return None
            return context.owner_id if context.run_id == task.id else None
        else:
            found = None
        return found["owner_id"] if found else None

    def _lease_decision(self, connection, task: TaskRef, row, *, action: str, execution_valid: bool, cancelled: bool):
        """Decide from the locked row; unresolved ownership never permits a write."""
        if row is None:
            return None
        owner = self._business_owner(connection, task, row)
        if owner is None:
            return None
        from .policy_adapters import (
            task_claim_request, task_commit_request, task_heartbeat_request, task_cancel_request,
        )
        from .policy_engine import decide_request
        builder = {
            "claim": task_claim_request,
            "heartbeat": task_heartbeat_request,
            "commit": task_commit_request,
            "cancel": task_cancel_request,
        }[action]
        thread_id = self._column(row, "thread_id")
        return decide_request(builder(
            owner_id=owner, resource_owner_id=owner, task_id=task.id, task_kind=str(TaskKind(task.kind)),
            thread_id=thread_id, resource_thread_id=thread_id,
            registered=True, allowed=True, identity_valid=True, source_valid=True,
            budget_available=True, execution_valid=execution_valid, cancelled=cancelled,
        ))

    def _lease_allowed(self, connection, task, row, *, action: str, execution_valid: bool, cancelled: bool) -> bool:
        decision = self._lease_decision(
            connection, task, row, action=action, execution_valid=execution_valid, cancelled=cancelled,
        )
        if decision is None:
            return False
        from .policy_engine import PolicyAction
        return decision.action is PolicyAction.ALLOW

    def require(self, connection, token: LeaseToken, *, allow_cancelled=False, action: str = "heartbeat"):
        row = self._row(connection, token.task)
        held = (
            row is not None and row["status"] == "RUNNING" and row["lease_owner"] == token.owner
            and row["lease_epoch"] == token.epoch and self._live(row, self.now(connection))
        )
        if not held:
            self._lease_decision(
                connection, token.task, row, action=action, execution_valid=False, cancelled=False,
            )
            raise LeaseLost(f"task lease lost: {token.task.id}")
        if not allow_cancelled and row["cancel_requested_at"] is not None:
            self._lease_decision(
                connection, token.task, row, action=action, execution_valid=True, cancelled=True,
            )
            raise TaskCancelled(f"task cancelled: {token.task.id}")
        if not self._lease_allowed(
            connection, token.task, row, action=action, execution_valid=True, cancelled=False,
        ):
            raise LeaseLost("task source thread is no longer active")
        return row

    def claim(self, connection, task: TaskRef, owner: str, seconds: float) -> TaskAttempt | None:
        """Take one live execution right for ``owner``.

        ``owner`` is the worker holding the lease, not the business owner.
        The business owner is read from the task's durable row and is what
        policy checks. Preconditions: a non-empty worker owner, a positive
        duration, an existing row, and a state that is queued or running with
        an already expired lease. Research and expert rows also wait until
        ``available_at`` and stay under ``max_attempts``. A denied policy
        decision or a closed projection returns ``None`` and writes nothing.
        Success sets ``RUNNING``, stores the worker owner, advances epoch and
        attempt by one, and sets ``lease_until``. A second claim while that
        lease is live returns ``None``.
        """
        if not owner or seconds <= 0:
            raise ValueError("claim requires an owner and positive duration")
        row = self._row(connection, task)
        now = self.now(connection)
        if row is None:
            return None
        if row["status"] != "QUEUED" and (row["status"] != "RUNNING" or self._live(row, now)):
            return None
        if task.kind == TaskKind.PROJECTION and row["cancel_requested_at"]:
            return None
        if task.kind in {TaskKind.RESEARCH, TaskKind.EXPERT}:
            if self._date(row["available_at"]) > now or row["attempts"] >= row["max_attempts"]:
                return None
        if not self._lease_allowed(connection, task, row, action="claim", execution_valid=True, cancelled=False):
            return None
        epoch, attempt = int(row["lease_epoch"]) + 1, int(row["attempts"]) + 1
        self._update(connection, task, status="RUNNING", lease_owner=owner, lease_epoch=epoch,
                     lease_until=(now + timedelta(seconds=seconds)).isoformat(), attempts=attempt)
        return TaskAttempt(LeaseToken(task, owner, epoch), attempt)

    def require_lease(self, connection, token: LeaseToken, *, allow_cancelled=False):
        """Return the locked row only when this token still owns a live lease.

        The row must be ``RUNNING``, with the same worker owner and epoch, and
        ``lease_until`` still ahead of the database clock. Any other token,
        including an older epoch of the same worker, raises ``LeaseLost``.
        A recorded cancel raises ``TaskCancelled`` unless ``allow_cancelled``.
        ``require`` is the same check.
        """
        return self.require(connection, token, allow_cancelled=allow_cancelled)

    def heartbeat(self, connection, token: LeaseToken, seconds: float):
        """Extend ``lease_until`` for the current token. Epoch stays unchanged.

        A non-positive duration raises ``ValueError``. A lost, expired, or
        superseded lease raises ``LeaseLost`` and does not move the deadline.
        """
        if seconds <= 0:
            raise ValueError("heartbeat requires positive duration")
        self.require_lease(connection, token)
        self._update(connection, token.task,
                     lease_until=(self.now(connection) + timedelta(seconds=seconds)).isoformat())

    def finish(self, connection, token: LeaseToken, status: str):
        """Write one allowed terminal or requeue state and clear the lease.

        The token must still be live. ``CANCELLED`` may be written after a
        cancel request; any other status sees that request as ``TaskCancelled``.
        A status outside the task kind's set raises ``ValueError`` and writes
        nothing. A second finish sees a row that is no longer ``RUNNING`` and
        raises ``LeaseLost``; the first status stays.
        """
        allowed = {
            TaskKind.RESEARCH: {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED", "QUEUED"},
            TaskKind.EXPERT: {"SUCCEEDED", "FAILED", "CANCELLED", "WAITING_CHILDREN", "QUEUED"},
            TaskKind.TURN: {"COMPLETED", "FAILED", "CANCELLED", "QUEUED"},
            TaskKind.PROJECTION: {"READY", "FAILED"},
            TaskKind.GOAL: {"QUEUED"},
        }
        if status not in allowed[token.task.kind]:
            raise ValueError("invalid task transition")
        row = self.require(
            connection, token, allow_cancelled=status == "CANCELLED" or token.task.kind == TaskKind.GOAL,
            action="commit",
        )
        self._update(connection, token.task, status=status, lease_owner=None, lease_until=None)
        return row

    def request_cancel(self, connection, task: TaskRef) -> bool:
        """Record a cancel request without taking the lease or finishing the row.

        Only research, expert and turn rows have this column. Other kinds raise
        ``ValueError`` because their domains own cancellation. A missing row
        raises ``KeyError``. A row that is not queued, running or waiting on
        children, or that already has a cancel time, returns ``False``. Success
        sets ``cancel_requested_at`` and leaves status and epoch unchanged, so
        the current worker observes the cancel on its next lease check.
        """
        if task.kind not in {TaskKind.RESEARCH, TaskKind.EXPERT, TaskKind.TURN}:
            raise ValueError("cancellation belongs to this task's domain state")
        row = self._row(connection, task)
        if row is None:
            raise KeyError(task.id)
        if row["status"] not in {"QUEUED", "RUNNING", "WAITING_CHILDREN"} or row["cancel_requested_at"]:
            return False
        if not self._lease_allowed(connection, task, row, action="cancel", execution_valid=True, cancelled=False):
            return False
        table, key = _TABLES[task.kind]
        connection.execute(f"UPDATE {table} SET cancel_requested_at=? WHERE {key}=?",
                           (self.now(connection).isoformat(), task.id))
        return True

    def recover_expired(self, connection, task: TaskRef, status: str) -> bool:
        """Release one expired running attempt. A live or absent lease returns ``False``.

        ``status`` has to be one of the kind's recovery states or this raises
        ``ValueError`` and writes nothing. Success clears the worker owner and
        lease deadline, writes ``status``, and for a turn also advances the
        epoch. Calling it again does not change the row. The old token can no
        longer heartbeat or finish. This does not claim the next attempt.
        """
        allowed = {
            TaskKind.RESEARCH: {"FAILED", "CANCELLED"},
            TaskKind.EXPERT: {"QUEUED", "FAILED", "CANCELLED"},
            TaskKind.TURN: {"QUEUED"},
        }
        if status not in allowed.get(task.kind, set()):
            raise ValueError("invalid expired-task transition")
        row = self._row(connection, task)
        if row is None or row["status"] != "RUNNING" or self._live(row, self.now(connection)):
            return False
        if not self._lease_allowed(connection, task, row, action="commit", execution_valid=True, cancelled=False):
            return False
        # The state change fences the expired worker; claim allocates the next
        # epoch. Turn recovery historically invalidates its queue token too.
        fields = {"lease_epoch": int(row["lease_epoch"]) + 1} if task.kind == TaskKind.TURN else {}
        self._update(connection, task, status=status, lease_owner=None, lease_until=None, **fields)
        return True

    def cancel_now(self, connection, task: TaskRef) -> bool:
        """Expert cancellation invalidates active children immediately."""
        if task.kind != TaskKind.EXPERT:
            raise ValueError("immediate cancellation is reserved for expert tasks")
        row = self._row(connection, task)
        if row is None or row["status"] not in {"QUEUED", "RUNNING", "WAITING_CHILDREN"}:
            return False
        if not self._lease_allowed(connection, task, row, action="cancel", execution_valid=True, cancelled=False):
            return False
        self._update(connection, task, status="CANCELLED", lease_owner=None, lease_until=None,
                     lease_epoch=int(row["lease_epoch"]) + 1,
                     cancel_requested_at=self.now(connection).isoformat())
        return True
