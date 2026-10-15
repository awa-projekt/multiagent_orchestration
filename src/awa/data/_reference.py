"""Checks against the real values: grounding of expected values and computed reference results."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

from awa.data._errors import DataError, RowLimitError
from awa.data._infer import _MONTHS, _year
from awa.data._model import ColumnProfile, DataIssue, DataModel, DataProfile, DataRef, TableProfile
from awa.data._validate import check_references, closest, find_file, issue, listing, record_tables
from awa.data.ports import DataReader, local_reader

_TOKEN = re.compile(r"[^\W_](?:[\w.,/\-]*[^\W_])?")
_NUMBER = re.compile(r"\d[\d.,]*\d|\d")
_NUMERIC_TOKEN = re.compile(r"[+-]?\d[\d.,]*")
_DATE_DMY = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_DATE_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DATE_SLASH = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_MAX_CELLS = 20_000_000
_MAX_JOIN_VALUES = 2_000
_AGGREGATES = ("sum", "avg", "min", "max", "count", "count_distinct")
_WHERE = re.compile(r"(?:^|\s)where\s", re.IGNORECASE)


def data_tokens(text: str) -> list[str]:
    """The parts of an expected text that claim to be data: numbers, amounts, dates and
    identifiers (a token with a digit that is at least four characters long or has a letter).
    Plain words and small counts are not data."""

    found = [
        token
        for token in _TOKEN.findall(text)
        if any(c.isdigit() for c in token) and (len(token) >= 4 or any(c.isalpha() for c in token))
    ]
    return list(dict.fromkeys(found))


def grounded(value: str, profile: DataProfile, *, reader: DataReader | None = None) -> bool:
    """Whether `value` occurs in the real data of `profile`, in any notation it knows.

    Text matches case-insensitively inside a cell, a number by value (`4250` is `4.250,00`), a date
    by day. When part of the data could not be read in full, an absent value counts as grounded.
    """

    text = value.strip()
    if not text:
        return True
    found, complete = _find(profile, [text], reader or local_reader())
    return text in found or not complete


class Claim(DataModel):
    """A value a design says occurs in the data.

    Attributes:
        path: Where the design says so; reported as the location of an issue.
        value: The expected text; its data-like parts are checked.
        request: Text in which the value may occur instead, for example the request of the case.
    """

    path: str
    value: str
    request: str = ""


def check_grounding(
    profile: DataProfile, claims: Sequence[Claim], *, reader: DataReader | None = None
) -> list[DataIssue]:
    """Check that the data-like parts of expected texts (ids, amounts, dates) occur in the data.

    Returns:
        One DF-V-008 error per ungrounded part, with the closest value of the data as suggestion.
    """

    wanted: list[tuple[Claim, str]] = []
    for claim in claims:
        context = _norm(claim.request)
        wanted += [(claim, t) for t in data_tokens(claim.value) if _norm(t) not in context]
    if not wanted:
        return []
    found, complete = _find(
        profile, list(dict.fromkeys(token for _, token in wanted)), reader or local_reader()
    )
    if not complete:
        return []  # part of the data stayed unseen: nothing can be called absent
    pool = _pool(profile)
    files = listing([f.path for f in profile.files], 6)
    return [
        issue(
            "DF-V-008",
            claim.path,
            f"'{token}' (in '{claim.value}') does not occur in the data ({files}) or in the "
            "request; expectations about data must use values of the real files",
            closest(token, pool, cutoff=0.8),
        )
        for claim, token in wanted
        if token not in found
    ]


class ReferenceCheck(DataModel):
    """An expected result a design states about the data, with how to compute it.

    `expression` uses the language of `query_csv`: `sum`, `avg`, `min`, `max`, `count`,
    `count_distinct` over one column, an optional `where` filter, arithmetic between results, and
    `values('file.csv', 'Column')` inside a filter.
    """

    path: str
    file: str
    expression: str
    expected: str
    sheet: str | None = None


def check_reference_values(
    profile: DataProfile, checks: Sequence[ReferenceCheck], *, reader: DataReader | None = None
) -> list[DataIssue]:
    """Compare the results a design expects with the ones the real rows compute to.

    Returns:
        DF-V-010 for a contradiction or an expression that cannot be evaluated (a warning when the
        data could not be read in full); DF-V-001/002/003 for an unknown file, sheet or column and
        DF-V-004 for a sum over text.

    Example:
        ```python
        check = ReferenceCheck(path="cases[0].check", file="orders.csv",
                               expression="count(where Status == 'open')", expected="9")
        check_reference_values(p, [check])  # DF-V-010: computed 6
        ```
    """

    issues: list[DataIssue] = []
    source = reader or local_reader()
    for check in checks:
        where, label = check.path, f"'{check.expression}'"
        found = check_references(profile, [DataRef(file=check.file, sheet=check.sheet)], where="r")
        if found:
            issues += [
                i.model_copy(update={"path": where + i.path.removeprefix("r[0]")}) for i in found
            ]
            continue
        try:
            value, rows = _compute(check.expression, profile, check.file, check.sheet, source)
        except _Failure as failure:
            issues.append(issue(failure.code, where, f"{label}: {failure}", failure.suggestion))
        except DataError as error:  # also: more rows than the limit
            message = f"{label} could not be verified: {error}"
            issues.append(issue("DF-V-010", where, message, severity="warning"))
        else:
            if value is not None and not _agrees(value, check.expected):
                shown = value if isinstance(value, str | int) else f"{value:.10g}"
                message = (
                    f"computed {check.expression} = {shown} over {rows} rows of '{check.file}', "
                    f"but the design expects '{check.expected}'"
                )
                issues.append(issue("DF-V-010", where, message, str(shown)))
    return issues


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _numbers(token: str) -> set[float]:
    """The numbers a token can mean (both readings of `1.234`), for plain numeric tokens."""

    from awa.kernel.conditions import as_number

    if not _NUMERIC_TOKEN.fullmatch(token):
        return set()
    values = (as_number(token, decimal) for decimal in (".", ","))
    return {round(float(v), 6) for v in values if v is not None}


def _iso(year: str, month: str, day: str) -> str | None:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _dates(token: str) -> set[str]:
    """The calendar dates a token can mean, as ISO text (`11/12/2024` is two dates)."""

    found: set[str | None] = set()
    for match in _DATE_ISO.finditer(token):
        found.add(_iso(*match.groups()))
    for match in _DATE_DMY.finditer(token):
        day, month, year = match.groups()
        found.add(_iso(year, month, day))
    for match in _DATE_SLASH.finditer(token):
        first, second, year = match.groups()
        found.update({_iso(year, second, first), _iso(year, first, second)})
    return {d for d in found if d}


_DATE_TOKENS = "YYYY|YY|MMMM|MMM|MM|DD"
_DATE_PARTS = {
    "YYYY": r"(?P<y>\d{4})",
    "YY": r"(?P<yy>\d{2})",
    "MMMM": r"(?P<n>[^\W\d_]+\.?)",
    "MMM": r"(?P<n>[^\W\d_]+\.?)",
    "MM": r"(?P<m>\d{1,2})",
    "DD": r"(?P<d>\d{1,2})",
}
"""The regular expression of each token of a column's `date_format`."""


