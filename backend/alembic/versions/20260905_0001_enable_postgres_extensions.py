"""Enable extensions required by memory retrieval.

Revision ID: 20260905_0001
Revises:
Create Date: 2026-09-05
"""

from typing import Sequence, Union
from pathlib import Path

from alembic import op


revision: str = "20260905_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    schema_path = Path(__file__).parents[1] / "postgres_schema.sql"
    op.execute(schema_path.read_text(encoding="utf-8"))
    op.execute("ALTER TABLE turn_jobs ADD COLUMN IF NOT EXISTS thread_id TEXT")
    op.execute("ALTER TABLE turn_jobs ADD COLUMN IF NOT EXISTS lease_epoch BIGINT NOT NULL DEFAULT 0")
    for column in ("lease_until", "cancel_requested_at", "started_at", "finished_at"):
        op.execute(
            f"ALTER TABLE turn_jobs ALTER COLUMN {column} TYPE TIMESTAMPTZ "
            f"USING NULLIF({column},'')::timestamptz"
        )
    op.execute(
        "UPDATE turn_jobs j SET thread_id=t.thread_id FROM turns t "
        "WHERE t.id=j.turn_id"
    )
    op.execute("ALTER TABLE turn_jobs ALTER COLUMN thread_id SET NOT NULL")
    op.execute(
        "ALTER TABLE turns ADD CONSTRAINT uq_turns_thread_id_id UNIQUE(thread_id,id)"
    )
    op.execute(
        "ALTER TABLE turn_jobs ADD CONSTRAINT fk_turn_jobs_thread_turn "
        "FOREIGN KEY(thread_id,turn_id) REFERENCES turns(thread_id,id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_turn_jobs_one_running_per_thread "
        "ON turn_jobs(thread_id) WHERE status='RUNNING'"
    )
    op.execute(
        "INSERT INTO app_settings(id,human_mode,updated_at) "
        "VALUES (1,0,clock_timestamp()::text) ON CONFLICT(id) DO NOTHING"
    )
    op.execute(
        "CREATE TABLE memory_fts ("
        "entry_id TEXT PRIMARY KEY REFERENCES memory_entries(id) ON DELETE CASCADE,"
        "owner_id TEXT NOT NULL,content TEXT NOT NULL,"
        "search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple',content)) STORED)"
    )
    op.execute("CREATE INDEX memory_fts_search_idx ON memory_fts USING gin(search_vector)")
    op.execute("CREATE INDEX memory_fts_trgm_idx ON memory_fts USING gin(content gin_trgm_ops)")
    op.execute(
        "CREATE TABLE embedding_profiles ("
        "id TEXT PRIMARY KEY,provider TEXT NOT NULL,model TEXT NOT NULL,model_revision TEXT NOT NULL DEFAULT '',"
        "dimensions INTEGER NOT NULL CHECK(dimensions=1024),distance_metric TEXT NOT NULL DEFAULT 'cosine',"
        "input_prefix TEXT NOT NULL DEFAULT '',active BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())"
    )
    op.execute("CREATE UNIQUE INDEX embedding_profiles_one_active_idx ON embedding_profiles((true)) WHERE active")
    op.execute(
        "CREATE TABLE memory_embeddings ("
        "revision_id TEXT NOT NULL REFERENCES memory_revisions(id) ON DELETE CASCADE,"
        "profile_id TEXT NOT NULL REFERENCES embedding_profiles(id),owner_id TEXT NOT NULL,"
        "scope_type TEXT NOT NULL,scope_id TEXT NOT NULL DEFAULT '',content_hash TEXT NOT NULL,"
        "embedding vector(1024) NOT NULL,embedded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),"
        "PRIMARY KEY(revision_id,profile_id))"
    )
    op.execute("CREATE INDEX memory_embeddings_scope_idx ON memory_embeddings(profile_id,owner_id,scope_type,scope_id)")
    op.execute(
        "CREATE INDEX memory_embeddings_hnsw_cos_idx ON memory_embeddings "
        "USING hnsw(embedding vector_cosine_ops) WITH (m=16,ef_construction=64)"
    )
    op.execute(
        "CREATE TABLE embedding_jobs ("
        "revision_id TEXT NOT NULL REFERENCES memory_revisions(id) ON DELETE CASCADE,"
        "profile_id TEXT NOT NULL REFERENCES embedding_profiles(id),content_hash TEXT NOT NULL,"
        "status TEXT NOT NULL,available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),"
        "lease_owner TEXT,lease_epoch BIGINT NOT NULL DEFAULT 0,lease_until TIMESTAMPTZ,"
        "attempts INTEGER NOT NULL DEFAULT 0,max_attempts INTEGER NOT NULL DEFAULT 5,"
        "last_error_code TEXT,last_error_json TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),"
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(revision_id,profile_id,content_hash))"
    )
    op.execute("CREATE INDEX embedding_jobs_claim_idx ON embedding_jobs(status,available_at,lease_until,created_at)")
    op.execute(
        "INSERT INTO embedding_profiles(id,provider,model,dimensions,active) "
        "VALUES ('siliconflow-qwen3-embedding-4b-1024','siliconflow','Qwen/Qwen3-Embedding-4B',1024,true)"
    )
    op.execute(
        "CREATE TABLE sqlite_import_completions ("
        "id SMALLINT PRIMARY KEY CHECK(id=1),"
        "source_manifest_sha256 TEXT NOT NULL CHECK(length(source_manifest_sha256)=64),"
        "manifest_json JSONB NOT NULL,"
        "completed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())"
    )
    op.execute(
        "CREATE FUNCTION reject_append_only_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END $$"
    )
    for table in (
        "thread_events", "events", "goal_program_events", "agent_events",
        "evolution_events", "cost_ledger", "skill_events", "evaluation_events",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()"
        )


def downgrade() -> None:
    raise RuntimeError("authoritative PostgreSQL schema downgrade is intentionally unsupported")
