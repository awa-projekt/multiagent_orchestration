"""Column inference: type, number format and date format, decided from the values."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from functools import lru_cache
from typing import Any, NamedTuple

from awa.data._model import ColumnProfile, NumberFormat, TopValue, column_key

Notes = list[tuple[str, str]]
"""Flag details of a column as `(code, message)`; the caller adds the column id."""

_MAX_ENUM = 20  # most distinct values of an enum column
_TYPED_SHARE = 0.95  # share of the values that must fit for a column to get a type
_MIXED_SHARE = 0.2  # below this share the best type is ignored and the column is text
_TOP = 8
_SIGN_STYLES = ("parentheses", "minus", "trailing_minus")  # by the marks of `_number_kind`
_EXACT_DIGITS = 15  # more digits do not survive as a number: an identifier
_NULL_TOKENS = frozenset(
    {"null", "none", "n/a", "na", "n.a.", "nan", "#n/a", "#nv", "-", "--", "—", "k.a.", "k. a."}
)
_BOOL_WORDS = frozenset({"true", "false", "yes", "no", "ja", "nein", "wahr", "falsch", "y", "n"})
_CURRENCIES = "EUR USD GBP CHF JPY CNY SEK NOK DKK PLN CZK HUF RON CAD AUD INR BRL MXN TRY ZAR"
_CUR = "(?:[€$£¥]|" + "|".join(_CURRENCIES.split()) + ")"
_NUMBER = re.compile(
    r"(?P<open>\()?\s*(?P<sign>[+-])?\s*(?P<lead>" + _CUR + r")?\s*(?P<sign2>[+-])?\s*"
    r"(?P<body>[0-9][0-9.,'’\xa0\N{NARROW NO-BREAK SPACE} ]*|[.,][0-9]+)\s*(?P<minus>-)?\s*"
    r"(?P<tail>%|" + _CUR + r")?\s*(?P<close>\))?",
    re.IGNORECASE,
)
_HAS_DIGIT = re.compile(r"[0-9]")
_THOUSANDS = ("", ",", ".", " ", "'")
_NOTATIONS = [(d, t) for d in (".", ",") for t in _THOUSANDS if d != t]
# Every digit becomes 9 (a leading 0 stays 0): values written alike share one mask.
_MASK = str.maketrans(
    {**dict.fromkeys("0123456789", "9"), "\xa0": " ", "\N{NARROW NO-BREAK SPACE}": " ", "’": "'"}
)
_SEPARATOR = "\x1f"  # joins values for the bulk mask; a value that holds it is read alone
_BYTE_MASK = bytes.maketrans(b"0123456789", b"9999999999")
_PLAIN = re.compile(r"[09.,' ]+")
_WHOLE = frozenset({("", "")})  # the readings of digits without any separator
_GROUP_MARKS = {ord(mark): None for mark in " '’\xa0\N{NARROW NO-BREAK SPACE}"}


def _mask(text: str) -> str:
    try:
        mask = text.encode("ascii").translate(_BYTE_MASK).decode("ascii")
    except UnicodeEncodeError:
        mask = text.translate(_MASK)
    return "0" + mask[1:] if text[0] == "0" else mask


def _forms() -> dict[tuple[str, str], re.Pattern[str]]:
    """One pattern per (decimal, thousands) notation over the mask alphabet (`9` and `0`)."""

    forms = {}
    for decimal in ("", ".", ","):
        for thousands in _THOUSANDS:
            if decimal and decimal == thousands:
                continue
            if thousands:
                whole = f"(?:9[09]{{0,2}}(?:{re.escape(thousands)}[09]{{3}})+)"
            else:
                whole = "(?:[09]+)" + ("?" if decimal else "")
            forms[decimal, thousands] = re.compile(
                whole + (f"{re.escape(decimal)}[09]+" if decimal else "")
            )
    return forms


_FORMS = _forms()
_ALIASED = re.compile(r"(?<!M)MM(?!M)|DD")
_UNPADDED_MONTH = re.compile(r"(?<!M)MM(?!M)")


@lru_cache(maxsize=2048)
def _readings(mask: str) -> frozenset[tuple[str, str]]:
    """Every (decimal, thousands) reading of a number written like this mask ('' = not used)."""

    return frozenset(form for form, pattern in _FORMS.items() if pattern.fullmatch(mask))


def _fits(form: tuple[str, str], notation: tuple[str, str]) -> bool:
    return form[0] in ("", notation[0]) and form[1] in ("", notation[1])


_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            "jan january januar",
            "feb february februar",
            "mar march märz maerz mär",
            "apr april",
            "may mai",
            "jun june juni",
            "jul july juli",
            "aug august",
            "sep sept september",
            "oct october okt oktober",
            "nov november",
            "dec december dez dezember",
        ),
        1,
    )
    for name in names.split()
}
_CLOCK = (
    r"(?:(?P<tsep>[T ])(?P<hh>[0-9]{1,2}):(?P<mm>[0-9]{2})(?::(?P<ss>[0-9]{2})"
    r"(?P<frac>[.,][0-9]{1,9})?)?\s?(?P<tz>Z|[+-][0-9]{2}:?[0-9]{2})?)?"
)
_DATETIME = re.compile(
    r"(?:(?P<a>[0-9]{1,4})(?P<s>[-./])(?P<b>[0-9]{1,2})(?P=s)(?P<c>[0-9]{1,4})"
    r"|(?P<d1>[0-9]{1,2})[ .-]+(?P<m1>[^\W\d_]{3,9})\.?[ .,-]+(?P<y1>[0-9]{4}|[0-9]{2})"
    r"|(?P<m2>[^\W\d_]{3,9})\.? (?P<d2>[0-9]{1,2}),? (?P<y2>[0-9]{4}))" + _CLOCK,
    re.IGNORECASE,
)
_TIME = re.compile(r"(?P<hh>[0-9]{1,2}):(?P<mm>[0-9]{2})(?::(?P<ss>[0-9]{2}))?")
_UNIT_WORDS = (
    f"{_CURRENCIES} kg g t mg lb l ml cl m cm mm km m2 m3 qm h hrs std min sec s tage days "
    "pcs pc stk st stück kwh mwh kw gb mb"
).split()
_UNIT = "|".join(sorted(_UNIT_WORDS, key=len, reverse=True))
_UNIT_SUFFIX = re.compile(rf"(?:^|[\s_/-])({_UNIT}|%|€|\$|£)$", re.IGNORECASE)
_UNIT_BRACKET = re.compile(rf"[(\[]\s*(?:in\s+)?({_UNIT}|%|€|\$|£)\s*[)\]]\s*$", re.IGNORECASE)
_ID_NAME = re.compile(
    r"(?:^|[^a-z0-9])(?:ids?|nr|no|num|number|nummer|key|code|ref|sku)(?:$|[^a-z0-9])"
)
_ID_SUFFIX = ("id", "nr", "nummer")

_GROUPS = {"int": "number", "float": "number", "num": "number", "datetime": "date"}
"""The kind group a kind counts in; every other kind is a group of its own."""
_NO_CLOCK = ("", False, 0, "")


class _Kind(NamedTuple):
    """What one distinct value is: its kind and the facts the column decision needs."""

    kind: str  # bool int float num date datetime time null text
    forms: frozenset[tuple[str, str]] = frozenset()  # num: the notations that read it
    body: str = ""  # num: the digits with their separators ('' for a plain number: the value)
    currency: str = ""
    percent: bool = False
    neg: bool = False
    style: str = ""  # num: minus, parentheses or trailing_minus when negative
    zero: bool = False  # num: digits with a leading zero (`007`)
    dates: tuple[tuple[str, datetime, str], ...] = ()  # date: (pattern, moment, unpadded `D`/`M`)
    clock: tuple[str, bool, int, str] = _NO_CLOCK  # (separator, seconds, fraction digits, zone)
    number: int | float | None = None  # a typed number
    moment: datetime | None = None  # a typed date or datetime


def _number_kind(text: str) -> _Kind | None:
    """A number with an affix, a sign or parentheses (`€ 5,00`, `(12)`, `12 %`)."""

    match = _NUMBER.fullmatch(text)
    if match is None:
        return None
    body = match["body"].rstrip()
    forms = _readings(_mask(body))
    lead, tail = (match["lead"] or "").upper(), (match["tail"] or "").upper()
    sign = match["sign"] or match["sign2"] or ""
    marks = (match["open"] is not None, sign == "-", match["minus"] is not None)
    if (
        not forms
        or (match["open"] is None) != (match["close"] is None)
        or (match["sign"] and match["sign2"])
        or sum(marks) > 1
        or (lead and tail)
    ):
        return None
    style = next(
        (name for name, marked in zip(_SIGN_STYLES, marks, strict=True) if marked), "minus"
    )
    return _Kind(
        "num",
        forms=forms,
        body=body,
        currency=lead or ("" if tail == "%" else tail),
        percent=tail == "%",
        neg=any(marks),
        style=style if any(marks) else "",
        zero=body.isdigit() and len(body) > 1 and body[0] == "0",
    )


@lru_cache(maxsize=64)
def _converter(form: tuple[str, str]) -> dict[int, int | str | None]:
    """The `str.translate` table that turns a number written in `form` into Python syntax."""

    table: dict[int, int | str | None] = dict(_GROUP_MARKS)
    if form[1] in (".", ","):
        table[ord(form[1])] = None
    if form[0] == ",":
        table[ord(",")] = "."
    return table


@lru_cache(maxsize=4096)
def _form_for(
    forms: frozenset[tuple[str, str]], notation: tuple[str, str]
) -> tuple[str, str] | None:
    return next((f for f in sorted(forms) if _fits(f, notation)), None)


def _read(text: str, kind: _Kind, notation: tuple[str, str]) -> float | int | None:
    """The number written as `text` (a `num` kind) under `notation`, `None` if it does not fit."""

    form = _form_for(kind.forms, notation)
    if form is None:
        return None
    body = kind.body or text
    digits = body if body.isdigit() else body.translate(_converter(form))
    number = float(digits) if form[0] else int(digits)
    return -number if kind.neg else number


def _year(text: str) -> int:
    year = int(text)
    return year if len(text) == 4 else year + (2000 if year < 69 else 1900)


def _pad(day: str, month: str) -> str:
    return ("D" if len(day) == 1 else "") + ("M" if len(month) == 1 else "")


_ClockSpans = tuple[
    tuple[int, int], tuple[int, int], tuple[int, int] | None, tuple[int, int] | None
]


class _Template(NamedTuple):
    """The reading of a numeric date by position: all values of one mask share it."""

    options: tuple[tuple[str, str, tuple[tuple[int, int], ...], bool], ...]
    clock: _ClockSpans
    shape: tuple[str, bool, int, str]


def _template(text: str) -> _Template | None:
    """The positional reading of the numeric date `text` (`2024-10-01`, `1.2.24 10:30`), or
    `None` when it is not written like one."""

    match = _DATETIME.fullmatch(text)
    if match is None or match["a"] is None:
        return None
    a, sep, b, c = match["a"], match["s"], match["b"], match["c"]
    options = []
    if len(a) == 4 and len(c) <= 2:
        unpadded = ("M" if len(b) == 1 else "") + ("D" if len(c) == 1 else "")
        spans = (match.span("a"), match.span("b"), match.span("c"))
        options.append((f"YYYY{sep}MM{sep}DD", unpadded, spans, False))
    elif len(a) <= 2 and len(c) in (2, 4):
        tail, short = ("YYYY", False) if len(c) == 4 else ("YY", True)
        year, first, second = match.span("c"), match.span("a"), match.span("b")
        options.append((f"DD{sep}MM{sep}{tail}", _pad(a, b), (year, second, first), short))
        options.append((f"MM{sep}DD{sep}{tail}", _pad(b, a), (year, first, second), short))
    if not options:
        return None
    zone = match["tz"] or ""
    if zone:
        zone = "Z" if zone.upper() == "Z" else zone[0] + ("HH:MM" if ":" in zone else "HHMM")
    shape = _NO_CLOCK
    spans_of_clock: _ClockSpans = ((0, 0), (0, 0), None, None)
    if match["tsep"] is not None:
        fraction = len(match["frac"]) - 1 if match["frac"] else 0
        shape = (match["tsep"], match["ss"] is not None, fraction, zone)
        seconds = match.span("ss") if match["ss"] is not None else None
        digits = (match.start("frac") + 1, match.end("frac")) if match["frac"] else None
        spans_of_clock = (match.span("hh"), match.span("mm"), seconds, digits)
    return _Template(tuple(options), spans_of_clock, shape)


def _numeric_date(text: str, mask: str, templates: dict[str, _Template | None]) -> _Kind | None:
    """A numeric date read by position; `templates` holds the readings found so far (one per mask)."""

    if mask not in templates:
        templates[mask] = _template(text)
    template = templates[mask]
    if template is None:
        return None
    (hs, he), (ms, me), seconds, fraction = template.clock
    hour = int(text[hs:he]) if he else 0
    minute = int(text[ms:me]) if me else 0
    second = int(text[seconds[0] : seconds[1]]) if seconds else 0
    micro = int(text[fraction[0] : fraction[1]][:6].ljust(6, "0")) if fraction else 0
    readings = []
    for pattern, unpadded, (year, month, day), short in template.options:
        y = text[year[0] : year[1]]
        try:
            moment = datetime(
                _year(y) if short else int(y),
                int(text[month[0] : month[1]]),
                int(text[day[0] : day[1]]),
                hour,
                minute,
                second,
                micro,
            )
        except ValueError:
            continue
        readings.append((pattern, moment, unpadded))
    if not readings:
        return None
    kind = "datetime" if template.shape[0] else "date"
    return _Kind(kind, dates=tuple(readings), clock=template.shape)


def _named_date(text: str) -> _Kind | None:
    """A date with a month name (`5 Jan 2024`, `5. Januar 2024`, `Jan 5, 2024`), or a time."""

    match = _DATETIME.fullmatch(text)
    if match is None:
        return _time_kind(text)
    if match["a"] is not None:
        return None
    day_first = match["m1"] is not None
    name, day, year = (
        (match["m1"], match["d1"], match["y1"]) if day_first else match.group("m2", "d2", "y2")
    )
    style = "MMMM" if len(name) > 3 and name.lower() != "sept" else "MMM"
    pattern = (
        f"DD {style} {'YYYY' if len(year) == 4 else 'YY'}" if day_first else f"{style} DD, YYYY"
    )
    parts = (_year(year), _MONTHS.get(name.lower()) or 0, int(day))
    shape = _NO_CLOCK
    hour = minute = second = micro = 0
    if match["tsep"] is not None:
        hour, minute, second = int(match["hh"]), int(match["mm"]), int(match["ss"] or 0)
        digits = (match["frac"] or ".")[1:]
        micro = int(digits[:6].ljust(6, "0")) if digits else 0
        shape = (match["tsep"], match["ss"] is not None, len(digits), "")
    try:
        moment = datetime(*parts, hour, minute, second, micro)
    except ValueError:
        return None
    unpadded = "D" if len(day) == 1 else ""
    kind = "datetime" if shape[0] else "date"
    return _Kind(kind, dates=((pattern, moment, unpadded),), clock=shape)


def _time_kind(text: str) -> _Kind | None:
    match = _TIME.fullmatch(text)
    if match is None:
        return None
    if int(match["hh"]) > 23 or int(match["mm"]) > 59 or int(match["ss"] or 0) > 59:
        return None
    return _Kind("time", clock=("", match["ss"] is not None, 0, ""))


_TEXT = _Kind("text")
_BOOL = _Kind("bool")
_NULL = _Kind("null")
_DATE = _Kind("date")  # a verdict: the values of this mask must be read one by one
_AFFIX = _Kind("num")  # a verdict: likewise (currency sign, percent, parentheses)


@lru_cache(maxsize=8192)
def _verdict(mask: str) -> _Kind:
    """How text written like `mask` is read: a shared `num` kind for a plain number, `_DATE`,
    `_AFFIX` or `_TEXT`. A pure function of the mask, cached: a column holds many values of a
    few shapes."""

    forms = _readings(mask) if _PLAIN.fullmatch(mask) else frozenset()
    if forms:
        zero = mask[0] == "0" and len(mask) > 1 and mask.isdigit()
        return _Kind("num", forms=forms, zero=zero)
    if _DATETIME.fullmatch(mask) or _TIME.fullmatch(mask):
        return _DATE
    return _AFFIX if _NUMBER.fullmatch(mask) else _TEXT


def _classify_text(
    text: str, mask: str | None = None, templates: dict[str, _Template | None] | None = None
) -> _Kind:
    """The kind of a text value; `mask` is its mask when already computed in bulk, `templates`
    the numeric date readings shared by the values of one column."""

    if not _HAS_DIGIT.search(text):
        if len(text) <= 16:
            lowered = text.lower()
            if lowered in _BOOL_WORDS:
                return _BOOL
            if lowered in _NULL_TOKENS:
                return _NULL
        return _TEXT
    if mask is None:
        mask = _mask(text)
    elif text[0] == "0":
        mask = "0" + mask[1:]
    verdict = _verdict(mask)
    if verdict is _DATE:
        found = _numeric_date(text, mask, {} if templates is None else templates)
        return found or _named_date(text) or _TEXT
    if verdict is _AFFIX:
        return _number_kind(text) or _TEXT
    return verdict


def _classify(value: Any) -> _Kind:
    """The kind of one value; text counts as a number, date or time only when it reads as one."""

    if isinstance(value, str):
        return _classify_text(value)
    if isinstance(value, bool):
        return _BOOL
    if isinstance(value, int):
        return _Kind("int", number=value)
    if isinstance(value, float | Decimal):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return _TEXT
        return _Kind("int" if number.is_integer() else "float", number=number)
    if isinstance(value, datetime):
        midnight = value.time() == time()
        return _Kind(
            "date" if midnight else "datetime",
            moment=value,
            clock=_NO_CLOCK if midnight else ("T", True, 0, ""),
        )
    if isinstance(value, date):
        return _Kind("date", moment=datetime(value.year, value.month, value.day))
    if isinstance(value, time):
        return _Kind("time", moment=datetime.combine(date(1, 1, 1), value))
    return _classify_text(str(value))


def cell_kind(value: Any) -> str:
    """`empty`, `number`, `date`, `bool` or `text`: what a spreadsheet or CSV cell looks like."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return "empty"
    kind = _classify(value.strip() if isinstance(value, str) else value).kind
    group = _GROUPS.get(kind, kind)
    return {"time": "date", "null": "text"}.get(group, group)


