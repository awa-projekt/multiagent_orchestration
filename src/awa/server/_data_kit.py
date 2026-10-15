"""Pure helpers every data-endpoint storage backend shares: quota ledger, names, records."""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from awa.data import DataError, IngestedFile, safe_name
from awa.server.errors import QuotaExceededError
from awa.server.ports import UploadedFile, UploadRecord

SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
"""What an id may look like: it is used as a folder name, so no dots and no separators."""

MAX_IN_FLIGHT = 4
"""Uploads of one tenant that may be received at the same time."""

STALE_SECONDS = 3600.0
"""How long a receive that never finished is kept before a purge removes its leftovers."""


def utcnow() -> datetime:
    """The current time, aware, in UTC: the default `ClockFn`."""

    return datetime.now(UTC)


def random_id(prefix: str) -> str:
    """A new unguessable id such as `up_3f9a...` (96 random bits): the default `IdFn`."""

    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def tenant_folder(tenant: str | None) -> str:
    """The folder name of a tenant: a hash, so no tenant name can climb out of the root."""

    return "_none" if tenant is None else "t-" + hashlib.sha256(tenant.encode()).hexdigest()[:16]


def valid_id(value: str) -> bool:
    """Whether `value` may be used as a folder name (see `SAFE_ID`)."""

    return SAFE_ID.fullmatch(value) is not None


def check_part_name(name: str, taken: Iterable[str]) -> None:
    """Refuse (`DataError`) a file name that is not what `safe_name` makes, or is taken."""

    if safe_name(name) != name:
        raise DataError(f"{name!r} is not a safe file name; use awa.data.safe_name")
    if name.lower() in {taken_name.lower() for taken_name in taken}:
        raise DataError(f"{name!r} is already used in this upload")


def uploaded_files(copies: Iterable[IngestedFile]) -> tuple[UploadedFile, ...]:
    """The accepted files in the order and with the ids (`f1`, `f2`, ...) a profile gives them."""

    ordered = sorted(copies, key=lambda copy: (copy.name.lower(), copy.name))
    return tuple(
        UploadedFile(f"f{number}", copy.name, copy.size, copy.kind, copy.sha256)
        for number, copy in enumerate(ordered, 1)
    )


def new_record(
    upload_id: str,
    tenant: str | None,
    principal: str,
    files: tuple[UploadedFile, ...],
    now: datetime,
    ttl: float,
) -> UploadRecord:
    """The record of an upload received `now` that is kept for `ttl` seconds."""

    return UploadRecord(upload_id, tenant, principal, now, now + timedelta(seconds=ttl), files)


def directory_size(folder: Path) -> int:
    """The bytes of the regular files below `folder` (links are not followed or counted)."""

    return sum(
        path.stat().st_size
        for path in folder.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


class Ledger:
    """Uploads being received and their staged bytes per tenant, under one lock.

    Bytes kept while another upload is still received count for that one too: its view of what
    is stored was taken when it began.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flight: dict[str, int] = {}
        self._staged: dict[str, int] = {}
        self._kept: dict[str, int] = {}  # bytes kept since the oldest upload in flight began

    def staged(self, key: str) -> int:
        """Bytes of the tenant's uploads that are being received."""

        with self._lock:
            return self._staged.get(key, 0)

    def slot(self, key: str, stored: int, quota: int) -> Slot:
        """Take an upload slot for the tenant `key`, which has `stored` of `quota` bytes.

        Raises:
            QuotaExceededError: Too many uploads at once, or the quota is used up.
        """

        with self._lock:
            if self._flight.get(key, 0) >= MAX_IN_FLIGHT:
                raise QuotaExceededError(
                    f"Too many uploads are being received at once ({MAX_IN_FLIGHT}); "
                    "retry when one has finished",
                    retry_after=5.0,
                )
            if stored + self._staged.get(key, 0) >= quota:
                raise QuotaExceededError(
                    "The storage quota is used up; delete uploads or wait for them to expire",
                    retry_after=60.0,
                )
            self._flight[key] = self._flight.get(key, 0) + 1
            return Slot(self, key, stored, quota, self._kept.get(key, 0))


class Slot:
    """One upload in flight: counts its bytes against the quota until it is released."""

    def __init__(self, ledger: Ledger, key: str, stored: int, quota: int, kept: int) -> None:
        self._ledger = ledger
        self._key = key
        self._stored = stored
        self._quota = quota
        self._kept_before = kept
        self._mine = 0
        self._open = True
        self._keep: int | None = None

    def add(self, amount: int) -> None:
        """Reserve `amount` more received bytes at once, so concurrent uploads never pass the quota.

        Raises:
            QuotaExceededError: The tenant would store more than its quota.
        """

        ledger = self._ledger
        with ledger._lock:
            staged = ledger._staged.get(self._key, 0) + amount
            ledger._staged[self._key] = staged
            self._mine += amount
            kept_since = ledger._kept.get(self._key, 0) - self._kept_before
            over = self._stored + kept_since + staged > self._quota
        if over:
            raise QuotaExceededError(
                "This upload would exceed the storage quota; delete uploads or wait for them "
                "to expire",
                retry_after=60.0,
            )

    def keep(self, amount: int) -> None:
        """The upload was stored with `amount` bytes: on release they move from staged to kept."""

        self._keep = amount

    def release(self) -> None:
        """Give the slot and the counted bytes back (once)."""

        ledger = self._ledger
        with ledger._lock:
            if not self._open:
                return
            self._open = False
            key = self._key
            ledger._flight[key] -= 1
            ledger._staged[key] = ledger._staged.get(key, 0) - self._mine
            if self._keep is not None:
                ledger._kept[key] = ledger._kept.get(key, 0) + self._keep
            if ledger._flight[key] == 0:  # nobody in flight holds an older view of the store
                ledger._flight.pop(key)
                ledger._staged.pop(key, None)
                ledger._kept.pop(key, None)
