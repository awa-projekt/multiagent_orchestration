"""Check what a design says about the data against the profile (`DF-V-NNN` issues)."""

from __future__ import annotations

import difflib
import fnmatch
import functools
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from awa.data._model import (
    ColumnProfile,
    ColumnType,
    DataIssue,
    DataProfile,
    DataRef,
    FileProfile,
    TableProfile,
    column_key,
)

_LISTED = 5
_SCAN_ROUTE = re.compile(
    r"\b(hold(s|ing)?|manual(ly)?|manuell\w*|ocr|vision|escalat\w*|no text layer|scanned"
    r"|image[- ]only)\b",
    re.IGNORECASE,
)
_PDF_TEXT_TOOLS = ("extract_pdf_text", "read_pdf", "pdf_text")
_HEADER_ROW = re.compile(r"header_row\s*[=:]?\s*(\d+)", re.IGNORECASE)
_NAMED = r"""\b{}\s*[=:]\s*(?:'([^'\n]+)'|"([^"\n]+)"|`([^`\n]+)`|([^\s,;)'"`]+))"""
_SHEET = re.compile(_NAMED.format("sheet"), re.IGNORECASE)
_PATH = re.compile(_NAMED.format("path"), re.IGNORECASE)
_READABLE = (".csv", ".tsv", ".xlsx", ".xlsm", ".pdf", ".docx", ".json", ".jsonl", ".db", ".sqlite")


