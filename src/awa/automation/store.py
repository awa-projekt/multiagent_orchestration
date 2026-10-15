"""Where process runs live: the `RunStore` protocol and its in-memory implementation."""

from __future__ import annotations

import builtins
import contextlib
import os
import sys
import threading
from collections.abc import Callable, Collection, Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from awa.automation._time import as_utc
from awa.automation.models import ProcessRun, statuses, with_history
from awa.errors import ProcessRunStateError, RunNotFoundError

if TYPE_CHECKING:
    from awa.core.settings import Settings
    from awa.persistence.retention import ErasureRequest, RetentionRequest

RunChange = Callable[[ProcessRun], ProcessRun | None]
"""Receives a copy of the stored run and returns the changed run, or `None` for no change."""


@runtime_checkable
class RunStore(Protocol):
    """Durable storage of process runs, shared by every worker of an automation.

    `claim` gives a queued run to one worker with a lease (`renew` extends it); `expired` finds runs
    of crashed workers; `modify` changes a run atomically; `compare_and_set_state` keeps small
    values such as a schedule's last tick. Times come from the caller, so keep worker clocks in
    sync. Payloads may come back in JSON form.
    """

    async def create(
        self, run: ProcessRun, *, dedup_since: datetime | None = None
    ) -> tuple[ProcessRun, bool]:
        """Store a new run unless one with the same automation and `dedup_key` exists.

        Args:
            dedup_since: Only runs created at or after this time count as duplicates.

        Returns:
            The stored run and `True`, or the existing duplicate and `False`.
        """
        ...

    async def get(self, run_id: str) -> ProcessRun | None:
        """Return the run, or `None`."""
        ...

    async def list(
        self,
        *,
        automation: str | None = None,
        status: str | Collection[str] | None = None,
        tenant: str | None = None,
        limit: int = 100,
    ) -> builtins.list[ProcessRun]:
        """Return runs, newest first, filtered by automation, status and tenant."""
        ...

    async def modify(self, run_id: str, change: RunChange, *, now: datetime) -> ProcessRun:
        """Apply `change` atomically; a returned run gets a new `revision` and `updated_at`.

        Raises:
            RunNotFoundError: If there is no such run.
        """
        ...

    async def claim(
        self,
        automation: str,
        *,
        worker: str,
        lease: timedelta,
        now: datetime,
        versions: Collection[str] | None = None,
        max_running: int | None = None,
    ) -> ProcessRun | None:
        """Give the oldest ready queued run to `worker` and mark it running, or `None`.

        A run is ready when `next_attempt_at` is not in the future; the attempt counter grows unless
        the run resumes a wait.

        Args:
            versions: Only runs pinned to one of these versions (`None`: any).
            max_running: Claim nothing while this many runs of the automation are running.
        """
        ...

    async def renew(
        self, run_id: str, *, worker: str, lease: timedelta, now: datetime, epoch: int | None = None
    ) -> bool:
        """Extend the lease of a run held by `worker` (and `epoch`); `False` if it lost the run."""
        ...

    async def expired(self, automation: str, *, now: datetime) -> builtins.list[ProcessRun]:
        """Running runs of the automation whose lease expired before `now`."""
        ...

    async def due(
        self, automation: str, *, now: datetime, limit: int = 100
    ) -> builtins.list[ProcessRun]:
        """Unfinished runs whose `due_at()` (SLA, wait deadline, reminder) is at or before `now`."""
        ...

    async def waiting_on(self, automation: str, key: str) -> builtins.list[ProcessRun]:
        """Waiting runs of the automation with an unanswered wait for `key`."""
        ...

    async def get_state(self, key: str) -> str | None:
        """Return a small stored value (for example a schedule's last tick)."""
        ...

    async def compare_and_set_state(self, key: str, expected: str | None, value: str) -> bool:
        """Set `key` to `value` if it currently holds `expected`; `True` on success."""
        ...

    async def delete(self, run_ids: Sequence[str]) -> int:
        """Delete runs for good (retention and erasure); returns how many were deleted."""
        ...

    async def close(self) -> None:
        """Release the store's resources."""
        ...


