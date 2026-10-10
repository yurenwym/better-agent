import pytest


def test_default_loop_and_explicit_rollback(monkeypatch):
    from app.config import agent_loop_mode
    monkeypatch.delenv('BETTER_AGENT_LOOP_MODE', raising=False)
    assert agent_loop_mode() == 'loop'
    monkeypatch.setenv('BETTER_AGENT_LOOP_MODE', 'legacy')
    assert agent_loop_mode() == 'legacy'
    monkeypatch.setenv('BETTER_AGENT_LOOP_MODE', 'invalid')
    with pytest.raises(ValueError):
        agent_loop_mode()
