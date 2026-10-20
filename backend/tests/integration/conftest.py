from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from pg_fixtures import (  # noqa: E402,F401  (re-exported for integration tests)
    RedactedDatabaseUrl,
    isolated_database_lifecycle,
    migrated_postgres_url,
    postgres_url,
    require_test_database as _require_test_database,
    truncate_application_tables as _truncate_application_tables,
)


@pytest.fixture(autouse=True)
def isolated_postgres_test(migrated_postgres_url: RedactedDatabaseUrl):
    """Give every integration test an empty application-data schema."""
    with isolated_database_lifecycle(str(migrated_postgres_url)):
        yield
