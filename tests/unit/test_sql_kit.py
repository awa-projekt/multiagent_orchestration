"""Tests for `awa.persistence.sql`: names, setup SQL, `PostgresTables` (fakes and a real database)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

import awa.persistence.sql as _postgres
from awa.errors import OptionalDependencyError
from awa.persistence.sql import (
    PostgresTables,
    check_conninfo,
    check_name,
    check_schema,
    fetch_all,
    fetch_one,
    import_psycopg,
    setup_schema,
)

pytest.importorskip("psycopg_pool")


@pytest.mark.parametrize("schema", ["awa", "_x", "a1_b2", "a" * 40])
def test_check_schema_accepts_lowercase_identifiers(schema: str) -> None:
    assert check_schema(schema) == schema


@pytest.mark.parametrize("schema", ["", "Awa", "1awa", "awa\n", "awa;drop", "a" * 41, "a-b", 5])
def test_check_schema_refuses_anything_else(schema: Any) -> None:
    with pytest.raises(ValueError, match="lowercase SQL identifier"):
        check_schema(schema)


def test_check_name() -> None:
    assert check_name("board 1", "name") == "board 1"
    with pytest.raises(ValueError, match="name must not be empty"):
        check_name("  ", "name")
    with pytest.raises(ValueError, match="at most 200"):
        check_name("x" * 201, "name")
    with pytest.raises(ValueError, match="without NUL"):
        check_name("a\x00b", "name")


def test_check_conninfo() -> None:
    assert check_conninfo("postgresql://x") == "postgresql://x"
    with pytest.raises(ValueError, match="must not be empty"):
        check_conninfo(" ")
    with pytest.raises(TypeError, match="got int"):
        check_conninfo(5)


def test_import_psycopg_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "psycopg_pool", None)
    with pytest.raises(OptionalDependencyError, match=r"\[postgres\]"):
        import_psycopg()


# ---------------------------------------------------------------------- fakes


class _Transaction:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> None:
        self.conn.log.append("BEGIN")

    async def __aexit__(self, *exc: object) -> None:
        self.conn.log.append("COMMIT")

    def __enter__(self) -> None:
        self.conn.log.append("BEGIN")

    def __exit__(self, *exc: object) -> None:
        self.conn.log.append("COMMIT")


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.query: tuple[str, Any] | None = None

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: Any) -> Any:
        self.query = (query, params)
        return _Awaitable(None)

    def fetchall(self) -> Any:
        return _Awaitable(list(self.rows))

    def fetchone(self) -> Any:
        return _Awaitable(self.rows[0] if self.rows else None)


class _Awaitable:
    def __init__(self, value: Any) -> None:
        self.value = value

    def __await__(self) -> Any:
        yield from ()
        return self.value


class _Conn:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.log: list[Any] = []
        self.rows = rows or []
        self.cursors: list[_Cursor] = []

    def execute(self, query: str, params: Any = None) -> Any:
        self.log.append((query, params) if params is not None else query)
        return _Awaitable(None)

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def cursor(self, row_factory: Any = None) -> _Cursor:
        self.cursors.append(_Cursor(self.rows))
        return self.cursors[-1]


async def test_fetch_helpers_return_tuples() -> None:
    conn = _Conn([(1, "a"), (2, "b")])

    assert await fetch_all(conn, "SELECT %s", (1,)) == [(1, "a"), (2, "b")]
    assert await fetch_one(conn, "SELECT %(x)s", {"x": 1}) == (1, "a")
    assert await fetch_one(_Conn(), "SELECT 1") is None
    assert conn.cursors[0].query == ("SELECT %s", (1,))


async def test_setup_schema_locks_creates_and_runs_ddl_in_one_transaction() -> None:
    conn = _Conn()

    await setup_schema(conn, "awa_x", ["CREATE TABLE a", "CREATE INDEX b"], lock_key=7)

    assert conn.log == [
        "BEGIN",
        ("SELECT pg_advisory_xact_lock(%s)", (7,)),
        "CREATE SCHEMA IF NOT EXISTS awa_x",
        "CREATE TABLE a",
        "CREATE INDEX b",
        "COMMIT",
    ]


class _Tables(PostgresTables):
    _setup_lock_key = 0x6177615F74657374  # "awa_test"

    def _ddl(self) -> list[str]:
        return [f"CREATE TABLE IF NOT EXISTS {self.schema}.core_notes (id TEXT PRIMARY KEY)"]


class _FakePool:
    def __init__(self) -> None:
        self.conn = _Conn()
        self.closed = False

    def connection(self) -> Any:
        pool = self

        class Context:
            async def __aenter__(self) -> _Conn:
                return pool.conn

            async def __aexit__(self, *exc: object) -> None:
                return None

        return Context()

    async def close(self) -> None:
        self.closed = True


def _fake_pools(monkeypatch: pytest.MonkeyPatch) -> list[_FakePool]:
    opened: list[_FakePool] = []

    async def open_pool(self: Any) -> _FakePool:
        opened.append(_FakePool())
        return opened[-1]

    monkeypatch.setattr(_postgres.LoopLocalPool, "_open", open_pool)
    return opened


async def test_postgres_tables_creates_its_tables_once_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = _fake_pools(monkeypatch)
    tables = _Tables("postgresql://fake", schema="awa_core")

    async with tables as entered:
        assert entered is tables
        async with await tables._connection() as conn:
            await conn.execute("SELECT 2")
        await tables.ping()

    [pool] = opened
    assert pool.closed
    assert pool.conn.log == [
        "BEGIN",
        ("SELECT pg_advisory_xact_lock(%s)", (_Tables._setup_lock_key,)),
        "CREATE SCHEMA IF NOT EXISTS awa_core",
        "CREATE TABLE IF NOT EXISTS awa_core.core_notes (id TEXT PRIMARY KEY)",
        "COMMIT",
        "SELECT 2",
        "SELECT 1",
    ]
    assert tables.schema == "awa_core"


async def test_postgres_tables_without_setup_or_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    class Unlocked(_Tables):
        _setup_lock_key = None

    opened = _fake_pools(monkeypatch)
    lazy = Unlocked("postgresql://fake", setup=False)
    await lazy.ping()
    assert opened[0].conn.log == ["SELECT 1"]
    await lazy.setup()
    assert opened[0].conn.log[1:] == [
        "BEGIN",
        "CREATE SCHEMA IF NOT EXISTS awa",
        "CREATE TABLE IF NOT EXISTS awa.core_notes (id TEXT PRIMARY KEY)",
        "COMMIT",
    ]
    await lazy.close()


def test_postgres_tables_opens_a_pool_per_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _fake_pools(monkeypatch)
    tables = _Tables("postgresql://fake")

    asyncio.run(tables.ping())
    asyncio.run(tables.ping())

    assert len(opened) == 2 and all(pool.closed for pool in opened)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"conninfo": ""}, ValueError),
        ({"conninfo": 5}, TypeError),
        ({"conninfo": "postgresql://x", "schema": "Bad"}, ValueError),
        ({"conninfo": "postgresql://x", "pool_size": 0}, ValueError),
    ],
)
def test_postgres_tables_validates_its_arguments(
    kwargs: dict[str, Any], error: type[Exception]
) -> None:
    with pytest.raises(error):
        _Tables(**kwargs)


def test_postgres_tables_requires_ddl() -> None:
    with pytest.raises(NotImplementedError):
        PostgresTables("postgresql://x")._ddl()


@pytest.mark.parametrize(
    ("factory", "lock_key"),
    [
        ("awa.server:PostgresIdempotencyCache", None),
        ("awa.automation:PostgresEventQueue", 0x6177615F70726F63),  # "awa_proc"
    ],
)
async def test_the_server_and_automation_stores_are_postgres_tables(
    monkeypatch: pytest.MonkeyPatch, factory: str, lock_key: int | None
) -> None:
    import importlib

    module, name = factory.split(":")
    cls = getattr(importlib.import_module(module), name)
    kwargs = {"name": "q"} if name == "PostgresEventQueue" else {}
    opened = _fake_pools(monkeypatch)
    store = cls("postgresql://fake", schema="awa_store", setup=False, **kwargs)
    assert isinstance(store, PostgresTables) and store.schema == "awa_store"

    await store.setup()
    await store.ping()
    await store.close()

    log = opened[0].conn.log
    lock = [("SELECT pg_advisory_xact_lock(%s)", (lock_key,))] if lock_key is not None else []
    assert log[: 2 + len(lock)] == ["BEGIN", *lock, "CREATE SCHEMA IF NOT EXISTS awa_store"]
    assert log[-2:] == ["COMMIT", "SELECT 1"]
    assert all(entry.startswith("CREATE ") for entry in log[2 + len(lock) : -2])
    assert opened[0].closed


@pytest.mark.parametrize(
    "factory", ["awa.server:PostgresThreadIndex", "awa.automation:PostgresRunStore"]
)
async def test_the_run_store_and_thread_index_take_their_tables_from_the_ledger(
    monkeypatch: pytest.MonkeyPatch, factory: str
) -> None:
    import importlib

    import awa.persistence.ledger as _ledger

    upgraded: list[tuple[str, list[str]]] = []

    async def upgrade(self: _ledger.PostgresLedger) -> list[object]:
        upgraded.append((self._schema, [m.name for m in self._migrations]))
        return []

    monkeypatch.setattr(_ledger.PostgresLedger, "upgrade", upgrade)
    module, name = factory.split(":")
    _fake_pools(monkeypatch)
    store = getattr(importlib.import_module(module), name)("postgresql://fake", schema="awa_store")

    await store.setup()
    await store.close()

    assert upgraded and set(map(str, upgraded)) == {
        str(("awa_store", ["runs", "threads", "run_log"]))
    }


# ---------------------------------------------------------------------- real database


@pytest.mark.postgres
async def test_postgres_tables_against_a_database(postgres_dsn: str) -> None:
    table = f"core_{uuid4().hex[:10]}"

    class Notes(_Tables):
        def _ddl(self) -> list[str]:
            return [f"CREATE TABLE IF NOT EXISTS {self.schema}.{table} (id TEXT PRIMARY KEY)"]

    first, second = Notes(postgres_dsn, schema="awa_test"), Notes(postgres_dsn, schema="awa_test")
    try:
        # Two objects setting up at once: the advisory lock serialises the DDL.
        await asyncio.gather(first.setup(), second.setup())
        async with await first._connection() as conn:
            await conn.execute(f"INSERT INTO awa_test.{table} VALUES (%s), (%s)", ("a", "b"))
        async with await second._connection() as conn:
            rows = await fetch_all(conn, f"SELECT id FROM awa_test.{table} ORDER BY id")
            one = await fetch_one(conn, f"SELECT id FROM awa_test.{table} WHERE id = %s", ("b",))
        assert rows == [("a",), ("b",)]
        assert one == ("b",)
    finally:
        async with await first._connection() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS awa_test.{table}")
        await first.close()
        await second.close()
