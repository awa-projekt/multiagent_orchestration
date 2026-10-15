"""Special cases: a rule table that decides what happens with a result, first match wins.

```yaml
cases:
  - {if: [{field: amount_eur, op: ">=", value: 5000}], then: ask, by: finance}
  - {if: "status == 'paid'", then: go, to: archive}
else: ask                 # required in the model; default `ask`
when_unclear: ask         # `ask` or `reject`, never `approve`
```

Each row compiles to one strict expression of the condition language
(`awa.kernel.conditions.predicate`) and gives true, false or unknown (a needed value missing,
empty, not a finite number, not a single value, or a two-way number like `5.000`). The first
true row decides; an unknown row stops the table and `when_unclear` decides; `else` decides only
when every row is false. The actions are returned, not carried out.
"""

from __future__ import annotations

import ast
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    Tag,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from awa._internal.upstream.tools import ToolException
from awa.kernel.conditions import SafeExpression, as_number, predicate

ActionName: TypeAlias = Literal["approve", "ask", "reject", "go"]
"""What a special case does: `approve` (no person needed for this step), `ask` (a person
decides), `reject` (the case is refused) or `go` (continue at another step)."""

Operator: TypeAlias = Literal["=", "!=", "<", "<=", ">", ">=", "one of", "not one of"]
"""The comparisons a condition row may use."""

Scalar: TypeAlias = bool | int | float | str
"""One value in a condition: a number, a text or true/false."""

OPERATORS: tuple[str, ...] = ("=", "!=", "<", "<=", ">", ">=", "one of", "not one of")
"""The operators of a condition, in the order the rule table offers them."""

ORDERED_OPERATORS = frozenset({"<", "<=", ">", ">="})
"""Operators that compare numbers by size."""

MAX_ROWS = 5_000
"""Most rows one table may have."""

MAX_TEXT = 500
"""Longest text value, message or list in a condition."""

_ALIASES = {"==": "=", "in": "one of", "not in": "not one of"}
_EXPRESSION_OPS = {
    "=": "==",
    "!=": "!=",
    "<": "<",
    "<=": "<=",
    ">": ">",
    ">=": ">=",
    "one of": "in",
    "not one of": "not in",
}
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
CASE_TABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
"""A step name, and the name of a table of special cases: letters, digits, `_` and `-`,
starting with a letter."""

_RESERVED = frozenset({"true", "false", "null"})


def _finite(value: Any) -> bool:
    return not isinstance(value, float) or math.isfinite(value)


def _two_readings(value: str) -> bool:
    """Whether numeric text reads as a different number with a decimal comma than with a point."""

    point, comma = as_number(value, "."), as_number(value, ",")
    return point is not None and comma is not None and point != comma


def _literal(value: Scalar | list[Scalar]) -> str:
    """`value` as a literal of the condition language."""

    if isinstance(value, list):
        return "[" + ", ".join(_literal(item) for item in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value)


