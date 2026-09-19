from __future__ import annotations

import asyncio

import pytest


def test_embedding_budget_blocks_before_provider_call(monkeypatch):
    from scripts.m1_live_acceptance import (
        AcceptanceFailure, BatchBudget, MAX_EMBEDDING_INPUT, MAX_EMBEDDING_REQUESTS,
    )

    calls = []

    class Client:
        def embed(self, texts):
            calls.append(texts)
            raise AssertionError("provider must not be called")

    budget = BatchBudget()
    budget.embedding_requests = [{}] * MAX_EMBEDDING_REQUESTS
    with pytest.raises(AcceptanceFailure, match="request hard limit"):
        budget.embedding_request(Client(), ["x"])
    budget.embedding_requests = []
    budget.embedding_input_upper_bound = MAX_EMBEDDING_INPUT
    with pytest.raises(AcceptanceFailure, match="input hard limit"):
        budget.embedding_request(Client(), ["x"])
    assert calls == []


def test_model_budget_blocks_before_network_call():
    from scripts.m1_live_acceptance import (
        AcceptanceFailure, BatchBudget, MAX_MODEL_ATTEMPTS,
    )

    budget = BatchBudget()
    budget.model_attempts = [{}] * MAX_MODEL_ATTEMPTS
    request = type("Request", (), {"role": "conversation", "purpose": "test"})()
    profile = type("Profile", (), {"timeout_seconds": 1})()
    with pytest.raises(AcceptanceFailure, match="attempt hard limit"):
        asyncio.run(budget.model_attempt(profile, request))
