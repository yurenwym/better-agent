"""Persist invocation-scoped model events.

``ModelControlStore.record_event`` only ever wrote to the run/goal event store
or the thread event store, both of which require identifiers a call may not
have. A capacity rejection or a skipped fallback on a call with no thread/turn
and no run/goal therefore left no trace at all. The invocation is the one
identifier every call is guaranteed to carry, so these records need their own
home keyed on it.
"""

from alembic import op

revision = "20260918_0015"
down_revision = "20260918_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE model_invocation_events (
          id TEXT PRIMARY KEY,
          invocation_id TEXT NOT NULL REFERENCES model_invocations(id),
          event_type TEXT NOT NULL,
          data_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_model_invocation_events_invocation"
        " ON model_invocation_events(invocation_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX idx_model_invocation_events_invocation")
    op.execute("DROP TABLE model_invocation_events")
