"""Thread ownership and idempotency records kept in this process, and the idempotency claim the
HTTP API, background jobs and the MCP server share."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from awa._internal.logging import get_logger
from awa.errors import (
    ConfigurationError,
    IdempotencyConflictError,
    IdempotencyMismatchError,
    SystemBusyError,
)
from awa.persistence.threads import (
    BaseIdempotencyCache,
    BaseThreadIndex,
    DecisionClaims,
    IdempotencyRecord,
    ThreadOwner,
    ThreadRecord,
)

_log = get_logger("server.idempotency")

_MAX_CLAIMS = 100_000

IDEMPOTENCY_KEY_PATTERN = r"^[\x21-\x7e]{1,255}$"
"""Idempotency keys (`Idempotency-Key`, `JobSpec.idempotency_key`): 1 to 255 visible ASCII characters."""


class LocalDecisionClaims:
    """`DecisionClaims` kept in this process (safe across its threads and event loops).

    At most 100,000 claims are kept; the oldest are dropped beyond it.
    """

    def __init__(self) -> None:
        self._claims: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._claims_lock = threading.Lock()

    async def claim_decision(self, system: str, thread_id: str, version: str) -> bool:
        """Claim one paused state of a thread; `False` when it was claimed before."""

        key = (system, thread_id, version)
        with self._claims_lock:
            if key in self._claims:
                return False
            self._claims[key] = None
            while len(self._claims) > _MAX_CLAIMS:
                self._claims.popitem(last=False)
            return True

    async def release_decision(self, system: str, thread_id: str, version: str) -> None:
        """Drop a claim."""

        with self._claims_lock:
            self._claims.pop((system, thread_id, version), None)

    def _forget_decisions(self, system: str, thread_id: str) -> None:
        """Drop every claim of one thread (its record is being removed)."""

        with self._claims_lock:
            for key in [k for k in self._claims if k[:2] == (system, thread_id)]:
                del self._claims[key]


def decision_claims(index: Any) -> DecisionClaims:
    """The decision claims of a thread index: its own, else a new in-process store."""

    return index if isinstance(index, DecisionClaims) else LocalDecisionClaims()


async def claim_paused_state(
    claims: DecisionClaims, system: str, thread_id: str, version: str
) -> None:
    """Claim the paused state of a thread that a decision answers.

    Raises:
        SystemBusyError: If another resume answers (or answered) the same paused state.
    """

    if not await claims.claim_decision(system, thread_id, version):
        raise SystemBusyError(
            f"The paused run of thread {thread_id!r} of system {system!r} is already being "
            "answered or was answered by another request; read its pending interrupts again "
            "before deciding"
        )


class InMemoryThreadIndex(LocalDecisionClaims, BaseThreadIndex):
    """Thread ownership kept in this process.

    Lost on restart and not shared between workers; a multi-tenant deployment with a durable
    checkpointer needs `PostgresThreadIndex`. The oldest of `max_threads` records are dropped.
    """

    def __init__(self, *, max_threads: int = 100_000) -> None:
        if max_threads < 1:
            raise ValueError("max_threads must be at least 1")
        super().__init__()
        self._max = max_threads
        self._owners: OrderedDict[tuple[str, str], ThreadOwner] = OrderedDict()
        self._lock = threading.Lock()  # held only for a synchronous check-and-set

    async def claim(self, system: str, thread_id: str, owner: ThreadOwner) -> ThreadOwner:
        """Record `owner` unless the thread has one; return the thread's owner."""

        with self._lock:
            key = (system, thread_id)
            existing = self._owners.get(key)
            if existing is not None:
                return existing
            self._owners[key] = owner
            while len(self._owners) > self._max:
                self._owners.popitem(last=False)
            return owner

    async def owner(self, system: str, thread_id: str) -> ThreadOwner | None:
        """The recorded owner, or `None`."""

        return self._owners.get((system, thread_id))

    async def threads(
        self, system: str, *, tenant: str | None, principal: str | None = None, limit: int = 50
    ) -> list[ThreadRecord]:
        """The newest threads of `tenant` (and `principal`), newest first."""

        found: list[ThreadRecord] = []
        for (name, thread_id), owner in reversed(self._owners.items()):
            if name != system or owner.tenant != tenant:
                continue
            if principal is not None and owner.principal != principal:
                continue
            found.append(ThreadRecord(thread_id, owner))
            if len(found) >= limit:
                break
        return found

    async def owned_by(self, principal: str) -> list[tuple[str, str]]:
        """`(system, thread_id)` of every thread owned by `principal`."""

        return [key for key, owner in self._owners.items() if owner.principal == principal]

    async def forget(self, system: str, thread_id: str) -> None:
        """Remove the thread's record and its answered decisions."""

        with self._lock:
            self._owners.pop((system, thread_id), None)
        self._forget_decisions(system, thread_id)