def listing(names: Sequence[str], limit: int = _LISTED) -> str:
    """`'a', 'b' and 3 more`: at most `limit` names, quoted."""

    shown = ", ".join(f"'{name}'" for name in names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def closest(name: str, candidates: Sequence[str], *, cutoff: float = 0.6) -> str | None:
    """The candidate that reads most like `name` (case and punctuation ignored), or `None` when
    none is at least `cutoff` (0 to 1) similar."""

    keys = {column_key(candidate) or candidate.lower(): candidate for candidate in candidates}
    wanted = column_key(name) or name.lower()
    found = difflib.get_close_matches(wanted, list(keys), n=1, cutoff=cutoff)
    return keys[found[0]] if found else None


def find_file(profile: DataProfile, name: str) -> FileProfile | None:
    """The profiled file a design means by `name`: an id, a path, a base name, or a path that
    ends with the profiled path (`data/files/bank.csv` finds `bank.csv`). `None` if none or if
    several files fit."""

    wanted = name.replace("\\", "/").removeprefix("./")
    found = profile.find_file(wanted)
    if found is not None:
        return found
    ending = [f for f in profile.files if wanted.lower().endswith("/" + f.path.lower())]
    return ending[0] if len(ending) == 1 else None


def sheet_names(file: FileProfile) -> list[str]:
    """The sheets of a workbook (also the ones holding no table), or the tables of a database."""

    if file.kind == "sqlite":
        return [table.name for table in file.tables if table.name]
    names = [sheet.name for sheet in file.xlsx.sheets] if file.xlsx else []
    return names + [t.sheet for t in file.tables if t.sheet and t.sheet not in names]


def tables_of(file: FileProfile, sheet: str | None) -> list[TableProfile]:
    """The tables a tool reads for `sheet`: its tables, or without a sheet the first sheet's
    tables of a workbook (what `read_excel` reads) and every table of the other kinds."""

    if sheet is None:
        first = file.tables[0].sheet if file.kind == "xlsx" and file.tables else None
        return [t for t in file.tables if file.kind != "xlsx" or t.sheet == first]
    return [t for t in file.tables if (t.sheet or t.name) == sheet]


def record_tables(file: FileProfile, sheet: str | None) -> list[TableProfile]:
    """`tables_of`, and for a JSON or JSONL file its records as one table: every path that holds
    a single value (no `[]` in it) is a column, named by its path (`address.city`)."""

    found = tables_of(file, sheet)
    if found or file.json_doc is None or sheet is not None:
        return found
    columns = [
        ColumnProfile(
            name=item.path,
            key=column_key(item.path),
            index=index,
            type=_json_type(item.types),
            nullable="null" in item.types or item.optional_share > 0,
        )
        for index, item in enumerate(i for i in file.json_doc.paths if "[]" not in i.path)
    ]
    return [TableProfile(id=f"{file.id}.t1", rows=file.json_doc.records or 0, columns=columns)]


def check_references(
    profile: DataProfile, refs: Sequence[DataRef], *, where: str = "refs"
) -> list[DataIssue]:
    """Check files, sheets, columns and header rows a design names against the profile.

    Names match like the data tools match them (case and punctuation do not matter); unknown ones
    come with the closest real name as `suggestion`.

    Returns:
        DF-V-001 (file), DF-V-002 (sheet), DF-V-003 (column), DF-V-005 (header row) issues.

    Example:
        ```python
        for issue in check_references(p, [DataRef(file="orders.csv", columns=["Amount"])]):
            print(issue.code, issue.message)  # DF-V-003 ... did you mean 'Amount_EUR'?
        ```
    """

    issues: list[DataIssue] = []
    for number, ref in enumerate(refs):
        at = f"{where}[{number}]"
        file = find_file(profile, ref.file)
        if file is None:
            paths = [f.path for f in profile.files]
            issues.append(
                issue(
                    "DF-V-001",
                    f"{at}.file",
                    f"file '{ref.file}' is not one of the profiled files ({listing(paths, 8)})",
                    _closest_file(ref.file, paths),
                )
            )
            continue
        if file.status in ("error", "unsupported"):
            reason = file.flags[0].message if file.flags else file.status
            issues.append(
                issue(
                    "DF-V-001",
                    f"{at}.file",
                    f"file '{file.path}' is profiled but cannot be used ({file.status}: "
                    f"{reason}); fix or replace the file first",
                )
            )
            continue
        issues.extend(_check_ref(file, ref, at))
    return issues


def routes_scans(text: str) -> bool:
    """Whether a text says what happens to documents that cannot be read as text (hold for
    manual entry, OCR, vision, escalate)."""

    return _SCAN_ROUTE.search(text) is not None


def reads_pdf_text(tools: Iterable[str]) -> bool:
    """Whether any tool reference (`builtin:extract_pdf_text`, `connector:files.read_pdf`) reads
    the text layer of a PDF."""

    return any(tool.rsplit(":", 1)[-1].rsplit(".", 1)[-1] in _PDF_TEXT_TOOLS for tool in tools)


def check_scan_route(
    profile: DataProfile, files: Sequence[str] | None, *, routed: bool, path: str
) -> list[DataIssue]:
    """Check that a design which reads PDF text says what happens to scans.

    Args:
        files: The files the design reads text from; `None` means every PDF of the profile.
        routed: Whether the design says what happens to documents without a text layer.

    Returns:
        DF-V-006 naming scanned (error) and partly scanned (warning) files when `routed` is false.
    """

    if routed:
        return []
    chosen = profile.files if files is None else [find_file(profile, name) for name in files]
    pdfs = [f for f in chosen if f is not None and f.pdf is not None]
    issues = []
    kinds: tuple[tuple[str, Literal["error", "warning"], str], ...] = (
        ("scanned", "error", "has no text layer"),
        ("mixed", "warning", "has pages without a text layer"),
    )
    for state, severity, what in kinds:
        names = [f.path for f in pdfs if f.pdf and f.pdf.document_class == state]
        if names:
            message = (
                f"the design reads PDF text but {listing(names)} {what} (a scan); say what "
                "happens to such documents: hold them for manual entry, use OCR or vision. "
                "Never extract values from nothing"
            )
            issues.append(issue("DF-V-006", path, message, severity=severity))
    return issues


def path_matches(pattern: str, path: str) -> bool:
    """Whether a design path (a file, glob or folder) names the profiled `path`, ignoring case."""

    wanted = path.lower().split("/")
    parts = pattern.replace("\\", "/").removeprefix("./").rstrip("/").lower().split("/")
    for start in range(len(parts)):
        tail = parts[start:]
        if any(_segments_match(tail, wanted[:k]) for k in range(1, len(wanted) + 1)):
            return True
    return False


def check_case_files(profile: DataProfile, files: Mapping[str, Sequence[str]]) -> list[DataIssue]:
    """Check the input files eval cases name (`{"cases[0]": ["orders.csv"]}`): DF-V-009 for each
    one the profile does not know, with the closest profiled name."""

    issues = []
    paths = [f.path for f in profile.files]
    for where, names in files.items():
        for position, name in enumerate(names):
            if find_file(profile, name) is None:
                message = (
                    f"the eval case uses the input file '{name}', which is not one of the "
                    f"profiled files ({listing(paths, 8)})"
                )
                near = _closest_file(name, paths)
                issues.append(issue("DF-V-009", f"{where}.files[{position}]", message, near))
    return issues


def check_outputs(profile: DataProfile, targets: Mapping[str, str]) -> list[DataIssue]:
    """Check that nothing the process writes is also a file it reads: DF-V-012 for every
    `{location: path}` whose path (or glob) matches a profiled file."""

    issues = []
    for where, target in targets.items():
        clash = next((f.path for f in profile.files if path_matches(target, f.path)), None)
        if clash is not None:
            message = (
                f"the output '{target}' is also an input file ('{clash}'); a process must never "
                "overwrite the data it reads: write results to a new file under data/output/"
            )
            issues.append(issue("DF-V-012", where, message))
    return issues


def check_instruction_text(profile: DataProfile, text: str, *, path: str) -> list[DataIssue]:
    """Check `sheet=`, `header_row=` and `path=` in instructions against the profile (DF-V-011)."""

    issues: list[DataIssue] = []
    workbook_sheets = [n for f in profile.files if f.kind == "xlsx" for n in sheet_names(f)]
    for match in _SHEET.finditer(text):
        name = _quoted(match)
        if workbook_sheets and name not in workbook_sheets:
            message = (
                f"the instructions read sheet '{name}', but the workbooks have "
                f"{listing(workbook_sheets)}"
            )
            issues.append(issue("DF-V-011", path, message, closest(name, workbook_sheets)))
    paths = [f.path for f in profile.files]
    for match in _PATH.finditer(text):
        name = _quoted(match)
        if name.lower().endswith(_READABLE) and find_file(profile, name) is None:
            message = (
                f"the instructions read '{name}', which is not one of the profiled files "
                f"({listing(paths, 8)})"
            )
            near = _closest_file(name, paths)
            issues.append(issue("DF-V-011", path, message, near, "warning"))
    mentions = _mentions(profile, text)
    previous = 0
    for match in _HEADER_ROW.finditer(text):
        issues += _header_row_mention(mentions, text, match, previous, path)
        previous = match.end()
    return issues


def issue(
    code: str,
    path: str,
    message: str,
    suggestion: str | None = None,
    severity: Literal["error", "warning"] = "error",
) -> DataIssue:
    """A `DataIssue` at `path`; an error unless `severity` says otherwise."""

    return DataIssue(
        code=code, severity=severity, path=path, message=message, suggestion=suggestion
    )


def _closest_file(name: str, paths: Sequence[str]) -> str | None:
    """The profiled path whose name reads most like the base name of `name`."""

    return closest(name.replace("\\", "/").rsplit("/", 1)[-1], paths)


def _json_type(types: list[str]) -> ColumnType:
    kinds = set(types) - {"null"}
    if kinds <= {"integer"} and kinds:
        return "integer"
    if kinds and kinds <= {"integer", "number"}:
        return "decimal"
    if kinds == {"boolean"}:
        return "bool"
    return "text" if len(kinds) <= 1 else "mixed"


def _check_ref(file: FileProfile, ref: DataRef, at: str) -> list[DataIssue]:
    issues: list[DataIssue] = []
    if ref.sheet is not None:
        sheets = sheet_names(file)
        if ref.sheet not in sheets:
            what = {"xlsx": "sheets", "sqlite": "tables"}.get(file.kind)
            issues.append(
                issue(
                    "DF-V-002",
                    f"{at}.sheet",
                    f"'{file.path}' has no sheet '{ref.sheet}'; "
                    + (f"its {what} are {listing(sheets)}" if sheets and what else "it has none")
                    + " (the tools need the exact name)",
                    closest(ref.sheet, sheets),
                )
            )
            return issues
    tables = tables_of(file, ref.sheet)
    if ref.header_row is not None and tables:
        found = tables[0]
        if found.header_row != ref.header_row:
            seen = (
                "found no header (the columns are named A, B, ...: header_row 0)"
                if found.header_row == 0
                else f"found the column names in row {found.header_row}"
            )
            issues.append(
                issue(
                    "DF-V-005",
                    f"{at}.header_row",
                    f"header_row {ref.header_row} for '{file.path}'"
                    + (f" sheet '{found.sheet}'" if found.sheet else "")
                    + f", but the profile {seen} (confidence {found.header_confidence:.2f})",
                    str(found.header_row),
                )
            )
    issues.extend(_check_columns(file, ref, tables, at))
    return issues


def _json_names(file: FileProfile) -> dict[str, str]:
    """The names a design may use for the fields of a JSON file, each mapped to its path: the
    whole path (`lines[].sku`) and the last part of it (`sku`), compared like column names."""

    names: dict[str, str] = {}
    for item in file.json_doc.paths if file.json_doc else []:
        leaf = re.split(r"[.\]]", item.path)[-1]
        for key in (column_key(item.path), column_key(leaf)):
            if key:  # `tags[]` has no leaf name
                names.setdefault(key, item.path)
    return names


def _check_columns(
    file: FileProfile, ref: DataRef, tables: list[TableProfile], at: str
) -> list[DataIssue]:
    """Columns of tables, or field paths of a JSON file; anything else has none."""

    if not ref.columns:
        return []
    fields = _json_names(file) if file.json_doc and not tables else {}
    if not tables and not fields:
        return [
            issue(
                "DF-V-003",
                f"{at}.columns",
                f"'{file.path}' is a {file.kind} file without columns; columns can only be "
                "named for CSV, Excel, SQLite and JSON files",
            )
        ]
    names = [c.name for t in tables for c in t.columns] or list(dict.fromkeys(fields.values()))
    what = "field" if fields else "column"
    issues = []
    for position, name in enumerate(ref.columns):
        if fields and column_key(name) in fields:
            continue
        if any(table.column(name) is not None for table in tables):
            continue
        near = closest(name, names + list(fields)) if fields else closest(name, names)
        issues.append(
            issue(
                "DF-V-003",
                f"{at}.columns[{position}]",
                f"{what} '{name}' does not exist in '{file.path}'"
                + (f" sheet '{ref.sheet}'" if ref.sheet else "")
                + f" (its {what}s: {listing(names)})",
                fields.get(column_key(near or ""), near),
            )
        )
    return issues


def _segments_match(pattern: list[str], parts: list[str]) -> bool:
    """Whether the path segments `parts` match the pattern segments (`*` stays inside a segment,
    `**` is any number of segments)."""

    @functools.cache
    def match(i: int, j: int) -> bool:
        if i == len(pattern):
            return j == len(parts)
        if pattern[i] == "**":
            return match(i + 1, j) or (j < len(parts) and match(i, j + 1))
        return j < len(parts) and fnmatch.fnmatchcase(parts[j], pattern[i]) and match(i + 1, j + 1)

    return match(0, 0)


def _quoted(match: re.Match[str]) -> str:
    return next(group for group in match.groups() if group)


def _mentions(profile: DataProfile, text: str) -> list[tuple[int, FileProfile, TableProfile]]:
    """Where the text names a sheet (its exact name) or a file (its base name, any case), in
    order of position: the sheet's table, or the first table of the file."""

    found: list[tuple[int, FileProfile, TableProfile]] = []
    for file in profile.files:
        base = file.path.rsplit("/", 1)[-1]
        if file.tables:
            found += [
                (h.start(), file, file.tables[0]) for h in re.finditer(re.escape(base), text, re.I)
            ]
        for table in file.tables:
            if table.sheet:
                found += [
                    (h.start(), file, table) for h in re.finditer(re.escape(table.sheet), text)
                ]
    return sorted(found, key=lambda item: item[0])


def _header_row_mention(
    mentions: list[tuple[int, FileProfile, TableProfile]],
    text: str,
    match: re.Match[str],
    previous: int,
    path: str,
) -> list[DataIssue]:
    """`header_row=N` compared with the closest sheet or file named before it."""

    floor = max(previous, match.start() - 200, text.rfind("\n\n", 0, match.start()) + 1)
    near = [item for item in mentions if floor <= item[0] < match.start()]
    if not near:
        return []
    _, file, table = near[-1]
    wanted = int(match[1])
    if table.header_row == wanted:
        return []
    where = f"sheet '{table.sheet}' of '{file.path}'" if table.sheet else f"'{file.path}'"
    seen = "no header row (header_row 0)" if table.header_row == 0 else f"row {table.header_row}"
    message = (
        f"the instructions say header_row {wanted} for {where}, but the profile found the "
        f"column names in {seen}"
    )
    return [issue("DF-V-011", path, message, str(table.header_row))]
