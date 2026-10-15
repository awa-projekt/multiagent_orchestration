"""The user's real files in a build: the profile, the user's notes and what the design is told.

The agents reach the files only through workspace connector instances: `files` (read only),
`output` (writes, each approved by a person) and one instance per SQLite file. No I/O and no
model call here.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awa._internal.util import preview
from awa.blueprints import _project_files as project_files
from awa.blueprints._schema_data import DataBlueprint, DataExpect
from awa.blueprints.ports import Mask, StagedFiles
from awa.data import DataError, DataProfile, FileProfile, TableProfile

if TYPE_CHECKING:
    from awa.blueprints._project import DataNote

DATA = "data"
"""Folder of a project that holds its data (and where `role: output` entries point)."""

FILES = "data/files"
"""Folder of a built project that holds the copies of the user's files."""

OUTPUT = "data/output"
PROFILE_FILE = "data/PROFILE.json"
"""The profile written next to the data by `awa build`; used while it matches the files."""

OUTPUT_OPERATIONS = ("write_file", "write_csv", "append_file")

_READS: Mapping[str, tuple[str, ...]] = {
    "csv": ("read_csv", "query_csv"),
    "xlsx": ("read_excel", "query_excel"),
    "pdf": ("read_pdf",),
    "docx": ("read_docx",),
    "text": ("read_file",),
    "json": ("read_file",),
    "mail": ("read_file",),
}
_USABLE = ("ok", "partial")
_MAX_NOTE = 1000
_MAX_ENTRIES = 20


@dataclass(frozen=True)
class DataConnector:
    """A workspace connector instance onto the data: its name, `connectors:` entry and operations."""

    key: str
    config: Mapping[str, Any]
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()

    def references(self) -> tuple[str, ...]:
        """The blueprint references of its tools (`connector:files.read_csv`)."""

        return tuple(f"connector:{self.key}.{op}" for op in (*self.reads, *self.writes))


