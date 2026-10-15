"""SQL backend kit: versioned plain-SQL migrations with one ledger table, SQLite and PostgreSQL.

Files `migrations/<component>/<sqlite|postgres>/NNNN_<name>.sql` are numbered without gaps,
start with `-- @kind additive|breaking` and separate statements with `-- @statement`. Each applied
file is a ledger row with the sha256 of its LF-normalized text; every file runs in one
transaction. On live data: generated columns only in `CREATE TABLE`, foreign keys on filled
tables `NOT VALID` then `VALIDATE CONSTRAINT`.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.resources
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from awa.errors import SchemaVersionError

LEDGER_TABLE = "awa_schema_migrations"
"""The one table that records which migration files were applied."""

Dialect = Literal["sqlite", "postgres"]

_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_STATEMENT = re.compile(r"^-- @statement\s*$", re.MULTILINE)
_KIND = re.compile(r"^-- @kind (additive|breaking)\s*$", re.MULTILINE)
_LOCK_KEY = 0x6177615F6C656467  # "awa_ledg"
_LOCK_RETRIES = 5
_BOM = chr(0xFEFF)


@dataclass(frozen=True)
class Migration:
    """One migration file.

    Attributes:
        component: The group of tables it belongs to (`core`).
        kind: `additive` (older awa versions keep working) or `breaking`.
        checksum: sha256 of the LF-normalized text without a byte-order mark.
    """

    component: str
    version: int
    name: str
    kind: Literal["additive", "breaking"]
    checksum: str
    statements: tuple[str, ...]

    @property
    def label(self) -> str:
        """`component/NNNN_name`, as messages name the file."""

        return f"{self.component}/{self.version:04d}_{self.name}"

    @property
    def ledger_row(self) -> tuple[str, int, str, str, str]:
        """Its ledger values: component, version, name, kind, checksum."""

        return (self.component, self.version, self.name, self.kind, self.checksum)


@dataclass(frozen=True)
class MigrationStatus:
    """Where one migration stands in a database.

    Attributes:
        state: `applied`, `pending` (not in the database), `changed` (the file differs now) or
            `ahead` (applied by a newer awa).
        applied_at: When it was applied (text as the database returned it).
    """

    component: str
    version: int
    name: str
    kind: str
    state: Literal["applied", "pending", "changed", "ahead"]
    applied_at: str | None = None

    @property
    def label(self) -> str:
        """`component/NNNN_name`, as messages name the file."""

        return f"{self.component}/{self.version:04d}_{self.name}"


def normalized_checksum(text: str) -> str:
    """sha256 of `text` with CRLF and CR made LF and a leading byte-order mark removed."""

    return hashlib.sha256(_normalized(text).encode("utf-8")).hexdigest()


def _normalized(text: str) -> str:
    return text.lstrip(_BOM).replace("\r\n", "\n").replace("\r", "\n")


def parse_migration(component: str, file_name: str, text: str) -> Migration:
    """A `Migration` from a file's name and text.

    Raises:
        SchemaVersionError: If the name is not `NNNN_<name>.sql` or `@kind` is missing.
    """

    match = _FILE.match(file_name)
    if match is None:
        raise SchemaVersionError(f"Migration file {file_name!r} is not named NNNN_<name>.sql")
    clean = _normalized(text)
    kind = _KIND.search(clean)
    if kind is None:
        raise SchemaVersionError(f"Migration file {file_name!r} has no '-- @kind' line")
    statements = tuple(
        part.strip() for part in _STATEMENT.split(clean) if _without_comments(part).strip()
    )
    return Migration(
        component=component,
        version=int(match.group(1)),
        name=match.group(2),
        kind=kind.group(1),  # type: ignore[arg-type]
        checksum=normalized_checksum(text),
        statements=statements,
    )


def _without_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))


def load_migrations(
    dialect: Dialect, component: str = "core", *, folder: Path | None = None
) -> list[Migration]:
    """The migration files of `component` for `dialect` (or from `folder`), by version.

    Raises:
        SchemaVersionError: If a number is missing or repeated.
    """

    if folder is None:
        root = importlib.resources.files("awa.persistence") / "migrations" / component / dialect
        files = {item.name: item.read_text(encoding="utf-8") for item in root.iterdir()}
    else:
        files = {path.name: path.read_text(encoding="utf-8") for path in folder.glob("*.sql")}
    migrations = sorted(
        (parse_migration(component, name, text) for name, text in files.items()),
        key=lambda migration: migration.version,
    )
    numbers = [migration.version for migration in migrations]
    if numbers != list(range(1, len(numbers) + 1)):
        raise SchemaVersionError(
            f"The {dialect} migrations of {component!r} must be numbered 1..n without gaps, "
            f"found {numbers}"
        )
    return migrations


def statuses(
    migrations: Sequence[Migration], applied: Sequence[tuple[Any, ...]]
) -> list[MigrationStatus]:
    """Compare the files with the ledger rows `(component, version, name, kind, checksum,
    applied_at)` of their component."""

    rows = {(row[0], int(row[1])): row for row in applied}
    found: list[MigrationStatus] = []
    for migration in migrations:
        row = rows.pop((migration.component, migration.version), None)
        if row is None:
            state: Literal["applied", "pending", "changed", "ahead"] = "pending"
        else:
            state = "applied" if row[4] == migration.checksum else "changed"
        found.append(
            MigrationStatus(
                migration.component,
                migration.version,
                migration.name,
                migration.kind,
                state,
                None if row is None else str(row[5]),
            )
        )
    components = {migration.component for migration in migrations}
    for (component, version), row in sorted(rows.items()):
        if component in components:
            found.append(
                MigrationStatus(component, version, str(row[2]), str(row[3]), "ahead", str(row[5]))
            )
    return found


def require_current(found: Sequence[MigrationStatus]) -> None:
    """Accept a schema that matches the files, or is ahead with additive migrations only.

    Raises:
        SchemaVersionError: If a migration is pending or changed, or a newer awa applied a breaking
            one.
    """

    pending = [s for s in found if s.state == "pending"]
    if pending:
        raise SchemaVersionError(
            f"The database schema is behind this awa version ({len(pending)} migration(s) "
            f"pending, first {pending[0].label}). Run `awa store upgrade`."
        )
    changed = [s for s in found if s.state == "changed"]
    if changed:
        raise SchemaVersionError(
            f"The applied migration {changed[0].label} differs from its file (checksum). "
            "Restore the file of the installed version; never edit an applied migration, add a "
            "new one."
        )
    breaking = [s for s in found if s.state == "ahead" and s.kind == "breaking"]
    if breaking:
        raise SchemaVersionError(
            f"A newer awa applied the breaking migration {breaking[0].label}; upgrade awa to use "
            "this database."
        )


_SQLITE_LEDGER = (
    f"CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} ("
    " component TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,"
    " kind TEXT NOT NULL CHECK (kind IN ('additive','breaking')), checksum TEXT NOT NULL,"
    " applied_at TEXT NOT NULL, PRIMARY KEY (component, version))"
)


class SqliteLedger:
    """Applies migration files to a SQLite database (each file in `BEGIN IMMEDIATE`).

    Args:
        conn: An open connection in autocommit mode (`isolation_level=None`).
    """

    def __init__(self, conn: sqlite3.Connection, migrations: Sequence[Migration]) -> None:
        self._conn = conn
        self._migrations = list(migrations)

    def status(self) -> list[MigrationStatus]:
        """Where every migration stands."""

        self._conn.execute(_SQLITE_LEDGER)
        components = sorted({migration.component for migration in self._migrations})
        rows = self._conn.execute(
            f"SELECT component, version, name, kind, checksum, applied_at FROM {LEDGER_TABLE}"
            f" WHERE component IN ({', '.join('?' for _ in components)})",
            components,
        ).fetchall()
        return statuses(self._migrations, rows)

    def upgrade(self) -> list[MigrationStatus]:
        """Apply the pending files in order; returns the statuses afterwards.

        Raises:
            SchemaVersionError: If an applied file changed or a newer awa made a breaking change.
        """

        found = self.status()
        _refuse_changed_or_breaking(found)
        pending = {s.version for s in found if s.state == "pending"}
        for migration in self._migrations:
            if migration.version not in pending:
                continue
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                done = self._conn.execute(
                    f"SELECT 1 FROM {LEDGER_TABLE} WHERE component = ? AND version = ?",
                    (migration.component, migration.version),
                ).fetchone()
                if done is None:  # another process may have applied it meanwhile
                    for statement in migration.statements:
                        self._conn.execute(statement)
                    self._conn.execute(
                        f"INSERT INTO {LEDGER_TABLE} (component, version, name, kind, checksum,"
                        " applied_at) VALUES (?, ?, ?, ?, ?,"
                        " strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                        migration.ledger_row,
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return self.status()


def _refuse_changed_or_breaking(found: Sequence[MigrationStatus]) -> None:
    require_current([s for s in found if s.state != "pending"])


class PostgresLedger:
    """Applies migration files to a PostgreSQL schema.

    Each file runs in its own transaction with its ledger row, after `pg_advisory_xact_lock` and
    with `lock_timeout = '5s'` (a busy table is retried with backoff instead of waiting).

    Args:
        pool: The shared `AsyncConnectionPool` (autocommit connections).
        schema: The schema of awa's tables (`SET LOCAL search_path` while a file runs).
    """

    def __init__(self, pool: Any, *, schema: str, migrations: Sequence[Migration]) -> None:
        from awa.persistence.sql import check_schema

        self._pool = pool
        self._schema = check_schema(schema)
        self._migrations = list(migrations)

    def _ledger_ddl(self) -> str:
        return (
            f"CREATE TABLE IF NOT EXISTS {self._schema}.{LEDGER_TABLE} ("
            " component TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,"
            " kind TEXT NOT NULL CHECK (kind IN ('additive','breaking')), checksum TEXT NOT NULL,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (component, version))"
        )

    async def status(self) -> list[MigrationStatus]:
        """Where every migration stands (a schema without a ledger has everything pending)."""

        from awa.persistence.sql import fetch_all, fetch_one

        components = sorted({migration.component for migration in self._migrations})
        async with self._pool.connection() as conn:
            exists = await fetch_one(
                conn, "SELECT to_regclass(%s)", (f"{self._schema}.{LEDGER_TABLE}",)
            )
            if exists is None or exists[0] is None:
                rows: list[Any] = []
            else:
                rows = await fetch_all(
                    conn,
                    f"SELECT component, version, name, kind, checksum, applied_at"
                    f" FROM {self._schema}.{LEDGER_TABLE} WHERE component = ANY(%s)",
                    (components,),
                )
        return statuses(self._migrations, rows)

    async def require_current(self) -> None:
        """See `require_current`."""

        require_current(await self.status())

    async def upgrade(self) -> list[MigrationStatus]:
        """Apply the pending files in order; returns the statuses afterwards.

        Raises:
            SchemaVersionError: If an applied file changed, a newer awa made a breaking change, or a
                table lock stays busy.
        """

        found = await self.status()
        _refuse_changed_or_breaking(found)
        pending = {s.version for s in found if s.state == "pending"}
        for migration in self._migrations:
            if migration.version not in pending:
                continue
            await self._with_lock_retries(migration)
        return await self.status()

    async def _with_lock_retries(self, migration: Migration) -> None:
        from awa._internal.retry import backoff_delay
        from awa.persistence.sql import import_psycopg

        psycopg, _ = import_psycopg()
        for attempt in range(_LOCK_RETRIES):
            try:
                await self._apply_in_transaction(migration)
                return
            except psycopg.errors.LockNotAvailable:
                if attempt == _LOCK_RETRIES - 1:
                    raise SchemaVersionError(
                        f"Migration {migration.label} waited too long for a table lock (live "
                        "traffic holds it). Retry `awa store upgrade` when the database is quieter."
                    ) from None
                await asyncio.sleep(backoff_delay(attempt, initial=0.5, cap=8.0))

    async def _apply_in_transaction(self, migration: Migration) -> None:
        from awa.persistence.sql import fetch_one

        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self._schema}")
            await conn.execute(self._ledger_ddl())
            done = await fetch_one(
                conn,
                f"SELECT 1 FROM {self._schema}.{LEDGER_TABLE} WHERE component = %s AND version = %s",
                (migration.component, migration.version),
            )
            if done is not None:
                return
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            await conn.execute(f"SET LOCAL search_path TO {self._schema}, public")
            for statement in migration.statements:
                await conn.execute(statement)
            await conn.execute(
                f"INSERT INTO {self._schema}.{LEDGER_TABLE}"
                " (component, version, name, kind, checksum) VALUES (%s, %s, %s, %s, %s)",
                migration.ledger_row,
            )
