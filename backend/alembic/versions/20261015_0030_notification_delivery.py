"""Notification ownership and delivery states.

Historical channels are not assigned an owner. Old PENDING deliveries become
UNKNOWN and are not resent. Downgrade stops because folding UNKNOWN back into
PENDING would hide whether a provider already received the message.
"""
from alembic import op

revision = "20261015_0030"
down_revision = "20261013_0029"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE notification_channels ADD COLUMN IF NOT EXISTS owner_id TEXT")
    op.execute("ALTER TABLE notification_deliveries ADD COLUMN IF NOT EXISTS owner_id TEXT")
    op.execute("ALTER TABLE notification_deliveries ADD COLUMN IF NOT EXISTS purpose TEXT")
    op.execute("ALTER TABLE notification_deliveries ADD COLUMN IF NOT EXISTS dispatch_key TEXT")
    op.execute("ALTER TABLE notification_deliveries ADD COLUMN IF NOT EXISTS error_code TEXT")
    op.execute("ALTER TABLE notification_deliveries ADD COLUMN IF NOT EXISTS provider_ref TEXT")
    op.execute("ALTER TABLE notification_deliveries ADD COLUMN IF NOT EXISTS claimed_at TEXT")
    op.execute("ALTER TABLE notification_deliveries ALTER COLUMN job_id DROP NOT NULL")
    op.execute(
        """
        DO $$
        DECLARE name text;
        BEGIN
          FOR name IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'notification_deliveries'::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) ILIKE '%status%'
          LOOP
            EXECUTE format('ALTER TABLE notification_deliveries DROP CONSTRAINT %I', name);
          END LOOP;
        END $$;
        """
    )
    op.execute(
        """
        UPDATE notification_deliveries AS d
        SET owner_id = t.owner_id
        FROM research_jobs AS j
        JOIN threads AS t ON t.id = j.thread_id
        WHERE d.job_id = j.id
          AND d.owner_id IS NULL
          AND t.deleted_at IS NULL
          AND t.owner_id IS NOT NULL
          AND btrim(t.owner_id) <> ''
        """
    )
    op.execute("UPDATE notification_deliveries SET status = 'UNKNOWN' WHERE status = 'PENDING'")
    op.execute("UPDATE notification_deliveries SET purpose = 'report' WHERE purpose IS NULL")
    op.execute("UPDATE notification_deliveries SET dispatch_key = 'legacy:' || id WHERE dispatch_key IS NULL")
    op.execute("ALTER TABLE notification_deliveries ALTER COLUMN purpose SET NOT NULL")
    op.execute("ALTER TABLE notification_deliveries ALTER COLUMN dispatch_key SET NOT NULL")
    op.execute(
        """
        ALTER TABLE notification_deliveries ADD CONSTRAINT notification_deliveries_status_check
        CHECK (status IN ('PENDING','SENDING','SENT','RETRY_WAIT','FAILED','UNKNOWN'))
        """
    )
    op.execute(
        """
        ALTER TABLE notification_deliveries ADD CONSTRAINT notification_deliveries_purpose_check
        CHECK (purpose IN ('report','test'))
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_deliveries_dispatch "
        "ON notification_deliveries(dispatch_key)"
    )
    op.execute(
        """
        DO $$
        DECLARE name text;
        BEGIN
          FOR name IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'notification_deliveries'::regclass AND contype = 'u'
              AND pg_get_constraintdef(oid) ILIKE '%attempt%'
          LOOP
            EXECUTE format('ALTER TABLE notification_deliveries DROP CONSTRAINT %I', name);
          END LOOP;
        END $$;
        """
    )


def downgrade():
    op.execute(
        """
        DO $$
        BEGIN
          RAISE EXCEPTION
            'notification delivery downgrade would hide UNKNOWN, SENDING, and test history';
        END $$;
        """
    )