@dataclass(eq=False)
class ProjectData:
    """The profile of the user's files, the user's notes and the staged copies.

    Keep it alive as long as the design that uses it: the copies belong to `files`.

    Attributes:
        profile: The profile of the copies (paths relative to the staged folder).
        notes: The user's notes by profiled file path.
        originals: Where each copy came from, by profiled file path.
        mask: What the model sees of personal data in `section`.
    """

    profile: DataProfile
    files: StagedFiles
    notes: dict[str, str]
    originals: dict[str, str]
    mask: Mask = "none"

    @property
    def connectors(self) -> tuple[DataConnector, ...]:
        """The connector instances that reach the data (`files`, `output`, one per database)."""

        return systems_from_profile(self.profile)

    def references(self) -> dict[str, str]:
        """Every tool of the data connectors: tool name (`files_read_csv`) to reference."""

        return {
            project_files.tool_name(ref): ref for c in self.connectors for ref in c.references()
        }

    def writing(self) -> set[str]:
        """The names of the tools that write (`output_write_csv`, `warehouse_execute`)."""

        return {
            project_files.tool_name(f"connector:{c.key}.{op}")
            for c in self.connectors
            for op in c.writes
        }

    def keywords(self) -> str:
        """File and column names, to look up similar packages with."""

        names = [Path(f.path).stem for f in self.profile.files]
        names += [c.name for _, table in self.profile.tables() for c in table.columns]
        return " ".join(names[:60])

    def section(self, budget_tokens: int = 6000) -> str:
        """The data for a model's brief: digest, the user's notes, the tools, how to read each file."""

        from awa.data import render_for_model

        digest = render_for_model(self.profile, budget_tokens)
        parts = [
            "## Your data (the user's real files; copies are in data/files, the originals are "
            f"never touched)\n{digest}"
        ]
        if self.notes:
            listing = "\n".join(f"- {name}: {note}" for name, note in self.notes.items())
            parts.append(f"## Notes of the user on the files (the user's own words)\n{listing}")
        tools = "\n".join([self._tools(), *self.reading()])
        parts.append(
            f"## How the files are reached (list the tools in agents' tools exactly as written)\n{tools}"
        )
        return "\n\n".join(parts)

    def _tools(self) -> str:
        from awa.connectors import get_connector

        operations = {op.name: op for op in get_connector("workspace").operations}
        lines = []
        for connector in self.connectors:
            for op in (*connector.reads, *connector.writes):
                reference = f"connector:{connector.key}.{op}"
                change = (
                    " [writes; waits for a person's approval]" if op in connector.writes else ""
                )
                lines.append(
                    f"- {reference} (tool {project_files.tool_name(reference)}){change}: "
                    f"{operations[op].description}"
                )
        return "\n".join(lines)

    def reading(self) -> list[str]:
        """Per usable file: the tool calls that read it, with the parameters the profile found."""

        lines: list[str] = _batch_reading(self.profile)
        databases = {
            (c.config["root"], c.config["database"]): c.key
            for c in self.connectors
            if "database" in c.config
        }
        for file in self.profile.files:
            if file.status not in _USABLE:
                continue
            if file.kind == "sqlite":
                key = databases.get(_database_place(file.path), "")
                lines.append(f"- {file.path}: {key}_schema, then {key}_query (read-only SQL)")
            elif file.kind == "pdf":
                scanned = file.pdf is not None and file.pdf.document_class in ("scanned", "mixed")
                note = (
                    " (scanned: pages without a text layer are read by a vision model, slower; "
                    "values under a legibility note need review)"
                    if scanned
                    else ""
                )
                lines.append(f"- {file.path}: files_read_pdf path={file.path!r}{note}")
            elif file.kind == "docx":
                lines.append(f"- {file.path}: files_read_docx path={file.path!r}")
            elif file.kind in ("csv", "xlsx"):
                lines += [f"- {file.path}: {call}" for call in _calls(file)]
                if file.kind == "xlsx":
                    lines.append(
                        f"- {file.path}: to add columns filled per row, output_fill_excel "
                        f"path={file.path!r} output='<new name>.xlsx' key_column=... once with "
                        "all rows (the source stays unchanged); it matches the keys itself and "
                        "returns the keys found in no row, so never read all rows or pass the "
                        "register's keys through an agent or a rule. Keys found in no row are "
                        "kept, with their values, on the sheet 'Unmatched keys' of the output "
                        "workbook: that sheet is the separate list of unmatched items (no other "
                        "file for them unless the user asks for one by name), and cases check "
                        "the sheet and the tool's unmatched_keys"
                    )
            elif file.kind in _READS:
                lines.append(f"- {file.path}: files_read_file path={file.path!r}")
        return lines

    def materialize(self, dest: Path, *, share: bool = True, replace: bool = False) -> None:
        """Put the files into `dest/files` and an empty `dest/output` (see `StagedFiles`)."""

        self.files.materialize(dest, share=share, replace=replace)

    def profile_json(self) -> str:
        """The profile as written to `data/PROFILE.json`: `source` is the original path of each file."""

        recorded = [
            f.model_copy(update={"source": self.originals.get(f.path)}) for f in self.profile.files
        ]
        return self.profile.model_copy(update={"files": recorded}).to_json()

    def entries(
        self, notes: Sequence[DataNote], instructions: str, *, writes_output: bool
    ) -> list[DataBlueprint]:
        """The `data:` entries: one per usable file (per sheet), or per folder and kind when there are many.

        Roles and descriptions come from the test designer's `notes`, `handling` from the user's
        notes, `expect` from the profile.
        """

        designed = {n.file: n for n in notes if resolve_file(self.profile, n.file) is not None}
        usable = [f for f in self.profile.files if f.status in _USABLE]
        taken: set[str] = set()
        entries: list[DataBlueprint] = []

        def add(
            stem: str,
            path: str,
            file: FileProfile,
            about: str = "",
            expect: DataExpect | None = None,
        ) -> None:
            note = next(
                (n for k, n in designed.items() if resolve_file(self.profile, k) is file), None
            )
            handling = " ".join(
                filter(None, [self.notes.get(file.path), note.handling if note else ""])
            )
            entries.append(
                DataBlueprint(
                    name=_unique(stem, taken),
                    path=path,
                    role=note.role if note else "input",
                    description=preview(
                        (note.description if note else "") or about or _what(file),
                        300,
                        flatten=True,
                        ellipsis="…",
                    ),
                    handling=preview(handling, 2000, flatten=True, ellipsis="…"),
                    expect=expect or DataExpect(),
                )
            )

        if len(usable) > _MAX_ENTRIES:
            groups: dict[tuple[str, str], list[FileProfile]] = {}
            for file in usable:
                folder, _, name = file.path.rpartition("/")
                groups.setdefault((folder, Path(name).suffix.lower()), []).append(file)
            for (folder, suffix), members in groups.items():
                pattern = f"{data_path(members[0]).rpartition('/')[0]}/*{suffix}"
                stem = f"{folder or 'files'}_{suffix.lstrip('.') or 'files'}"
                add(stem, pattern, members[0], f"{len(members)} {members[0].kind} files")
        else:
            for file in usable:
                tables: list[TableProfile | None] = (
                    [*file.tables] if file.kind in ("csv", "xlsx") else []
                )
                for index, table in enumerate(tables or [None]):
                    name = (
                        f"{Path(file.path).stem}_{table.sheet}"
                        if table and table.sheet and index
                        else Path(file.path).stem
                    )
                    add(
                        name,
                        data_path(file),
                        file,
                        expect=_expect(table, instructions) if table else None,
                    )
        if writes_output:
            entries.append(
                DataBlueprint(
                    name=_unique("results", taken),
                    path=OUTPUT,
                    role="output",
                    description="Where the process writes its results.",
                )
            )
        return entries

    def profile_md(self, entries: Sequence[DataBlueprint]) -> str:
        """`data/PROFILE.md`: the profile for a person, the same facts as the README section."""

        lines = self.readme(entries)
        lines[0] = "# Profile of your files"
        return "\n".join(lines)

    def readme(self, entries: Sequence[DataBlueprint]) -> list[str]:
        """The README section "Your data": what each file is, how it is read, what to watch."""

        lines = [
            "## Your data",
            "",
            "The process works on your real files. Copies are in `data/files/` (SQLite databases "
            "directly in `data/`; your originals were never touched), results go to "
            "`data/output/`, and `data/PROFILE.json` is the full profile awa made before "
            "designing anything.",
            "",
        ]
        roles = {e.path: e for e in entries}
        for file in self.profile.files:
            role = _role(roles.get(data_path(file)))
            lines.append(f"- `{file.path}` ({_what(file)}; {file.status}){role}")
            lines += [f"  - {fact}" for fact in _facts(file)]
        return [*lines, ""]