class CaseCondition(BaseModel):
    """One comparison of a row: `field op value`, for example `amount_eur >= 5000`.

    Attributes:
        field: The name of the value compared (letters, digits and `_`, starting with a letter).
        op: One of `=`, `!=`, `<`, `<=`, `>`, `>=`, `one of`, `not one of` (`==`, `in` and
            `not in` are read as `=`, `one of` and `not one of`).
        value: A number for `<`, `<=`, `>`, `>=`; one number, text or true/false for `=` and
            `!=`; a list of them for `one of` and `not one of`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    op: Operator
    value: Scalar | list[Scalar]

    @field_validator("field")
    @classmethod
    def _field_name(cls, value: str) -> str:
        if not _FIELD.fullmatch(value) or value in _RESERVED:
            raise ValueError(
                f"field {value!r} is not a usable name; use letters, digits and _ only, "
                "starting with a letter (for example amount_eur)"
            )
        return value

    @field_validator("op", mode="before")
    @classmethod
    def _operator(cls, value: Any) -> Any:
        if isinstance(value, str):
            text = " ".join(value.split()).lower()
            text = _ALIASES.get(text, text)
            if text not in OPERATORS:
                raise ValueError(f"op {value!r} is not known; use one of: {', '.join(OPERATORS)}")
            return text
        return value

    @model_validator(mode="after")
    def _value_fits(self) -> CaseCondition:
        value, op = self.value, self.op
        if op in ("one of", "not one of"):
            if not isinstance(value, list) or not value:
                raise ValueError(f"'{op}' needs a list of values, for example [Acme, Bolt]")
            if len(value) > MAX_TEXT:
                raise ValueError(f"'{op}' takes at most {MAX_TEXT} values")
            for item in value:
                self._one_value(item)
            return self
        if isinstance(value, list):
            raise ValueError(f"'{op}' compares with one value; for a list use 'one of'")
        self._one_value(value)
        if op in ORDERED_OPERATORS and isinstance(value, bool | str):
            hint = ""
            if isinstance(value, str) and as_number(value) is not None:
                hint = "; write it without quotes, separators or a currency, for example 5000"
            raise ValueError(f"'{op}' needs a number as its value, got {value!r}{hint}")
        return self

    @staticmethod
    def _one_value(value: Scalar) -> None:
        if not _finite(value):
            raise ValueError(f"the value {value!r} is not a finite number")
        if isinstance(value, str):
            if len(value) > MAX_TEXT:
                raise ValueError(f"a text value may have at most {MAX_TEXT} characters")
            if _two_readings(value):
                raise ValueError(
                    f"the value {value!r} can be read as two different numbers; "
                    "write it as a plain number such as 5000 or 5.5"
                )

    def expression(self) -> str:
        """The condition in the shared condition language, for example `amount_eur >= 5000`."""

        return f"{self.field} {_EXPRESSION_OPS[self.op]} {_literal(self.value)}"


class CaseAction(BaseModel):
    """What happens: `approve`, `ask` (a person decides), `reject` or `go` (to another step).

    Written as a word (`ask`), as `go: <step>`, or as a mapping with `then` and its options.

    Attributes:
        to: The step to continue at; required for `go` only. Checked where the table is attached
            to a process.
        by: The role that decides; only for `ask`.
        message: One sentence the person or the record reads, for example why it was rejected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    then: ActionName
    to: str | None = Field(default=None, max_length=64)
    by: str | None = Field(default=None, min_length=1, max_length=80)
    message: str = Field(default="", max_length=MAX_TEXT)

    @model_validator(mode="before")
    @classmethod
    def _short_forms(cls, data: Any) -> Any:
        if isinstance(data, str):
            data = {"then": data}
        if isinstance(data, Mapping) and isinstance(data.get("then"), str):
            then = data["then"].strip()
            word, colon, step = then.partition(":")
            if colon and word.strip().lower() == "go" and "to" not in data:
                return {**data, "then": "go", "to": step.strip()}
            return {**data, "then": then.lower()}
        return data

    @model_validator(mode="after")
    def _options_fit(self) -> CaseAction:
        if self.then == "go":
            if not self.to:
                raise ValueError("'go' needs the step to continue at: then: go, to: <step>")
            if not CASE_TABLE_NAME.fullmatch(self.to):
                raise ValueError(
                    f"step {self.to!r} is not a step name; use letters, digits, _ and -, "
                    "starting with a letter"
                )
        elif self.to is not None:
            raise ValueError(
                f"'to' belongs to 'go' only; remove it or write then: go (not {self.then})"
            )
        if self.by is not None and self.then != "ask":
            raise ValueError(f"'by' names who decides and belongs to 'ask' only (not {self.then})")
        return self

    def describe(self) -> str:
        """The action in plain words: `a person decides (finance)`, `approved`, `continue at archive`."""

        if self.then == "approve":
            words = "approved without asking a person"
        elif self.then == "ask":
            words = "a person decides" + (f" ({self.by})" if self.by else "")
        elif self.then == "reject":
            words = "rejected"
        else:
            words = f"continue at step {self.to}"
        return f"{words}. {self.message}" if self.message else words


class CaseRow(CaseAction):
    """One row of the table: a condition and what happens when it holds.

    Attributes:
        if_: The condition, written `if:`: a list of `{field, op, value}` conditions that must all
            hold, or a condition-language expression.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True
    )

    if_: Annotated[
        Annotated[list[CaseCondition], Tag("conditions")] | Annotated[str, Tag("expression")],
        Discriminator(lambda value: "expression" if isinstance(value, str) else "conditions"),
    ] = Field(alias="if")

    @model_serializer(mode="wrap")
    def _if_first(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Write `if` before what happens, as people read a row."""

        data: dict[str, Any] = handler(self)
        key = "if" if "if" in data else "if_"
        return {key: data.pop(key), **data} if key in data else data

    @field_validator("if_")
    @classmethod
    def _condition_given(cls, value: list[CaseCondition] | str) -> list[CaseCondition] | str:
        if isinstance(value, str):
            text = " ".join(value.split())
            if not text:
                raise ValueError("'if' is empty; write a condition such as amount_eur >= 5000")
            return text
        if not value:
            raise ValueError("'if' has no conditions; add at least one {field, op, value}")
        return value

    def expression(self) -> str:
        """The row's condition as one expression of the condition language."""

        if isinstance(self.if_, str):
            return self.if_
        return " and ".join(condition.expression() for condition in self.if_)