def number_reader(column: ColumnProfile) -> Callable[[Any], float | int | None]:
    """A function that reads one cell of `column` as a number under the notation the column has."""

    fmt = column.format
    notation = ((fmt.decimal or ".") if fmt else ".", (fmt.thousands or "") if fmt else "")

    def read(value: Any) -> float | int | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int | float):
            return value
        text = value.strip() if isinstance(value, str) else ""
        kind = _number_kind(text) if text else None
        return None if kind is None else _read(text, kind, notation)

    return read


def unit_of(name: str) -> str | None:
    """The unit the header names (`Betrag_EUR`, `Gewicht [kg]`, `Anteil (%)`), or `None`."""

    found = _UNIT_BRACKET.search(name) or _UNIT_SUFFIX.search(name)
    if found is None:
        return None
    unit = found.group(1)
    return unit.upper() if unit.upper() in _CURRENCIES.split() else unit.lower()


def id_named(name: str) -> bool:
    """Whether a column name says identifier (`customer_id`, `Bestellnr`, `Rechnungsnummer`)."""

    lowered = name.lower()
    return bool(_ID_NAME.search(lowered)) or lowered.replace("_", "").endswith(_ID_SUFFIX)


def infer_column(
    values: Sequence[Any],
    *,
    name: str = "",
    key: str | None = None,
    index: int = 0,
    cell_chars: int = 200,
    notes: Notes | None = None,
) -> ColumnProfile:
    """The `ColumnProfile` of one column: type, number or date format, statistics, flags.

    Every value is read under each notation it could have (`1.234` is a decimal point or a thousands
    separator); the notation that reads the most values wins. When two notations read every value
    and disagree on one, the column is `ambiguous` and no format is decided; dates work the same
    way.

    Args:
        values: The cells in file order. Text is parsed; typed values keep their type; `None` and
            blank text are empty.
        name: The header, used for the key, the unit (`Amount_EUR`) and identifier hints.
        key: The column key; default `column_key(name)`.
        index: 0-based position of the column.
        cell_chars: Examples and top values are cut to this length.
        notes: When given, receives `(flag code, message)` for every decision or oddity.

    Returns:
        The column; `type` is `mixed` when no type fits at least 95 % of the filled values.

    Example:
        ```python
        from awa.data import infer_column

        notes: list[tuple[str, str]] = []
        column = infer_column(["4.250,00", "880,75", "1.200,00"], name="Amount_EUR", notes=notes)
        print(column.type, column.format.decimal, column.format.thousands, column.unit)
        # decimal , . EUR        (and notes[0][0] is 'DF-W-DECIMAL-COMMA')
        ```
    """

    sink: Notes = notes if notes is not None else []
    counts = _distinct_counts(values)
    column = ColumnProfile(name=name, key=column_key(name) if key is None else key, index=index)
    column.type = "empty"
    column.unit = unit_of(name)
    filled = sum(counts.values())
    column.null_share = round(1 - filled / len(values), 4) if values else 0.0
    column.nullable = filled < len(values)
    column.distinct = len(counts)
    column.unique = bool(filled) and len(counts) == filled
    column.examples = [_text(value, cell_chars) for value in list(counts)[:3]]
    if counts:
        _decide_type(column, len(values), counts, cell_chars, sink)
    return column


