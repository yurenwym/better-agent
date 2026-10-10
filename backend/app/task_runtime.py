"""Execution rights on existing task rows; domain services own business state."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum


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

    def require(self, connection, token: LeaseToken, *, allow_cancelled=False):
        row = self._row(connection, token.task)
        if (row is None or row["status"] != "RUNNING" or row["lease_owner"] != token.owner
                or row["lease_epoch"] != token.epoch or not self._live(row, self.now(connection))):
            raise LeaseLost(f"task lease lost: {token.task.id}")
        if not allow_cancelled and row["cancel_requested_at"] is not None:
            raise TaskCancelled(f"task cancelled: {token.task.id}")
        if not allow_cancelled:
            if token.task.kind == TaskKind.RESEARCH:
                thread_id = row["thread_id"]
            elif token.task.kind == TaskKind.EXPERT:
                run = connection.execute("SELECT thread_id FROM agent_runs WHERE id=?", (row["agent_run_id"],)).fetchone()
                thread_id = run["thread_id"]
            else:
                thread_id = None
            if thread_id is not None:
                lock = " FOR UPDATE" if self.db.backend == "postgresql" else ""
                active = connection.execute("SELECT 1 FROM threads WHERE id=? AND deleted_at IS NULL" + lock, (thread_id,)).fetchone()
                if active is None:
                    raise LeaseLost("task source thread is no longer active")
        return row

    def claim(self, connection, task: TaskRef, owner: str, seconds: float) -> TaskAttempt | None:
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
        epoch, attempt = int(row["lease_epoch"]) + 1, int(row["attempts"]) + 1
        self._update(connection, task, status="RUNNING", lease_owner=owner, lease_epoch=epoch,
                     lease_until=(now + timedelta(seconds=seconds)).isoformat(), attempts=attempt)
        return TaskAttempt(LeaseToken(task, owner, epoch), attempt)

    def heartbeat(self, connection, token: LeaseToken, seconds: float):
        if seconds <= 0:
            raise ValueError("heartbeat requires positive duration")
        self.require(connection, token)
        self._update(connection, token.task,
                     lease_until=(self.now(connection) + timedelta(seconds=seconds)).isoformat())

    def finish(self, connection, token: LeaseToken, status: str):
        allowed = {
            TaskKind.RESEARCH: {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED", "QUEUED"},
            TaskKind.EXPERT: {"SUCCEEDED", "FAILED", "CANCELLED", "WAITING_CHILDREN", "QUEUED"},
            TaskKind.TURN: {"COMPLETED", "FAILED", "CANCELLED", "QUEUED"},
            TaskKind.PROJECTION: {"READY", "FAILED"},
            TaskKind.GOAL: {"QUEUED"},
        }
        if status not in allowed[token.task.kind]:
            raise ValueError("invalid task transition")
        row = self.require(connection, token, allow_cancelled=status == "CANCELLED" or token.task.kind == TaskKind.GOAL)
        self._update(connection, token.task, status=status, lease_owner=None, lease_until=None)
        return row

    def request_cancel(self, connection, task: TaskRef) -> bool:
        if task.kind not in {TaskKind.RESEARCH, TaskKind.EXPERT, TaskKind.TURN}:
            raise ValueError("cancellation belongs to this task's domain state")
        row = self._row(connection, task)
        if row is None:
            raise KeyError(task.id)
        if row["status"] not in {"QUEUED", "RUNNING", "WAITING_CHILDREN"} or row["cancel_requested_at"]:
            return False
        table, key = _TABLES[task.kind]
        connection.execute(f"UPDATE {table} SET cancel_requested_at=? WHERE {key}=?",
                           (self.now(connection).isoformat(), task.id))
        return True

    def recover_expired(self, connection, task: TaskRef, status: str) -> bool:
        """Release an expired attempt under lock; the domain records its outcome."""
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
        self._update(connection, task, status="CANCELLED", lease_owner=None, lease_until=None,
                     lease_epoch=int(row["lease_epoch"]) + 1,
                     cancel_requested_at=self.now(connection).isoformat())
        return True
