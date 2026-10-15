"""Run-scoped shared state: every run of a team or network gets its own bus, blackboard and board.

An object passed explicitly is shared by every run. The `Scoped*` facades act on the current
run's object, outside any run on the object of the run started last.
"""

from __future__ import annotations

import builtins
import inspect
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from awa._internal.logging import get_logger
from awa.contracts.events import MessageUndeliveredEvent
from awa.messaging.blackboard import (
    BaseBlackboard,
    Blackboard,
    BlackboardEntry,
    BlackboardWatcher,
    check_own_session_key,
    is_session_key,
    session_scope_of,
)
from awa.messaging.bus import DeadLetter, InMemoryMessageBus, MessageBus, MessageListener
from awa.runtime.context import RunContext, current_run_context
from awa.tasks.board import BaseTaskBoard, TaskBoard

_log = get_logger("team.scope")

MAX_PAUSED_RUNS = 100
"""Paused runs whose in-memory state is kept for `resume` (oldest dropped first)."""


@dataclass(eq=False)
class RunResources:
    """The bus, blackboard and task board one run works with (`None` when not enabled)."""

    bus: MessageBus | None
    blackboard: BaseBlackboard | None
    task_board: BaseTaskBoard | None
    fresh_bus: bool = False


class RunScope:
    """Creates each run's resources and finds those of the run in progress.

    Args:
        key: `RunContext.internal` key of the per-run state whose `resources` attribute is used.
        bus: The shared bus, or a factory for a fresh one per run, or `None`.
        blackboard: The shared blackboard, `True` for a fresh one per run, or `None`.
        task_board: The shared task board, `True` for a fresh one per run, or `None`.
    """

    def __init__(
        self,
        key: str,
        *,
        bus: MessageBus | Callable[[], MessageBus] | None,
        blackboard: BaseBlackboard | bool | None,
        task_board: BaseTaskBoard | bool | None,
        listener: MessageListener | None = None,
    ) -> None:
        self._key = key
        self._bus = bus
        self._blackboard = blackboard
        self._task_board = task_board
        self._listener = listener
        self._paused: OrderedDict[str, RunResources] = OrderedDict()
        self.latest = self._new()

    @property
    def shared(self) -> bool:
        """Whether any resource is an explicitly passed (shared) object."""

        return any(
            isinstance(value, MessageBus | BaseBlackboard | BaseTaskBoard)
            for value in (self._bus, self._blackboard, self._task_board)
        )

    def _owns_state(self) -> bool:
        """Whether any resource is created per run (and so lost with the process)."""

        return callable(self._bus) or self._blackboard is True or self._task_board is True

    def _new(self) -> RunResources:
        bus = self._bus() if callable(self._bus) else self._bus
        fresh = callable(self._bus)
        if fresh and bus is not None and self._listener is not None:
            bus.add_listener(self._listener)
        return RunResources(
            bus=bus,
            blackboard=_pick(self._blackboard, Blackboard),
            task_board=_pick(self._task_board, TaskBoard),
            fresh_bus=fresh,
        )

    def start(self, thread: str | None = None) -> RunResources:
        """The resources of a run starting now: those of its paused run on `thread`, or new ones.

        A resume whose paused state is no longer remembered goes on with fresh resources and logs a
        warning.
        """

        resources = self._paused.pop(thread, None) if thread is not None else None
        if thread is not None and resources is None and not self.shared and self._owns_state():
            _log.warning(
                "%s: resuming thread %r found no saved state of its paused run (the process "
                "restarted, or the run was dropped after %d newer ones paused); the run goes on "
                "with an empty bus, blackboard and task board",
                self._key,
                thread,
                MAX_PAUSED_RUNS,
            )
        self.latest = resources or self._new()
        return self.latest

    def current(self) -> RunResources:
        """The resources of the run in progress, else of the run started last."""

        ctx = current_run_context()
        state = ctx.internal.get(self._key) if ctx is not None else None
        resources = getattr(state, "resources", None)
        return resources if isinstance(resources, RunResources) else self.latest

    async def finish(
        self, ctx: RunContext[Any], resources: RunResources, *, paused_on: str | None
    ) -> None:
        """End a run: keep its state when it paused on a thread, else report unread mail as undelivered."""

        if paused_on is not None:
            self._paused[paused_on] = resources
            while len(self._paused) > MAX_PAUSED_RUNS:
                self._paused.popitem(last=False)
            return
        bus = resources.bus
        if not resources.fresh_bus or not isinstance(bus, InMemoryMessageBus):
            return
        try:
            bus.drain()
            for letter in bus.dead_letters:
                await ctx.emit(_undelivered(ctx, letter))
        except Exception:  # noqa: BLE001 - reporting must not replace the run's own outcome
            _log.warning("%s: reporting undelivered messages failed", self._key, exc_info=True)