def _distinct_counts(values: Sequence[Any]) -> dict[Any, int]:
    try:  # text only (a CSV column): counting at C speed
        counts: dict[Any, int] = Counter(map(str.strip, values))
        counts.pop("", None)
        return counts
    except TypeError:  # typed values or empty cells
        counts = {}
        for value in values:
            if value.__class__ is str:
                value = value.strip()
            if value is not None and value != "":
                counts[value] = counts.get(value, 0) + 1
        return counts


def _decide_type(
    column: ColumnProfile, size: int, counts: dict[Any, int], cell_chars: int, sink: Notes
) -> None:
    items, tally = _census(counts)
    filled = sum(counts.values())
    tokens, total = tally["null"], filled - tally["null"]
    group, fitting = max(
        ((g, tally[g]) for g in ("number", "date", "time", "bool")), key=lambda pair: pair[1]
    )
    if group == "bool" and sum(k.kind == "bool" for _, _, k in items) < 2:
        fitting = 0  # a single yes/no word is a value, not a yes/no column
    typed = fitting > 0 and fitting >= _TYPED_SHARE * total
    if tokens and typed:
        column.null_share = round(1 - total / size, 4)
        column.nullable = True
        seen = sorted({str(v) for v, _, k in items if k.kind == "null"})[:3]
        _say(
            column,
            sink,
            "DF-W-NULL-TOKENS",
            f"{tokens} values ({', '.join(map(repr, seen))}) stand for 'no value' in this {group} column and count as empty",
        )
    elif tokens:
        total = filled
    if typed:
        mine = [(v, c, k) for v, c, k in items if _GROUPS.get(k.kind, k.kind) == group]
        if group == "number":
            _numbers(column, mine, sink)
        elif group == "date":
            _dates(column, mine, sink)
        elif group == "time":
            _times(column, mine)
        else:
            _bools(column, mine, cell_chars)
        if fitting < total:
            bad = [v for v, _, k in items if _GROUPS.get(k.kind, k.kind) not in (group, "null")]
            shown = ", ".join(repr(_text(v, 20)) for v in bad[:3])
            _say(
                column,
                sink,
                "DF-W-MIXED-TYPES",
                f"{total - fitting} of {total} values do not fit the {column.type} type (e.g. {shown}); the column is read as {column.type}",
            )
    elif fitting and fitting >= _MIXED_SHARE * total:
        _mixed(column, tally, sink)
    elif (
        len(counts) <= _MAX_ENUM
        and len(counts) * 2 <= total
        and max(len(str(v)) for v in counts) <= 40
    ):
        column.type = "enum"
        column.top = _top(counts, cell_chars)
    else:
        column.type = "text"


