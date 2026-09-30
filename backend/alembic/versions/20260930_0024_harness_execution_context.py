"""Persist the harness execution context beside the row it identifies.

A turn, a tool call and a model invocation each gain one JSON envelope plus the
digest over its canonical form.  The context records *who* executed a logical
call and which trace/span it belongs to; it is deliberately separate from
``context_snapshot_digest``, which records what the model was shown.  Historical
rows keep both columns NULL and stay readable.
"""
from alembic import op

revision = "20260930_0024"
down_revision = "20260921_0023"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("turns", "turn_tool_calls", "model_invocations"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN execution_context_json TEXT")
        op.execute(f"ALTER TABLE {table} ADD COLUMN execution_context_digest TEXT")


def downgrade():
    raise RuntimeError("execution context history must not be discarded")
