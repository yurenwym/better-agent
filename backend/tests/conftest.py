from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def allow_legacy_sqlite_unit_fixtures(monkeypatch):
    """SQLite remains available only to legacy unit fixtures and migration tests."""
    monkeypatch.setenv("BETTER_AGENT_TEST_ALLOW_SQLITE", "1")
    # Developer-machine provider credentials must never turn an offline test
    # into a paid network call. Tests that exercise model configuration set
    # their own values after this fixture has removed the ambient ones.
    for name in (
        "LLM_AP_PATH",
        "AGENT_MODEL_API_KEY",
        "AGENT_MODEL_API_KEY_ENV",
        "AGENT_MODEL_BASE_URL",
        "AGENT_MODEL_ID",
        "AGENT_MODEL_PROVIDER",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