def _mixed(column: ColumnProfile, tally: dict[str, int], sink: Notes) -> None:
    labels = (
        ("number", "numbers"),
        ("date", "dates"),
        ("time", "times"),
        ("bool", "yes/no words"),
        ("text", "text values"),
    )
    parts = ", ".join(f"{tally[g]} {label}" for g, label in labels if tally[g])
    column.type = "mixed"
    _say(
        column,
        sink,
        "DF-W-MIXED-TYPES",
        f"the values are of different kinds ({parts}); no single type fits 95 % of them",
    )


def _census(counts: dict[Any, int]) -> tuple[list[tuple[Any, int, _Kind]], dict[str, int]]:
    """Every distinct value with its count and kind, and the number of values per kind group."""

    items: list[tuple[Any, int, _Kind]] = []
    tally = dict.fromkeys(("number", "date", "time", "bool", "null", "text"), 0)
    groups = _GROUPS
    templates: dict[str, _Template | None] = {}
    values = list(counts)
    masks = _masks(values) if all(v.__class__ is str for v in values) else None
    if masks is None:  # typed values: one by one
        for value, count in counts.items():
            kind = _classify(value)
            items.append((value, count, kind))
            tally[groups.get(kind.kind, kind.kind)] += count
        return items, tally
    for (value, count), mask in zip(counts.items(), masks, strict=True):
        kind = _classify_text(value, mask, templates)
        items.append((value, count, kind))
        tally[groups.get(kind.kind, kind.kind)] += count
    return items, tally