def resolve_file(profile: DataProfile, name: str) -> FileProfile | None:
    """The profiled file `name` means (id, path, or base name, ignoring case), or `None`."""

    wanted = name.strip().replace("\\", "/").lower().removeprefix("./")
    for matches in (
        [f for f in profile.files if wanted in (f.id.lower(), f.path.lower())],
        [f for f in profile.files if f.path.lower().endswith("/" + wanted)],
    ):
        if len(matches) == 1:
            return matches[0]
    return None


def resolve_notes(profile: DataProfile, notes: Mapping[str, str]) -> dict[str, str]:
    """The user's notes by profiled path; a name, base name, id or glob picks the files.

    Raises:
        DataError: A note matches no file or is empty.
    """

    resolved: dict[str, str] = {}
    for pattern, text in notes.items():
        lowered = pattern.strip().replace("\\", "/").lower()
        hits = [
            f
            for f in profile.files
            if f.id.lower() == lowered
            or fnmatch.fnmatchcase(f.path.lower(), lowered)
            or fnmatch.fnmatchcase(f.path.lower().rsplit("/", 1)[-1], lowered)
        ]
        if not hits:
            names = ", ".join(repr(f.path) for f in profile.files[:8])
            raise DataError(f"the note for {pattern!r} matches no file; the files are {names}")
        clean = " ".join(text.split())
        if not clean:
            raise DataError(f"the note for {pattern!r} is empty")
        for file in hits:
            joined = " ".join(filter(None, [resolved.get(file.path), clean]))
            resolved[file.path] = preview(joined, _MAX_NOTE, flatten=True, ellipsis="…")
    return resolved