class InMemoryRunStore:
    """A `RunStore` in the memory of one process (`SqliteRunStore` persists)."""

    def __init__(self) -> None:
        self._runs: dict[str, ProcessRun] = {}
        self._state: dict[str, str] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._runs)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(runs={len(self._runs)})"

    def _select(self, predicate: Callable[[ProcessRun], bool]) -> builtins.list[ProcessRun]:
        with self._lock:
            return [run.model_copy(deep=True) for run in self._runs.values() if predicate(run)]

    async def create(
        self, run: ProcessRun, *, dedup_since: datetime | None = None
    ) -> tuple[ProcessRun, bool]:
        """See `RunStore.create`."""

        with self._lock:
            if run.dedup_key is not None:
                since = as_utc(dedup_since) if dedup_since is not None else None
                for existing in sorted(
                    self._runs.values(), key=lambda item: item.created_at, reverse=True
                ):
                    if (
                        existing.automation == run.automation
                        and existing.dedup_key == run.dedup_key
                        and (since is None or existing.created_at >= since)
                    ):
                        return existing.model_copy(deep=True), False
            if run.id in self._runs:
                raise ProcessRunStateError(f"Process run {run.id!r} already exists")
            self._runs[run.id] = run.model_copy(deep=True)
            return run.model_copy(deep=True), True

    async def get(self, run_id: str) -> ProcessRun | None:
        """See `RunStore.get`."""

        with self._lock:
            run = self._runs.get(run_id)
            return run.model_copy(deep=True) if run is not None else None

    async def list(
        self,
        *,
        automation: str | None = None,
        status: str | Collection[str] | None = None,
        tenant: str | None = None,
        limit: int = 100,
    ) -> builtins.list[ProcessRun]:
        """See `RunStore.list`."""

        wanted = statuses(status)
        found = self._select(
            lambda run: (
                (automation is None or run.automation == automation)
                and (wanted is None or run.status in wanted)
                and (tenant is None or run.tenant == tenant)
            )
        )
        found.sort(key=lambda run: (run.created_at, run.id), reverse=True)
        return found[: max(0, limit)]

    async def modify(self, run_id: str, change: RunChange, *, now: datetime) -> ProcessRun:
        """See `RunStore.modify`."""

        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise RunNotFoundError(f"No process run {run_id!r}")
            changed = change(current.model_copy(deep=True))
            if changed is None:
                return current.model_copy(deep=True)
            stored = changed.model_copy(
                update={"revision": current.revision + 1, "updated_at": as_utc(now)}, deep=True
            )
            self._runs[run_id] = stored
            return stored.model_copy(deep=True)

    async def claim(
        self,
        automation: str,
        *,
        worker: str,
        lease: timedelta,
        now: datetime,
        versions: Collection[str] | None = None,
        max_running: int | None = None,
    ) -> ProcessRun | None:
        """See `RunStore.claim`."""

        now = as_utc(now)
        with self._lock:
            runs = [run for run in self._runs.values() if run.automation == automation]
            if max_running is not None:
                running = sum(1 for run in runs if run.status == "running")
                if running >= max_running:
                    return None
            ready = [
                run
                for run in runs
                if run.status == "queued"
                and (run.next_attempt_at is None or run.next_attempt_at <= now)
                and (versions is None or run.version is None or run.version in versions)
            ]
            if not ready:
                return None
            chosen = min(ready, key=lambda run: (run.next_attempt_at or run.created_at, run.id))
            claimed = claimed_copy(chosen, worker=worker, lease=lease, now=now)
            self._runs[chosen.id] = claimed
            return claimed.model_copy(deep=True)

    async def renew(
        self, run_id: str, *, worker: str, lease: timedelta, now: datetime, epoch: int | None = None
    ) -> bool:
        """See `RunStore.renew`."""

        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.status != "running" or run.lease_owner != worker:
                return False
            if epoch is not None and run.lease_epoch != epoch:
                return False
            expires = as_utc(now) + lease
            self._runs[run_id] = run.model_copy(update={"lease_expires_at": expires}, deep=True)
            return True

    async def expired(self, automation: str, *, now: datetime) -> builtins.list[ProcessRun]:
        """See `RunStore.expired`."""

        moment = as_utc(now)
        return self._select(
            lambda run: (
                run.automation == automation
                and run.status == "running"
                and run.lease_expires_at is not None
                and run.lease_expires_at < moment
            )
        )

    async def due(
        self, automation: str, *, now: datetime, limit: int = 100
    ) -> builtins.list[ProcessRun]:
        """See `RunStore.due`."""

        moment = as_utc(now)

        def is_due(run: ProcessRun) -> bool:
            due = run.due_at()
            return run.automation == automation and due is not None and due <= moment

        found = self._select(is_due)
        found.sort(key=lambda run: run.due_at() or moment)
        return found[: max(0, limit)]

    async def waiting_on(self, automation: str, key: str) -> builtins.list[ProcessRun]:
        """See `RunStore.waiting_on`."""

        return self._select(
            lambda run: (
                run.automation == automation and run.status == "waiting" and key in run.wait_keys
            )
        )

    async def get_state(self, key: str) -> str | None:
        """See `RunStore.get_state`."""

        with self._lock:
            return self._state.get(key)

    async def compare_and_set_state(self, key: str, expected: str | None, value: str) -> bool:
        """See `RunStore.compare_and_set_state`."""

        with self._lock:
            if self._state.get(key) != expected:
                return False
            self._state[key] = value
            return True

    async def delete(self, run_ids: Sequence[str]) -> int:
        """Delete runs for good (retention and erasure); returns how many were deleted."""

        with self._lock:
            return sum(self._runs.pop(run_id, None) is not None for run_id in set(run_ids))

    async def close(self) -> None:
        """Nothing to release."""