def _masks(values: list[str]) -> list[str] | None:
    """The mask of every value at once: translating one joined text is much faster than one call
    per value. `None` when a value holds the separator (the masks would not line up)."""

    joined = _SEPARATOR.join(values).encode("utf-8", "surrogatepass")
    masks = joined.translate(_BYTE_MASK).decode("utf-8", "surrogatepass").split(_SEPARATOR)
    return masks if len(masks) == len(values) else None


def _bools(column: ColumnProfile, mine: list[tuple[Any, int, _Kind]], cell_chars: int) -> None:
    column.type = "bool"
    column.top = _top({v: c for v, c, _ in mine}, cell_chars)


def _times(column: ColumnProfile, mine: list[tuple[Any, int, _Kind]]) -> None:
    column.type = "time"
    written = [k for _, _, k in mine if k.moment is None]  # typed times have no text format
    if written:
        column.date_format = "HH:MM:SS" if any(k.clock[1] for k in written) else "HH:MM"


class _Numbers(NamedTuple):
    """The number values of a column grouped by what they are: text numbers per written kind
    (`kinds`, their `values` and `weight`), and the typed numbers (`typed`)."""

    kinds: dict[int, _Kind]
    values: dict[int, list[str]]
    weight: dict[int, int]
    typed: list[_Kind]


