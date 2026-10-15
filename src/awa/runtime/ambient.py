"""Process-wide event subscribers: observers that see every run without being passed to one.

Global subscribers get every event after the run's own subscribers, in subscription order; one
that raises is logged and skipped. Registering and removing is thread-safe.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from awa.runtime.context import EventSubscriber


class _Registration:
    """One `subscribe_globally` call (the same subscriber may be registered twice)."""

    __slots__ = ("subscriber",)

    def __init__(self, subscriber: EventSubscriber) -> None:
        self.subscriber = subscriber


_lock = threading.Lock()
_registrations: tuple[_Registration, ...] = ()


def subscribe_globally(subscriber: EventSubscriber) -> Callable[[], None]:
    """Deliver every event of every run in the process to `subscriber`.

    Returns:
        A function that removes this registration; calling it again does nothing.

    Example:
        ```python
        unsubscribe = subscribe_globally(AuditLog(JsonlAuditSink("audit.jsonl")))
        try:
            await serve_forever()
        finally:
            unsubscribe()
        ```
    """

    global _registrations
    if not callable(subscriber):
        raise TypeError(f"A subscriber must be callable, got {type(subscriber).__name__}")
    registration = _Registration(subscriber)
    with _lock:
        _registrations = (*_registrations, registration)

    def unsubscribe() -> None:
        global _registrations
        with _lock:
            _registrations = tuple(r for r in _registrations if r is not registration)

    return unsubscribe


@contextmanager
def use_global_subscriber(subscriber: EventSubscriber) -> Iterator[EventSubscriber]:
    """Subscribe `subscriber` globally for the duration of the block."""

    unsubscribe = subscribe_globally(subscriber)
    try:
        yield subscriber
    finally:
        unsubscribe()


def global_subscribers() -> tuple[EventSubscriber, ...]:
    """The globally registered subscribers, in subscription order."""

    return tuple(registration.subscriber for registration in _registrations)


__all__ = ["global_subscribers", "subscribe_globally", "use_global_subscriber"]
