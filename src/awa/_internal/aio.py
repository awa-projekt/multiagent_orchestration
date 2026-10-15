"""Run a coroutine from synchronous code, portably, and keep one value per event loop."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from typing import Any, Generic, TypeVar, cast

T = TypeVar("T")


def refuse_running_loop(call: str, alternative: str) -> None:
    """Raise `RuntimeError` naming `call` and its `alternative` if an event loop runs in this thread."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(f"{call} cannot be called from a running event loop; use `{alternative}`")


def run(coro: Coroutine[Any, Any, T], *, selector_loop: bool = False) -> T:
    """Run `coro` to completion in a new event loop and return its result.

    The process-wide event loop policy is never changed.

    Args:
        selector_loop: On Windows, run on a `SelectorEventLoop`, which psycopg's async driver (the
            Postgres backends) needs; it cannot start subprocesses. Ignored elsewhere.

    Example:
        ```python
        run(main(), selector_loop=True)  # Postgres backends on Windows
        ```
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coro.close()
        raise RuntimeError(
            "awa.ext.run_sync() cannot be called from a running event loop; "
            "await the coroutine instead."
        )

    loop_factory: Callable[[], asyncio.AbstractEventLoop] | None = None
    if selector_loop and sys.platform == "win32":
        loop_factory = asyncio.SelectorEventLoop
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(coro)


class LoopLocal(Generic[T]):
    """One value per event loop, built on first use in that loop; thread-safe.

    For long-lived objects whose async resources (pools, locks) must not outlive their loop. Values
    of loops closed without shutting down their async generators are dropped unclosed.

    Args:
        factory: Builds the value for a loop; called in that loop, without arguments.
        aclose: Closes a value; awaited in its loop when the loop shuts down (errors ignored).

    Example:
        ```python
        locks: LoopLocal[asyncio.Lock] = LoopLocal(asyncio.Lock)

        async def update() -> None:
            async with locks.get():
                ...
        ```
    """

    def __init__(
        self, factory: Callable[[], T], *, aclose: Callable[[T], Awaitable[object]] | None = None
    ) -> None:
        self._factory = factory
        self._aclose = aclose
        self._values: dict[
            asyncio.AbstractEventLoop, tuple[T, AsyncGenerator[None, None] | None]
        ] = {}
        self._lock = threading.RLock()

    def get(self) -> T:
        """Return the value of the running event loop, building it on first use."""

        loop = asyncio.get_running_loop()
        with self._lock:
            entry = self._values.get(loop)
            if entry is not None:
                return entry[0]
            for stale in [other for other in self._values if other.is_closed()]:
                self._drop(stale)
            value = self._factory()
            closer = None
            if self._aclose is not None:
                # An async generator started in `loop` is closed by the loop's
                # `shutdown_asyncgens()`, which `asyncio.run` awaits before closing it.
                closer = self._closer(loop, value)
                _step(closer.__anext__())
            self._values[loop] = (value, closer)
            return value

    def peek(self) -> T | None:
        """Return the running loop's value if it was built (`None` outside a loop)."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        with self._lock:
            entry = self._values.get(loop)
        return entry[0] if entry is not None else None

    def __len__(self) -> int:
        """The number of values held, including those of closed loops not yet dropped."""

        with self._lock:
            return len(self._values)

    def _drop(self, loop: asyncio.AbstractEventLoop) -> None:
        entry = self._values.pop(loop, None)
        if entry is not None and entry[1] is not None:
            # Finish the closer now: its loop is closed, so it skips `aclose` and
            # returns without awaiting, instead of being finalized by a dead loop later.
            _step(entry[1].aclose())

    async def _closer(
        self, loop: asyncio.AbstractEventLoop, value: T
    ) -> AsyncGenerator[None, None]:
        try:
            yield
        finally:
            with self._lock:
                entry = self._values.get(loop)
                if entry is not None and entry[0] is value:
                    del self._values[loop]
            if self._aclose is not None and not loop.is_closed():
                with contextlib.suppress(Exception):
                    await self._aclose(value)


def _step(awaitable: Awaitable[Any]) -> None:
    """Run an async generator step that does not await, synchronously."""

    step = cast(Coroutine[Any, Any, Any], awaitable)
    try:
        step.send(None)
    except (StopIteration, StopAsyncIteration):
        return
    step.close()  # it awaited: not a step this helper can finish
