"""The data map: for every agent, which data it reads or writes, how, and whether it is live.

Made only from what the project already says, so every view shows the same words. A source
is `live` (a declared or connected system), `copy` (one of the user's files), `stand-in`
(sample records awa wrote for a system not connected yet) or `output`. Pure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from awa.blueprints._project_files import tool_name
from awa.blueprints._systems import DeclaredSystem
from awa.blueprints.schema import Blueprint

State = Literal["live", "copy", "stand-in", "output"]

_KINDS = {
    "sql": "SQL database",
    "openapi": "REST API",
    "rest": "REST API",
    "odata": "OData service",
    "graphql": "GraphQL API",
    "smtp": "email server",
    "workspace": "file share",
}
_FILE_KINDS = {
    "read_csv": "CSV file",
    "query_csv": "CSV file",
    "read_excel": "Excel workbook",
    "query_excel": "Excel workbook",
    "read_pdf": "PDF document",
    "read_pdfs": "PDF documents",
    "read_docx": "Word document",
}


@dataclass(frozen=True)
class SourceUse:
    """One agent's use of one source through one tool (`reads` or `writes`, approved or not)."""

    agent: str
    source: str
    tool: str
    reference: str
    access: Literal["reads", "writes"]
    approval: bool


@dataclass(frozen=True)
class Source:
    """One source of data of the system.

    Attributes:
        key: The connector instance (`ledger`, `files`).
        state: `live`, `copy`, `stand-in` or `output`.
        what: What it holds (tables, files or operations), shortened.
        origin: Where a copy or a stand-in came from.
        sentence: Its state in one or two plain sentences (with what to do, if anything).
        details: The exact technical names (connector, settings, tools, blueprint entry).
    """

    key: str
    name: str
    kind: str
    state: State
    what: str
    how: str
    origin: str
    sentence: str
    used_by: tuple[str, ...]
    details: tuple[str, ...]


