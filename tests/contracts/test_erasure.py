"""Erasure leaves nothing: the local stack (SQLite and files) and the PostgreSQL stack."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from _helpers.erasure import WithProcessRuns
from awa.automation import PostgresRunStore, SqliteRunStore
from awa.core.settings import Settings
from awa.persistence.artifacts import FileArtifactRepository, open_artifact_repository
from awa.persistence.checkpointer import open_checkpointer
from awa.persistence.runlog import PostgresRunLog, SqliteDatabase, SqliteRunLog
from awa.persistence.sqlite import SqliteCheckpointer
from awa.persistence.store import open_store
from awa.persistence.threads import PostgresThreadIndex, SqliteThreadIndex
from awa.testing.contracts import ErasureContract, ErasureStack, Place
from awa.testing.contracts.erasure import KEY


def scan_sqlite(path: Path, needle: str) -> list[str]:
    found: list[str] = []
    with sqlite3.connect(path) as db:
        tables = [
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not row[0].startswith("sqlite_")
        ]
        for table in tables:
            for row in db.execute(f"SELECT * FROM {table}"):  # noqa: S608
                if needle in repr(row):
                    found.append(f"{path.name}:{table}")
                    break
    return found


class TestLocalStack(WithProcessRuns):
    def opened(
        self, place: Place, *, pseudonym: bool = True
    ) -> AbstractAsyncContextManager[ErasureStack]:
        return self._local(place, pseudonym)

    @asynccontextmanager
    async def _local(self, place: Place, pseudonym: bool) -> AsyncIterator[ErasureStack]:
        database = SqliteDatabase(place.path / "awa.db")
        saver = SqliteCheckpointer(place.path / "checkpoints.db", retention=None)
        try:
            yield ErasureStack(
                store=InMemoryStore(),
                checkpointer=saver,
                artifacts=FileArtifactRepository(place.path / "artifacts"),
                run_log=SqliteRunLog(database, pseudonym=KEY if pseudonym else None),
                threads_index=SqliteThreadIndex(database),
                run_store=SqliteRunStore(database),
                location=place.path,
            )
        finally:
            saver.close()
            database.close()

    async def mentions(self, stack: ErasureStack, needle: str) -> list[str]:
        folder: Path = stack.location
        found = scan_sqlite(folder / "awa.db", needle) + scan_sqlite(
            folder / "checkpoints.db", needle
        )
        for path in (folder / "artifacts").rglob("*"):
            if path.is_file() and (needle in path.name or needle in path.read_text("utf-8")):
                found.append(f"artifacts:{path.name}")
        for item in await stack.store.asearch((), limit=10_000):
            if needle in repr((item.namespace, item.key, item.value)):
                found.append(f"store:{'/'.join(item.namespace)}")
        return found


@pytest.mark.postgres
class TestPostgresStack(WithProcessRuns):
    @pytest.fixture
    def place(self, pg_place: Place) -> Place:
        return pg_place

    def opened(
        self, place: Place, *, pseudonym: bool = True
    ) -> AbstractAsyncContextManager[ErasureStack]:
        return self._postgres(place, pseudonym)

    @asynccontextmanager
    async def _postgres(self, place: Place, pseudonym: bool) -> AsyncIterator[ErasureStack]:
        assert place.dsn is not None
        separator = "&" if "?" in place.dsn else "?"
        dsn = f"{place.dsn}{separator}options={quote(f'-csearch_path={place.name}')}"
        settings: Any = Settings(
            checkpointer_backend="postgres",
            store_backend="postgres",
            artifact_backend="postgres",
            postgres_dsn=dsn,  # type: ignore[arg-type]
        )
        async with AsyncExitStack() as stack:
            saver = await stack.enter_async_context(open_checkpointer(settings))
            store = await stack.enter_async_context(open_store(settings))
            artifacts = await stack.enter_async_context(open_artifact_repository(settings))
            log = PostgresRunLog(dsn, schema=place.name, pseudonym=KEY if pseudonym else None)
            index = PostgresThreadIndex(dsn, schema=place.name)
            runs = PostgresRunStore(dsn, schema=place.name)
            for closer in (log, index, runs):
                stack.push_async_callback(closer.close)
            yield ErasureStack(
                store=store,
                checkpointer=saver,
                artifacts=artifacts,
                run_log=log,
                threads_index=index,
                run_store=runs,
                location=place,
            )

    async def mentions(self, stack: ErasureStack, needle: str) -> list[str]:
        import psycopg

        place: Place = stack.location
        assert place.dsn is not None
        found: list[str] = []
        async with await psycopg.AsyncConnection.connect(place.dsn, autocommit=True) as conn:
            tables = await (
                await conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                    (place.name,),
                )
            ).fetchall()
            for (table,) in tables:
                cursor = await conn.execute(
                    f'SELECT t::text FROM {place.name}."{table}" t'  # noqa: S608
                )
                if any(needle in row[0] for row in await cursor.fetchall()):
                    found.append(table)
        return found


async def test_a_stack_with_a_run_store_must_seed_its_process_runs(tmp_path: Path) -> None:
    database = SqliteDatabase(tmp_path / "awa.db")
    stack = ErasureStack(
        store=InMemoryStore(),
        checkpointer=InMemorySaver(),
        artifacts=FileArtifactRepository(tmp_path / "artifacts"),
        run_log=SqliteRunLog(database, pseudonym=KEY),
        threads_index=SqliteThreadIndex(database),
        run_store=SqliteRunStore(database),
    )
    try:
        with pytest.raises(NotImplementedError, match="seed_process_run"):
            await ErasureContract().seed(stack, "alice")
        assert await ErasureContract().seed_process_run(replace(stack, run_store=None), "a") is None
    finally:
        database.close()
