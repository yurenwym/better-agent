"""Resolve Experience provenance from persisted source relations, never LLM claims."""
from __future__ import annotations

import hashlib


def user_sources(db, experience, owner_id):
    kind, source_id = experience["source_kind"], experience["source_id"]
    with db.connection() as connection:
        if kind == "thread_message":
            where, args = "m.id=?", (source_id,)
        elif kind == "turn":
            where, args = "m.turn_id=?", (source_id,)
        elif kind in {"run", "research"}:
            table = "runs" if kind == "run" else "research_jobs"
            where, args = f"m.turn_id IN (SELECT source_turn_id FROM {table} WHERE id=?)", (source_id,)
        else:
            return []
        rows = connection.execute(
            "SELECT m.id,m.content,m.turn_id,t.project_id FROM thread_messages m "
            "JOIN threads t ON t.id=m.thread_id WHERE t.owner_id=? AND t.deleted_at IS NULL "
            "AND m.role='user' AND " + where + " ORDER BY m.id", (owner_id, *args),
        ).fetchall()
    return [{"id": row["id"], "content": row["content"], "project_id": row["project_id"] or "",
             "content_hash": hashlib.sha256(row["content"].encode("utf-8")).hexdigest()} for row in rows]