def _group_numbers(mine: list[tuple[Any, int, _Kind]]) -> _Numbers:
    """Plain numbers written alike share one kind object: work per kind, not per value."""

    kinds: dict[int, _Kind] = {}
    values: dict[int, list[str]] = {}
    weight: dict[int, int] = {}
    typed: list[_Kind] = []
    for value, count, kind in mine:
        if kind.kind == "num":
            slot = id(kind)
            if slot in kinds:
                values[slot].append(value)
                weight[slot] += count
            else:
                kinds[slot], values[slot], weight[slot] = kind, [value], count
        else:
            typed.append(kind)
    return _Numbers(kinds, values, weight, typed)


def _is_identifier(found: _Numbers) -> bool:
    """Whether the digits are an identifier (leading zeros, or too long to be a quantity)."""

    zero = any(k.zero for k in found.kinds.values())
    whole = all(
        not k.neg
        and not k.currency
        and not k.percent
        and (k.body.isdigit() if k.body else k.forms == _WHOLE)
        for k in found.kinds.values()
    ) and all(k.kind == "int" and k.number is not None and k.number >= 0 for k in found.typed)
    long = whole and any(len(v) > _EXACT_DIGITS for vs in found.values.values() for v in vs)
    return whole and (zero or long)


class _Notation(NamedTuple):
    """The notation that reads the most text numbers: `best` of them, the notation itself, and
    the decimal and thousands separators that are certain (`None`: not decided)."""

    groups: Counter[frozenset[tuple[str, str]]]
    best: int
    optimal: list[tuple[str, str]]
    decimal: str | None
    grouping: str | None

    @property
    def ambiguous(self) -> bool:
        decimals = {n[0] for n in self.optimal}
        return len(decimals) > 1 and any(len(forms) > 1 for forms in self.groups)

    @property
    def chosen(self) -> tuple[str, str]:
        return (
            (self.decimal or self.optimal[0][0], self.grouping or "") if self.optimal else (".", "")
        )


