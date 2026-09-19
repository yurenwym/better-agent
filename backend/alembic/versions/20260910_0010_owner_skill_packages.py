"""Identical packages can be installed independently by different owners."""
from alembic import op
revision = "20260910_0010"
down_revision = "20260910_0009"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("ALTER TABLE skill_versions DROP CONSTRAINT skill_versions_package_digest_key")

def downgrade():
    raise RuntimeError("independent owner installations cannot be collapsed")