@dataclass(frozen=True)
class CaseOutcome:
    """What the table decided for one set of values.

    Attributes:
        source: `"row"` (a row held), `"else"` (every row was false) or `"unclear"` (a row could
            not be decided, so `when_unclear` applies).
        row: Number of the deciding or unclear row, from 1; `None` for `"else"`.
        reason: For `"unclear"`, what was unclear, in one sentence.
    """

    action: CaseAction
    source: Literal["row", "else", "unclear"]
    row: int | None = None
    reason: str = ""

    @property
    def then(self) -> ActionName:
        """The action's name: `approve`, `ask`, `reject` or `go`."""

        return self.action.then

    def describe(self) -> str:
        """One line: `row 2: approved ...`, `else: a person decides`, `unclear at row 1 (...): ...`."""

        if self.source == "row":
            return f"row {self.row}: {self.action.describe()}"
        if self.source == "else":
            return f"else: {self.action.describe()}"
        return f"unclear at row {self.row} ({self.reason}): {self.action.describe()}"


@dataclass(frozen=True)
class _Compiled:
    parsed: SafeExpression
    single_values: bool  # condition rows: each field holds one value, never a list
    number_fields: frozenset[str]


class SpecialCases(BaseModel):
    """A rule table of special cases: rows tried in order, `else`, and `when_unclear`.

    A bare list of rows is the short form of `cases`.

    Attributes:
        cases: The rows, in order; the first row that holds decides. Up to 5,000.
        else_: What happens when no row holds, written `else:`; default `ask`. `approve` gives a
            warning (see `warnings`).
        when_unclear: What happens when a row cannot be decided: `ask` (default) or `reject`.
        decimal: The decimal separator assumed for numbers written as text, `"."` (default) or
            `","`; `5.000` and `5,000` stay unclear either way.

    Example:
        ```python
        table = SpecialCases.model_validate({
            "cases": [{"if": [{"field": "amount_eur", "op": ">=", "value": 5000}], "then": "ask"}],
            "else": "approve",
        })
        table.decide({"amount_eur": 6200}).then      # "ask"
        table.decide({}).source                      # "unclear"
        ```
    """

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, serialize_by_alias=True, frozen=True
    )

    cases: list[CaseRow] = Field(default_factory=list, max_length=MAX_ROWS)
    else_: CaseAction = Field(default=CaseAction(then="ask"), alias="else")
    when_unclear: CaseAction = CaseAction(then="ask")
    decimal: Literal[".", ","] = "."

    _compiled: list[_Compiled] = PrivateAttr(default_factory=list)

    @field_serializer("else_", "when_unclear", mode="wrap")
    def _action_word(self, action: CaseAction, handler: SerializerFunctionWrapHandler) -> Any:
        """An action without options as its word (`ask`, `go: archive`), as it is written."""

        if action.by is None and not action.message:
            return f"go: {action.to}" if action.then == "go" else action.then
        return handler(action)

    @model_validator(mode="before")
    @classmethod
    def _bare_list(cls, data: Any) -> Any:
        return {"cases": data} if isinstance(data, list) else data

    @field_validator("when_unclear")
    @classmethod
    def _never_approve(cls, value: CaseAction) -> CaseAction:
        if value.then not in ("ask", "reject"):
            raise ValueError(
                f"when_unclear cannot be '{value.then}': a missing or unclear value must never "
                "decide on its own; use 'ask' (a person decides) or 'reject'"
            )
        return value

    @model_validator(mode="after")
    def _compile(self) -> SpecialCases:
        compiled: list[_Compiled] = []
        for number, row in enumerate(self.cases, 1):
            try:
                parsed = predicate(_stripped(row.expression()), decimal=self.decimal, strict=True)
            except ToolException as exc:
                raise ValueError(f"row {number}: the condition cannot be read: {exc}") from None
            compiled.append(
                _Compiled(
                    parsed,
                    single_values=isinstance(row.if_, list),
                    number_fields=_number_names(row.expression()),
                )
            )
        self._compiled = compiled
        return self

    def __eq__(self, other: object) -> bool:
        """Tables are equal when their rows and settings are (the compiled form is not compared)."""

        if not isinstance(other, SpecialCases):
            return NotImplemented
        return self.model_dump() == other.model_dump()

    __hash__ = None  # type: ignore[assignment]  # rows are lists: a table is not hashable

    @property
    def warnings(self) -> list[str]:
        """Things that are allowed but worth a second look (shown amber): `else: approve`."""

        if self.else_.then == "approve":
            return [
                "else is 'approve': every case no row decides is approved without a person; "
                "check that the rows catch every case that needs a person, or use else: ask"
            ]
        return []

    def fields(self, row: int | None = None) -> list[str]:
        """The value names the rows read (only row `row`, from 1, when given), in order of
        first use; names inside an `if:` text in alphabetical order."""

        names: dict[str, None] = {}
        pairs = list(zip(self.cases, self._compiled, strict=True))
        for entry, compiled in pairs if row is None else [pairs[row - 1]]:
            if isinstance(entry.if_, list):
                names.update((c.field, None) for c in entry.if_)
            else:
                names.update((name, None) for name in sorted(compiled.parsed.names))
        return list(names)

    def check_row(self, number: int, values: Mapping[str, Any]) -> tuple[bool | None, str]:
        """Whether row `number` (from 1) holds for `values`: `True`, `False` or `None` with the reason."""

        if not 1 <= number <= len(self._compiled):
            raise IndexError(f"the table has {len(self._compiled)} row(s), not row {number}")
        compiled = self._compiled[number - 1]
        try:
            result = compiled.parsed.evaluate(_reader(values, compiled, self.decimal))
        except ToolException as exc:
            return None, str(exc)
        if not isinstance(result, bool):
            return None, "the condition does not give true or false"
        return result, ""

    def decide(self, values: Mapping[str, Any]) -> CaseOutcome:
        """Decide one case: the first row that holds, `when_unclear` at the first unknown row, else `else`.

        A name that is absent from `values` or `None` is missing, so unclear.
        """

        for number, row in enumerate(self.cases, 1):
            holds, reason = self.check_row(number, values)
            if holds is None:
                return CaseOutcome(self.when_unclear, "unclear", number, reason)
            if holds:
                return CaseOutcome(row, "row", number)
        return CaseOutcome(self.else_, "else")

    def describe(self) -> list[str]:
        """The table in plain words: one line per row, then `else` and `when_unclear`."""

        lines = [
            f"If {describe_condition(row, self.decimal)}: {row.describe()}" for row in self.cases
        ]
        lines.append(f"Otherwise: {self.else_.describe()}")
        lines.append(f"When a value is missing or unclear: {self.when_unclear.describe()}")
        return lines


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _reader(values: Mapping[str, Any], compiled: _Compiled, decimal: str) -> Callable[[str], Any]:
    """The name resolver of one row: what is missing, not finite, a list where one value is
    expected, or not one clear number where only numbers are compared raises (unknown)."""

    def read(name: str) -> Any:
        value = values.get(name)
        if value is None:
            raise ToolException(f"{name} has no value")
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ToolException(f"{name} is not a finite number")
            value = int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, float) and not math.isfinite(value):
            raise ToolException(f"{name} is not a finite number")
        if compiled.single_values and not isinstance(value, bool | int | float | str):
            raise ToolException(f"{name} holds {_kind(value)}, not one value")
        if name in compiled.number_fields:
            if isinstance(value, bool):
                raise ToolException(f"{name} is true or false, not a number")
            if isinstance(value, str):
                if _two_readings(value):
                    raise ToolException(
                        f"the number {value.strip()!r} in {name} can be read two ways "
                        "(decimal or thousands)"
                    )
                if as_number(value, decimal) is None:
                    raise ToolException(f"{name} is text that does not read as a number")
        # Text compares without surrounding spaces (case still counts), as the rows do.
        return value.strip() if isinstance(value, str) else value

    return read


