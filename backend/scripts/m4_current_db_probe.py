"""Exercise M4 partial delivery on the current PostgreSQL and remove only probe rows."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from app.conversation import ConversationService
from app.db import Database, POSTGRES_SCHEMA_HEAD
from app.research.service import ResearchService


def main() -> int:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL must identify the current PostgreSQL runtime database")
    db = Database(database_url, workspace=Path(__file__).resolve().parents[1])
    if db.backend != "postgresql":
        raise SystemExit("M4 current-database probe requires PostgreSQL")

    owner = f"m4-current-probe-{uuid.uuid4().hex}"
    conversation = ConversationService(db)
    research = ResearchService(db, conversation.events)
    removed = 0
    try:
        with db.connection() as connection:
            head = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            if head is None or head[0] != POSTGRES_SCHEMA_HEAD:
                raise AssertionError("current PostgreSQL schema is not at the required head")
            if connection.execute("SELECT current_database()").fetchone()[0] != "better_agent":
                raise AssertionError("M4 probe must use the current better_agent database")

        thread = conversation.create_thread("M4 current PostgreSQL probe", owner_id=owner)
        job = research.create_manual(thread.id, "验证部分研究", "partial-probe", ("local_note",))
        research.claim_next(owner, 30)
        matrix = ({
            "requirement": "已支持项", "conclusion": "有证据的结论",
            "evidence_ids": ["evidence-probe"], "source_ids": ["source-probe"],
            "citation_source_ids": ["source-probe"],
            "source_versions": [{
                "source_id": "source-probe", "content_hash": "probe-hash",
                "retrieved_at": "2026-09-09T00:00:00+00:00",
            }],
            "supported": True,
        },)
        result = research.complete_partial(
            job.id, owner, "部分研究", "# 部分研究\n\n有证据的结论。", 1, 1,
            matrix, ("缺失项",),
        )
        if result.status != "PARTIAL" or result.phase != "partial":
            raise AssertionError("partial terminal state was not persisted")
        if result.traceability != matrix or result.missing_requirements != ("缺失项",):
            raise AssertionError("partial traceability was not round-tripped")
        if "research.partial" not in [item.type for item in conversation.events.list(thread.id)]:
            raise AssertionError("partial event was not emitted")
    finally:
        with db.transaction() as connection:
            thread_ids = "SELECT id FROM threads WHERE owner_id=?"
            connection.execute("ALTER TABLE thread_events DISABLE TRIGGER thread_events_append_only")
            for statement, parameters in (
                (f"DELETE FROM research_jobs WHERE thread_id IN ({thread_ids})", (owner,)),
                (f"DELETE FROM thread_events WHERE thread_id IN ({thread_ids})", (owner,)),
                (f"DELETE FROM thread_messages WHERE thread_id IN ({thread_ids})", (owner,)),
                (f"DELETE FROM turn_jobs WHERE thread_id IN ({thread_ids})", (owner,)),
                (f"DELETE FROM turns WHERE thread_id IN ({thread_ids})", (owner,)),
                ("DELETE FROM threads WHERE owner_id=?", (owner,)),
                ("DELETE FROM cost_budgets WHERE owner_id=?", (owner,)),
                ("DELETE FROM task_budget_roots WHERE owner_id=?", (owner,)),
            ):
                cursor = connection.execute(statement, parameters)
                if cursor.rowcount > 0:
                    removed += cursor.rowcount
            connection.execute("ALTER TABLE thread_events ENABLE TRIGGER thread_events_append_only")
        with db.connection() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM threads WHERE owner_id=?", (owner,),
            ).fetchone()[0]
        db.close()
        if remaining:
            raise AssertionError("M4 current-database probe left test rows")

    print(f"M4_CURRENT_POSTGRES_PARTIAL_PROBE=PASS removed={removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
