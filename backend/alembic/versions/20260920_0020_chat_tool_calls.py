"""Durable business-tool calls issued from plain chat.

A plain conversation turn now exposes the goal business tools. READ calls run
inline, but a WRITE call must pause the turn until the user decides, and the
result must survive a worker restart so the same call is never re-executed with
new parameters. ``turn_tool_calls`` stores that call identity; the approval
itself keeps living in the existing ``approvals`` table, and execution claims
stay in ``tool_execution_claims``/``tool_calls``.

The continuation turn created when the user decides is stored on the row so a
replayed decision returns the same continuation instead of starting a second
one.
"""

from alembic import op

revision = "20260920_0020"
down_revision = "20260918_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS turn_tool_calls (
            id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            params_json TEXT NOT NULL,
            params_hash TEXT NOT NULL,
            risk TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING_APPROVAL',
            approval_id TEXT,
            binding_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT,
            error_code TEXT,
            continuation_turn_id TEXT,
            decision_idempotency_key TEXT UNIQUE,
            created_at TEXT NOT NULL,
            acted_at TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_turn_tool_calls_turn ON turn_tool_calls(turn_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_turn_tool_calls_continuation ON turn_tool_calls(continuation_turn_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS turn_tool_calls")
