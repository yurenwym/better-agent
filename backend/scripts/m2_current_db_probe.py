"""Rollback-only M2 root-budget verification against the current PostgreSQL DB."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

from app.agents import AgentTaskService
from app.conversation import ConversationService
from app.costs import CostService
from app.db import Database, POSTGRES_SCHEMA_HEAD
from app.research.service import ResearchService


class _RollbackProbe(Exception):
    pass


class _Skills:
    @staticmethod
    def validate(names):
        return tuple(names)


def main() -> int:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL must identify the current PostgreSQL runtime database")
    db = Database(database_url, workspace=Path(__file__).resolve().parents[1])
    if db.backend != "postgresql":
        raise SystemExit("M2 current-database probe requires PostgreSQL")

    owner = f"m2-current-rollback-{uuid.uuid4().hex}"
    thread_id = f"thread_{uuid.uuid4().hex}"
    costs = CostService(db)
    runtime = SimpleNamespace(skills=_Skills(), evolution=None, costs=costs)
    conversation = ConversationService(db, agent_runtime=runtime)
    agents = AgentTaskService(db, thread_events=conversation.events)
    research = ResearchService(db, conversation.events)

    try:
        try:
            with db.transaction() as connection:
                head = connection.execute("SELECT version_num FROM alembic_version").fetchone()
                if head is None or head[0] != POSTGRES_SCHEMA_HEAD:
                    raise AssertionError("current PostgreSQL schema is not at the required head")
                database_name = connection.execute("SELECT current_database()").fetchone()[0]
                if database_name != "better_agent":
                    raise AssertionError(f"expected better_agent, got {database_name}")

                now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
                connection.execute(
                    "INSERT INTO threads(id,title,owner_id,version,next_event_seq,created_at,updated_at) "
                    "VALUES (?,?,?,0,1,?,?)",
                    (thread_id, "M2 rollback probe", owner, now, now),
                )
                first = conversation.accept_turn(
                    thread_id, "probe-1", "first independent request", [],
                    connection=connection, owner_id=owner,
                )
                second = conversation.accept_turn(
                    thread_id, "probe-2", "second independent request", [],
                    connection=connection, owner_id=owner,
                )
                rows = connection.execute(
                    "SELECT id,root_budget_id FROM turns WHERE id IN (?,?) ORDER BY id",
                    (first.turn_id, second.turn_id),
                ).fetchall()
                root_ids = {row["root_budget_id"] for row in rows}
                if len(root_ids) != 2 or None in root_ids:
                    raise AssertionError("independent turns did not receive distinct root budgets")

                job = research.create_from_turn(
                    first.turn_id, "probe research", ("local_note",), connection=connection,
                )
                job_root = connection.execute(
                    "SELECT root_budget_id FROM research_jobs WHERE id=?", (job.id,),
                ).fetchone()[0]
                first_root = next(row["root_budget_id"] for row in rows if row["id"] == first.turn_id)
                if job_root != first_root:
                    raise AssertionError("research job did not inherit its source turn root budget")

                bundle_id = connection.execute(
                    "SELECT bundle_id FROM runtime_channels WHERE name='stable'",
                ).fetchone()[0]
                connection.execute(
                    "UPDATE turns SET runtime_bundle_id=? WHERE id=?", (bundle_id, second.turn_id),
                )
                run = agents.create_run(
                    owner, "probe experts", {"request": "probe"}, bundle_id,
                    thread_id=thread_id, idempotency_key=f"probe-agent:{second.turn_id}",
                    append_thread_message=False, connection=connection,
                    parent_turn_id=second.turn_id,
                )
                second_root = next(row["root_budget_id"] for row in rows if row["id"] == second.turn_id)
                if run["root_budget_id"] != second_root:
                    raise AssertionError("agent run did not inherit its parent turn root budget")
                raise _RollbackProbe
        except _RollbackProbe:
            pass

        with db.connection() as connection:
            checks = {
                "threads": connection.execute(
                    "SELECT COUNT(*) FROM threads WHERE owner_id=?", (owner,),
                ).fetchone()[0],
                "roots": connection.execute(
                    "SELECT COUNT(*) FROM task_budget_roots WHERE owner_id=?", (owner,),
                ).fetchone()[0],
                "budgets": connection.execute(
                    "SELECT COUNT(*) FROM cost_budgets WHERE owner_id=?", (owner,),
                ).fetchone()[0],
                "agent_runs": connection.execute(
                    "SELECT COUNT(*) FROM agent_runs WHERE owner_id=?", (owner,),
                ).fetchone()[0],
            }
        if any(checks.values()):
            raise AssertionError(f"rollback probe left current-database rows: {checks}")
        print("M2_CURRENT_POSTGRES_ROLLBACK_PROBE=PASS")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