def _cell_date(cell: str, pattern: str | None) -> str | None:
    """ISO date of a cell written like `pattern` (`DD.MM.YYYY`), or of an ISO cell."""

    iso = _DATE_ISO.match(cell)
    if iso:
        return _iso(*iso.groups())
    if not pattern:
        return None
    regex = re.sub(_DATE_TOKENS, lambda m: _DATE_PARTS[m[0]], re.escape(pattern.upper()))
    match = re.match(regex, cell.strip())
    if match is None:
        return None
    found = match.groupdict()
    year = found.get("y") or (str(_year(found["yy"])) if found.get("yy") else None)
    month = found.get("m") or str(_MONTHS.get(str(found.get("n", "")).lower().rstrip("."), 0))
    if year is None or found.get("d") is None:
        return None
    return _iso(year, month, found["d"])


class _Search:
    """Looks for a set of values in cells and texts; remembers which were found."""

    def __init__(self, values: Sequence[str]) -> None:
        self.texts = {value: _norm(value) for value in values if _norm(value)}
        self.numbers: dict[float, list[str]] = {}
        self.dates: dict[str, list[str]] = {}
        for value in self.texts:
            for number in _numbers(value.strip()):
                self.numbers.setdefault(number, []).append(value)
            for day in _dates(value):
                self.dates.setdefault(day, []).append(value)
        self.found: set[str] = set()
        self.cells = 0

    @property
    def pending(self) -> bool:
        return len(self.found) < len(self.texts)

    def number(self, number: float) -> None:
        for value in self.numbers.get(round(float(number), 6), ()):
            self.found.add(value)

    def cell(self, cell: object, column: ColumnProfile) -> None:
        self.cells += 1
        if cell is None or isinstance(cell, bool):
            return
        if isinstance(cell, int | float):
            self.number(cell)
            return
        text = str(cell)
        self.text(_norm(text))
        if self.numbers and column.type in ("integer", "decimal", "mixed", "id", "text"):
            from awa.kernel.conditions import as_number

            decimal = column.format.decimal if column.format and column.format.decimal else "."
            parsed = as_number(text, decimal)
            if parsed is not None:
                self.number(parsed)
        if self.dates and len(text) <= 40:
            days = {_cell_date(text, column.date_format)} if column.date_format else _dates(text)
            for day in days:
                self.found.update(self.dates.get(day or "", ()))

    def text(self, text: str) -> None:
        for value, wanted in self.texts.items():
            if value not in self.found and wanted in text:
                self.found.add(value)

    def document(self, text: str) -> None:
        self.text(_norm(text))
        if self.numbers:
            from awa.kernel.conditions import as_number

            for token in _NUMBER.findall(text):
                for decimal in (".", ","):
                    parsed = as_number(token, decimal)
                    if parsed is not None:
                        self.number(parsed)


