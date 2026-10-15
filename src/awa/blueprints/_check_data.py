"""Checks of the blueprint's `data:` section against a profile (see `Blueprint.check`).

Reads data only through a `DataReader`, when a composite key needs the rows.
"""

from __future__ import annotations

import re

from awa.blueprints._project_data import DATA
from awa.blueprints._schema_data import DataBlueprint
from awa.blueprints.errors import BlueprintIssue, Severity
from awa.blueprints.schema import Blueprint
from awa.data import (
    ColumnProfile,
    DataError,
    DataIssue,
    DataProfile,
    DataReader,
    DataRef,
    FileProfile,
    RowLimitError,
    TableProfile,
    check_instruction_text,
    check_outputs,
    check_references,
    check_scan_route,
    local_reader,
    reads_pdf_text,
    routes_scans,
)
from awa.data._validate import closest, listing, path_matches, tables_of

_BACKTICKED = re.compile(r"`([^`\n]{1,60})`")
_COLUMN_WORD = re.compile(
    r"(?:columns?|fields?|spalten?|felder?|feld)\s+`([^`\n]{1,60})`", re.IGNORECASE
)
_MAX_PER_ENTRY = 8
_MAX_KEY_ROWS = 200_000
_WITH_HINT = ("DF-V-001", "DF-V-002", "DF-V-003", "DF-V-009", "DF-V-011")


def blueprint_issue(issue: DataIssue, path: str | None = None) -> BlueprintIssue:
    """The blueprint issue for a data issue: same severity, the code in brackets at the end, and
    the closest real name for unknown files, sheets and columns."""

    hint = (
        f"; did you mean '{issue.suggestion}'?"
        if issue.suggestion and issue.code in _WITH_HINT and not issue.suggestion.isdigit()
        else ""
    )
    severity: Severity = "error" if issue.severity == "error" else "warning"
    return BlueprintIssue(severity, path or issue.path, f"{issue.message}{hint} [{issue.code}]")


def check_data(
    blueprint: Blueprint, profile: DataProfile, *, reader: DataReader | None = None
) -> list[BlueprintIssue]:
    """Check the `data:` section of `blueprint` against the profile of its files.

    An entry matches a profiled file when its path ends with the profiled path; globs and
    folders work. Issues are located at `data[i]...` and `agents[i].instructions`.

    Args:
        reader: Where rows come from when a check needs them; default the profiled local files.
    """

    if not blueprint.data:
        return []
    return _Checker(blueprint, profile, reader or local_reader()).run()