def _choose_notation(found: _Numbers) -> _Notation:
    """The notation (decimal and thousands separator) under which most text numbers are valid."""

    groups: Counter[frozenset[tuple[str, str]]] = Counter()
    for slot, kind in found.kinds.items():
        groups[kind.forms] += found.weight[slot]
    best, optimal = -1, []
    for notation in _NOTATIONS if groups else ():
        accepted = sum(c for forms, c in groups.items() if any(_fits(f, notation) for f in forms))
        if accepted > best:
            best, optimal = accepted, [notation]
        elif accepted == best:
            optimal.append(notation)
    decimals, thousands = {n[0] for n in optimal}, {n[1] for n in optimal}
    decimal = (
        next(iter(decimals))
        if len(decimals) == 1 and any(all(d for d, _ in f) for f in groups)
        else None
    )
    grouping = (next(iter(thousands)) or None) if len(thousands) == 1 else None
    return _Notation(groups, best, optimal, decimal, grouping)


def _read_numbers(found: _Numbers, notation: tuple[str, str]) -> tuple[list[int | float], bool]:
    """Every number of the column read under `notation`, and whether any has decimals."""

    numbers: list[int | float] = [k.number for k in found.typed if k.number is not None]
    decimal_seen = any(k.kind == "float" for k in found.typed)
    for slot, kind in found.kinds.items():
        form = _form_for(kind.forms, notation)
        if form is None:
            continue
        values = found.values[slot]
        if kind.body:  # an affix, a sign or parentheses: one value per kind
            read = _read(values[0], kind, notation)
            parsed = [] if read is None else [read]
        elif form == ("", ""):
            parsed = list(map(int, values))
        else:
            table = _converter(form)
            parsed = [
                float(v.translate(table)) if form[0] else int(v.translate(table)) for v in values
            ]
        decimal_seen = decimal_seen or (bool(form[0]) and bool(parsed))
        numbers += parsed
    return numbers, decimal_seen


def _numbers(column: ColumnProfile, mine: list[tuple[Any, int, _Kind]], sink: Notes) -> None:
    found = _group_numbers(mine)
    if _is_identifier(found):  # the digits are an identifier, not a quantity
        column.type = "id"
        if any(k.zero for k in found.kinds.values()):
            _say(
                column,
                sink,
                "DF-W-LEADING-ZEROS",
                "values keep their leading zeros: read this column as text, never as numbers",
            )
        return
    count = sum(found.weight.values())
    chosen = _choose_notation(found)
    if chosen.ambiguous:
        column.ambiguous = True
        column.type = "decimal"
        slot = next(slot for slot, k in found.kinds.items() if len(k.forms) > 1)
        example = found.kinds[slot].body or found.values[slot][0]
        _say(
            column,
            sink,
            "DF-W-NUMBER-AMBIGUOUS",
            f"{count} numbers such as {example!r} read as a decimal point or as a thousands separator and no value rules out either reading; ask which notation the file uses",
        )
        return
    numbers, decimal_seen = _read_numbers(found, chosen.chosen)
    column.type = "decimal" if decimal_seen else "integer"
    if numbers:
        column.min, column.max = min(numbers), max(numbers)
    if chosen.groups and chosen.best < count:
        _say(
            column,
            sink,
            "DF-W-MIXED-NUMBER-FORMAT",
            f"{count - chosen.best} of {count} text numbers are written in another notation than the other {chosen.best}",
        )
    if found.kinds:
        _number_format(column, found, chosen, count, sink)


def _number_format(
    column: ColumnProfile, found: _Numbers, chosen: _Notation, count: int, sink: Notes
) -> None:
    """The `NumberFormat` of the text numbers and the flags about currencies, percent signs and
    the separators."""

    kinds, weight = found.kinds, found.weight
    decimal, grouping, best = chosen.decimal, chosen.grouping, chosen.best
    currencies = sorted({k.currency for k in kinds.values() if k.currency})
    percents = sum(weight[s] for s, k in kinds.items() if k.percent)
    styles = sorted({k.style for k in kinds.values() if k.style} - {"minus"})
    column.format = NumberFormat(
        decimal=decimal,
        thousands=grouping,
        currency=currencies[0] if len(currencies) == 1 else None,
        percent=percents == count,
        negative=styles[0] if styles else "minus",
    )
    if column.format == NumberFormat():
        column.format = None
    if len(currencies) > 1:
        _say(
            column,
            sink,
            "DF-W-MIXED-CURRENCY",
            f"values carry different currencies ({', '.join(currencies)})",
        )
    if 0 < percents < count:
        _say(
            column,
            sink,
            "DF-W-MIXED-NUMBER-FORMAT",
            f"{percents} of {count} numbers carry a percent sign, the others do not",
        )
    separators = f" and thousands separator {grouping!r}" if grouping else ""
    if decimal == ",":
        _say(
            column,
            sink,
            "DF-W-DECIMAL-COMMA",
            f"decimal comma{separators} in {best} of {count} text numbers; read them with decimal ','",
        )
    elif grouping:
        _say(
            column,
            sink,
            "DF-I-THOUSANDS-SEPARATOR",
            f"thousands separator {grouping!r} in {best} of {count} text numbers",
        )


