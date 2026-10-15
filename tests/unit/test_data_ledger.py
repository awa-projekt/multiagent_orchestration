"""The migration ledger: versioned plain-SQL files, one ledger table, both dialects, and the command it names."""

from __future__ import annotations

import asyncio
import re
import shlex
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

import awa
import awa.persistence.sql as _sql
from awa.cli import build_parser
from awa.errors import DataStoreError, SchemaVersionError
from awa.persistence.ledger import (
    LEDGER_TABLE,
    Migration,
    MigrationStatus,
    PostgresLedger,
    SqliteLedger,
    load_migrations,
    normalized_checksum,
    parse_migration,
    require_current,
)
from awa.persistence.sql import SqliteDatabase
from awa.testing.doubles import FakeClock


def test_every_version_exists_in_both_dialects() -> None:
    sqlite = load_migrations("sqlite")
    postgres = load_migrations("postgres")

    assert [(m.version, m.name, m.kind) for m in sqlite] == [
        (m.version, m.name, m.kind) for m in postgres
    ]
    assert [m.version for m in sqlite] == list(range(1, len(sqlite) + 1))


def test_crlf_and_a_byte_order_mark_do_not_change_the_checksum() -> None:
    text = "-- @kind additive\nCREATE TABLE t (a INTEGER)\n"

    assert normalized_checksum("\ufeff" + text.replace("\n", "\r\n")) == normalized_checksum(text)
    assert normalized_checksum(text + " ") != normalized_checksum(text)


