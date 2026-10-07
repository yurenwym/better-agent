"""Durable storage for frozen model input snapshots.

Every method here runs on a **caller-supplied transaction** (or a plain read
connection); the store never opens and commits a transaction of its own.  That is
what lets ``ModelInputSnapshot``, ``ModelInvocation`` and the execution-context
binding land in one atomic write — if any of them fails, the whole thing rolls
back and no request is ever sent.

Two things are deliberately *not* stored here:

* The snapshot's ``id`` is not part of the envelope, so two invocations that send
  identical content share a digest and still get independent rows.
* The binding is never written as a follow-up ``UPDATE``.  It is a column of the
  invocation's own ``INSERT`` (see :meth:`require_bindable`), so a call is either
  created together with its input or not created at all: there is no window in
  which an existing invocation — open, finished or historical — could be given
  an input after the fact.  The database refuses any later change to the pair.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .model_input_snapshot import (
    ModelInputSnapshot,
    SnapshotBindingConflict,
    SnapshotError,
    SnapshotIntegrityError,
    deserialize_envelope,
    snapshot_digest,
)

SNAPSHOT_ID_PREFIX = "model_input_snapshot_"


class SnapshotMissing(SnapshotError):
    """No such snapshot is visible to this owner.

    Raised both when the row does not exist and when it belongs to somebody else,
    so a read cannot be used to probe for another owner's data.
    """

    code = "SNAPSHOT_MISSING"


class ModelInvocationMissing(SnapshotError):
    code = "MODEL_INVOCATION_MISSING"


class SnapshotBindingMissing(SnapshotError):
    """The invocation has no snapshot: a legacy or incomplete record.

    This is a classification, not corruption.  Callers must not treat it as a
    complete record of what was sent, and must not repair it by attaching a
    snapshot after the fact.
    """

    code = "SNAPSHOT_BINDING_MISSING"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ModelInputSnapshotStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ write

    def insert(
        self,
        connection: Any,
        snapshot: ModelInputSnapshot,
        *,
        snapshot_id: str | None = None,
    ) -> str:
        """Persist ``snapshot`` on the caller's transaction and return its id."""
        resolved = snapshot_id or snapshot.id or f"{SNAPSHOT_ID_PREFIX}{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO model_input_snapshots"
            "(id,owner_id,schema_version,content_json,content_digest,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                resolved,
                snapshot.owner_id,
                snapshot.schema_version,
                snapshot.content_json,
                snapshot.content_digest,
                _now(),
            ),
        )
        return resolved

    def require_bindable(
        self,
        connection: Any,
        snapshot_id: str,
        *,
        owner_id: str,
        runtime_bundle_id: str | None,
        role: str,
        purpose: str,
    ) -> ModelInputSnapshot:
        """Read a persisted snapshot back for a call that is being created.

        This is the only way a snapshot enters a call, and it is a *read*: the
        binding itself is a column of the invocation's ``INSERT``, so it lands in
        the same transaction that creates the row.  Consequently a call can never
        be given an input later — not while it is open, not after it finished,
        and not as a repair of a historical record.

        The snapshot must exist, belong to the same owner and describe the same
        logical call (role, purpose, runtime bundle).  A mismatch is refused here
        rather than recorded, because a self-consistent envelope that describes a
        different call is exactly the impersonation this check exists to stop.
        """
        snapshot = self._load_row(connection, owner_id, snapshot_id)
        if not snapshot.bound_to(
            owner_id,
            runtime_bundle_id=runtime_bundle_id,
            role=role,
            purpose=purpose,
        ):
            raise SnapshotBindingConflict(
                f"input snapshot {snapshot_id} does not describe this model call"
            )
        return snapshot

    # ------------------------------------------------------------------- read

    def load(self, owner_id: str, snapshot_id: str) -> ModelInputSnapshot:
        """Load one snapshot, recomputing its digest and re-checking its binding."""
        with self.db.connection() as connection:
            return self._load_row(connection, owner_id, snapshot_id)

    def load_for_invocation(self, owner_id: str, invocation_id: str) -> ModelInputSnapshot:
        """Load the input of one logical call.

        Raises :class:`SnapshotBindingMissing` for a legacy call that predates
        snapshots, and never returns another owner's snapshot.
        """
        with self.db.connection() as connection:
            invocation = connection.execute(
                "SELECT owner_id,runtime_bundle_id,role,purpose,context_snapshot_id,"
                "context_snapshot_digest FROM model_invocations WHERE id=? AND owner_id=?",
                (invocation_id, owner_id),
            ).fetchone()
            if invocation is None:
                raise ModelInvocationMissing(f"unknown model invocation: {invocation_id}")
            snapshot_id = invocation["context_snapshot_id"]
            if not snapshot_id:
                # A populated digest with no id is exactly the shape rule 3.5/6
                # calls out: the old column may hold a value from before the
                # snapshot existed.  It stays legacy, it is not upgraded.
                raise SnapshotBindingMissing(
                    f"model invocation {invocation_id} has no input snapshot"
                )
            snapshot = self._load_row(connection, owner_id, snapshot_id)

        if snapshot.content_digest != invocation["context_snapshot_digest"]:
            raise SnapshotIntegrityError(
                f"model invocation {invocation_id} and its input snapshot disagree on the digest"
            )
        if not snapshot.bound_to(
            owner_id,
            runtime_bundle_id=invocation["runtime_bundle_id"],
            role=invocation["role"],
            purpose=invocation["purpose"],
        ):
            raise SnapshotBindingConflict(
                f"input snapshot {snapshot_id} does not describe model invocation {invocation_id}"
            )
        return snapshot

    # ----------------------------------------------------------------- private

    def _load_row(self, connection: Any, owner_id: str, snapshot_id: str) -> ModelInputSnapshot:
        row = connection.execute(
            "SELECT id,owner_id,schema_version,content_json,content_digest "
            "FROM model_input_snapshots WHERE id=? AND owner_id=?",
            (snapshot_id, owner_id),
        ).fetchone()
        if row is None:
            raise SnapshotMissing(f"unknown model input snapshot: {snapshot_id}")

        content_json = row["content_json"]
        # The stored digest must be a digest of the stored bytes.  A row where
        # only one of the two was rewritten is corrupt, and saying so is the
        # whole point: it must never be reported as "legacy" instead.
        if snapshot_digest(content_json) != row["content_digest"]:
            raise SnapshotIntegrityError(
                f"model input snapshot {snapshot_id} does not match its stored digest"
            )

        # An unknown or malformed envelope is refused here, before any of it is
        # handed back as if it were a valid record.
        binding = deserialize_envelope(content_json)["binding"]
        if binding["owner_id"] != row["owner_id"]:
            raise SnapshotBindingConflict(
                f"model input snapshot {snapshot_id} disagrees with its stored owner"
            )
        return ModelInputSnapshot(
            owner_id=row["owner_id"],
            role=binding["role"],
            purpose=binding["purpose"],
            runtime_bundle_id=binding["runtime_bundle_id"],
            content_json=content_json,
            content_digest=row["content_digest"],
            schema_version=row["schema_version"],
            id=row["id"],
        )
