"""Task-local send guard, installed only by the worker holding execution rights."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

from .policy_engine import PolicyAction, PolicyInput, decide


_guard: ContextVar[Callable[[], None] | None] = ContextVar("send_authority", default=None)


@contextmanager
def send_authority(check: Callable[[], None]) -> Iterator[None]:
    token = _guard.set(check)
    try:
        yield
    finally:
        _guard.reset(token)


def assert_send_authority() -> None:
    check = _guard.get()
    if check is not None:
        failure = None
        try:
            check()
        except Exception as exc:
            failure = exc
        if decide(PolicyInput(True, True, True, execution_valid=failure is None)).action == PolicyAction.DENY:
            raise failure