def data_path(file: FileProfile) -> str:
    """Where the project keeps a file: `data/files/<name>`, a SQLite database `data/<name>` (the
    place the workspace connector's SQL operations look in)."""

    return f"{DATA}/{file.path}" if file.kind == "sqlite" else f"{FILES}/{file.path}"


def systems_from_profile(profile: DataProfile) -> tuple[DataConnector, ...]:
    """The connector instances that reach the files of a profile: `files` for the readable
    kinds found (its description names the columns of every table, so a query uses the real
    names), one per SQLite database, and `output`, the only place that is written."""

    usable = [f for f in profile.files if f.status in _USABLE]
    reads = ["list_files"]
    for kind in dict.fromkeys(f.kind for f in usable):
        reads += [op for op in _READS.get(kind, ()) if op not in reads]
    if sum(f.kind == "pdf" for f in usable) > 1:
        reads.insert(reads.index("read_pdf") + 1, "read_pdfs")  # many PDFs: one call reads all
    files: dict[str, Any] = {"use": "workspace", "root": FILES, "include": reads}
    tabular = [(f, t) for f in usable if f.kind in ("csv", "xlsx") for t in f.tables if t.columns]
    columns = "; ".join(
        f"{f.path}{f' sheet {t.sheet!r}' if t.sheet else ''} ({', '.join(c.name for c in t.columns)})"
        for f, t in tabular
    )
    if columns:
        files["description"] = preview(f"Columns: {columns}", 600, flatten=True, ellipsis="…")
    connectors = [DataConnector("files", files, tuple(reads))]
    taken = {"files", "output"}
    for file in usable:
        if file.kind == "sqlite":
            root, name = _database_place(file.path)
            key = _unique(re.sub(r"[^a-z0-9]+", "_", Path(name).stem.lower())[:24], taken)
            tables = "; ".join(
                f"{t.name or t.id} ({', '.join(c.name for c in t.columns)})" for t in file.tables
            )
            config = {
                "use": "workspace",
                "root": root,
                "database": name,
                "include": ["schema", "query", "execute"],
                "description": preview(
                    f"Database {name}: {tables}", 600, flatten=True, ellipsis="…"
                ),
            }
            connectors.append(DataConnector(key, config, ("schema", "query"), ("execute",)))
    workbook = any(f.kind == "xlsx" for f in usable)
    writes: tuple[str, ...] = (*OUTPUT_OPERATIONS, *(("fill_excel",) if workbook else ()))
    output: dict[str, Any] = {"use": "workspace", "root": OUTPUT, "include": list(writes)}
    if workbook:
        output["sources"] = FILES
    connectors.append(DataConnector("output", output, (), writes))
    return tuple(connectors)


def _database_place(path: str) -> tuple[str, str]:
    """The connector root and database name of a profiled SQLite file (`data/<folder>`, name)."""

    folder, _, name = path.rpartition("/")
    return (f"{DATA}/{folder}" if folder else DATA), name


def _unique(stem: str, taken: set[str]) -> str:
    """A snake_case name for `stem` that is not in `taken` (and is added to it)."""

    base = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")[:40] or "file"
    base = base if base[0].isalpha() else f"f_{base}"
    name, number = base, 1
    while name in taken:
        number += 1
        name = f"{base}_{number}"
    taken.add(name)
    return name


def _what(file: FileProfile) -> str:
    if file.pdf is not None:
        return f"{file.kind}, {file.pdf.pages} pages"
    if file.tables:
        return f"{file.kind}, {sum(t.rows for t in file.tables)} rows"
    return f"{file.kind} file"


def _role(entry: DataBlueprint | None) -> str:
    if entry is None:
        return ""
    return preview(
        f": {entry.role}" + (f", {entry.handling}" if entry.handling else ""), 240, flatten=True
    )