def _find(profile: DataProfile, values: Sequence[str], reader: DataReader) -> tuple[set[str], bool]:
    """The values found in the real data, and whether all of the data could be read."""

    search = _Search(values)
    complete = True
    limit = profile.limits.max_rows_exact
    for file in profile.files:
        if not search.pending:
            break
        if file.status == "error":
            continue
        try:
            if file.kind in ("csv", "xlsx", "sqlite") and file.tables:
                for table in file.tables:
                    for row in reader.rows(file, table, max_rows=limit):
                        for cell, column in zip(row, table.columns, strict=False):
                            search.cell(cell, column)
                        if search.cells > _MAX_CELLS:
                            raise RowLimitError("too many cells")
            elif file.kind in ("pdf", "docx", "json", "text", "mail"):
                text, whole = reader.text(file)
                search.document(text)
                complete = complete and whole
            else:  # a mailbox, a scan, an image, a kind awa cannot read: unknown content
                complete = False
        except DataError:  # RowLimitError is one: the rest of the file stays unseen
            complete = False
        complete = complete and (file.kind == "mail" or not file.children)
    return search.found, complete


def _pool(profile: DataProfile) -> list[str]:
    """Values the profile shows (examples, frequent values, sample cells) for suggestions."""

    pool: dict[str, None] = {}
    for _, table in profile.tables():
        for column in table.columns:
            pool.update(dict.fromkeys(column.examples))
            pool.update(dict.fromkeys(t.value for t in column.top))
        for row in table.sample_rows:
            pool.update(dict.fromkeys(str(c) for c in row if isinstance(c, str)))
    return list(pool)[:2000]


class _Failure(Exception):
    """A reference expression cannot be computed; carries the issue code and the fix."""

    def __init__(self, code: str, message: str, suggestion: str | None = None) -> None:
        super().__init__(message)
        self.code, self.suggestion = code, suggestion


