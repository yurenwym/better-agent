"""Record the original token-file identity of a notification send.

Existing rows stay NULL. Recovery must not treat a missing identity as the
file it finds later. Downgrade stops once a row has either value.
"""
from alembic import op

revision = "20261019_0033"
down_revision = "20261019_0032"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE notification_deliveries ADD COLUMN IF NOT EXISTS executor_file_dev TEXT")
    op.execute("ALTER TABLE notification_deliveries ADD COLUMN IF NOT EXISTS executor_file_ino TEXT")


def downgrade():
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM notification_deliveries
            WHERE executor_file_dev IS NOT NULL OR executor_file_ino IS NOT NULL
          ) THEN
            RAISE EXCEPTION
              'notification downgrade would hide a token file identity';
          END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE notification_deliveries DROP COLUMN IF EXISTS executor_file_ino")
    op.execute("ALTER TABLE notification_deliveries DROP COLUMN IF EXISTS executor_file_dev")
