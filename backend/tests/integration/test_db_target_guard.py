"""T01: the integration suite must refuse to migrate or truncate a non-test database.

`isolated_postgres_test` truncates every application table around each test, so a
mis-pointed `TEST_DATABASE_URL` destroys real data. These tests pin the guard rules
and — more importantly — the *ordering*: the refusal has to happen before alembic
runs, not after the schema has already been rewritten.
"""
from __future__ import annotations

import subprocess

import pytest

from db_target_guard import (
    TEST_DATABASE_ALLOWLIST_ENV,
    UnsafeDatabaseTarget,
    assert_isolated_test_database,
    describe_unsafe_target,
    redact,
)


@pytest.fixture(autouse=True)
def isolated_postgres_test():
    """Shadow the conftest fixture on purpose: these tests must not touch a database."""
    yield


@pytest.fixture(autouse=True)
def _clear_allowlist(monkeypatch):
    monkeypatch.delenv(TEST_DATABASE_ALLOWLIST_ENV, raising=False)


DEVELOPMENT = "postgresql://better_agent:secret@127.0.0.1:5432/better_agent"
SAFE = "postgresql://better_agent:secret@127.0.0.1:5432/better_agent_v3_test"


# --------------------------------------------------------------- what is refused

def test_the_development_database_is_rejected():
    problems = describe_unsafe_target(DEVELOPMENT)
    assert problems, "pointing the suite at better_agent must be refused"
    assert any("application database" in problem for problem in problems)


@pytest.mark.parametrize("query", ["dbname=better_agent", "host=remote", "hostaddr=10.0.0.1",
                                 "service=production", "options=-csearch_path=public", "%64bname=better_agent"])
def test_driver_target_overrides_are_rejected(query):
    assert describe_unsafe_target(SAFE + "?" + query)


def test_percent_encoded_application_database_is_rejected():
    assert describe_unsafe_target("postgresql://u:p@127.0.0.1/%62etter_agent")


@pytest.mark.parametrize("name", ["better_agent", "postgres", "template0", "template1"])
def test_maintenance_and_application_databases_are_rejected(name):
    assert describe_unsafe_target(f"postgresql://u:p@127.0.0.1:5432/{name}")


def test_a_name_without_the_test_convention_is_rejected():
    problems = describe_unsafe_target("postgresql://u:p@127.0.0.1:5432/better_agent_staging")
    assert any("not registered as a test database" in problem for problem in problems)


def test_a_non_loopback_host_is_rejected():
    problems = describe_unsafe_target("postgresql://u:p@10.20.30.40:5432/better_agent_v3_test")
    assert any("not a loopback" in problem for problem in problems)


def test_a_url_without_a_database_name_is_rejected():
    problems = describe_unsafe_target("postgresql://u:p@127.0.0.1:5432/")
    assert any("names no database" in problem for problem in problems)


def test_the_allowlist_does_not_override_the_forbidden_names(monkeypatch):
    monkeypatch.setenv(TEST_DATABASE_ALLOWLIST_ENV, "better_agent")
    assert describe_unsafe_target(DEVELOPMENT), (
        "an allowlist entry must not bless the development database"
    )


# --------------------------------------------------------------- what is accepted

def test_a_registered_test_database_is_accepted():
    assert describe_unsafe_target(SAFE) == []
    assert_isolated_test_database(SAFE)


@pytest.mark.parametrize("name", ["scratch_test", "x_tests", "y_pgtest"])
def test_the_suffix_convention_is_accepted(name):
    assert describe_unsafe_target(f"postgresql://u:p@127.0.0.1:5432/{name}") == []


def test_an_unusual_name_can_be_registered_explicitly(monkeypatch):
    monkeypatch.setenv(TEST_DATABASE_ALLOWLIST_ENV, "better_agent_v3_scratch")
    assert describe_unsafe_target(
        "postgresql://u:p@127.0.0.1:5432/better_agent_v3_scratch"
    ) == []


# -------------------------------------------------------------------- redaction

def test_the_error_never_leaks_the_password():
    with pytest.raises(UnsafeDatabaseTarget) as excinfo:
        assert_isolated_test_database(DEVELOPMENT)
    message = str(excinfo.value)
    assert "secret" not in message
    assert "better_agent:***@" in message


def test_redaction_keeps_the_host_and_database():
    assert redact(DEVELOPMENT) == (
        "postgresql://better_agent:***@127.0.0.1:5432/better_agent"
    )


# --------------------------------------------------------------------- ordering

def test_alembic_never_runs_against_a_rejected_target(monkeypatch, request):
    """The whole point of T01: the refusal must precede `alembic upgrade`."""
    monkeypatch.setenv("TEST_DATABASE_URL", DEVELOPMENT)
    calls: list[tuple] = []
    real_run = subprocess.run

    def spy(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)

    with pytest.raises(pytest.fail.Exception) as excinfo:
        request.getfixturevalue("migrated_postgres_url")

    assert "refusing to migrate or truncate" in str(excinfo.value)
    assert calls == [], "alembic must not be invoked for a rejected target"
