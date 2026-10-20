"""PostgreSQL parity for the frozen skill-version columns.

SQLite has enforced ``skill_versions_frozen`` since migration 11: an UPDATE
that changes any immutable column, or any DELETE, is rejected.  The
authoritative PostgreSQL schema never gained the equivalent trigger, so a
direct UPDATE could rewrite an already-bound version.  This migration adds the
same guard without changing the writable ``status`` column.
"""
from alembic import op

revision = "20261019_0035"
down_revision = "20261019_0034"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE FUNCTION reject_frozen_skill_version() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'skill version is frozen';
          END IF;
          IF OLD.skill_id IS DISTINCT FROM NEW.skill_id
             OR OLD.version IS DISTINCT FROM NEW.version
             OR OLD.package_digest IS DISTINCT FROM NEW.package_digest
             OR OLD.manifest_digest IS DISTINCT FROM NEW.manifest_digest
             OR OLD.manifest_json IS DISTINCT FROM NEW.manifest_json
             OR OLD.title IS DISTINCT FROM NEW.title
             OR OLD.description IS DISTINCT FROM NEW.description
             OR OLD.content IS DISTINCT FROM NEW.content
             OR OLD.requested_tools_json IS DISTINCT FROM NEW.requested_tools_json
             OR OLD.connectors_json IS DISTINCT FROM NEW.connectors_json
             OR OLD.phases_json IS DISTINCT FROM NEW.phases_json
             OR OLD.storage_path IS DISTINCT FROM NEW.storage_path
             OR OLD.created_at IS DISTINCT FROM NEW.created_at
          THEN
            RAISE EXCEPTION 'skill version is frozen';
          END IF;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER skill_versions_frozen BEFORE UPDATE OR DELETE ON skill_versions "
        "FOR EACH ROW EXECUTE FUNCTION reject_frozen_skill_version()"
    )


def downgrade():
    raise RuntimeError("frozen skill versions cannot be unfrozen")