def _stripped(expression: str) -> str:
    """`expression` with every text literal stripped; case still counts."""

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return expression  # `predicate` reports the error
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = node.value.strip()
    return ast.unparse(tree)


def _number_names(expression: str) -> frozenset[str]:
    """Names the expression compares with numbers only, so text that is not one clear number is unclear."""

    numbers: set[str] = set()
    others: set[str] = set()
    for node in ast.walk(ast.parse(expression, mode="eval")):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        names = {o.id for o in operands if isinstance(o, ast.Name) and o.id not in _RESERVED}
        constants = [value for o in operands for value in _constants(o)]
        if constants and all(_is_number(value) for value in constants):
            numbers |= names
        elif constants:
            others |= names
    return frozenset(numbers - others)


def _constants(node: ast.AST) -> list[Any]:
    """The fixed values of one operand: a literal, `true`/`false`, a negative number or a list."""

    ok, value = _constant(node)
    if not ok:
        return []
    return list(value) if isinstance(value, list) else [value]


def _kind(value: Any) -> str:
    if isinstance(value, list | tuple | set | frozenset):
        return "a list"
    if isinstance(value, Mapping):
        return "an object"
    return f"a {type(value).__name__}"


_UNITS = {"eur": "EUR", "usd": "USD", "gbp": "GBP", "chf": "CHF", "pct": "percent"}
_WORDS = {
    "=": "is {v}",
    "!=": "is not {v}",
    "<": "is less than {v}",
    "<=": "is {v} or less",
    ">": "is more than {v}",
    ">=": "is {v} or more",
    "one of": "is one of {v}",
    "not one of": "is none of {v}",
}