class _Checker:
    def __init__(self, blueprint: Blueprint, profile: DataProfile, reader: DataReader) -> None:
        self._bp = blueprint
        self._profile = profile
        self._reader = reader
        self._issues: list[BlueprintIssue] = []
        self._matches: dict[int, list[FileProfile]] = {}

    def _add(self, severity: Severity, path: str, message: str) -> None:
        self._issues.append(BlueprintIssue(severity, path, message))

    def run(self) -> list[BlueprintIssue]:
        names: dict[str, int] = {}
        for index, entry in enumerate(self._bp.data):
            if entry.name in names:
                self._add(
                    "error",
                    f"data[{index}].name",
                    f"duplicate data name {entry.name!r} (also data[{names[entry.name]}])",
                )
            names.setdefault(entry.name, index)
            if entry.role != "output":
                self._matches[index] = [
                    f for f in self._profile.files if path_matches(entry.path, f.path)
                ]
        for index, entry in enumerate(self._bp.data):
            if entry.role == "output":
                self._check_output(index, entry)
            else:
                self._check_input(index, entry)
        self._check_instructions()
        return self._issues

    def _check_input(self, index: int, entry: DataBlueprint) -> None:
        at = f"data[{index}]"
        files = self._matches.get(index, [])
        if not files:
            message = f"path {entry.path!r} matches no file in the profile [DF-V-001]"
            self._add("error", f"{at}.path", message)
            return
        start = len(self._issues)
        for file in files:
            self._check_file(at, entry, file)
            if len(self._issues) - start >= _MAX_PER_ENTRY:
                self._add(
                    "warning", at, f"more problems follow for {entry.path!r}; fix these first"
                )
                break

    def _check_file(self, at: str, entry: DataBlueprint, file: FileProfile) -> None:
        expect = entry.expect
        ref = DataRef(
            file=file.path,
            sheet=expect.sheet,
            columns=[*expect.columns, *expect.key],
            header_row=expect.header_row,
        )
        issues = check_references(self._profile, [ref], where="r")
        for issue in issues:
            place = self._location(at, issue.path, len(expect.columns))
            self._issues.append(blueprint_issue(issue, place))
        tables = tables_of(file, expect.sheet)
        if not tables or any(i.code in ("DF-V-001", "DF-V-002") for i in issues):
            return
        table = tables[0]
        if expect.key:
            self._check_key(at, file, table, expect.key)
        if expect.min_rows is not None and table.rows < expect.min_rows:
            self._add(
                "error",
                f"{at}.expect.min_rows",
                f"'{file.path}' has {table.rows} rows but min_rows is {expect.min_rows}",
            )
        self._check_formats(at, entry, file, table)

    @staticmethod
    def _location(at: str, path: str, columns: int) -> str:
        """`r[0].columns[3]` of `check_references` as a place in the YAML (`expect.key[1]`);
        the first `columns` columns are `expect.columns`, the rest `expect.key`."""

        tail = path.split(".", 1)[1]
        if tail == "file":
            return f"{at}.path"
        if not tail.startswith("columns["):
            return f"{at}.expect.{tail}"
        position = int(tail.removeprefix("columns[").removesuffix("]"))
        if position < columns:
            return f"{at}.expect.columns[{position}]"
        return f"{at}.expect.key[{position - columns}]"

    def _check_key(self, at: str, file: FileProfile, table: TableProfile, key: list[str]) -> None:
        found = [column for name in key if (column := table.column(name)) is not None]
        if len(found) != len(key):
            return  # unknown columns are reported already
        keys = {column.key for column in found}
        if any(column.unique for column in found) or any(
            set(candidate) <= keys for candidate in table.key_candidates
        ):
            return
        if len(found) == 1:
            column = found[0]
            self._add(
                "error",
                f"{at}.expect.key",
                f"key column '{column.name}' of '{file.path}' is not unique "
                f"({column.distinct} distinct values in {table.rows} rows)",
            )
        else:
            self._check_composite_key(at, file, table, found)

    def _check_composite_key(
        self, at: str, file: FileProfile, table: TableProfile, columns: list[ColumnProfile]
    ) -> None:
        place = {column.key: position for position, column in enumerate(table.columns)}
        positions = [place[column.key] for column in columns]
        label = f"the key ({', '.join(column.name for column in columns)}) of '{file.path}'"
        seen: set[tuple[str, ...]] = set()
        try:
            for row in self._reader.rows(file, table, max_rows=_MAX_KEY_ROWS):
                value = tuple(str(row[p]).strip() for p in positions)
                if value in seen:
                    self._add(
                        "error",
                        f"{at}.expect.key",
                        f"{label} is not unique: the combination {value} occurs more than once",
                    )
                    return
                seen.add(value)
        except RowLimitError:
            self._add("warning", f"{at}.expect.key", f"{label} was not verified: too many rows")
        except DataError as error:
            self._add("warning", f"{at}.expect.key", f"{label} was not verified: {error}")

    def _check_formats(
        self, at: str, entry: DataBlueprint, file: FileProfile, table: TableProfile
    ) -> None:
        expect = entry.expect
        named = [c for name in expect.columns if (c := table.column(name)) is not None]
        columns = named or table.columns
        if expect.decimal is not None:
            clash = [
                c
                for c in columns
                if c.format and c.format.decimal not in (None, expect.decimal) and not c.ambiguous
            ]
            for column in clash[:3]:
                found = column.format.decimal if column.format else ""
                example = f" (for example '{column.examples[0]}')" if column.examples else ""
                self._add(
                    "error",
                    f"{at}.expect.decimal",
                    f"decimal '{expect.decimal}' expected, but '{column.name}' of '{file.path}' "
                    f"is written with '{found}'{example}",
                )
        else:
            comma = [
                c
                for c in named
                if (c.format and c.format.decimal == ",") or (c.ambiguous and c.type == "decimal")
            ]
            self._unstated(at, "decimal", file, comma)
        if expect.date_format is not None:
            wanted = re.sub(r"\s+", "", expect.date_format).upper()
            for column in columns:
                found = re.sub(r"\s+", "", column.date_format or "").upper()
                if found and found != wanted and not column.ambiguous:
                    self._add(
                        "error",
                        f"{at}.expect.date_format",
                        f"date format '{expect.date_format}' expected, but '{column.name}' of "
                        f"'{file.path}' is written as '{column.date_format}'",
                    )
        else:
            dates = [c for c in named if c.ambiguous and c.type in ("date", "datetime")]
            self._unstated(at, "date_format", file, dates)

    def _unstated(
        self, at: str, field: str, file: FileProfile, columns: list[ColumnProfile]
    ) -> None:
        if columns:
            self._add(
                "warning",
                f"{at}.expect.{field}",
                f"{listing([c.name for c in columns])} in '{file.path}' can be read in more than "
                f"one way; state `{field}` so the process does not guess (4.250,00 is not 4.25) "
                "[DF-V-007]",
            )

    def _check_output(self, index: int, entry: DataBlueprint) -> None:
        at = f"data[{index}].path"
        text = entry.path.replace("\\", "/").removeprefix("./")
        if text != DATA and not text.startswith(DATA + "/"):
            self._add(
                "error",
                at,
                f"the output path {entry.path!r} must be inside the project's {DATA}/ "
                f"folder (results go to {DATA}/output/)",
            )
        for issue in check_outputs(self._profile, {at: text}):
            self._issues.append(blueprint_issue(issue))

    def _check_instructions(self) -> None:
        """What the agents' instructions say about the files: scans, tool parameters, names."""

        profiled = self._profile
        if not profiled.files:
            return
        texts = [agent.instructions or agent.description for agent in self._bp.agents]
        routed = routes_scans(" ".join([self._bp.description, *texts]))
        for index, (agent, text) in enumerate(zip(self._bp.agents, texts, strict=True)):
            at = f"agents[{index}].instructions"
            found = check_instruction_text(profiled, text, path=at)
            if reads_pdf_text(agent.tools):
                found += check_scan_route(profiled, None, routed=routed, path=at)
            self._issues += [blueprint_issue(issue) for issue in found]
            self._check_names(at, text)

    def _check_names(self, at: str, text: str) -> None:
        """Backticked names that are no column of a declared file (a probable misspelling)."""

        profiled = self._profile
        columns = {c.name for _, t in profiled.tables() for c in t.columns}
        known = {c.lower() for c in columns} | {
            c.key for _, t in profiled.tables() for c in t.columns
        }
        known |= {s.lower() for f in profiled.files for s in (f.path, f.path.rsplit("/", 1)[-1])}
        known |= {(t.sheet or t.name or "").lower() for _, t in profiled.tables()}
        named = {m[1].strip() for m in _COLUMN_WORD.finditer(text)}
        for token in sorted(named | {m[1].strip() for m in _BACKTICKED.finditer(text)}):
            if token.lower() in known:
                continue
            guess = closest(token, sorted(columns), cutoff=0.6 if token in named else 0.8)
            if token in named or guess is not None:
                self._add(
                    "warning",
                    at,
                    f"`{token}` is not a column of any declared data file"
                    + (f"; did you mean '{guess}'?" if guess else ""),
                )
