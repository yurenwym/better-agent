from __future__ import annotations

import pytest


def test_postgres_schema_contract_requires_exact_head_and_extensions() -> None:
    from app.db import POSTGRES_SCHEMA_HEAD, _postgres_schema_is_current

    assert _postgres_schema_is_current(POSTGRES_SCHEMA_HEAD, {"vector", "pg_trgm"})
    assert not _postgres_schema_is_current("old-revision", {"vector", "pg_trgm"})
    assert not _postgres_schema_is_current(POSTGRES_SCHEMA_HEAD, {"vector"})
    assert not _postgres_schema_is_current(POSTGRES_SCHEMA_HEAD, {"pg_trgm"})


def test_runtime_requires_postgres_database_url_outside_legacy_tools(
    tmp_path, monkeypatch
) -> None:
    from app.startup import build_runtime

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BETTER_AGENT_TEST_ALLOW_SQLITE", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        build_runtime(tmp_path)


@pytest.mark.parametrize(
    "database_url",
    ["sqlite:///agent.db", "agent.db", "postgresql+psycopg://localhost/agent"],
)
def test_runtime_rejects_non_postgres_database_url(
    tmp_path, monkeypatch, database_url
) -> None:
    from app.startup import build_runtime

    monkeypatch.delenv("BETTER_AGENT_TEST_ALLOW_SQLITE", raising=False)
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        build_runtime(tmp_path, database_url=database_url)
