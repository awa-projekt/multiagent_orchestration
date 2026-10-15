"""Uploads and builds kept in folders on one machine: `FileUploadStore` and `FileBuildStore`."""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from awa.data import Limits, UploadScanner, ingest
from awa.server._data_kit import (
    STALE_SECONDS,
    Ledger,
    Slot,
    check_part_name,
    new_record,
    tenant_folder,
    uploaded_files,
    valid_id,
)
from awa.server.errors import BuildNotFoundError, UploadNotFoundError
from awa.server.ports import (
    RUNNING,
    TERMINAL,
    BuildRecord,
    BuildStore,
    ClockFn,
    IdFn,
    UploadRecord,
    UploadSession,
    UploadStore,
)

if TYPE_CHECKING:
    from awa.data._model import FileKind


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


class FileUploadStore(UploadStore):
    """`UploadStore` on disk under `root` (created when first used).

    Layout: `<tenant hash>/uploads/<id>/{upload.json,files/}`, receives in `<tenant hash>/.staging/`.
    A record becomes visible only once renamed into place, so a crash leaves staging leftovers
    (removed by `purge`), never a half-kept upload.
    """

    def __init__(
        self,
        root: Path,
        *,
        ttl: float,
        clock: ClockFn,
        ids: IdFn,
        stale_after: float = STALE_SECONDS,
    ) -> None:
        self._root = root
        self._ttl = ttl
        self._stale_after = stale_after
        self._clock = clock
        self._ids = ids
        self._ledger = Ledger()

    def _tenant(self, tenant: str | None) -> Path:
        return self._root / tenant_folder(tenant)

    def _folder(self, tenant: str | None, upload_id: str) -> Path:
        return self._tenant(tenant) / "uploads" / upload_id

    def _stored(self, tenant: str | None) -> int:
        total = 0
        for meta in (self._tenant(tenant) / "uploads").glob("*/upload.json"):
            with contextlib.suppress(OSError, ValueError):
                total += UploadRecord.from_json(meta.read_text(encoding="utf-8")).bytes
        return total

    @contextmanager
    def begin(self, tenant: str | None, principal: str, *, quota: int) -> Iterator[UploadSession]:
        key = tenant_folder(tenant)
        slot = self._ledger.slot(key, self._stored(tenant), quota)
        try:
            stamp = int(self._clock().timestamp())
            folder = self._tenant(tenant) / ".staging" / f"{stamp}_{self._ids('st')}"
            (folder / "in").mkdir(parents=True)
            try:
                yield _FileSession(self, tenant, principal, folder, slot)
            finally:
                shutil.rmtree(folder, ignore_errors=True)
        finally:
            slot.release()

    def get(self, tenant: str | None, upload_id: str, *, expired_ok: bool = False) -> UploadRecord:
        missing = UploadNotFoundError(f"No upload {upload_id!r} is known here")
        if not valid_id(upload_id):
            raise missing
        try:
            record = UploadRecord.from_json(
                (self._folder(tenant, upload_id) / "upload.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise missing from error
        if record.tenant != tenant or (not expired_ok and record.expires_at <= self._clock()):
            raise missing
        return record

    @contextmanager
    def open_files(self, record: UploadRecord) -> Iterator[Path]:
        folder = self._folder(record.tenant, record.id) / "files"
        if not valid_id(record.id) or not folder.is_dir():
            raise UploadNotFoundError(f"No upload {record.id!r} is known here")
        yield folder

    def delete(self, record: UploadRecord) -> None:
        if valid_id(record.id):
            shutil.rmtree(self._folder(record.tenant, record.id), ignore_errors=True)

    def purge(self, keep: Collection[str] = ()) -> int:
        now = self._clock()
        removed = 0
        for tenant in self._root.glob("*"):
            for meta in tenant.glob("uploads/*/upload.json"):
                try:
                    record = UploadRecord.from_json(meta.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue  # damaged, or written by another version of awa: left alone
                if record.expires_at <= now and record.id not in keep:
                    shutil.rmtree(meta.parent, ignore_errors=True)
                    removed += 1
            for leftover in tenant.glob(".staging/*"):
                started = leftover.name.partition("_")[0]
                if started.isdigit() and now.timestamp() - int(started) > self._stale_after:
                    shutil.rmtree(leftover, ignore_errors=True)
        return removed

    def used_bytes(self, tenant: str | None) -> int:
        return self._stored(tenant) + self._ledger.staged(tenant_folder(tenant))


class _FileSession(UploadSession):
    def __init__(
        self, store: FileUploadStore, tenant: str | None, principal: str, folder: Path, slot: Slot
    ) -> None:
        self._store = store
        self._tenant = tenant
        self._principal = principal
        self._folder = folder
        self._slot = slot
        self._names: list[str] = []

    def open(self, name: str) -> BinaryIO:
        check_part_name(name, self._names)
        self._names.append(name)
        return (self._folder / "in" / name).open("xb")

    def count(self, amount: int) -> None:
        self._slot.add(amount)

    def commit(
        self,
        limits: Limits,
        *,
        kinds: Collection[FileKind] | None = None,
        scanner: UploadScanner | None = None,
    ) -> UploadRecord:
        store = self._store
        out = self._folder / "out"
        copies = ingest(self._folder / "in", out / "files", limits, kinds=kinds, scanner=scanner)
        record = new_record(
            store._ids("up"),
            self._tenant,
            self._principal,
            uploaded_files(copies),
            store._clock(),
            store._ttl,
        )
        _write_atomic(out / "upload.json", record.to_json())
        target = store._folder(self._tenant, record.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(out, target)
        self._slot.keep(record.bytes)
        return record


class FileBuildStore(BuildStore):
    """`BuildStore` on disk under `root` (may be the upload store's folder).

    Layout: `<tenant hash>/builds/<id>/{build.json,project/}`.
    """

    def __init__(self, root: Path, *, ttl: float, clock: ClockFn) -> None:
        self._root = root
        self._ttl = ttl
        self._clock = clock

    def _folder(self, tenant: str | None, build_id: str) -> Path:
        return self._root / tenant_folder(tenant) / "builds" / build_id

    def save(self, record: BuildRecord) -> None:
        if not valid_id(record.id):
            raise ValueError(f"{record.id!r} is not a usable build id")
        _write_atomic(self._folder(record.tenant, record.id) / "build.json", record.to_json())

    def _read(self, meta: Path) -> BuildRecord:
        return BuildRecord.from_json(meta.read_text(encoding="utf-8"))

    def get(self, tenant: str | None, build_id: str) -> BuildRecord:
        missing = BuildNotFoundError(f"No build {build_id!r} is known here")
        if not valid_id(build_id):
            raise missing
        try:
            record = self._read(self._folder(tenant, build_id) / "build.json")
        except (OSError, ValueError) as error:
            raise missing from error
        if record.tenant != tenant:
            raise missing
        return record

    def list_builds(
        self, tenant: str | None, *, owner: str | None = None, limit: int = 50
    ) -> list[BuildRecord]:
        records: list[BuildRecord] = []
        for meta in (self._root / tenant_folder(tenant) / "builds").glob("*/build.json"):
            with contextlib.suppress(OSError, ValueError):
                record = self._read(meta)
                if record.tenant == tenant and (owner is None or record.principal == owner):
                    records.append(record)
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records[:limit]

    def project_dir(self, tenant: str | None, build_id: str) -> Path:
        self.get(tenant, build_id)
        folder = self._folder(tenant, build_id) / "project"
        folder.mkdir(exist_ok=True)
        return folder

    def delete(self, tenant: str | None, build_id: str) -> None:
        if valid_id(build_id):
            shutil.rmtree(self._folder(tenant, build_id), ignore_errors=True)

    def purge(self, keep: Collection[str] = ()) -> int:
        now = self._clock()
        removed = 0
        for meta in self._root.glob("*/builds/*/build.json"):
            try:
                record = self._read(meta)
            except (OSError, ValueError):
                continue
            ended = record.finished_at
            if (
                ended is not None
                and record.status not in RUNNING
                and ended + timedelta(seconds=self._ttl) <= now
                and record.id not in keep
            ):
                shutil.rmtree(meta.parent, ignore_errors=True)
                removed += 1
        return removed

    def used_bytes(self, tenant: str | None) -> int:
        return sum(record.bytes for record in self.list_builds(tenant, limit=1_000_000))

    def unfinished(self) -> list[BuildRecord]:
        found: list[BuildRecord] = []
        for meta in self._root.glob("*/builds/*/build.json"):
            with contextlib.suppress(OSError, ValueError):
                record = self._read(meta)
                if record.status not in TERMINAL:
                    found.append(record)
        return found
