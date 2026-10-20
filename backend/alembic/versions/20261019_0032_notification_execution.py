"""Record the local execution identity of a notification send.

Existing rows stay NULL. Recovery must not invent a scope or token for them.
Downgrade stops once a row has either value, because dropping them would hide
which send the lock belonged to.
"""
from alembic import op

revision = "20261019_0032"
down_revision = "20261015_0031"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE notification_deliveries ADD COLUMN IF NOT EXISTS recovery_scope_id TEXT")
    op.execute("ALTER TABLE notification_deliveries ADD COLUMN IF NOT EXISTS executor_token TEXT")


def downgrade():
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM notification_deliveries
            WHERE recovery_scope_id IS NOT NULL OR executor_token IS NOT NULL
          ) THEN
            RAISE EXCEPTION
              'notification downgrade would hide an execution scope or token';
          END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE notification_deliveries DROP COLUMN IF EXISTS executor_token")
    op.execute("ALTER TABLE notification_deliveries DROP COLUMN IF EXISTS recovery_scope_id")
