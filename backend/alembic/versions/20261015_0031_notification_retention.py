"""Keep notification history when a channel is removed.

Channels are soft-deleted so an in-flight delivery can still record its
terminal state. A confirmed resend of UNKNOWN stores the delivery it may
duplicate. Downgrade stops once either fact exists.
"""
from alembic import op

revision = "20261015_0031"
down_revision = "20261015_0030"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE notification_channels ADD COLUMN IF NOT EXISTS deleted_at TEXT")
    op.execute("ALTER TABLE notification_deliveries ADD COLUMN IF NOT EXISTS prior_delivery_id TEXT")


def downgrade():
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM notification_channels WHERE deleted_at IS NOT NULL)
             OR EXISTS (SELECT 1 FROM notification_deliveries WHERE prior_delivery_id IS NOT NULL) THEN
            RAISE EXCEPTION
              'notification downgrade would hide a deleted channel or an unknown-resend link';
          END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE notification_deliveries DROP COLUMN IF EXISTS prior_delivery_id")
    op.execute("ALTER TABLE notification_channels DROP COLUMN IF EXISTS deleted_at")
