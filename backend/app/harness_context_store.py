"""Durable storage for the execution context of a turn, tool call or model call.

The context is an *identity* record, not a snapshot of model input: it is stored
next to the row it belongs to, with a digest over the canonical envelope, and
every read re-derives both.  Three rules shape this module:

* JSON and digest are written in one statement, inside the caller's transaction.
  A row with one and not the other is corrupt, never "half migrated".
* The stored context can never be overwritten by a different one.  A repeated
  write is idempotent only when the canonical content is identical.
* Every write and every read re-checks the context against the row's own
  columns and against the trusted records it is associated with.  A mismatch is
  a refusal; nothing is repaired or silently re-pointed.

``execution_context_digest`` identifies the *execution identity*; the existing
``context_snapshot_digest`` identifies the *model input*.  They are not
interchangeable and neither one substitutes for the other.
"""

from __future__ import annotations

from typing import Any

from .db import Database
from .execution_context import (
    ContextIdentityConflict,
    HarnessContextError,
    HarnessExecutionContext,
    LegacyContextMissing,
    UnknownSchemaVersion,
    create_root_context,
    deserialize_context,
    execution_context_digest,
    serialize_context,
)

#: Kept as a literal: importing ``chat_tools`` here would close an import cycle
#: (chat_tools needs this module for the approval-resume path).
CHAT_RUN_PREFIX = "chat-turn:"


class ContextStoreConflict(ContextIdentityConflict):
    """The stored context disagrees with the row it is bound to."""


def _row_get(row: Any, name: str, default: Any = None) -> Any:
    """Read an optional column so pre-migration rows stay readable."""
    try:
        keys = row.keys()
    except AttributeError:
        return default
    return row[name] if name in keys else default


def _stored(row: Any) -> tuple[Any, Any]:
    return _row_get(row, "execution_context_json"), _row_get(row, "execution_context_digest")


class HarnessContextStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ #
    # turns
    # ------------------------------------------------------------------ #

    def load_or_create_turn_context(
        self,
        turn_id: str,
        *,
        owner_id: str,
        thread_id: str,
        project_id: str | None = None,
        run_id: str | None = None,
        root_budget_id: str | None = None,
        runtime_bundle_id: str | None = None,
    ) -> HarnessExecutionContext:
        """The turn's trace root.  A restart re-reads it instead of re-minting."""
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if row is None:
                raise HarnessContextError(f"turn {turn_id} does not exist")
            payload, _ = _stored(row)
            if payload is not None:
                return self._read(connection, "turns", row)
            # The chat run scope is a property of the turn, not of the caller: a
            # tool call bound to this turn can only ever carry `chat-turn:<id>`,
            # so defaulting here removes a way to mint an unusable root.
            context = create_root_context(
                owner_id=owner_id, thread_id=thread_id, turn_id=turn_id,
                run_id=run_id or f"{CHAT_RUN_PREFIX}{turn_id}",
                project_id=project_id, root_budget_id=root_budget_id,
                runtime_bundle_id=runtime_bundle_id,
            )
            self._write(connection, "turns", row, context)
            return context

    def save_turn_context(self, connection: Any, turn_id: str, context: HarnessExecutionContext) -> None:
        row = connection.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        if row is None:
            raise HarnessContextError(f"turn {turn_id} does not exist")
        self._write(connection, "turns", row, context)

    def load_turn_context(self, turn_id: str) -> HarnessExecutionContext:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if row is None:
                raise HarnessContextError(f"turn {turn_id} does not exist")
            return self._read(connection, "turns", row)

    # ------------------------------------------------------------------ #
    # tool calls
    # ------------------------------------------------------------------ #

    def save_tool_call_context(self, connection: Any, call_id: str, context: HarnessExecutionContext) -> None:
        row = connection.execute("SELECT * FROM turn_tool_calls WHERE id=?", (call_id,)).fetchone()
        if row is None:
            raise HarnessContextError(f"tool call {call_id} does not exist")
        self._write(connection, "turn_tool_calls", row, context)

    def load_tool_call_context(self, call_id: str, *, connection: Any | None = None) -> HarnessExecutionContext:
        if connection is not None:
            row = connection.execute("SELECT * FROM turn_tool_calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                raise HarnessContextError(f"tool call {call_id} does not exist")
            return self._read(connection, "turn_tool_calls", row)
        with self.db.connection() as active:
            row = active.execute("SELECT * FROM turn_tool_calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                raise HarnessContextError(f"tool call {call_id} does not exist")
            return self._read(active, "turn_tool_calls", row)

    # ------------------------------------------------------------------ #
    # model invocations
    # ------------------------------------------------------------------ #

    def save_invocation_context(self, connection: Any, invocation_id: str, context: HarnessExecutionContext) -> None:
        row = connection.execute("SELECT * FROM model_invocations WHERE id=?", (invocation_id,)).fetchone()
        if row is None:
            raise HarnessContextError(f"model invocation {invocation_id} does not exist")
        self._write(connection, "model_invocations", row, context)

    def load_invocation_context(self, invocation_id: str) -> HarnessExecutionContext:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM model_invocations WHERE id=?", (invocation_id,)).fetchone()
            if row is None:
                raise HarnessContextError(f"model invocation {invocation_id} does not exist")
            return self._read(connection, "model_invocations", row)

    # ------------------------------------------------------------------ #
    # shared mechanics
    # ------------------------------------------------------------------ #

    def _write(self, connection: Any, table: str, row: Any, context: HarnessExecutionContext) -> None:
        if not isinstance(context, HarnessExecutionContext):
            raise HarnessContextError("only a HarnessExecutionContext can be persisted")
        payload, digest = _stored(row)
        self._assert_bindings(connection, table, row, context)
        if payload is None and digest is None:
            written = connection.execute(
                f"UPDATE {table} SET execution_context_json=?, execution_context_digest=? "
                "WHERE id=? AND execution_context_json IS NULL AND execution_context_digest IS NULL",
                (serialize_context(context), execution_context_digest(context), row["id"]),
            )
            if written.rowcount == 1:
                return
            # Another transaction won the first binding. Re-read its committed
            # value and apply the same digest, binding and equality checks.
            current = connection.execute(f"SELECT * FROM {table} WHERE id=?", (row["id"],)).fetchone()
            if current is None or self._read(connection, table, current) != context:
                raise ContextStoreConflict(f"{table} row {row['id']} was bound concurrently to another context")
            return
        if payload is None or digest is None:
            raise ContextStoreConflict(f"{table} row {row['id']} has a half-written execution context")
        if execution_context_digest(payload) != digest:
            raise ContextStoreConflict(
                f"{table} row {row['id']} stored execution context digest does not match its content"
            )
        stored = deserialize_context(payload)
        if stored != context:
            raise ContextStoreConflict(
                f"{table} row {row['id']} is already bound to a different execution context"
            )

    def _read(self, connection: Any, table: str, row: Any) -> HarnessExecutionContext:
        payload, digest = _stored(row)
        if payload is None and digest is None:
            raise LegacyContextMissing(f"{table} row {row['id']} predates the execution context")
        if payload is None or digest is None:
            raise ContextStoreConflict(f"{table} row {row['id']} has a half-written execution context")
        if execution_context_digest(payload) != digest:
            raise ContextStoreConflict(
                f"{table} row {row['id']} stored execution context digest does not match its content"
            )
        context = deserialize_context(payload)
        self._assert_bindings(connection, table, row, context)
        return context

    def _assert_bindings(
        self, connection: Any, table: str, row: Any, context: HarnessExecutionContext,
    ) -> None:
        expected = self._expected_bindings(connection, table, row)
        for field_name, value in expected:
            actual = getattr(context, field_name)
            if actual != value:
                raise ContextStoreConflict(
                    f"{table} row {row['id']}: context.{field_name} does not match its bound value"
                )
        if table == "turns":
            # A turn has no run column to compare against, so the run binding is
            # derived from the turn itself instead of being accepted as given.
            if context.run_id is not None and context.run_id != f"{CHAT_RUN_PREFIX}{row['id']}":
                raise ContextStoreConflict(
                    f"turns row {row['id']}: context.run_id is not this turn's chat run"
                )

    def _expected_bindings(self, connection: Any, table: str, row: Any) -> list[tuple[str, Any]]:
        if table == "model_invocations":
            return [
                ("owner_id", row["owner_id"]),
                ("run_id", row["run_id"]),
                ("thread_id", row["thread_id"]),
                ("turn_id", row["turn_id"]),
                ("runtime_bundle_id", row["runtime_bundle_id"]),
                ("root_budget_id", _row_get(row, "root_budget_id")),
                ("task_id", row["agent_task_id"]),
            ]
        if table == "turns":
            thread = connection.execute(
                "SELECT owner_id,project_id FROM threads WHERE id=?", (row["thread_id"],),
            ).fetchone()
            return [
                ("turn_id", row["id"]),
                ("thread_id", row["thread_id"]),
                ("runtime_bundle_id", _row_get(row, "runtime_bundle_id")),
                ("root_budget_id", _row_get(row, "root_budget_id")),
                ("owner_id", thread["owner_id"] if thread is not None else None),
                ("project_id", thread["project_id"] if thread is not None else None),
            ]
        if table == "turn_tool_calls":
            turn = connection.execute("SELECT * FROM turns WHERE id=?", (row["turn_id"],)).fetchone()
            thread = connection.execute(
                "SELECT owner_id,project_id FROM threads WHERE id=?", (row["thread_id"],),
            ).fetchone()
            return [
                ("turn_id", row["turn_id"]),
                ("thread_id", row["thread_id"]),
                # The original tool call keeps the original turn's run scope; the
                # continuation turn that resumes it must never replace it.
                ("run_id", f"{CHAT_RUN_PREFIX}{row['turn_id']}"),
                ("owner_id", thread["owner_id"] if thread is not None else None),
                ("project_id", thread["project_id"] if thread is not None else None),
                ("root_budget_id", _row_get(turn, "root_budget_id") if turn is not None else None),
                ("runtime_bundle_id", _row_get(turn, "runtime_bundle_id") if turn is not None else None),
            ]
        raise HarnessContextError(f"unsupported context table: {table}")