def _alias(pattern: str) -> str:
    return _ALIASED.sub("X", pattern)


def _dates(column: ColumnProfile, mine: list[tuple[Any, int, _Kind]], sink: Notes) -> None:
    column.type = "datetime" if any(k.kind == "datetime" for _, _, k in mine) else "date"
    text = [(c, k) for _, c, k in mine if k.dates]
    moments = [k.moment for _, _, k in mine if k.moment is not None]  # typed values: no format
    if text:
        written = _written_dates(column, text, sink)
        if written is None:
            return
        moments += written
    if moments:
        naive = [_naive_utc(moment) for moment in moments]  # aware and naive values compare
        low, high = min(naive), max(naive)
        day = column.type == "date"
        column.min = low.date().isoformat() if day else low.isoformat()
        column.max = high.date().isoformat() if day else high.isoformat()


def _naive_utc(moment: datetime) -> datetime:
    """A naive moment as it is; an aware one in UTC without its zone."""

    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _written_dates(
    column: ColumnProfile, text: list[tuple[int, _Kind]], sink: Notes
) -> list[datetime] | None:
    """Decide the date format of the dates written as text; their moments, `None` if ambiguous."""

    count = sum(c for c, _ in text)
    coverage: dict[str, int] = {}
    for c, kind in text:
        for pattern, _, _ in kind.dates:
            coverage[pattern] = coverage.get(pattern, 0) + c
    best = max(coverage.values())
    optimal = sorted(p for p, c in coverage.items() if c == best)
    if best < count:
        ranked = sorted(coverage.items(), key=lambda pair: (-pair[1], pair[0]))
        listed = ", ".join(f"{p} ({n})" for p, n in ranked)
        _say(
            column,
            sink,
            "DF-W-MIXED-DATE-FORMATS",
            f"{count - best} of {count} dates do not fit the most common format; formats seen: {listed}",
        )
    if len(optimal) > 1 and len({_alias(p) for p in optimal}) == 1:
        column.ambiguous = True
        _say(
            column,
            sink,
            "DF-W-DATE-AMBIGUOUS",
            f"{best} of {count} dates fit {' or '.join(optimal)} and none has a day above 12; ask which order the file uses",
        )
        return None
    chosen = optimal[0]
    moments: list[datetime] = []
    unpadded: set[str] = set()
    clocks: set[tuple[str, bool, int, str]] = set()
    for _, kind in text:
        for pattern, moment, letters in kind.dates:
            if pattern == chosen:
                moments.append(moment)
                unpadded.update(letters)
                if kind.clock[0]:
                    clocks.add(kind.clock)
    pattern = chosen.replace("DD", "D", 1) if "D" in unpadded else chosen
    pattern = _UNPADDED_MONTH.sub("M", pattern, count=1) if "M" in unpadded else pattern
    if clocks:
        first = min(clocks)
        pattern += first[0] if first[0] == "T" else " "
        pattern += "HH:MM" + (":SS" if any(c[1] for c in clocks) else "")
        pattern += ("." + "S" * max(c[2] for c in clocks)) if any(c[2] for c in clocks) else ""
        pattern += next((c[3] for c in sorted(clocks) if c[3]), "")
    column.date_format = pattern
    if best == count and not chosen.startswith("YYYY-"):
        siblings = [p for p in sorted(coverage) if p != chosen and _alias(p) == _alias(chosen)]
        rule = f"; a day above 12 rules out {siblings[0]}" if siblings else ""
        _say(
            column,
            sink,
            "DF-I-DATE-FORMAT",
            f"dates are written as {pattern} in {count} of {count} values{rule}",
        )
    return moments


def _text(value: Any, limit: int) -> str:
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return str(value)[:limit]


def _say(column: ColumnProfile, sink: Notes, code: str, message: str) -> None:
    sink.append((code, message))
    if code not in column.flags:
        column.flags.append(code)


def _top(counts: dict[Any, int], cell_chars: int) -> list[TopValue]:
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], str(pair[0])))
    return [TopValue(value=_text(v, min(cell_chars, 60)), count=c) for v, c in ranked[:_TOP]]
