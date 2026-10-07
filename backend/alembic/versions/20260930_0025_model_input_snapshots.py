"""Persist the frozen logical input of one model call.

``model_input_snapshots`` holds the canonical JSON envelope a logical call was
built from, plus the digest over its UTF-8 bytes.  The row is append-only: an
UPDATE would rewrite the record of what the model was actually shown, and a
DELETE would erase it.  ``model_invocations.context_snapshot_id`` is the
nullable link from a call to its input; it stays NULL for historical rows,
which remain readable as incomplete records rather than being back-filled.

The column is deliberately not on ``turns``: one turn can issue several logical
calls, each with its own frozen input.
"""
from alembic import op

revision = "20260930_0025"
down_revision = "20260930_0024"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE model_input_snapshots ("
        "id TEXT PRIMARY KEY,"
        "owner_id TEXT NOT NULL,"
        "schema_version TEXT NOT NULL "
        "CHECK(schema_version = 'model-input-snapshot-v1'),"
        "content_json TEXT NOT NULL,"
        "content_digest TEXT NOT NULL CHECK(length(content_digest) = 64),"
        "created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())"
    )
    op.execute(
        "CREATE INDEX idx_model_input_snapshots_owner "
        "ON model_input_snapshots(owner_id, created_at)"
    )
    op.execute(
        "CREATE INDEX idx_model_input_snapshots_digest "
        "ON model_input_snapshots(content_digest)"
    )
    op.execute(
        "CREATE TRIGGER model_input_snapshots_append_only "
        "BEFORE UPDATE OR DELETE ON model_input_snapshots "
        "FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()"
    )
    op.execute(
        "ALTER TABLE model_invocations ADD COLUMN context_snapshot_id TEXT "
        "REFERENCES model_input_snapshots(id)"
    )
    # The binding is part of the invocation's INSERT and is immutable from then
    # on.  Any UPDATE of either column is refused: replacing the pair, clearing
    # it, or back-filling a call that was created without one.  `UPDATE OF`
    # fires on the columns being assigned, so status, budget and attempt updates
    # are unaffected.
    op.execute(
        "CREATE FUNCTION reject_snapshot_rebind() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN "
        "RAISE EXCEPTION 'model invocation snapshot binding is immutable'; "
        "END $$"
    )
    op.execute(
        "CREATE TRIGGER model_invocations_snapshot_binding_immutable "
        "BEFORE UPDATE OF context_snapshot_id, context_snapshot_digest ON model_invocations "
        "FOR EACH ROW EXECUTE FUNCTION reject_snapshot_rebind()"
    )


def downgrade():
    raise RuntimeError("model input snapshot history must not be discarded")
