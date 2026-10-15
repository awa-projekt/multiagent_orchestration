"""Boundary test inputs and overlap notes for a special-case table, with no model.

`boundary_cases` probes every row at its edges (just below, at and above a limit; inside and
outside a list; other case; missing; a two-way number such as `5.000`). `case_notes` finds rows
that can never apply and numbers no row decides.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from awa.workflows._special_cases import (
    ORDERED_OPERATORS,
    CaseCondition,
    CaseOutcome,
    SpecialCases,
    field_label,
    format_value,
    simple_conditions,
)


@dataclass(frozen=True)
class BoundaryCase:
    """One probe of a row and the outcome the table gives for it.

    Attributes:
        row: The row probed, counted from 1.
        probe: What the probe is, in words: `just below 5,000`, `missing`, ...
        values: The full input of the probe (a missing field is absent).
    """

    row: int
    field: str
    probe: str
    values: dict[str, Any]
    expected: CaseOutcome


@dataclass(frozen=True)
class CaseNote:
    """Something about the table worth a look.

    Attributes:
        kind: `"shadowed"` (earlier rows decide every value this row covers), `"contradiction"`
            (the row's conditions can never hold together) or `"gap"` (numbers no row decides).
        row: The row concerned, from 1; `None` for a gap.
    """

    kind: Literal["shadowed", "contradiction", "gap"]
    row: int | None
    text: str


@dataclass(frozen=True)
class _Interval:
    low: float = float("-inf")
    low_in: bool = False
    high: float = float("inf")
    high_in: bool = False

    def empty(self) -> bool:
        return self.low > self.high or (
            self.low == self.high and not (self.low_in and self.high_in)
        )

    def meet(self, other: _Interval) -> _Interval:
        if (other.low, not other.low_in) > (self.low, not self.low_in):
            low, low_in = other.low, other.low_in
        else:
            low, low_in = self.low, self.low_in
        if (other.high, other.high_in) < (self.high, self.high_in):
            high, high_in = other.high, other.high_in
        else:
            high, high_in = self.high, self.high_in
        return _Interval(low, low_in, high, high_in)


def _interval(condition: CaseCondition) -> _Interval | None:
    value = condition.value
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return {
        "<": _Interval(high=number),
        "<=": _Interval(high=number, high_in=True),
        ">": _Interval(low=number),
        ">=": _Interval(low=number, low_in=True),
        "=": _Interval(number, True, number, True),
    }.get(condition.op)


def _number_rows(table: SpecialCases) -> dict[int, tuple[str, _Interval]]:
    """Rows that test one number field only, as the interval of values they cover."""

    found: dict[int, tuple[str, _Interval]] = {}
    for number, row in enumerate(table.cases, 1):
        conditions = simple_conditions(row)
        if not conditions or len({c.field for c in conditions}) != 1:
            continue
        pieces = [_interval(c) for c in conditions]
        if any(piece is None for piece in pieces):
            continue
        covered = _Interval()
        for piece in pieces:
            assert piece is not None
            covered = covered.meet(piece)
        found[number] = (conditions[0].field, covered)
    return found


def _union(intervals: Iterable[_Interval]) -> list[_Interval]:
    """Merge intervals that overlap or touch into a sorted, disjoint list."""

    merged: list[_Interval] = []
    for piece in sorted(intervals, key=lambda i: (i.low, not i.low_in)):
        if piece.empty():
            continue
        if merged:
            last = merged[-1]
            touches = piece.low < last.high or (
                piece.low == last.high and (piece.low_in or last.high_in)
            )
            if touches:
                if (piece.high, piece.high_in) > (last.high, last.high_in):
                    merged[-1] = _Interval(last.low, last.low_in, piece.high, piece.high_in)
                continue
        merged.append(piece)
    return merged


def _covers(union: list[_Interval], piece: _Interval) -> bool:
    return any(part.meet(piece) == piece for part in union)


def _gaps(union: list[_Interval]) -> list[_Interval]:
    gaps: list[_Interval] = []
    low, low_in = float("-inf"), False
    for part in union:
        gap = _Interval(low, low_in, part.low, not part.low_in)
        if not gap.empty() and not (gap.low == gap.high == float("-inf")):
            gaps.append(gap)
        low, low_in = part.high, not part.high_in
    tail = _Interval(low, low_in, float("inf"), False)
    if not tail.empty() and tail.low != float("inf"):
        gaps.append(tail)
    return gaps


def _number(value: float, decimal: str, unit: str) -> str:
    return format_value(int(value) if value.is_integer() else value, decimal, unit)


def _range_words(field_name: str, piece: _Interval, decimal: str) -> str:
    label, unit = field_label(field_name)
    if piece.low == piece.high:
        return f"{label} exactly {_number(piece.low, decimal, unit)}"
    parts = []
    if piece.low != float("-inf"):
        low = _number(piece.low, decimal, unit)
        parts.append(f"from {low}" if piece.low_in else f"above {low}")
    if piece.high != float("inf"):
        high = _number(piece.high, decimal, unit)
        parts.append(f"up to {high}" if piece.high_in else f"below {high}")
    return f"{label} {' and '.join(parts) or 'of any value'}"


def case_notes(table: SpecialCases) -> list[CaseNote]:
    """Rows that can never apply, and numbers no row decides.

    Only rows that test a single number field are analyzed, so no note does not prove that every
    row can apply.
    """

    rows = _number_rows(table)
    notes: list[CaseNote] = []
    for number, (name, covered) in rows.items():
        if covered.empty():
            notes.append(
                CaseNote(
                    "contradiction",
                    number,
                    f"Row {number} never applies: its conditions on {name} cannot hold together.",
                )
            )
            continue
        earlier = [(n, piece) for n, (f, piece) in rows.items() if n < number and f == name]
        if earlier and _covers(_union(piece for _, piece in earlier), covered):
            which = [str(n) for n, piece in earlier if not piece.empty()]
            rows_words = (
                f"row {which[0]} already decides"
                if len(which) == 1
                else (f"rows {', '.join(which[:-1])} and {which[-1]} already decide")
            )
            notes.append(
                CaseNote(
                    "shadowed",
                    number,
                    f"Row {number} never applies: {rows_words} every "
                    f"{field_label(name)[0]} it covers.",
                )
            )
    fields = {name for name, _ in rows.values()}
    if len(rows) == len(table.cases) and len(fields) == 1 and rows:
        (name,) = fields
        for gap in _gaps(_union(piece for _, piece in rows.values())):
            notes.append(
                CaseNote(
                    "gap",
                    None,
                    f"No row decides {_range_words(name, gap, table.decimal)}; "
                    f"these go to else ({table.else_.describe()}).",
                )
            )
    return notes


@dataclass
class _Field:
    whole: bool = True
    digits: int = 0
    texts: list[str] = field(default_factory=list)
    numbers: list[float] = field(default_factory=list)
    neutral: Any = 0


def _digits(value: float) -> int:
    exponent = Decimal(repr(value)).as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _field_facts(table: SpecialCases, rows: list[list[CaseCondition] | None]) -> dict[str, _Field]:
    facts: dict[str, _Field] = {name: _Field() for name in table.fields()}
    for conditions in rows:
        for condition in conditions or []:
            fact = facts.setdefault(condition.field, _Field())
            values = condition.value if isinstance(condition.value, list) else [condition.value]
            first = not fact.texts and not fact.numbers
            for value in values:
                if isinstance(value, bool):
                    continue
                if isinstance(value, float):
                    fact.whole = False
                    fact.digits = max(fact.digits, _digits(value))
                if isinstance(value, int | float):
                    fact.numbers.append(value)
                elif isinstance(value, str):
                    fact.texts.append(value)
            if first:
                fact.neutral = _failing(condition)
    return facts


def _step(fact: _Field) -> int | Decimal:
    return 1 if fact.whole else Decimal(1).scaleb(-max(fact.digits, 2))


def _shift(value: Any, step: int | Decimal, sign: int) -> Any:
    if isinstance(step, int) and isinstance(value, int):
        return value + sign * step
    return float(Decimal(repr(value)) + sign * Decimal(step))


def _other_text(texts: Iterable[str]) -> str:
    taken = set(texts)
    candidate = "other value"
    number = 2
    while candidate in taken:
        candidate = f"other value {number}"
        number += 1
    return candidate


def _outside(values: list[Any]) -> Any:
    numbers = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    if numbers and len(numbers) == len(values):
        return max(numbers) + 1
    return _other_text(str(v) for v in values)


def _failing(condition: CaseCondition) -> Any:
    """A value for which `condition` is false (used for fields the probed row does not read)."""

    value, op = condition.value, condition.op
    if op in ("one of", "=") and isinstance(value, bool):
        return not value
    if op == "one of":
        return _outside(value if isinstance(value, list) else [value])
    if op == "not one of":
        return value[0] if isinstance(value, list) else value
    if op == "=":
        return _outside([value])
    if op == "!=":
        return value
    if isinstance(value, bool) or not isinstance(value, int | float):
        return value  # not reached: ordered conditions hold numbers only
    step = 1 if isinstance(value, int) else 0.01
    return {"<": value, "<=": value + step, ">": value, ">=": value - step}[op]


def _holding(conditions: list[CaseCondition], facts: Mapping[str, _Field]) -> dict[str, Any]:
    """Values for which every condition of a row holds, where one is easy to find."""

    values: dict[str, Any] = {}
    numbers: dict[str, _Interval] = {}
    for condition in conditions:
        piece = _interval(condition)
        if piece is not None:
            numbers[condition.field] = numbers.get(condition.field, _Interval()).meet(piece)
            continue
        value, op = condition.value, condition.op
        if op == "one of" and isinstance(value, list):
            values[condition.field] = value[0]
        elif op == "not one of" and isinstance(value, list):
            values[condition.field] = _outside(value)
        elif op == "=":
            values[condition.field] = value
        elif op == "!=":
            values[condition.field] = (
                _outside([value]) if not isinstance(value, bool) else not value
            )
    for name, piece in numbers.items():
        step = _step(facts.get(name, _Field()))
        if piece.low != float("-inf"):
            point: Any = piece.low if piece.low_in else _shift(piece.low, step, 1)
        elif piece.high != float("inf"):
            point = piece.high if piece.high_in else _shift(piece.high, step, -1)
        else:
            point = 0
        if isinstance(step, int) and isinstance(point, float) and point.is_integer():
            point = int(point)
        values[name] = point
    return values


def _ambiguous(numbers: Iterable[float], decimal: str) -> str:
    """A number that reads two ways under the table's decimal setting (`5.000` or `5,000`)."""

    whole = [int(n) for n in numbers if 1000 <= abs(n) < 1_000_000 and float(n).is_integer()]
    text = f"{whole[0] if whole else 5000:,}"
    return text.replace(",", ".") if decimal == "." else text


def _probes(condition: CaseCondition, fact: _Field, decimal: str) -> list[tuple[str, Any]]:
    value, op = condition.value, condition.op
    _, unit = field_label(condition.field)
    probes: list[tuple[str, Any]] = []
    if op in ORDERED_OPERATORS or (
        op in ("=", "!=") and isinstance(value, int | float) and not isinstance(value, bool)
    ):
        shown = format_value(value, decimal, unit)  # type: ignore[arg-type]
        step = _step(fact)
        probes += [
            (f"just below {shown}", _shift(value, step, -1)),
            (f"exactly {shown}", value),
            (f"just above {shown}", _shift(value, step, 1)),
        ]
    elif isinstance(value, bool):
        probes += [("true", True), ("false", False)]
    elif isinstance(value, str):
        probes += [(f'exactly "{value}"', value), ("another text", _other_text([value]))]
        if value.swapcase() != value:
            probes.append((f'"{value}" in other case', value.swapcase()))
    elif isinstance(value, list):
        probes += [("in the list", value[0]), ("not in the list", _outside(value))]
        first = value[0]
        if isinstance(first, str) and first.swapcase() != first:
            probes.append((f'"{first}" in other case', first.swapcase()))
    return probes


def boundary_cases(table: SpecialCases) -> list[BoundaryCase]:
    """Probe every row at its edges; each probe carries the outcome the table gives for it.

    The other fields are set so the row's own conditions hold where that is easy; rows with
    conditions beyond simple comparisons are probed with missing and two-way numbers only.
    """

    rows = [simple_conditions(row) for row in table.cases]
    facts = _field_facts(table, rows)
    neutral = {name: fact.neutral for name, fact in facts.items()}
    cases: list[BoundaryCase] = []
    for number, conditions in enumerate(rows, 1):
        base = {**neutral, **_holding(conditions or [], facts)}
        probes: list[tuple[str, str, dict[str, Any]]] = []
        for condition in conditions or []:
            for probe, value in _probes(condition, facts[condition.field], table.decimal):
                probes.append((condition.field, probe, {**base, condition.field: value}))
        read = [c.field for c in conditions] if conditions else table.fields(number)
        for name in dict.fromkeys(read):
            limits = [
                v
                for c in conditions or []
                if c.field == name
                for v in (c.value if isinstance(c.value, list) else [c.value])
                if isinstance(v, int | float) and not isinstance(v, bool)
            ]
            text = _ambiguous([*limits, *facts[name].numbers], table.decimal)
            probes.append((name, "missing", {k: v for k, v in base.items() if k != name}))
            probes.append((name, f"two-way number {text}", {**base, name: text}))
        seen: set[str] = set()
        for name, probe, values in probes:
            key = repr(sorted(values.items()))
            if key not in seen:
                seen.add(key)
                cases.append(
                    BoundaryCase(number, name, f"{name} {probe}", values, table.decide(values))
                )
    return cases