@contextlib.contextmanager
def hold_triggers(store: Path) -> Iterator[None]:
    """Hold the OS lock `<store>.triggers` while the block runs, so one process runs the triggers.

    Raises:
        ProcessRunStateError: If another process holds the claim.
    """

    lock_path = store.with_name(store.name + ".triggers")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")
    try:
        _lock_file(handle)
    except OSError:
        handle.close()
        raise ProcessRunStateError(
            f"Another awa process (`awa serve` or `awa worker`) already runs the triggers of "
            f"the runs in {store}. Run only one of them per local store, or start this one "
            "without triggers (`awa worker --no-triggers`), or share a PostgreSQL database "
            "(--dsn) so triggers fire once across processes."
        ) from None
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            _unlock_file(handle)
        handle.close()


if sys.platform == "win32":
    import msvcrt

    def _lock_file(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def claimed_copy(run: ProcessRun, *, worker: str, lease: timedelta, now: datetime) -> ProcessRun:
    """The run as a worker claims it: running, leased under a new epoch, one more attempt unless
    it resumes."""

    claimed = run.model_copy(
        update={
            "status": "running",
            "attempts": run.attempts if run.resuming else run.attempts + 1,
            "lease_owner": worker,
            "lease_expires_at": now + lease,
            "lease_epoch": run.lease_epoch + 1,
            "next_attempt_at": None,
            "updated_at": now,
            "revision": run.revision + 1,
        },
        deep=True,
    )
    return with_history(claimed, now, "running", f"claimed by {worker}")


class ProcessRunsErasure:
    """The `process_runs` erasure plugin: erases and prunes process runs.

    An erasure deletes the runs found in the run log, the person's threads and runs the person
    started; retention deletes finished runs older than the policy's `runs` age.
    """

    category = "process_runs"

    def __init__(self, run_store: RunStore) -> None:
        self._store = run_store

    @classmethod
    def create(
        cls,
        *,
        store: object,
        run_log: object,
        run_store: RunStore | None,
        settings: Settings | None,
        memory: object,
    ) -> ProcessRunsErasure | None:
        """The participant for `run_store`, or `None` when none was passed."""

        return cls(run_store) if run_store is not None else None

    async def _all(self) -> builtins.list[ProcessRun]:
        return await self._store.list(limit=10**9)

    async def _matching(self, request: ErasureRequest) -> builtins.list[ProcessRun]:
        runs, threads = set(request.runs), set(request.threads)
        return [
            run
            for run in await self._all()
            if run.id in runs
            or (run.thread_id is not None and run.thread_id in threads)
            or run.actor == request.subject_id
        ]

    async def _expired(self, request: RetentionRequest) -> builtins.list[ProcessRun]:
        age = request.policy.age_for("runs")
        if age is None:
            return []
        cutoff = request.now - age
        return [run for run in await self._all() if run.is_finished and run.created_at < cutoff]

    async def erase(self, request: ErasureRequest) -> builtins.list[str]:
        """Delete the records of the person's runs; return their ids."""

        ids = [run.id for run in await self._matching(request)]
        if ids and not request.dry_run:
            await self._store.delete(ids)
        return ids

    async def prune(self, request: RetentionRequest) -> builtins.list[str]:
        """Delete finished records older than the policy's `runs` age; return their ids."""

        ids = [run.id for run in await self._expired(request)]
        if ids and not request.dry_run:
            await self._store.delete(ids)
        return ids

    async def threads(self, request: ErasureRequest | RetentionRequest) -> builtins.list[str]:
        """The threads of the records about to be deleted that no other record uses."""

        from awa.persistence.retention import ErasureRequest

        gone = await (
            self._matching(request)
            if isinstance(request, ErasureRequest)
            else self._expired(request)
        )
        gone_ids = {run.id for run in gone}
        kept = {
            run.thread_id for run in await self._all() if run.id not in gone_ids and run.thread_id
        }
        return sorted({run.thread_id for run in gone if run.thread_id} - kept)

    async def held_runs(self, request: RetentionRequest) -> set[str]:
        """The ids of runs that are not finished: their run log is kept."""

        return {run.id for run in await self._all() if not run.is_finished}


def builtin_erasure() -> builtins.list[tuple[str, type[ProcessRunsErasure], str]]:
    """The `process_runs` plugin of kind `erasure` (read by `awa.plugins`)."""

    return [("process_runs", ProcessRunsErasure, "process run records and their threads")]


__all__ = [
    "InMemoryRunStore",
    "ProcessRunsErasure",
    "RunChange",
    "RunStore",
    "claimed_copy",
    "hold_triggers",
]
