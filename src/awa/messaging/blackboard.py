"""A shared, versioned key-value workspace for a team of agents."""

from __future__ import annotations

import asyncio
import copy
import json
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from awa._internal.aio import LoopLocal
from awa._internal.logging import get_logger
from awa._internal.util import add_callback, call_watchers
from awa.errors import BlackboardConflictError
from awa.runtime.context import current_run_context

_logger = get_logger("messaging")

SESSION_SCOPE = "session:"
"""Prefix of the keys that belong to the session, not to the run (see `session_key`)."""

_SESSION_STORE = "\x1fsession\x1f"
_SEP = "\x1f"


class BlackboardEntry(BaseModel):
    """One version of a blackboard key.

    Attributes:
        value: The JSON-serializable value; `None` for a deletion.
        version: Starts at 1 and increases with every write or deletion of the key.
        author_run_id: Id of the run that made the change, when it was made inside one.
        agent_call_id: The `RunIds.agent_call_id` of that run, if any.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    value: Any = None
    version: int
    author: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deleted: bool = False
    author_run_id: str | None = None
    agent_call_id: str | None = None


BlackboardWatcher = Callable[[BlackboardEntry], Awaitable[None] | None]
"""A callback receiving every new `BlackboardEntry`. May be sync or async."""


class BaseBlackboard(ABC):
    """Interface of a shared, versioned key-value workspace.

    Every write or deletion is a new version of its key; `expected_version` makes a write
    compare-and-set; watchers see every new entry. Values are JSON and copied in and out.
    """

    @abstractmethod
    async def get(self, key: str) -> BlackboardEntry | None:
        """Return the current entry for `key`, or `None` if it does not exist."""

    async def read(self, key: str, default: Any = None) -> Any:
        """Return a copy of the current value of `key`, or `default`."""

        entry = await self.get(key)
        return default if entry is None else entry.value

    @abstractmethod
    async def write(
        self, key: str, value: Any, *, author: str, expected_version: int | None = None
    ) -> BlackboardEntry:
        """Store `value` under `key` as a new version; see `Blackboard.write`.

        Raises:
            BlackboardConflictError: If `expected_version` is not the current version.
        """

    @abstractmethod
    async def delete(self, key: str, *, author: str) -> None:
        """Delete `key`, recording the deletion as a new version; see `Blackboard.delete`."""

    @abstractmethod
    async def keys(self, prefix: str = "") -> list[str]:
        """Return the existing keys starting with `prefix`, sorted."""

    @abstractmethod
    async def snapshot(self) -> dict[str, Any]:
        """Return a copy of every existing key and its current value."""

    @abstractmethod
    def history(self, key: str) -> list[BlackboardEntry]:
        """Return the retained versions of `key`, oldest first, including deletions."""

    @abstractmethod
    def watch(self, callback: BlackboardWatcher) -> Callable[[], None]:
        """Call `callback` with every new entry; returns a function that removes it."""


class Blackboard(BaseBlackboard):
    """In-memory blackboard keeping `max_versions` versions per key for `history`.

    Raises:
        ValueError: If `max_versions` is less than 1.

    Example:
        ```python
        board = Blackboard()
        entry = await board.write("plan", {"steps": ["search", "draft"]}, author="lead")
        await board.write("plan", {"steps": ["search"]}, author="writer",
                          expected_version=entry.version)
        ```
    """

    def __init__(self, *, max_versions: int = 100) -> None:
        if max_versions < 1:
            raise ValueError("max_versions must be >= 1")
        self._lock: LoopLocal[asyncio.Lock] = LoopLocal(asyncio.Lock)
        self._latest: dict[str, BlackboardEntry] = {}
        self._versions: dict[str, deque[BlackboardEntry]] = {}
        self._max_versions = max_versions
        self._watchers: list[BlackboardWatcher] = []

    async def get(self, key: str) -> BlackboardEntry | None:
        """Return the current entry for `key`, or `None` if it does not exist."""

        entry = self._latest.get(key)
        if entry is None or entry.deleted:
            return None
        return entry.model_copy(deep=True)

    async def write(
        self, key: str, value: Any, *, author: str, expected_version: int | None = None
    ) -> BlackboardEntry:
        """Store `value` under `key` as a new version.

        Args:
            expected_version: Current version the write is based on; `0` means the key must not
                exist, `None` writes unconditionally.

        Raises:
            BlackboardConflictError: If `expected_version` is not the current version.
            ValueError: If `key` or `author` is empty or `value` contains NaN or infinity.
            TypeError: If `value` is not JSON-serializable.
        """

        _check_key_and_author(key, author)
        stored = json.loads(json.dumps(value, allow_nan=False))
        async with self._lock.get():
            previous = self._latest.get(key)
            current = 0 if previous is None or previous.deleted else previous.version
            if expected_version is not None and expected_version != current:
                state = "does not exist" if current == 0 else f"is at version {current}"
                raise BlackboardConflictError(
                    f"Key {key!r} {state}, but the write expected version {expected_version}"
                )
            entry = BlackboardEntry(
                key=key,
                value=stored,
                version=(previous.version if previous else 0) + 1,
                author=author,
                **provenance(),
            )
            self._commit(entry)
        await self._notify(entry)
        return entry.model_copy(deep=True)

    async def delete(self, key: str, *, author: str) -> None:
        """Delete `key` as a new version; a missing key is a no-op.

        Raises:
            ValueError: If `key` or `author` is empty.
        """

        _check_key_and_author(key, author)
        async with self._lock.get():
            previous = self._latest.get(key)
            if previous is None or previous.deleted:
                return
            entry = BlackboardEntry(
                key=key, version=previous.version + 1, author=author, deleted=True, **provenance()
            )
            self._commit(entry)
        await self._notify(entry)

    async def keys(self, prefix: str = "") -> list[str]:
        """Return the existing keys starting with `prefix`, sorted."""

        return sorted(
            key
            for key, entry in self._latest.items()
            if not entry.deleted and key.startswith(prefix)
        )

    async def snapshot(self) -> dict[str, Any]:
        """Return a copy of every existing key and its current value."""

        return {
            key: copy.deepcopy(entry.value)
            for key, entry in sorted(self._latest.items())
            if not entry.deleted
        }

    def history(self, key: str) -> list[BlackboardEntry]:
        """Return the retained versions of `key`, oldest first, including deletions."""

        return [entry.model_copy(deep=True) for entry in self._versions.get(key, ())]

    def watch(self, callback: BlackboardWatcher) -> Callable[[], None]:
        """Call `callback` with every committed entry; returns a function that removes the watcher.

        Watchers may use the blackboard themselves; their errors are logged and swallowed.
        """

        return add_callback(self._watchers, callback)

    def _commit(self, entry: BlackboardEntry) -> None:
        self._latest[entry.key] = entry
        self._versions.setdefault(entry.key, deque(maxlen=self._max_versions)).append(entry)

    async def _notify(self, entry: BlackboardEntry) -> None:
        await call_watchers(
            self._watchers,
            entry.model_copy(deep=True),
            _logger,
            "Blackboard watcher failed on key %r",
            entry.key,
        )


def provenance() -> dict[str, str | None]:
    """`author_run_id` and `agent_call_id` of the run making a change (empty outside a run)."""

    ctx = current_run_context()
    if ctx is None:
        return {}
    return {"author_run_id": ctx.run_id, "agent_call_id": ctx.ids.agent_call_id}


def session_key(key: str) -> str:
    """The stored key of `key`: a `session:` key is scoped to the run's tenant and session.

    Raises:
        ValueError: If a `session:` key is used outside a run, or `key` contains the reserved
            separator.
    """

    if _SEP in key:
        raise ValueError("Blackboard keys must not contain the reserved separator character")
    if not key.startswith(SESSION_SCOPE):
        return key
    ctx = current_run_context()
    if ctx is None or not ctx.ids.session_id:
        raise ValueError(f"The session key {key!r} can only be used inside a run")
    rest = key.removeprefix(SESSION_SCOPE)
    return f"{_SESSION_STORE}{ctx.ids.tenant}{_SEP}{ctx.ids.session_id}{_SEP}{rest}"


def visible_key(stored: str) -> str | None:
    """The key a stored key is shown as: a session key of the current run's tenant and
    session as `session:<key>`, the session key of another tenant or session as `None`."""

    if not stored.startswith(_SESSION_STORE):
        return stored
    ctx = current_run_context()
    if ctx is None:
        return None
    own = f"{_SESSION_STORE}{ctx.ids.tenant}{_SEP}{ctx.ids.session_id}{_SEP}"
    return SESSION_SCOPE + stored.removeprefix(own) if stored.startswith(own) else None


def is_session_key(stored: str) -> bool:
    """Whether `stored` (a key as stored) belongs to a session scope."""

    return stored.startswith(_SESSION_STORE)


def session_scope_of(stored: str) -> str | None:
    """The stored prefix of the tenant and session a stored session key belongs to, or
    `None` for a key outside a session scope."""

    if not is_session_key(stored):
        return None
    tenant_and_session = stored.removeprefix(_SESSION_STORE).split(_SEP, 2)[:2]
    return _SESSION_STORE + _SEP.join(tenant_and_session) + _SEP


def check_own_session_key(stored: str) -> None:
    """Refuse a stored session key of another tenant or session than the current run's.

    Raises:
        ValueError: If `stored` belongs to another tenant or session, or is used outside a run.
    """

    if is_session_key(stored) and visible_key(stored) is None:
        raise ValueError("This session key belongs to another tenant or session")


def _check_key_and_author(key: str, author: str) -> None:
    if not key:
        raise ValueError("Blackboard key must not be empty")
    if not author:
        raise ValueError("Blackboard author must not be empty")


__all__ = [
    "SESSION_SCOPE",
    "BaseBlackboard",
    "Blackboard",
    "BlackboardEntry",
    "BlackboardWatcher",
    "check_own_session_key",
    "is_session_key",
    "provenance",
    "session_key",
    "session_scope_of",
    "visible_key",
]
