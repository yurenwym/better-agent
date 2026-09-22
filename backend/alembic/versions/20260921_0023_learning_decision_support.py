"""Record the distribution support the routing gate actually read.

`confidence` is JEV's self-reported confidence; `support` is the probability mass
its own distribution put behind the chosen target, and it is the number
`LearningDecision.from_answers` gates on. Persisting only the self-report makes the
audit misleading: a row can read `confidence=0.48, target=SKILL`, which looks like
the threshold was bypassed when in fact the distribution was confident.
"""
from alembic import op

revision = "20260921_0023"
down_revision = "20260921_0022"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE learning_decisions ADD COLUMN support REAL NOT NULL DEFAULT 0")


def downgrade():
    raise RuntimeError("learning decision audit must not be discarded")
