"""PostgreSQL parity for the SQLite turn-completion archive signal.

SQLite has an ``AFTER UPDATE OF status`` trigger that inserts one durable
``memory_archive_signals`` row when a turn transitions to COMPLETED. PostgreSQL
had the table but no equivalent trigger, so the managed archive worker never
received a signal on PostgreSQL. This adds the same transition trigger; the
insert is idempotent through the table's ``turn_id`` primary key.
"""
from alembic import op

revision = "20261019_0034"
down_revision = "20261019_0033"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE OR REPLACE FUNCTION memory_archive_signal_on_completed()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO memory_archive_signals(turn_id, thread_id, created_at)
            VALUES (NEW.id, NEW.thread_id, clock_timestamp()::text)
            ON CONFLICT (turn_id) DO NOTHING;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER memory_archive_signal_on_completed
        AFTER UPDATE OF status ON turns
        FOR EACH ROW
        WHEN (NEW.status = 'COMPLETED' AND OLD.status IS DISTINCT FROM 'COMPLETED')
        EXECUTE FUNCTION memory_archive_signal_on_completed();
        """
    )


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS memory_archive_signal_on_completed ON turns")
    op.execute("DROP FUNCTION IF EXISTS memory_archive_signal_on_completed()")