def test_a_gap_in_the_numbers_is_refused(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("-- @kind additive\nSELECT 1\n", encoding="utf-8")
    (tmp_path / "0003_c.sql").write_text("-- @kind additive\nSELECT 1\n", encoding="utf-8")

    with pytest.raises(SchemaVersionError, match="without gaps"):
        load_migrations("sqlite", folder=tmp_path)


def test_a_file_without_kind_is_refused() -> None:
    with pytest.raises(SchemaVersionError, match="@kind"):
        parse_migration("core", "0001_a.sql", "SELECT 1\n")


def test_statements_are_split_on_the_marker_and_comments_dropped() -> None:
    migration = parse_migration(
        "core", "0001_a.sql", "-- @kind breaking\n-- a note\nSELECT 1\n-- @statement\nSELECT 2\n"
    )

    assert migration.kind == "breaking"
    assert [s.splitlines()[-1] for s in migration.statements] == ["SELECT 1", "SELECT 2"]


# ---------------------------------------------------------------------- SQLite


def _files(folder: Path, *bodies: tuple[str, str]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for number, (kind, body) in enumerate(bodies, start=1):
        (folder / f"{number:04d}_m{number}.sql").write_text(
            f"-- @kind {kind}\n{body}\n", encoding="utf-8", newline="\n"
        )
    return folder


def _ledger(path: Path, folder: Path) -> SqliteLedger:
    conn = sqlite3.connect(path, isolation_level=None)
    return SqliteLedger(conn, load_migrations("sqlite", folder=folder))


def test_a_fresh_migrate_reaches_the_head_and_a_second_is_a_no_op(tmp_path: Path) -> None:
    database = SqliteDatabase(tmp_path / "awa.db")
    first = [(s.version, s.state) for s in database.status()]
    applied = database.upgrade()

    assert all(state == "applied" for _, state in first)
    assert [(s.version, s.state) for s in applied] == first
    assert database.path.with_name("backups").exists() is False  # nothing to back up
    database.close()


def test_a_changed_applied_file_is_refused(tmp_path: Path) -> None:
    folder = _files(tmp_path / "m", ("additive", "CREATE TABLE a (x INTEGER)"))
    _ledger(tmp_path / "x.db", folder).upgrade()
    (folder / "0001_m1.sql").write_text(
        "-- @kind additive\nCREATE TABLE a (x TEXT)\n", encoding="utf-8"
    )

    with pytest.raises(SchemaVersionError, match="checksum"):
        _ledger(tmp_path / "x.db", folder).upgrade()


def test_the_same_file_with_crlf_counts_as_unchanged(tmp_path: Path) -> None:
    folder = _files(tmp_path / "m", ("additive", "CREATE TABLE a (x INTEGER)"))
    _ledger(tmp_path / "x.db", folder).upgrade()
    path = folder / "0001_m1.sql"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes().replace(b"\n", b"\r\n"))

    found = _ledger(tmp_path / "x.db", folder).upgrade()

    assert [s.state for s in found] == ["applied"]


def test_behind_fails_ahead_additive_starts_ahead_breaking_fails(tmp_path: Path) -> None:
    one = _files(tmp_path / "one", ("additive", "CREATE TABLE a (x INTEGER)"))
    two = _files(
        tmp_path / "two",
        ("additive", "CREATE TABLE a (x INTEGER)"),
        ("additive", "CREATE TABLE b (x INTEGER)"),
    )
    three = _files(
        tmp_path / "three",
        ("additive", "CREATE TABLE a (x INTEGER)"),
        ("breaking", "CREATE TABLE b (x INTEGER)"),
    )
    _ledger(tmp_path / "x.db", one).upgrade()
    with pytest.raises(SchemaVersionError, match="behind"):
        require_current(_ledger(tmp_path / "x.db", two).status())

    _ledger(tmp_path / "x.db", two).upgrade()
    require_current(_ledger(tmp_path / "x.db", one).status())  # a newer awa added a table

    _ledger(tmp_path / "y.db", three).upgrade()
    with pytest.raises(SchemaVersionError, match="breaking"):
        require_current(_ledger(tmp_path / "y.db", one).status())


def test_a_failed_file_leaves_no_ledger_row_and_no_table(tmp_path: Path) -> None:
    folder = _files(
        tmp_path / "m", ("additive", "CREATE TABLE a (x INTEGER)\n-- @statement\nNOT SQL")
    )
    ledger = _ledger(tmp_path / "x.db", folder)

    with pytest.raises(sqlite3.OperationalError):
        ledger.upgrade()

    conn = sqlite3.connect(tmp_path / "x.db")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" not in tables
    assert conn.execute(f"SELECT COUNT(*) FROM {LEDGER_TABLE}").fetchone() == (0,)


def test_an_upgrade_of_a_file_with_data_writes_a_backup_first_and_keeps_three(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    old = _files(tmp_path / "old", ("additive", "CREATE TABLE a (x TEXT)"))
    path = tmp_path / "awa.db"
    SqliteDatabase(path, migrations_folder=old).close()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO a VALUES ('kept')")
    conn.commit()
    conn.close()
    for number in range(2, 7):
        newer = _files(
            tmp_path / f"v{number}",
            ("additive", "CREATE TABLE a (x TEXT)"),
            *(("additive", f"CREATE TABLE t{n} (x TEXT)") for n in range(2, number + 1)),
        )
        clock.advance(minutes=1)
        SqliteDatabase(path, clock=clock, migrations_folder=newer).close()

    backups = sorted((tmp_path / "backups").glob("awa-v*.db"))
    assert len(backups) == 3
    assert [b.name.split("-")[1] for b in backups] == ["v0003", "v0004", "v0005"]
    restored = sqlite3.connect(backups[0])
    assert restored.execute("SELECT x FROM a").fetchall() == [("kept",)]
    restored.close()


def test_upgrade_false_only_checks(tmp_path: Path) -> None:
    one = _files(tmp_path / "one", ("additive", "CREATE TABLE a (x INTEGER)"))
    two = _files(
        tmp_path / "two",
        ("additive", "CREATE TABLE a (x INTEGER)"),
        ("additive", "CREATE TABLE b (x INTEGER)"),
    )
    SqliteDatabase(tmp_path / "awa.db", migrations_folder=one).close()

    with pytest.raises(SchemaVersionError, match="behind"):
        SqliteDatabase(tmp_path / "awa.db", migrations_folder=two, upgrade=False)


def test_a_network_path_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(DataStoreError, match="network drive"):
        SqliteDatabase(r"\\fileserver\share\awa.db")

    monkeypatch.setattr(_sql, "is_network_path", lambda path: True)
    with pytest.raises(DataStoreError, match="network drive"):
        SqliteDatabase(tmp_path / "awa.db")
    assert not (tmp_path / "awa.db").exists()


def test_a_sqlite_library_older_than_3_38_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_sql, "sqlite_version", lambda: (3, 37, 2))

    with pytest.raises(DataStoreError, match=r"3\.37\.2.*3\.38"):
        SqliteDatabase(tmp_path / "awa.db")


def test_the_file_is_opened_with_the_safe_settings(tmp_path: Path) -> None:
    database = SqliteDatabase(tmp_path / "awa.db")

    def pragmas(conn: sqlite3.Connection) -> tuple[object, ...]:
        return tuple(
            conn.execute(f"PRAGMA {name}").fetchone()[0]
            for name in ("journal_mode", "foreign_keys", "busy_timeout", "secure_delete")
        )

    assert asyncio.run(database.read(pragmas)) == ("wal", 1, 5000, 1)
    database.close()


def test_backup_copies_everything_and_refuses_an_existing_target(tmp_path: Path) -> None:
    database = SqliteDatabase(tmp_path / "awa.db")
    asyncio.run(
        database.write(
            lambda conn: conn.execute(
                "INSERT INTO process_state (key, value, updated_at)"
                " VALUES ('k', 'v', '2026-03-02T09:00:00.000000Z')"
            )
        )
    )

    copy = database.backup(tmp_path / "copy" / "awa-backup.db")

    restored = sqlite3.connect(copy)
    assert restored.execute("SELECT value FROM process_state").fetchall() == [("v",)]
    restored.close()
    with pytest.raises(DataStoreError, match="exists"):
        database.backup(copy)
    database.close()


# ---------------------------------------------------------------------- PostgreSQL


@pytest.fixture
async def pg_pool(postgres_dsn: str) -> AsyncIterator[tuple[object, str]]:
    import psycopg

    from awa.persistence.sql import shared_pool

    schema = f"awa_test_l{uuid4().hex[:10]}"
    pool = shared_pool(postgres_dsn, max_size=4)
    await pool.open(wait=True)
    try:
        yield pool, schema
    finally:
        await pool.close()
        async with await psycopg.AsyncConnection.connect(postgres_dsn, autocommit=True) as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def _logical_schema_sqlite(path: Path) -> dict[str, object]:
    conn = sqlite3.connect(path)
    tables = sorted(
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    found: dict[str, object] = {}
    for table in tables:
        columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        keys = [c[1] for c in sorted((c for c in columns if c[5]), key=lambda c: c[5])]
        foreign = sorted(
            (row[2], row[3], row[4], row[6].upper())
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        )
        found[table] = {
            "columns": sorted(c[1] for c in columns),
            "primary_key": keys,
            "foreign_keys": foreign,
        }
    conn.close()
    return found


async def _logical_schema_postgres(pool: object, schema: str) -> dict[str, object]:
    from awa.persistence.sql import fetch_all

    async with pool.connection() as conn:  # type: ignore[attr-defined]
        tables = [
            row[0]
            for row in await fetch_all(
                conn,
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s"
                " ORDER BY table_name",
                (schema,),
            )
        ]
        found: dict[str, object] = {}
        for table in tables:
            columns = await fetch_all(
                conn,
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = %s",
                (schema, table),
            )
            keys = await fetch_all(
                conn,
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid"
                " AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary"
                " ORDER BY array_position(i.indkey, a.attnum)",
                (f"{schema}.{table}",),
            )
            foreign = await fetch_all(
                conn,
                "SELECT cl.relname, a.attname, af.attname, c.confdeltype FROM pg_constraint c"
                " JOIN pg_class cl ON cl.oid = c.confrelid"
                " JOIN LATERAL unnest(c.conkey, c.confkey) AS k(col, fcol) ON true"
                " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.col"
                " JOIN pg_attribute af ON af.attrelid = c.confrelid AND af.attnum = k.fcol"
                " WHERE c.conrelid = %s::regclass AND c.contype = 'f'",
                (f"{schema}.{table}",),
            )
            actions = {"c": "CASCADE", "n": "SET NULL", "a": "NO ACTION", "r": "RESTRICT"}
            found[table] = {
                "columns": sorted(row[0] for row in columns),
                "primary_key": [row[0] for row in keys],
                "foreign_keys": sorted((r[0], r[1], r[2], actions[r[3]]) for r in foreign),
            }
    return found


@pytest.mark.postgres
async def test_both_dialects_produce_the_same_logical_schema(
    tmp_path: Path, pg_pool: tuple[object, str]
) -> None:
    pool, schema = pg_pool
    SqliteDatabase(tmp_path / "awa.db").close()
    await PostgresLedger(pool, schema=schema, migrations=load_migrations("postgres")).upgrade()

    sqlite = _logical_schema_sqlite(tmp_path / "awa.db")
    postgres = await _logical_schema_postgres(pool, schema)

    # The SQLite side table stands in for PostgreSQL's GIN index over process_runs.wait_keys.
    assert sqlite.pop("process_run_wait_keys")
    assert sqlite == postgres
    for table, shape in postgres.items():
        assert shape["primary_key"], f"{table} has no primary key"  # type: ignore[index]


@pytest.mark.postgres
async def test_postgres_migrate_is_idempotent_and_adopts_existing_tables(
    pg_pool: tuple[object, str],
) -> None:
    pool, schema = pg_pool
    async with pool.connection() as conn:  # type: ignore[attr-defined]
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(  # the table PostgresRunStore created before the ledger existed
            f"CREATE TABLE {schema}.process_state (key TEXT PRIMARY KEY, value TEXT NOT NULL,"
            " updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        await conn.execute(f"INSERT INTO {schema}.process_state (key, value) VALUES ('k', 'v')")
    ledger = PostgresLedger(pool, schema=schema, migrations=load_migrations("postgres"))

    first = await ledger.upgrade()
    second = await ledger.upgrade()

    assert {s.state for s in first} == {"applied"}
    assert [(s.version, s.applied_at) for s in second] == [(s.version, s.applied_at) for s in first]
    async with pool.connection() as conn:  # type: ignore[attr-defined]
        rows = await (await conn.execute(f"SELECT value FROM {schema}.process_state")).fetchall()
    assert rows == [("v",)]


@pytest.mark.postgres
async def test_two_concurrent_upgrades_apply_each_migration_once(
    pg_pool: tuple[object, str],
) -> None:
    pool, schema = pg_pool
    migrations = load_migrations("postgres")

    await asyncio.gather(
        *(PostgresLedger(pool, schema=schema, migrations=migrations).upgrade() for _ in range(2))
    )

    async with pool.connection() as conn:  # type: ignore[attr-defined]
        rows = await (
            await conn.execute(f"SELECT version FROM {schema}.{LEDGER_TABLE} ORDER BY version")
        ).fetchall()
    assert [row[0] for row in rows] == [m.version for m in migrations]


SOURCE = Path(awa.__file__).parent


def hints(*files: Path) -> list[str]:
    return sorted(
        {
            match.group(1)
            for file in files
            for match in re.finditer(r"`awa (store [a-z]+[^`]*)`", file.read_text(encoding="utf-8"))
        }
    )


def test_every_store_command_named_in_a_ledger_message_parses() -> None:
    found = hints(SOURCE / "persistence" / "ledger.py", SOURCE / "errors.py")

    assert found and "store upgrade" in found
    for command in found:
        build_parser().parse_args(shlex.split(command))


def test_no_message_or_page_names_the_old_db_command() -> None:
    roots = [SOURCE, SOURCE.parents[1] / "docs"]
    offenders = [
        str(path)
        for root in roots
        for path in root.rglob("*")
        if path.suffix in {".py", ".md"} and "awa db" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_a_migration_and_its_status_share_one_label() -> None:
    migration = Migration("core", 3, "run_log", "additive", "sum", ("SELECT 1",))
    status = MigrationStatus("core", 3, "run_log", "additive", "pending")

    assert migration.label == status.label == "core/0003_run_log"