class InMemoryIdempotencyCache(BaseIdempotencyCache):
    """Idempotency records kept in this process (at most `max_entries`, oldest dropped)."""

    def __init__(
        self, *, max_entries: int = 10_000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max = max_entries
        self._clock = clock
        self._records: OrderedDict[tuple[str, str], tuple[float, IdempotencyRecord]] = OrderedDict()
        self._lock = threading.Lock()  # held only for a synchronous check-and-set

    async def begin(
        self, scope: str, key: str, fingerprint: str, ttl: float
    ) -> IdempotencyRecord | None:
        """Claim `key`, or return its unexpired record."""

        with self._lock:
            now = self._clock()
            entry = self._records.get((scope, key))
            if entry is not None and entry[0] > now:
                return entry[1]
            self._records[(scope, key)] = (now + ttl, IdempotencyRecord(fingerprint))
            self._records.move_to_end((scope, key))
            self._evict(now)
            return None

    async def complete(self, scope: str, key: str, record: IdempotencyRecord) -> None:
        """Store the response for a claimed key."""

        with self._lock:
            entry = self._records.get((scope, key))
            if entry is not None:
                self._records[(scope, key)] = (entry[0], record)

    async def release(self, scope: str, key: str) -> None:
        """Drop a claimed key."""

        with self._lock:
            entry = self._records.get((scope, key))
            if entry is not None and not entry[1].completed:
                del self._records[(scope, key)]

    def _evict(self, now: float) -> None:
        # Records are ordered by when they were claimed, so with one TTL the
        # expired ones are at the front.
        while self._records:
            oldest = next(iter(self._records))
            if self._records[oldest][0] > now:
                break
            del self._records[oldest]
        while len(self._records) > self._max:
            self._records.popitem(last=False)


def thread_index_for(
    checkpointers: Iterable[Any],
    thread_index: BaseThreadIndex | None,
    *,
    several_callers: bool,
    where: str,
) -> BaseThreadIndex:
    """The thread index to serve with: `thread_index`, else a new `InMemoryThreadIndex`.

    Raises:
        ConfigurationError: If the default is asked for, a checkpointer is durable and there are
            several callers (unrecorded threads would be refused after a restart).
    """

    if thread_index is not None:
        return thread_index
    from awa._internal.upstream import checkpoint

    durable = sorted(
        {
            type(checkpointer).__name__
            for checkpointer in checkpointers
            if checkpointer is not None and not isinstance(checkpointer, checkpoint.InMemorySaver)
        }
    )
    if durable and several_callers:
        raise ConfigurationError(
            f"{where} keeps threads in a durable checkpointer ({', '.join(durable)}) but would "
            "record who owns them only in memory, so after a restart no caller could reach those "
            "threads. Pass thread_index=PostgresThreadIndex(dsn) (kept across restarts and shared "
            "by every worker), or thread_index=InMemoryThreadIndex() to accept that threads made "
            "before a restart are refused."
        )
    return InMemoryThreadIndex()


def fingerprint(value: Any) -> str:
    """SHA-256 digest of `value` as canonical JSON (sorted keys, `default=str`)."""

    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def scope(*parts: str | None) -> str:
    """A cache scope from its parts (`None` counts as empty), joined by a unit separator."""

    return "\x1f".join(part or "" for part in parts)


@dataclass(frozen=True)
class IdempotencyClaim:
    """An idempotency key of one request, and what the request asked for.

    `begin` claims the key or returns the stored response to replay; the claimant then calls
    `finish` (a retryable failure releases the key) or `release`. Neither raises: an unreleased
    key expires with its TTL.

    Attributes:
        subject: Names the request in log lines (`"job job_1"`).
    """

    cache: BaseIdempotencyCache
    scope: str
    key: str
    fingerprint: str
    subject: str

    async def begin(self, ttl: float, *, mismatch: str, conflict: str) -> IdempotencyRecord | None:
        """Claim the key; return `None` when claimed, or the stored record to replay.

        Raises:
            IdempotencyMismatchError: If the key was used for a different request.
            IdempotencyConflictError: If a request with the key is still being processed.
        """

        existing = await self.cache.begin(self.scope, self.key, self.fingerprint, ttl)
        if existing is None:
            return None
        if existing.fingerprint != self.fingerprint:
            raise IdempotencyMismatchError(mismatch)
        if not existing.completed:
            raise IdempotencyConflictError(conflict)
        return existing

    async def finish(
        self,
        status: int,
        body: Any,
        headers: Mapping[str, str] | None = None,
        *,
        retryable: bool = False,
    ) -> None:
        """Store the response; a `retryable` failure releases the key so a retry runs again."""

        if retryable:
            await self.release()
            return
        record = IdempotencyRecord(self.fingerprint, status, body, dict(headers or {}))
        try:
            await self.cache.complete(self.scope, self.key, record)
        except Exception:  # noqa: BLE001 - the response itself is fine
            _log.exception("Could not store the response of %s", self.subject)

    async def release(self) -> None:
        """Drop the claim without a response, so a retry runs again."""

        try:
            await self.cache.release(self.scope, self.key)
        except Exception:  # noqa: BLE001 - the key then expires with its TTL
            _log.exception("Could not release the idempotency key of %s", self.subject)