class _Call(DataModel):
    function: str
    column: str
    where: str


def _scan(text: str, start: int) -> int:
    """Index of the `)` that closes the `(` at `start`; quoted text is skipped."""

    depth, quote = 0, ""
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    raise _Failure("DF-V-010", "the expression has an opening parenthesis without a closing one")


def _blank_strings(text: str) -> str:
    """`text` with the inside of quoted strings replaced by `_` (same length)."""

    return re.sub(r"'[^']*'|\"[^\"]*\"", lambda m: m[0][0] + "_" * (len(m[0]) - 2) + m[0][-1], text)


def _call(function: str, inner: str) -> _Call | None:
    """The aggregate `function(column where filter)`; `None` for `min(a, b)` and `max(a, b)`."""

    blank = _blank_strings(inner)
    if function in ("min", "max") and "," in re.sub(r"\([^()]*\)", "", blank):
        return None  # the arithmetic function, not an aggregate
    split = _WHERE.search(blank)
    column = inner[: split.start()] if split else inner
    where = inner[split.end() :] if split else ""
    column = column.strip()
    if function != "count" and column in ("", "*"):
        raise _Failure("DF-V-010", f"{function}() needs a column, for example {function}(Amount)")
    return _Call(function=function, column="" if column == "*" else column, where=where.strip())


def _rewrite(expression: str) -> tuple[str, list[_Call]]:
    """The expression with every aggregate replaced by a name (`agg0`), and the aggregates."""

    out: list[str] = []
    calls: list[_Call] = []
    index = 0
    while index < len(expression):
        char = expression[index]
        if char in "'\"":
            end = expression.find(char, index + 1)
            end = len(expression) - 1 if end < 0 else end
            out.append(expression[index : end + 1])
            index = end + 1
        elif char.isalpha() or char == "_":
            end = index
            while end < len(expression) and (expression[end].isalnum() or expression[end] == "_"):
                end += 1
            word, after = expression[index:end], end
            while after < len(expression) and expression[after].isspace():
                after += 1
            call = None
            if word.lower() in _AGGREGATES and expression[after : after + 1] == "(":
                close = _scan(expression, after)
                call = _call(word.lower(), expression[after + 1 : close])
            if call is None:
                out.append(word)
                index = end
            else:
                out.append(f"agg{len(calls)}")
                calls.append(call)
                index = close + 1
        else:
            out.append(char)
            index += 1
    return "".join(out), calls


def _file_decimal(table: TableProfile) -> str:
    """The decimal separator of the numbers of a table: `,` when its columns use it, else `.`."""

    found = {c.format.decimal for c in table.columns if c.format and c.format.decimal}
    return "," if "," in found and "." not in found else "."


def _resolve(table: TableProfile, name: str) -> int:
    column = table.column(name)
    if column is None:
        names = [c.name for c in table.columns]
        message = f"column '{name}' does not exist (its columns: {listing(names)})"
        raise _Failure("DF-V-003", message, closest(name, names))
    return table.columns.index(column)


def _values_function(
    profile: DataProfile, cache: dict[tuple[str, str], list[Any]], reader: DataReader
) -> Callable[[Any, Any], list[Any]]:
    """`values('file.csv', 'Column')`: the cells of a column of another file, for `in` checks."""

    def values(file: Any, column: Any) -> list[Any]:
        from awa._internal.upstream.tools import ToolException

        key = (str(file), str(column))
        if key not in cache:
            other = find_file(profile, key[0])
            tables = record_tables(other, None) if other else []
            if other is None or not tables:
                raise ToolException(f"values(): no table in '{key[0]}'")
            position = _resolve(tables[0], key[1])
            rows = reader.rows(other, tables[0], max_rows=_MAX_JOIN_VALUES)
            cache[key] = [row[position] for row in rows]
        return cache[key]

    return values


