from __future__ import annotations

import pytest
import os


@pytest.fixture(autouse=True)
def allow_legacy_sqlite_unit_fixtures(monkeypatch):
    """SQLite remains available only to legacy unit fixtures and migration tests."""
    monkeypatch.setenv("BETTER_AGENT_TEST_ALLOW_SQLITE", "1")
    # Historical fixtures exercise the rollback contract. Native-loop tests
    # select loop explicitly; the default-mode test removes this override.
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", os.getenv("BETTER_AGENT_LOOP_MODE", "legacy"))
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
    # A configured fallback alone becomes the primary at startup. Strip all
    # fallback settings as well so offline fixtures cannot launch a paid Judge.
    for name in tuple(os.environ):
        if name.startswith("AGENT_FALLBACK_MODEL_"):
            monkeypatch.delenv(name, raising=False)
