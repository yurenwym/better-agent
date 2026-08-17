import pytest


def test_state_machine_enforces_approved_transitions_and_blocked_resume() -> None:
    from app.domain import AgentState, InvalidTransition, StateMachine

    machine = StateMachine()
    assert machine.transition(AgentState.RECEIVED, AgentState.PLANNING).state == AgentState.PLANNING

    blocked = machine.transition(AgentState.EXECUTING, AgentState.BLOCKED)
    assert blocked.state == AgentState.BLOCKED
    assert blocked.resume_state == AgentState.EXECUTING
    assert machine.transition(
        AgentState.BLOCKED,
        AgentState.EXECUTING,
        resume_state=blocked.resume_state,
    ).state == AgentState.EXECUTING

    with pytest.raises(InvalidTransition):
        machine.transition(AgentState.RECEIVED, AgentState.EXECUTING)
    with pytest.raises(InvalidTransition):
        machine.transition(AgentState.BLOCKED, AgentState.PLANNING, resume_state=blocked.resume_state)
    with pytest.raises(InvalidTransition):
        machine.transition(AgentState.COMPLETED, AgentState.EXECUTING)


def test_failed_and_cancelled_are_global_terminal_transitions() -> None:
    from app.domain import AgentState, StateMachine

    machine = StateMachine()

    assert machine.transition(AgentState.PLANNING, AgentState.FAILED).state == AgentState.FAILED
    assert machine.transition(AgentState.AWAITING_OUTCOME, AgentState.CANCELLED).state == AgentState.CANCELLED