def _pick(value: Any, factory: Callable[[], Any]) -> Any:
    """`True` makes a fresh object, `False`/`None` means none, an object is shared."""

    if value is True:
        return factory()
    return None if value is False or value is None else value


def facade(value: Any, scoped: Any) -> Any:
    """What a team or network acts on: the object passed (shared by every run), the per-run
    `scoped` facade for `True`, or `None`."""

    return _pick(value, lambda: scoped)


def _undelivered(ctx: RunContext[Any], letter: DeadLetter) -> MessageUndeliveredEvent:
    return MessageUndeliveredEvent(
        message=letter.message,
        recipient=letter.recipient,
        reason=letter.reason,
        **ctx.event_fields(),
    )


_T = TypeVar("_T")


def _scoped(name: str, base: Callable[..., _T], resource: str) -> Callable[[RunScope], _T]:
    """A subclass of `base` whose abstract members act on the current run's `resource`."""
    cls: type[Any] = cast("type[Any]", base)

    def target(self: Any) -> Any:
        value = getattr(self._scope.current(), resource)
        assert value is not None
        return value

    def forward(member: str) -> Any:
        original = getattr(cls, member)
        if isinstance(original, property):
            return property(lambda self: getattr(target(self), member), doc=original.__doc__)
        if inspect.iscoroutinefunction(original):

            async def call_async(self: Any, *args: Any, **kwargs: Any) -> Any:
                return await getattr(target(self), member)(*args, **kwargs)

            call_async.__name__ = member
            return call_async

        def call(self: Any, *args: Any, **kwargs: Any) -> Any:
            return getattr(target(self), member)(*args, **kwargs)

        call.__name__ = member
        return call

    def __init__(self: Any, scope: RunScope) -> None:
        self._scope = scope

    namespace: dict[str, Any] = {member: forward(member) for member in cls.__abstractmethods__}
    namespace |= {"__init__": __init__, "__module__": __name__}
    namespace["__doc__"] = f"A `{cls.__name__}` acting on the {resource} of the current run."
    return cast("Callable[[RunScope], _T]", type(name, (cls,), namespace))


ScopedBus = _scoped("ScopedBus", MessageBus, "bus")
ScopedTaskBoard = _scoped("ScopedTaskBoard", BaseTaskBoard, "task_board")


MAX_SESSIONS = 1000
"""Sessions whose shared entries a team or network keeps in memory (least recently used go)."""


class ScopedBlackboard(BaseBlackboard):
    """A `BaseBlackboard` acting on the blackboard of the current run.

    Session keys (`awa.messaging.blackboard.session_key`) go to an in-process board per session
    instead, kept for the lifetime of the team for the `max_sessions` most recently used sessions.
    """

    def __init__(self, scope: RunScope, *, max_sessions: int = MAX_SESSIONS) -> None:
        self._scope = scope
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[str, Blackboard] = OrderedDict()  # least recently used first

    def _board(self, key: str = "", *, create: bool = False) -> BaseBlackboard:
        if is_session_key(key):
            return self._session_board(key, create)
        board = self._scope.current().blackboard
        assert board is not None
        return board

    def _session_board(self, key: str, create: bool) -> Blackboard:
        """The board of the session `key` belongs to; evicting a session drops its whole history."""

        check_own_session_key(key)
        scope = session_scope_of(key) or ""
        board = self._sessions.get(scope)
        if board is None:
            if not create:
                return Blackboard()
            board = self._sessions[scope] = Blackboard()
        self._sessions.move_to_end(scope)
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)
        return board

    async def get(self, key: str) -> BlackboardEntry | None:
        return await self._board(key).get(key)

    async def write(
        self, key: str, value: Any, *, author: str, expected_version: int | None = None
    ) -> BlackboardEntry:
        board = self._board(key, create=True)
        return await board.write(key, value, author=author, expected_version=expected_version)

    async def delete(self, key: str, *, author: str) -> None:
        await self._board(key).delete(key, author=author)

    async def keys(self, prefix: str = "") -> builtins.list[str]:
        return await self._board(prefix).keys(prefix)

    async def snapshot(self) -> dict[str, Any]:
        return await self._board().snapshot()

    def history(self, key: str) -> builtins.list[BlackboardEntry]:
        return self._board(key).history(key)

    def watch(self, callback: BlackboardWatcher) -> Callable[[], None]:
        return self._board().watch(callback)