def _expect(table: TableProfile, instructions: str) -> DataExpect:
    """What a process can rely on in a table: its sheet, header row, number and date format, and
    the columns the instructions name."""

    decimals = {
        c.format.decimal
        for c in table.columns
        if c.type == "decimal" and c.format and c.format.decimal
    }
    dates = {
        c.date_format for c in table.columns if c.type in ("date", "datetime") and c.date_format
    }
    named = [
        c.name
        for c in table.columns[:200]
        if re.search(rf"(?<!\w){re.escape(c.name)}(?!\w)", instructions, re.IGNORECASE)
    ]
    return DataExpect(
        columns=named[:30],
        sheet=table.sheet,
        header_row=table.header_row,
        decimal="," if decimals == {","} else "." if decimals == {"."} else None,
        date_format=dates.pop() if len(dates) == 1 else None,
        min_rows=1 if table.rows else None,
    )


def _calls(file: FileProfile) -> list[str]:
    """The tool calls that read the tables of a csv or xlsx file, with what the profile found."""

    calls = []
    for table in file.tables:
        decimals = {c.format.decimal for c in table.columns if c.type == "decimal" and c.format}
        if file.kind == "xlsx":
            where = f" sheet={table.sheet!r}" if table.sheet else ""
            call = (
                f"files_read_excel / files_query_excel path={file.path!r}{where} "
                f"header_row={table.header_row}"
            )
        else:
            delimiter = file.text.delimiter if file.text else None
            call = f"files_read_csv / files_query_csv path={file.path!r}"
            call += f" delimiter={delimiter!r}" if delimiter and delimiter != "," else ""
            call += f" decimal={next(iter(decimals))!r}" if len(decimals) == 1 else ""
        if table.totals_rows:
            call += f"; row(s) {', '.join(map(str, table.totals_rows))} are totals, not data"
        calls.append(call)
    return calls


def _facts(file: FileProfile) -> list[str]:
    """What the profile decided about a file, as plain sentences for the README."""

    facts = []
    if file.text and file.text.encoding:
        delimiter = f", delimiter {file.text.delimiter!r}" if file.text.delimiter else ""
        facts.append(f"read as {file.text.encoding}{delimiter}")
    for table in file.tables:
        where = f"sheet {table.sheet!r}, " if table.sheet else ""
        header = f"column names in row {table.header_row}" if table.header_row else "no header row"
        facts.append(f"{where}{table.rows:,} rows, {header}")
        if table.totals_rows:
            rows = ", ".join(map(str, table.totals_rows))
            facts.append(f"totals row(s) {rows} are left out of the rows")
        for column in table.columns:
            if column.format and column.format.decimal == ",":
                facts.append(f"`{column.name}`: decimal comma")
            if column.date_format:
                facts.append(f"`{column.name}`: dates as {column.date_format}")
            if column.ambiguous:
                facts.append(f"`{column.name}`: ambiguous format; check before relying on it")
    facts += [flag.message for flag in file.flags if flag.severity != "info"][:3]
    return [preview(fact, 240, flatten=True, ellipsis="…") for fact in facts][:8]


def _batch_reading(profile: DataProfile) -> list[str]:
    """For several usable PDFs: one `files_read_pdfs` call per folder that reads them all (scans
    in parallel) instead of one call per file."""

    folders: dict[str, list[FileProfile]] = {}
    for file in profile.files:
        if file.kind == "pdf" and file.status in _USABLE:
            folders.setdefault(file.path.rpartition("/")[0], []).append(file)
    if sum(len(files) for files in folders.values()) < 2:
        return []
    lines = []
    for folder, files in folders.items():
        pattern = f"{folder}/*.pdf" if folder else "*.pdf"
        scanned = sum(
            f.pdf is not None and f.pdf.document_class in ("scanned", "mixed") for f in files
        )
        note = f", {scanned} scanned" if scanned else ""
        lines.append(
            f"- {pattern} ({len(files)} PDFs{note}): files_read_pdfs paths=[{pattern!r}] reads "
            "them all in one call, scanned pages in parallel; prefer it to one files_read_pdf "
            "call per file"
        )
    return lines
