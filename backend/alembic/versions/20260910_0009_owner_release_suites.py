"""Allow independently frozen identical replay suites for different owners."""
from alembic import op

revision = "20260910_0009"
down_revision = "20260910_0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE evolution_release_suites DROP CONSTRAINT evolution_release_suites_pkey")
    op.execute("ALTER TABLE evolution_release_suites ADD PRIMARY KEY(owner_id,digest)")


def downgrade():
    raise RuntimeError("owner-scoped replay audit cannot be collapsed")
