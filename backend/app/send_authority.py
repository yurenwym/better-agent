"""Task-local send guard, installed only by the worker holding execution rights."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator


_guard: ContextVar[Callable[[], None] | None] = ContextVar("send_authority", default=None)


@contextmanager
def send_authority(check: Callable[[], None]) -> Iterator[None]:
    token = _guard.set(check)
    try:
        yield
    finally:
        _guard.reset(token)


def execution_authority() -> Exception | None:
    """Evaluate the installed guard and return the refusal it raised, if any.

    This is a *fact* for the decision boundary, not a decision here.  A model or
    tool adapter folds ``execution_valid = execution_authority() is None`` into
    its :class:`~app.policy_engine.PolicyRequest`, so the single
    ``decide_request`` call owns the decision and the original domain error is
    raised unchanged when execution rights were lost.
    """
    check = _guard.get()
    if check is None:
        return None
    try:
        check()
    except Exception as exc:
        return exc
    return None


def assert_send_authority() -> None:
    """Assert execution rights directly, for boundaries that issue no request."""
    failure = execution_authority()
    if failure is not None:
        raise failure
