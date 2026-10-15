"""At-most-once execution of side effects: claimed de-duplication keys and stable write keys."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from typing import TypeVar

from awa._internal.aio import LoopLocal
from awa.errors import AwaError, ConfigurationError
from awa.runtime._run import THREAD_METADATA_KEY
from awa.runtime.context import current_run_context
from awa.runtime.ids import DEFAULT_TENANT

ResultT = TypeVar("ResultT")


class IdempotencyKeyAlreadyClaimedError(AwaError):
    """Raised by `run_idempotent` for an already claimed key without an `on_duplicate` handler."""


class IdempotencyStore(ABC):
    """Backend that claims de-duplication keys atomically (check and set in one operation)."""

    @abstractmethod
    async def try_claim(self, key: str) -> bool:
        """Claim `key`: `True` if this call claimed it, `False` if it was already claimed."""

    @abstractmethod
    async def release(self, key: str) -> None:
        """Release `key` so a later call may claim it again."""


class InMemoryIdempotencyStore(IdempotencyStore):
    """Process-local store, safe for concurrent coroutines in one event loop.

    It keeps at most `max_claims` keys; beyond that the oldest claim is forgotten.
    """

    def __init__(self, max_claims: int = 10_000) -> None:
        if max_claims < 1:
            raise ConfigurationError("max_claims must be at least 1")
        self._max_claims = max_claims
        self._claimed: OrderedDict[str, None] = OrderedDict()
        self._lock: LoopLocal[asyncio.Lock] = LoopLocal(asyncio.Lock)  # one per event loop

    async def try_claim(self, key: str) -> bool:
        """Claim `key`; return `False` if it is already claimed."""

        async with self._lock.get():
            if key in self._claimed:
                return False
            self._claimed[key] = None
            while len(self._claimed) > self._max_claims:
                self._claimed.popitem(last=False)
            return True

    async def release(self, key: str) -> None:
        """Release `key`. Releasing an unclaimed key is a no-op."""

        async with self._lock.get():
            self._claimed.pop(key, None)


async def run_idempotent(
    side_effect: Callable[[], Awaitable[ResultT]],
    *,
    key: str,
    store: IdempotencyStore,
    on_duplicate: Callable[[str], ResultT] | None = None,
) -> ResultT:
    """Run `side_effect` at most once per `key`.

    The key stays claimed after success. The effect is never retried. If it raises, the key is
    released and the error re-raised; if the call is cancelled, the key stays claimed, because the
    effect may already have happened.

    Args:
        on_duplicate: Called with `key` when it is already claimed; its result is returned instead
            of raising.

    Raises:
        IdempotencyKeyAlreadyClaimedError: If the key is already claimed and `on_duplicate` is not
            given.

    Example:
        ```python
        receipt = await run_idempotent(
            lambda: charge_card(order_id, amount),
            key=f"charge:{order_id}",
            store=store,
        )
        ```
    """

    if not await store.try_claim(key):
        if on_duplicate is not None:
            return on_duplicate(key)
        raise IdempotencyKeyAlreadyClaimedError(f"Idempotency key already claimed: {key!r}")

    try:
        return await side_effect()
    except Exception:
        await store.release(key)
        raise


_current_tool_call: ContextVar[tuple[str, str] | None] = ContextVar(
    "awa_current_tool_call", default=None
)


@contextlib.contextmanager
def use_tool_call(call_id: str, namespace: str = "") -> Iterator[None]:
    """Mark the code in the block as running the tool call `call_id` (in graph `namespace`)."""

    token = _current_tool_call.set((call_id, namespace) if call_id else None)
    try:
        yield
    finally:
        _current_tool_call.reset(token)


def idempotency_key(
    *, scope: str, tool_call_id: str, operation: str, tenant: str | None = None, namespace: str = ""
) -> str:
    """The stable de-duplication key of one external write: 32 hex characters of a SHA-256.

    Args:
        scope: The thread id of a durable run, else the root run id.
        namespace: The LangGraph checkpoint namespace of the call (`""` at the top level).

    Raises:
        ValueError: If `scope`, `tool_call_id` or `operation` is empty.
    """

    if not scope or not tool_call_id or not operation:
        raise ValueError("idempotency_key needs a scope, a tool call id and an operation")
    parts = (tenant or "", scope, namespace, tool_call_id, operation)
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


def run_idempotency_key(operation: str) -> str | None:
    """The `idempotency_key` of `operation` in the current tool call, or `None` outside one."""

    ctx = current_run_context()
    call = _current_tool_call.get()
    if ctx is None or call is None:
        return None
    thread = ctx.metadata.get(THREAD_METADATA_KEY)
    # The frozen run ids, not the metadata a tool could change, name the tenant.
    tenant = ctx.ids.tenant if ctx.ids.tenant != DEFAULT_TENANT else None
    return idempotency_key(
        scope=str(thread) if thread else ctx.root_run_id,
        tool_call_id=call[0],
        operation=operation,
        tenant=tenant,
        namespace=call[1],
    )


__all__ = [
    "IdempotencyKeyAlreadyClaimedError",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "idempotency_key",
    "run_idempotency_key",
    "run_idempotent",
    "use_tool_call",
]