@dataclass(frozen=True)
class DataMap:
    """The data map of a system: its sources and every agent's use of them."""

    sources: tuple[Source, ...] = ()
    uses: tuple[SourceUse, ...] = ()
    agents: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON data for a UI: `sources`, `agents` (each with its `uses`)."""

        return {
            "sources": [
                {**asdict(s), "used_by": list(s.used_by), "details": list(s.details)}
                for s in self.sources
            ],
            "agents": [
                {"name": agent, "uses": [asdict(u) for u in self.uses if u.agent == agent]}
                for agent in self.agents
            ],
        }

    def lines(self) -> list[str]:
        """The map as Markdown lines: the sources with their state, then every agent."""

        if not self.sources:
            return ["The agents use no data sources: they work on what each request carries."]
        lines = ["Where the data comes from:", ""]
        for source in self.sources:
            lines.append(f"- {source.name} ({source.kind}): {source.sentence}")
        lines += ["", "What each agent reads and writes:", ""]
        for agent in self.agents:
            uses = [u for u in self.uses if u.agent == agent]
            if not uses:
                lines.append(f"- `{agent}` uses no data source.")
                continue
            parts = []
            for use in uses:
                gate = ", a person approves each call" if use.approval else ""
                parts.append(f"{use.access} {use.source} (`{use.tool}`{gate})")
            lines.append(f"- `{agent}` " + "; ".join(parts) + ".")
        lines += ["", "Technical names:", ""]
        for source in self.sources:
            lines.append(f"- {source.name}: " + "; ".join(source.details))
        return lines


@dataclass(frozen=True)
class StandIn:
    """A system that started on a stand-in, as `CONNECT.md` describes it (settings by name only)."""

    key: str
    name: str
    connector: str | None
    settings: tuple[str, ...] = ()


def data_map(
    blueprint: Blueprint,
    *,
    declared: Sequence[DeclaredSystem] = (),
    stand_ins: Sequence[StandIn] = (),
    files: Sequence[str] = (),
    connect_hint: str = "on the Connect step",
) -> DataMap:
    """The data map of `blueprint`.

    Args:
        stand_ins: The systems that started on a stand-in (from `CONNECT.md`).
        files: The user's files in the project (`data:` paths under `data/files/` when empty).
        connect_hint: Where a person connects a stand-in, for the sentences.
    """

    live = {system.key: system for system in declared}
    standing = {item.key: item for item in stand_ins}
    gated = {approval.tool for approval in blueprint.approvals}
    listed = list(files) or [
        entry.path.removeprefix("data/files/")
        for entry in blueprint.data
        if entry.role != "output" and entry.path.startswith("data/files/")
    ]
    uses: list[SourceUse] = []
    found: dict[str, dict[str, Any]] = {}
    for spec in blueprint.agents:
        for reference in spec.tools:
            if not reference.startswith("connector:"):
                continue
            key, _, operation = reference.removeprefix("connector:").partition(".")
            options = dict(blueprint.connectors.get(key) or {})
            info = found.setdefault(
                key, _source(key, options, live, standing, listed, connect_hint)
            )
            info["operations"].add(operation)
            info["used_by"].append(spec.name)
            tool = tool_name(reference)
            uses.append(
                SourceUse(
                    agent=spec.name,
                    source=info["name"],
                    tool=tool,
                    reference=reference,
                    access="writes" if _writes(operation, options) else "reads",
                    approval=reference in gated or tool in gated,
                )
            )
    sources = tuple(
        Source(
            key=key,
            name=info["name"],
            kind=info["kind"] if key != "files" else _file_kind(info["operations"]),
            state=info["state"],
            what=info["what"],
            how=info["how"],
            origin=info["origin"],
            sentence=info["sentence"],
            used_by=tuple(dict.fromkeys(info["used_by"])),
            details=(*info["details"], "tools " + ", ".join(_tools(key, info["operations"]))),
        )
        for key, info in found.items()
    )
    return DataMap(sources, tuple(uses), tuple(spec.name for spec in blueprint.agents))


def _source(
    key: str,
    options: Mapping[str, Any],
    live: Mapping[str, DeclaredSystem],
    standing: Mapping[str, StandIn],
    files: Sequence[str],
    connect_hint: str,
) -> dict[str, Any]:
    use = str(options.get("use") or key)
    info: dict[str, Any] = {"operations": set(), "used_by": [], "origin": "", "what": ""}
    entry = f"blueprint.yaml connectors: {key}: use {use}"
    if key in live:
        system = live[key]
        info.update(
            name=system.name,
            kind=system.kind,
            state="live",
            what=system.summary(240).partition(": ")[2],
            how=_how(use, reads_only=True),
            sentence=f"live. The agents reach it {_how(use, reads_only=True)}; the test runs "
            "read it too. It was declared under Your data, so the design uses its real "
            f"{'tables' if system.is_database else 'operations'}.",
            details=[entry, "read-only", f"settings {', '.join(system.variables) or 'none'}"],
        )
        return info
    if use == "workspace":
        root = str(options.get("root") or "data")
        if key in standing:
            item = standing[key]
            kind = _KINDS.get(item.connector or "", "") or "system"
            where = f"data/{options.get('database')}" if options.get("database") else root + "/"
            info.update(
                name=item.name,
                kind=kind,
                state="stand-in",
                what=_described(options),
                how=f"a local stand-in in {where}",
                origin="sample records awa wrote from your process description",
                sentence="not connected yet. The test runs used a stand-in with sample records "
                f"awa wrote from your description ({where}). Connect it {connect_hint}; or "
                "declare it under Your data > External systems and build again, so the "
                "design uses its real structure.",
                details=[
                    entry,
                    f"stand-in {where}",
                    f"connects through {item.connector or 'a connector'}",
                    f"settings {', '.join(item.settings) or 'none'}",
                ],
            )
            return info
        if root.rstrip("/") == "data/output" and not options.get("database"):
            info.update(
                name="Results folder",
                kind="folder data/output/",
                state="output",
                how="written into the project folder data/output/",
                sentence="where the process writes its results (data/output/ in the project "
                "folder); a person approves each write.",
                details=[entry, "root data/output"],
            )
            return info
        if options.get("database"):
            name = str(options["database"])
            info.update(
                name=name,
                kind="SQLite database file",
                state="copy",
                what=_described(options),
                how=f"a copy in data/{name}, read with SQL",
                origin="copied from the file you added under Your data",
                sentence=f"your file, copied into the project (data/{name}) and read there with "
                "read-only SQL; the original is never changed.",
                details=[entry, f"database data/{name}"],
            )
            return info
        shown = ", ".join(files[:6]) + (f" and {len(files) - 6} more" if len(files) > 6 else "")
        info.update(
            name="Your files",
            kind="files",
            state="copy",
            what=shown,
            how="copies in data/files/, read by the file tools",
            origin="copied from the files you added under Your data",
            sentence=f"your files ({shown or 'data/files/'}), copied into the project "
            "(data/files/) and read there by the file tools; the originals are never changed.",
            details=[entry, f"root {root}"],
        )
        return info
    from awa.connectors import get_connector

    try:
        title = get_connector(use).title
    except Exception:  # noqa: BLE001 - an unknown connector is named by its name
        title = use
    step = standing.get(key)
    name = step.name if step else _title(options, title)
    info.update(
        name=name,
        kind=_KINDS.get(use, title),
        state="live",
        what=_described(options),
        how=_how(use, reads_only=bool(options.get("read_only"))),
        sentence=f"live. The agents reach it {_how(use, reads_only=bool(options.get('read_only')))}.",
        details=[
            entry,
            *([f"settings {', '.join(step.settings)}"] if step and step.settings else []),
        ],
    )
    return info


def _tools(key: str, operations: set[str]) -> list[str]:
    return sorted(tool_name(f"connector:{key}.{op}") if op else key for op in operations)


def _how(use: str, *, reads_only: bool) -> str:
    if use == "sql":
        return "through the SQL connector with read-only queries" + (
            "" if reads_only else "; a change waits for a person's approval"
        )
    if use in ("openapi", "rest"):
        return "through the REST connector (the operations of its API description)"
    from awa.connectors import get_connector

    try:
        return f"through the {get_connector(use).title} connector"
    except Exception:  # noqa: BLE001 - an unknown connector is named by its name
        return f"through the {use} connector"


def _writes(operation: str, options: Mapping[str, Any]) -> bool:
    if operation in ("execute", "write_file", "append_file", "write_csv", "fill_excel"):
        return True
    from awa.connectors._catalog import connector_context

    try:
        ctx = connector_context(
            "x_map", env={}, config={k: v for k, v in options.items() if k != "env"}
        )
        if ctx.spec.dynamic or not operation:
            return False
        return ctx.spec.operation(operation).write
    except Exception:  # noqa: BLE001 - unknown here: shown as a read
        return False


def _described(options: Mapping[str, Any]) -> str:
    text = str(options.get("description") or "")
    _, colon, rest = text.partition(":")
    return " ".join((rest if colon else text).split())[:240]


def _title(options: Mapping[str, Any], fallback: str) -> str:
    head, colon, _ = str(options.get("description") or "").partition(":")
    return head.strip() if colon and 0 < len(head.strip()) <= 40 else fallback


def _file_kind(operations: set[str]) -> str:
    kinds = sorted({_FILE_KINDS[op] for op in operations if op in _FILE_KINDS})
    return ", ".join(kinds) if kinds else "files"


def stand_ins_from_steps(steps: Sequence[Mapping[str, Any]]) -> list[StandIn]:
    """The stand-ins `CONNECT.md` names (its machine-readable steps)."""

    return [
        StandIn(
            key=str(step.get("key", "")),
            name=str(step.get("system", "")),
            connector=step.get("connector"),
            settings=tuple(str(name) for name in step.get("env_vars") or ()),
        )
        for step in steps
        if step.get("key")
    ]