def field_label(field: str) -> tuple[str, str]:
    """A field name as words and its unit: `amount_eur` gives `("amount", "EUR")`."""

    base, _, suffix = field.rpartition("_")
    if base and suffix.lower() in _UNITS:
        return base.replace("_", " "), _UNITS[suffix.lower()]
    return field.replace("_", " "), ""


def format_value(value: Scalar, decimal: str = ".", unit: str = "") -> str:
    """A value as people read it: `5,000 EUR` (or `5.000 EUR` with decimal `","`), `"Acme"`, `yes`."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        text = f"{value:,}"
        if decimal == ",":
            text = text.translate(str.maketrans(",.", ".,"))
        return f"{text} {unit}" if unit else text
    return f'"{value}"'


def _listing(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def describe_condition(row: CaseRow, decimal: str = ".") -> str:
    """A row's condition in plain words: `amount is 5,000 EUR or more and supplier is "Acme"`."""

    conditions = simple_conditions(row)
    if conditions is None:
        return str(row.if_)
    parts = []
    for condition in conditions:
        label, unit = field_label(condition.field)
        values = condition.value if isinstance(condition.value, list) else [condition.value]
        shown = _listing([format_value(v, decimal, unit) for v in values])
        parts.append(f"{label} {_WORDS[condition.op].format(v=shown)}")
    return " and ".join(parts)


_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=", "!=": "!="}
_AST_OPS: dict[type[ast.cmpop], str] = {
    ast.Eq: "=",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.In: "one of",
    ast.NotIn: "not one of",
}
_NAMED = {"true": True, "false": False}


def _constant(node: ast.AST) -> tuple[bool, Any]:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool | int | float | str):
        return True, node.value
    if isinstance(node, ast.Name) and node.id in _NAMED:
        return True, _NAMED[node.id]
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int | float)
        and not isinstance(node.operand.value, bool)
    ):
        return True, -node.operand.value
    if isinstance(node, ast.List | ast.Tuple):
        items = [_constant(item) for item in node.elts]
        if items and all(ok for ok, _ in items):
            return True, [value for _, value in items]
    return False, None


def _compare(node: ast.AST) -> CaseCondition | None:
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1):
        return None
    op = _AST_OPS.get(type(node.ops[0]))
    left, right = node.left, node.comparators[0]
    if op is None:
        return None
    if isinstance(left, ast.Name) and left.id not in _NAMED:
        ok, value = _constant(right)
        field = left.id
    elif isinstance(right, ast.Name) and right.id not in _NAMED and op in _FLIP:
        ok, value = _constant(left)
        field, op = right.id, _FLIP[op]
    else:
        return None
    if not ok:
        return None
    try:
        return CaseCondition(field=field, op=op, value=value)
    except ValueError:
        return None


def simple_conditions(row: CaseRow) -> list[CaseCondition] | None:
    """The row as `{field, op, value}` conditions joined with AND, or `None` when it is more complex."""

    if isinstance(row.if_, list):
        return list(row.if_)
    body = ast.parse(row.if_, mode="eval").body
    parts = body.values if isinstance(body, ast.BoolOp) and isinstance(body.op, ast.And) else [body]
    found = [_compare(part) for part in parts]
    if any(condition is None for condition in found):
        return None
    return [condition for condition in found if condition is not None]
