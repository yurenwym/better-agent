"""Give ``turn_asks`` a direct owning scope.

``call_id`` was already a stable identifier for a tool result, but the owning
scope could only be reached by joining ``turns`` to ``threads``. A paged read of
a large result should not have to trust a caller-supplied thread id, so persist
the owner directly and backfill it from the existing join.
"""

from alembic import op

revision = "20260918_0017"
down_revision = "20260918_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE turn_asks ADD COLUMN owner_id TEXT")
    op.execute(
        """
        UPDATE turn_asks SET owner_id = (
          SELECT t.owner_id FROM turns tt JOIN threads t ON t.id = tt.thread_id
          WHERE tt.id = turn_asks.turn_id
        ) WHERE owner_id IS NULL
        """
    )
    op.execute("CREATE INDEX idx_turn_asks_owner ON turn_asks(owner_id, call_id)")


def downgrade() -> None:
    op.execute("DROP INDEX idx_turn_asks_owner")
    op.execute("ALTER TABLE turn_asks DROP COLUMN owner_id")
