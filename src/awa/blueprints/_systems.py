"""Systems the user declared under "Your data": what a design may use live, read-only.

A declaration holds the connector, the names of its `.env` settings (never values) and the
structure read once the connection test passed. Example rows are kept only when the user
allowed samples, and are masked like the user's files. Pure: JSON in, JSON out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from awa._internal.util import preview
from awa.blueprints.ports import Mask
from awa.errors import ConfigurationError

SYSTEMS_FILE = "data/SYSTEMS.json"
"""Where a project keeps its declared systems (no secret is ever in it)."""

FORMAT = "awa.data.systems/v1"

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_RESERVED = frozenset({"files", "output", "workspace"})
_MAX_LINE = 600
_MAX_TABLES_SHOWN = 40
_MAX_OPERATIONS_SHOWN = 60
_SQL_READS = ("schema", "query")


class SystemTable(BaseModel):
    """A table or view of a declared database, as its catalog describes it (no row values,
    except `sample` when the user allowed samples)."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: Literal["table", "view"] = "table"
    rows: int | None = Field(default=None, description="Approximate number of rows.")
    columns: tuple[str, ...] = Field(default=(), description="`name type [NOT NULL]` each.")
    keys: tuple[str, ...] = ()
    sample_columns: tuple[str, ...] = ()
    sample_rows: tuple[tuple[Any, ...], ...] = ()


class SystemOperation(BaseModel):
    """One operation of a declared API or connector."""

    model_config = ConfigDict(frozen=True)

    name: str
    summary: str = ""
    method: str = ""
    path: str = ""
    write: bool = False


class DeclaredSystem(BaseModel):
    """A system the user declared under "Your data", with the structure awa read from it.

    Attributes:
        key: The connector instance name (snake_case), e.g. `ledger`.
        connector: The catalog connector it is reached through (`sql`, `openapi`, `jira`, ...).
        options: The connector options besides `use`; never a secret.
        variables: The settings it reads from `.env` (names only).
        samples: Whether example rows may be read and shown to the design model.
        warnings: What the connection test found worth knowing (a login with write rights).
        tested: What the last connection test answered (secrets removed).
    """

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    connector: str
    kind: str
    options: dict[str, Any] = Field(default_factory=dict)
    variables: tuple[str, ...] = ()
    tables: tuple[SystemTable, ...] = ()
    operations: tuple[SystemOperation, ...] = ()
    samples: bool = False
    warnings: tuple[str, ...] = ()
    tested: str = ""

    @property
    def is_database(self) -> bool:
        return self.connector == "sql"

    def reads(self) -> tuple[str, ...]:
        """The operations the agents may use: a database's `schema` and `query`, an API's
        operations that change nothing."""

        if self.is_database:
            return _SQL_READS
        return tuple(op.name for op in self.operations if not op.write)

    def references(self) -> tuple[str, ...]:
        """The blueprint references of its tools (`connector:ledger.query`)."""

        return tuple(f"connector:{self.key}.{op}" for op in self.reads())

    def connector_entry(self) -> dict[str, Any]:
        """Its `connectors:` entry: the connector, its options, read-only, and what it holds."""

        from awa.connectors import get_connector

        entry: dict[str, Any] = {"use": self.connector, **self.options, "read_only": True}
        if "description" in get_connector(self.connector).options:  # not every one takes it
            entry["description"] = self.summary()
        return entry

    def summary(self, limit: int = _MAX_LINE) -> str:
        """`Invoice ledger: tables invoices (id, vendor, ...); vendors (...)` in one line."""

        if self.tables:
            parts = [
                f"{t.name} ({', '.join(c.split(' ', 1)[0] for c in t.columns)})"
                for t in self.tables
            ]
            text = f"{self.name}: tables " + "; ".join(parts)
        elif self.operations:
            text = f"{self.name}: operations " + ", ".join(op.name for op in self.operations)
        else:
            text = f"{self.name} ({self.kind})"
        return preview(text, limit, flatten=True, ellipsis="…")


class _SystemsFile(BaseModel):
    format: str = FORMAT
    systems: list[DeclaredSystem] = Field(default_factory=list)


def systems_from_json(text: str, *, where: str = SYSTEMS_FILE) -> list[DeclaredSystem]:
    """The declared systems in the JSON `text` of `data/SYSTEMS.json`.

    Raises:
        ConfigurationError: If it is not valid, or was written by a newer awa.
    """

    try:
        found = _SystemsFile.model_validate_json(text)
    except ValidationError as exc:
        raise ConfigurationError(f"{where} is not a valid list of systems: {exc}") from None
    if found.format != FORMAT:
        raise ConfigurationError(
            f"{where} was written by a different awa version ({found.format!r}); "
            "move it away and declare the systems again"
        )
    return list(found.systems)


def systems_json(systems: Iterable[DeclaredSystem]) -> str:
    """`data/SYSTEMS.json` for `systems`."""

    return _SystemsFile(systems=list(systems)).model_dump_json(indent=2) + "\n"