class _Step:
    """One aggregate of an expression, prepared against a table."""

    def __init__(self, call: _Call, table: TableProfile, decimal: str, functions: Any) -> None:
        from awa.kernel.conditions import SafeExpression

        self.call = call
        self.position = _resolve(table, call.column) if call.column else None
        self.column = table.columns[self.position] if self.position is not None else None
        constants = {"true": True, "false": False, "null": None}
        self.where = (
            SafeExpression(call.where, functions=functions, constants=constants, decimal=decimal)
            if call.where
            else None
        )
        self.names = {n: _resolve(table, n) for n in self.where.names} if self.where else {}
        self.decimal = (
            self.column.format.decimal
            if self.column and self.column.format and self.column.format.decimal
            else decimal
        )
        self.cells: list[Any] = []
        numeric = ("integer", "decimal", "mixed")
        if call.function in ("sum", "avg") and self.column and self.column.type not in numeric:
            seen = ", ".join(repr(e) for e in self.column.examples[:3])
            raise _Failure(
                "DF-V-004",
                f"{call.function}() needs numbers but '{self.column.name}' holds "
                f"{self.column.type}" + (f" (values such as {seen})" if seen else ""),
            )

    def take(self, row: list[Any]) -> None:
        if self.where is not None and not self.where.evaluate(lambda name: row[self.names[name]]):
            return
        self.cells.append(row[self.position] if self.position is not None else 1)

    def result(self) -> Any:
        from awa.toolkits.data import _aggregate_value

        function = self.call.function
        if function == "count" and self.position is None:
            return len(self.cells)
        value = _aggregate_value(function, 0, [[cell] for cell in self.cells], self.decimal)
        return 0 if value is None and function in ("sum", "count", "count_distinct") else value


def _compute(
    expression: str, profile: DataProfile, file_name: str, sheet: str | None, reader: DataReader
) -> tuple[float | int | str | None, int]:
    """The value of `expression` over the table of a file, and the rows it read."""

    from awa._internal.upstream.tools import ToolException
    from awa.kernel.conditions import MATH_FUNCTIONS, SafeExpression, filter_functions

    rewritten, calls = _rewrite(expression)
    file = find_file(profile, file_name)
    tables = record_tables(file, sheet) if file else []
    if file is None:
        raise _Failure("DF-V-001", f"'{file_name}' is not a profiled file")
    if not tables:
        raise _Failure(
            "DF-V-010",
            f"'{file.path}' has no rows to compute over; expressions read CSV, Excel and "
            "SQLite tables",
        )
    table, decimal = tables[0], _file_decimal(tables[0])
    functions = {**filter_functions(decimal), "values": _values_function(profile, {}, reader)}
    try:
        steps = [_Step(call, table, decimal, functions) for call in calls]
        rows = 0
        for row in reader.rows(file, table, max_rows=profile.limits.max_rows_exact):
            rows += 1
            for step in steps:
                step.take(row)
        results = {f"agg{number}": step.result() for number, step in enumerate(steps)}

        def name(identifier: str) -> Any:
            if identifier not in results:
                raise ToolException(
                    f"unknown name {identifier!r}: columns belong inside sum(...), count(...), ..."
                )
            return results[identifier]

        evaluation = SafeExpression(
            rewritten, functions=MATH_FUNCTIONS, allow_comparisons=False, allow_strings=False
        )
        value = evaluation.evaluate(name)
    except ToolException as error:
        raise _Failure("DF-V-010", f"cannot compute it: {error}") from None
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        raise _Failure("DF-V-010", "no row matches, so there is no value")
    return (value if isinstance(value, str | int | float) else None), rows


def _agrees(value: float | int | str, expected: str) -> bool:
    if isinstance(value, str):
        return _norm(value) == _norm(expected)
    from awa.kernel.conditions import as_number

    readings = (as_number(expected, decimal) for decimal in (".", ","))
    return any(
        r is not None and math.isclose(r, value, rel_tol=1e-9, abs_tol=0.005) for r in readings
    )
