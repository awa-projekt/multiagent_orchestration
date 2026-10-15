"""The persistence backends run the shared suites: run logs, thread indexes, checkpointers,
cache stores, artifact repositories and audit sinks."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from typing import Any
from urllib.parse import quote

import pytest

from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa.core.settings import Settings
from awa.errors import ConfigurationError
from awa.observability.audit import AuditLogSink, InMemoryAuditSink, JsonlAuditSink
from awa.persistence import ArtifactRepository
from awa.persistence.artifacts import (
    FileArtifactRepository,
    InMemoryArtifactRepository,
    PostgresArtifactRepository,
)
from awa.persistence.cache import InMemoryCacheStore, SqliteCacheStore
from awa.persistence.checkpointer import StateInMemorySaver, open_checkpointer
from awa.persistence.runlog import PostgresRunLog, PseudonymKey, RunLog, SqliteRunLog
from awa.persistence.sqlite import FORMAT_VERSION, SqliteCheckpointer
from awa.persistence.threads import PostgresThreadIndex, SqliteThreadIndex
from awa.runtime.ports import CacheScope, CacheStore
from awa.server._stores import InMemoryThreadIndex
from awa.testing.contracts import (
    ArtifactRepositoryCapabilities,
    ArtifactRepositoryContract,
    AuditSinkCapabilities,
    AuditSinkContract,
    CacheStoreContract,
    CheckpointerCapabilities,
    CheckpointerContract,
    Place,
    RunLogContract,
    ThreadIndexCapabilities,
    ThreadIndexContract,
)
from awa.testing.doubles import FakeClock, FixedIds
from contracts.registry import postgres_savers


@asynccontextmanager
async def closing(log: RunLog) -> AsyncIterator[RunLog]:
    try:
        yield log
    finally:
        await log.close()


def _filter(table: str, run_id: str | None, mark: str) -> tuple[str, tuple[str, ...]]:
    if run_id is None:
        return "", ()
    if table == "captured_content_read":
        return (
            f" WHERE content_ref IN (SELECT content_ref FROM captured_content WHERE run_id = {mark})",
            (run_id,),
        )
    return f" WHERE run_id = {mark}", (run_id,)


class TestSqliteRunLog(RunLogContract):
    adapter = SqliteRunLog

    def opened(
        self, place: Place, ids: FixedIds, pseudonym: PseudonymKey | None
    ) -> AbstractAsyncContextManager[RunLog]:
        return closing(SqliteRunLog(place.path / "awa.db", ids=ids, pseudonym=pseudonym))

    async def count_rows(self, place: Place, table: str, run_id: str | None = None) -> int:
        where, params = _filter(table, run_id, "?")
        conn = sqlite3.connect(place.path / "awa.db")
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}{where}", params).fetchone()[0])  # noqa: S608
        finally:
            conn.close()


@pytest.mark.postgres
class TestPostgresRunLog(RunLogContract):
    adapter = PostgresRunLog

    @pytest.fixture
    def place(self, pg_place: Place) -> Place:
        return pg_place

    def opened(
        self, place: Place, ids: FixedIds, pseudonym: PseudonymKey | None
    ) -> AbstractAsyncContextManager[RunLog]:
        return closing(PostgresRunLog(place.dsn, schema=place.name, ids=ids, pseudonym=pseudonym))

    async def count_rows(self, place: Place, table: str, run_id: str | None = None) -> int:
        import psycopg

        where, params = _filter(table, run_id, "%s")
        where = where.replace("FROM captured_content", f"FROM {place.name}.captured_content")
        async with await psycopg.AsyncConnection.connect(place.dsn, autocommit=True) as conn:
            cursor = await conn.execute(f"SELECT COUNT(*) FROM {place.name}.{table}{where}", params)
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])


@asynccontextmanager
async def closing_thread_index(index: Any) -> AsyncIterator[Any]:
    try:
        yield index
    finally:
        await index.close()


@asynccontextmanager
async def kept(index: Any) -> AsyncIterator[Any]:
    yield index


class TestInMemoryThreadIndex(ThreadIndexContract):
    adapter = InMemoryThreadIndex

    def opened(self, place: Place) -> AbstractAsyncContextManager[Any]:
        return kept(InMemoryThreadIndex())


class TestSqliteThreadIndex(ThreadIndexContract):
    adapter = SqliteThreadIndex
    capabilities = ThreadIndexCapabilities(survives_restart=True)

    def opened(self, place: Place) -> AbstractAsyncContextManager[Any]:
        return closing_thread_index(SqliteThreadIndex(place.path / "awa.db"))


@pytest.mark.postgres
class TestPostgresThreadIndex(ThreadIndexContract):
    adapter = PostgresThreadIndex
    capabilities = ThreadIndexCapabilities(survives_restart=True)

    @pytest.fixture
    def place(self, pg_place: Place) -> Place:
        return pg_place

    def opened(self, place: Place) -> AbstractAsyncContextManager[Any]:
        return closing_thread_index(PostgresThreadIndex(place.dsn, schema=place.name))


@asynccontextmanager
async def kept_checkpointer(
    saver: BaseCheckpointSaver[Any],
) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    yield saver


@asynccontextmanager
async def closing_checkpointer(saver: SqliteCheckpointer) -> AsyncIterator[SqliteCheckpointer]:
    try:
        yield saver
    finally:
        saver.close()


class TestStateInMemorySaver(CheckpointerContract):
    adapter = StateInMemorySaver

    def opened(self, place: Place) -> AbstractAsyncContextManager[BaseCheckpointSaver[Any]]:
        return kept_checkpointer(StateInMemorySaver())


class TestSqliteCheckpointer(CheckpointerContract):
    adapter = SqliteCheckpointer
    capabilities = CheckpointerCapabilities(survives_restart=True)

    def opened(self, place: Place) -> AbstractAsyncContextManager[BaseCheckpointSaver[Any]]:
        return closing_checkpointer(
            SqliteCheckpointer(place.path / "checkpoints.db", retention=None)
        )

    async def test_the_newest_checkpoints_per_thread_are_all_that_is_kept(
        self, place: Place
    ) -> None:
        saver = SqliteCheckpointer(
            place.path / "checkpoints.db", retention=None, keep_checkpoints=4
        )
        try:
            for number in range(1, 31):
                await self.step(saver, place, "busy", number)

            found = [c async for c in saver.alist(self.config(place, "busy"))]
            latest = await saver.aget_tuple(self.config(place, "busy"))

            assert 4 <= len(found) <= 6  # four, plus a batch that is not dropped yet
            assert latest is not None and latest.checkpoint["id"] == "000030"
        finally:
            saver.close()

    def test_a_file_without_a_format_version_is_upgraded_and_a_newer_one_refused(
        self, place: Place
    ) -> None:
        path = place.path / "checkpoints.db"
        SqliteCheckpointer(path, retention=None).close()
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version=0")  # written before the version was set
        SqliteCheckpointer(path, retention=None).close()
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone() == (FORMAT_VERSION,)
            db.execute(f"PRAGMA user_version={FORMAT_VERSION + 1}")

        with pytest.raises(ConfigurationError, match="format version"):
            SqliteCheckpointer(path, retention=None)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_the_file_is_readable_by_its_owner_only(self, place: Place) -> None:
        previous = os.umask(0o022)
        try:
            SqliteCheckpointer(place.path / "checkpoints.db", retention=None).close()
        finally:
            os.umask(previous)

        assert stat.S_IMODE((place.path / "checkpoints.db").stat().st_mode) == 0o600


POSTGRES_SAVERS = postgres_savers()

if POSTGRES_SAVERS:  # the `postgres` extra is installed

    @pytest.mark.postgres
    class TestPostgresCheckpointer(CheckpointerContract):
        adapter = POSTGRES_SAVERS[0]
        capabilities = CheckpointerCapabilities(survives_restart=True)

        @pytest.fixture
        def place(self, pg_place: Place) -> Place:
            return pg_place

        def opened(self, place: Place) -> AbstractAsyncContextManager[BaseCheckpointSaver[Any]]:
            assert place.dsn is not None
            separator = "&" if "?" in place.dsn else "?"
            dsn = f"{place.dsn}{separator}options={quote(f'-csearch_path={place.name}')}"
            settings = Settings(checkpointer_backend="postgres", postgres_dsn=dsn)  # type: ignore[arg-type]
            return open_checkpointer(settings)


class TestInMemoryCacheStore(CacheStoreContract):
    adapter = InMemoryCacheStore

    def opened(self, place: Place, clock: FakeClock) -> CacheStore:
        return InMemoryCacheStore(clock=clock)

    def test_pin_the_least_recently_used_entry_goes_first(self, clock: FakeClock) -> None:
        store = InMemoryCacheStore(max_entries=2, clock=clock)
        scope = CacheScope("acme", "p", "tools")
        store.put(scope, "a", b"1")
        store.put(scope, "b", b"2")
        store.get(scope, "a")
        store.put(scope, "c", b"3")

        assert store.get(scope, "b") is None
        assert store.get(scope, "a") == b"1"

    def test_pin_the_byte_limit_holds(self, clock: FakeClock) -> None:
        store = InMemoryCacheStore(max_bytes=10, clock=clock)
        scope = CacheScope("acme", "p", "tools")
        store.put(scope, "a", b"123456")
        store.put(scope, "b", b"123456")
        store.put(scope, "huge", b"x" * 11)

        assert store.get(scope, "a") is None
        assert store.get(scope, "b") == b"123456"
        assert store.get(scope, "huge") is None

    def test_pin_an_oversize_value_leaves_no_stale_value_behind(self, clock: FakeClock) -> None:
        store = InMemoryCacheStore(max_bytes=10, clock=clock)
        scope = CacheScope("local", "demo", "model")
        store.put(scope, "k", b"old")
        store.put(scope, "k", b"x" * 11)

        assert store.get(scope, "k") is None
        assert store.stats() == []

    def test_pin_a_value_that_fits_replaces_the_old_one(self, clock: FakeClock) -> None:
        store = InMemoryCacheStore(max_bytes=10, clock=clock)
        scope = CacheScope("local", "demo", "model")
        store.put(scope, "k", b"old")
        store.put(scope, "k", b"newer")

        assert store.get(scope, "k") == b"newer"
        [line] = store.stats()
        assert (line.entries, line.bytes) == (1, 5)

    def test_pin_the_oldest_entries_go_first_and_close_drops_them(self, clock: FakeClock) -> None:
        store = InMemoryCacheStore(max_entries=2, clock=clock)
        scope = CacheScope("local", "demo", "model")
        for key in ("a", "b", "c"):
            store.put(scope, key, key.encode())

        assert [key for key, _ in store.entries(scope)] == ["c", "b"]
        store.close()
        assert store.entries(scope) == []

    def test_pin_puts_at_the_entry_limit_stay_fast(self, clock: FakeClock) -> None:
        store = InMemoryCacheStore(max_entries=1_000, clock=clock)
        scope = CacheScope("local", "demo", "model")
        value = b"x" * 2048
        started = time.perf_counter()
        for n in range(10_000):
            store.put(scope, str(n), value)
            store.get(scope, str(n))

        assert time.perf_counter() - started < 5
        [line] = store.stats()
        assert (line.entries, line.bytes) == (1_000, 1_000 * 2048)


class TestSqliteCacheStore(CacheStoreContract):
    adapter = SqliteCacheStore

    def opened(self, place: Place, clock: FakeClock) -> CacheStore:
        return SqliteCacheStore(place.path / "cache" / "cache.db", clock=clock)

    def test_pin_entries_survive_a_new_store_on_the_same_file(
        self, place: Place, clock: FakeClock
    ) -> None:
        scope = CacheScope("acme", "p", "replay")
        SqliteCacheStore(place.path / "c.db", clock=clock).put(scope, "k", b"kept")

        assert SqliteCacheStore(place.path / "c.db", clock=clock).get(scope, "k") == b"kept"

    def test_pin_threads_after_close_share_one_new_connection(
        self, place: Place, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = SqliteCacheStore(place.path / "c.db", clock=clock)
        store.close()
        opened = []
        real_open = store._open

        def counting_open() -> object:
            opened.append(1)
            time.sleep(0.05)
            return real_open()

        monkeypatch.setattr(store, "_open", counting_open)
        start = threading.Barrier(4)
        scope = CacheScope("acme", "p", "replay")

        def read() -> None:
            start.wait()
            store.get(scope, "k")

        workers = [threading.Thread(target=read) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        assert len(opened) == 1
        store.close()


class TestInMemoryArtifactRepository(ArtifactRepositoryContract):
    adapter = InMemoryArtifactRepository
    capabilities = ArtifactRepositoryCapabilities(survives_restart=False, refs_with_nul=True)

    @asynccontextmanager
    async def opened(self, place: Place) -> AsyncIterator[ArtifactRepository]:  # type: ignore[override]
        yield InMemoryArtifactRepository()


class TestFileArtifactRepository(ArtifactRepositoryContract):
    adapter = FileArtifactRepository
    capabilities = ArtifactRepositoryCapabilities(survives_restart=True, refs_with_nul=True)

    @asynccontextmanager
    async def opened(self, place: Place) -> AsyncIterator[ArtifactRepository]:  # type: ignore[override]
        yield FileArtifactRepository(place.path / "artifacts")


@pytest.mark.postgres
class TestPostgresArtifactRepository(ArtifactRepositoryContract):
    adapter = PostgresArtifactRepository
    capabilities = ArtifactRepositoryCapabilities(survives_restart=True)

    @pytest.fixture
    def place(self, pg_place: Place) -> Place:
        return pg_place

    @asynccontextmanager
    async def opened(self, place: Place) -> AsyncIterator[ArtifactRepository]:  # type: ignore[override]
        import psycopg

        assert place.dsn is not None
        async with await psycopg.AsyncConnection.connect(
            place.dsn, autocommit=True, options=f"-csearch_path={place.name}"
        ) as conn:
            repository = PostgresArtifactRepository(conn)
            await repository.setup()
            yield repository


class TestInMemoryAuditSink(AuditSinkContract):
    adapter = InMemoryAuditSink
    capabilities = AuditSinkCapabilities(survives_restart=False)

    @contextmanager
    def opened(self, place: Place) -> Iterator[AuditLogSink]:  # type: ignore[override]
        yield InMemoryAuditSink()


class TestJsonlAuditSink(AuditSinkContract):
    adapter = JsonlAuditSink
    capabilities = AuditSinkCapabilities(survives_restart=True)

    @contextmanager
    def opened(self, place: Place) -> Iterator[AuditLogSink]:  # type: ignore[override]
        yield JsonlAuditSink(place.path / "audit.jsonl", fsync=False)
