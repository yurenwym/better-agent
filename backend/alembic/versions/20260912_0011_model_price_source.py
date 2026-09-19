"""Persist an auditable source for every immutable model price snapshot."""

from alembic import op


revision = "20260912_0011"
down_revision = "20260910_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE model_price_snapshots "
        "ADD COLUMN source_url TEXT NOT NULL DEFAULT 'legacy:unspecified'"
    )


def downgrade() -> None:
    raise RuntimeError("immutable price provenance cannot be discarded")