def system_key(name: str, taken: Iterable[str] = ()) -> str:
    """A unique instance name for the user's name (`Invoice ledger` -> `invoice_ledger`), never a reserved one.

    Raises:
        ValueError: If `name` has no letter or digit to build a name from.
    """

    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24].strip("_")
    if not base:
        raise ValueError("Give the system a name with letters, for example Invoice ledger")
    if not base[0].isalpha():
        base = f"s_{base}"[:24]
    used = set(taken) | _RESERVED
    key, number = base, 1
    while key in used:
        number += 1
        key = f"{base}_{number}"
    return key


def check_key(key: str) -> str:
    """`key` if it can name a declared system; raises `ValueError` if it is not snake_case (32 at most) or reserved."""

    if not _KEY.fullmatch(key) or key in _RESERVED:
        raise ValueError(
            f"{key!r} cannot name a system: use lowercase letters, digits and _ (not "
            "files, output or workspace)"
        )
    return key


def mask_rows(columns: Sequence[str], rows: Sequence[Sequence[Any]], mask: Mask) -> list[list[Any]]:
    """Example rows as a model may see them: `none`, `placeholders` (stable per value, e.g. `<EMAIL_1>`) or `drop`."""

    if mask == "drop":
        return []
    table = [list(row) for row in rows]
    if mask == "none":
        return table
    from awa.data import classify_pii

    numbers: dict[tuple[str, str], int] = {}
    counters: dict[str, int] = {}
    for index, column in enumerate(columns):
        values = [row[index] for row in table if index < len(row)]
        kinds = classify_pii(values, column).kinds
        if not kinds:
            continue
        label = str(kinds[0]).upper()
        for row in table:
            if index >= len(row) or row[index] in (None, ""):
                continue
            key = (label, str(row[index]).casefold())
            if key not in numbers:
                counters[label] = counters.get(label, 0) + 1
                numbers[key] = counters[label]
            row[index] = f"<{label}_{numbers[key]}>"
    return table


def systems_section(systems: Sequence[DeclaredSystem], mask: Mask = "none") -> str:
    """The "Your systems and interfaces" section of a designer's brief: each declared system,
    its tools, and its structure (example rows only where the user allowed them, masked)."""

    if not systems:
        return ""
    lines = [
        "## Your systems and interfaces (declared by the user; real, connected and reached LIVE, "
        "read-only; list their tools in agents' tools exactly as written)"
    ]
    for system in systems:
        tools = ", ".join(f"{ref} (tool {_tool(ref)})" for ref in system.references())
        lines.append(f"- {system.name} ({system.kind}), key {system.key}: {tools or 'no tools'}")
        if system.is_database:
            lines.append(
                "  The query tool takes one read-only SQL statement; the schema tool lists the "
                "tables, columns and keys."
            )
        for table in system.tables[:_MAX_TABLES_SHOWN]:
            rows = f", about {table.rows:,} rows" if table.rows is not None else ""
            keys = f"; {'; '.join(table.keys)}" if table.keys else ""
            text = f"{table.kind} {table.name}{rows}: {', '.join(table.columns)}{keys}"
            lines.append("  - " + _disarmed(preview(text, 900, flatten=True, ellipsis="…")))
            shown = mask_rows(table.sample_columns, table.sample_rows, mask)
            if system.samples and shown:
                body = "\n".join("| " + ", ".join(_cell(value) for value in row) for row in shown)
                lines.append(
                    f"    example rows (columns {', '.join(table.sample_columns)}):\n"
                    f"<<<DATA {system.key}.{table.name}>>>\n{body}\n<<<END DATA>>>"
                )
        if len(system.tables) > _MAX_TABLES_SHOWN:
            lines.append(f"  ... and {len(system.tables) - _MAX_TABLES_SHOWN} more tables")
        for op in system.operations[:_MAX_OPERATIONS_SHOWN]:
            if op.write:
                continue
            where = f" ({op.method} {op.path})" if op.method or op.path else ""
            summary = preview(f"{op.name}{where}: {op.summary}", 300, flatten=True, ellipsis="…")
            lines.append("  - " + _disarmed(summary))
    lines.append(
        "These systems are real: never describe them under systems and never invent their "
        "tables or records. Their rows are unknown to you unless example rows are shown: a case "
        "expects a value of them only when the request or the example rows hold it; otherwise "
        "check that the tool is called and judge the answer. They are read-only: a change the "
        "process makes goes to another system or to the output."
    )
    return "\n".join(lines)


def declared_tools(systems: Iterable[DeclaredSystem]) -> dict[str, str]:
    """Every tool of the declared systems: tool name (`ledger_query`) to reference."""

    return {_tool(ref): ref for system in systems for ref in system.references()}


def _tool(reference: str) -> str:
    from awa.connectors._engine import tool_name

    instance, _, operation = reference.removeprefix("connector:").partition(".")
    return tool_name(instance, operation)


def _cell(value: Any) -> str:
    return "" if value is None else _disarmed(preview(str(value), 60, flatten=True, ellipsis="…"))


def _disarmed(text: str) -> str:
    """Text read from a system, unable to open or close a `<<<DATA>>>` block."""

    return text.replace("<<<", "<< <").replace(">>>", "> >>")
