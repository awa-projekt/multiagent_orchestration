"""A small, safe expression language: arithmetic, comparisons and a few functions.

Parsed with `ast` and evaluated over an allowlist of node types (never `eval`); names resolve
through a callback; attributes, subscripts, lambdas and comprehensions are rejected, and numbers
are bounded so `9**9**9` cannot exhaust memory.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from langchain_core.tools import ToolException

MAX_EXPRESSION_CHARS = 2_000
"""Longest expression accepted."""

_MAX_INT_BITS = 16_384
_MAX_NODES = 500
_MAX_ROUND_DIGITS = 400

_ARITHMETIC: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_COMPARISONS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _numeric(value: Any) -> Any:
    """Numeric text (a CSV cell, a JSON string argument) as a number; other values unchanged."""

    number = as_number(value) if isinstance(value, str) else None
    return value if number is None else number


def _round(value: Any, digits: Any = None) -> Any:
    """`round`, refusing digit counts whose power of ten would take minutes to compute."""

    if digits is None:
        return round(_numeric(value))
    if not isinstance(digits, int) or abs(digits) > _MAX_ROUND_DIGITS:
        raise ToolException(f"round() takes a whole number of digits up to {_MAX_ROUND_DIGITS}")
    return round(_numeric(value), digits)


MATH_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "round": _round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "floor": math.floor,
    "ceil": math.ceil,
    "hypot": math.hypot,
}
"""Functions the calculator understands."""

MATH_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}
"""Named constants the calculator understands."""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def filter_functions(decimal: str = ".") -> dict[str, Callable[..., Any]]:
    """The functions of filter expressions, reading numeric text with `decimal` (see `as_number`)."""

    def numeric(value: Any) -> Any:
        number = as_number(value, decimal) if isinstance(value, str) else None
        return value if number is None else number

    def rounded(value: Any, digits: Any = None) -> Any:
        return _round(numeric(value), digits)

    return {
        "contains": lambda value, part: _text(part).lower() in _text(value).lower(),
        "startswith": lambda value, prefix: _text(value).lower().startswith(_text(prefix).lower()),
        "endswith": lambda value, suffix: _text(value).lower().endswith(_text(suffix).lower()),
        "lower": lambda value: _text(value).lower(),
        "upper": lambda value: _text(value).upper(),
        "len": lambda value: (
            len(value) if isinstance(value, list | dict | str) else len(_text(value))
        ),
        "number": lambda value: as_number(value, decimal),
        "empty": lambda value: value is None or _text(value).strip() == "",
        "abs": lambda value: abs(numeric(value)),
        "round": rounded,
        "min": min,
        "max": max,
    }


FILTER_FUNCTIONS: dict[str, Callable[..., Any]] = filter_functions()
"""Functions available in filter expressions (`where`, `[?...]`)."""


_AFFIX = re.compile(r"^(?:[€$£¥]|(?:EUR|USD|GBP|CHF)\b)\s*|\s*(?:[€$£¥%]|\b(?:EUR|USD|GBP|CHF))$")
_GROUP_SPACE = re.compile(r"(?<=\d)[   '’](?=\d{3}(?!\d))")


def _grouped(text: str, separator: str) -> bool:
    """Whether `text` is digits in groups of three after the first (`1.234.567`)."""

    first, *rest = text.split(separator)
    return 1 <= len(first) <= 3 and bool(rest) and all(len(g) == 3 for g in rest)


def _formatted(text: str, decimal: str) -> float | int | None:
    """Numbers as reports write them: `1.234,56 €`, `(1,234.56)`, `12,5 %`, `1.234,56-`."""

    text = text.replace("−", "-")
    negative = signed = False
    for _ in range(2):  # signs and parentheses outside and inside a currency sign
        if text[:1] == "(" and text[-1:] == ")":
            negative, text = True, text[1:-1].strip()
        if text[-1:] == "-" or text[:1] in ("-", "+"):
            if signed:
                return None  # `--1`, `+-1`
            signed = True
            if text[-1:] == "-":
                negative, text = True, text[:-1].strip()
            else:
                negative, text = negative or text[0] == "-", text[1:].strip()
        text = _AFFIX.sub("", text).strip()
    digits = _GROUP_SPACE.sub("", text)
    if not re.fullmatch(r"\d[\d.,]*\d|\d", digits):
        return None
    commas, dots = digits.count(","), digits.count(".")
    if commas and dots:
        point = "," if digits.rfind(",") > digits.rfind(".") else "."
    elif commas:
        point = "," if commas == 1 and (decimal == "," or not _grouped(digits, ",")) else "."
    elif dots:
        point = "." if dots == 1 and (decimal == "." or not _grouped(digits, ".")) else ","
    else:
        point = "."
    group = "," if point == "." else "."
    whole, _, fraction = digits.partition(point)
    if point in fraction or group in fraction or (group in whole and not _grouped(whole, group)):
        return None
    whole = whole.replace(group, "")
    number: float | int = float(f"{whole}.{fraction}") if fraction else int(whole)
    return -number if negative else number


def as_number(value: Any, decimal: str = ".") -> float | int | None:
    """Return `value` as a number when it is one or reads as one, else `None`.

    Lenient on purpose, for reading reports: thousands separators (`1,234.56`, `1.234,56`,
    `1 234`), currency signs and codes (`€ 12`, `12 EUR`), percent (`12,5 %` is 12.5), negatives in
    parentheses or with a trailing minus, and underscores are accepted; `inf` and `nan` are not.
    `decimal` settles `1,234` / `1.234`. Digit-only IDs (`004711`) read as numbers too; compare
    such columns as text.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    plain = text.replace("_", "")
    try:
        return int(plain)
    except ValueError:
        pass
    if decimal == "." or "." not in plain:
        try:
            number = float(plain)
        except ValueError:
            pass
        else:
            return number if math.isfinite(number) else None
    return _formatted(text, decimal)


_COMMA_DECIMAL = re.compile(r"\d{1,3}(?:\.\d{3})+,\d+|\d+,(?:\d{1,2}|\d{4,})")
_POINT_DECIMAL = re.compile(r"\d{1,3}(?:,\d{3})+\.\d+|\d+\.(?:\d{1,2}|\d{4,})")


def detect_decimal(cells: Iterable[Any]) -> str:
    """`","` when the text cells use a decimal comma (`12,5`, `1.234,56`) more often than a
    decimal point (`12.5`, `1,234.56`), else `"."`."""

    comma = point = 0
    for cell in cells:
        if not isinstance(cell, str):
            continue
        text = _AFFIX.sub("", cell.strip().strip("()-+")).strip()
        if _COMMA_DECIMAL.fullmatch(text):
            comma += 1
        elif _POINT_DECIMAL.fullmatch(text):
            point += 1
    return "," if comma > point else "."


def _coerced(left: Any, right: Any, decimal: str = ".") -> tuple[Any, Any]:
    """Compare numbers as numbers (also numeric text, as in CSV cells), else as values."""

    left_number, right_number = as_number(left, decimal), as_number(right, decimal)
    if left_number is not None and right_number is not None:
        return left_number, right_number
    if isinstance(left, str) or isinstance(right, str):
        if left is None or right is None:
            return left, right
        return _text(left), _text(right)
    return left, right


_AMBIGUOUS_NUMBER = re.compile(r"\s*[+-]?\d{1,3}[.,]\d{3}\s*")


def _refuse_guess(op: ast.cmpop, left: Any, right: Any) -> None:
    """Strict comparisons: no answer for a missing, non-finite, non-numeric or two-way (`5.000`) value."""

    ordered = isinstance(op, ast.Lt | ast.LtE | ast.Gt | ast.GtE)
    equality = isinstance(op, ast.Eq | ast.NotEq)
    if ordered and (left is None or right is None):
        raise ToolException("a value needed for the comparison is missing")
    for value in (left, right):
        if isinstance(value, float) and not math.isfinite(value):
            raise ToolException(f"a number is needed for the comparison, got {_kind_of(value)}")
    if ordered:
        for value, other in ((left, right), (right, left)):
            if as_number(other) is not None and as_number(value) is None:
                raise ToolException(f"a number is needed for the comparison, got {_kind_of(value)}")
    if equality:
        # `total == 0` with a list, an object, true or text that is no number cannot decide; a
        # missing value is a plain no, and text compared with text stays a text comparison.
        for value, other in ((left, right), (right, left)):
            is_number = isinstance(other, int | float) and not isinstance(other, bool)
            if is_number and value is not None and as_number(value) is None:
                raise ToolException(f"a number is needed for the comparison, got {_kind_of(value)}")
    for text, other in ((left, right), (right, left)):
        if (
            isinstance(text, str)
            and _AMBIGUOUS_NUMBER.fullmatch(text)
            and as_number(other) is not None
        ):
            raise ToolException(f"the number {text.strip()!r} is ambiguous (decimal or thousands)")


_NUMERIC_FUNCTIONS = frozenset({"abs", "round", "min", "max", "number"})
"""Filter functions that read their arguments as numbers (checked in strict mode)."""


def _refuse_non_number(value: Any) -> None:
    """Strict numeric arguments: finite numbers, or text that reads as one without a guess."""

    if isinstance(value, list | tuple):
        for item in value:
            _refuse_non_number(item)
        return
    if isinstance(value, bool | dict) or (isinstance(value, float) and not math.isfinite(value)):
        raise ToolException(f"a number is needed, got {_kind_of(value)}")
    if isinstance(value, str) and _AMBIGUOUS_NUMBER.fullmatch(value):
        raise ToolException(f"the number {value.strip()!r} is ambiguous (decimal or thousands)")
    if isinstance(value, str) and value.strip().lower().lstrip("+-") in {"nan", "inf", "infinity"}:
        raise ToolException(f"a number is needed, got {_kind_of(value)}")


def _kind_of(value: Any) -> str:
    """A short, value-free description of what was given instead of a number."""

    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, float):
        return "a value that is not a finite number"
    if isinstance(value, list | tuple):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, str):
        return "text that does not read as a number"
    return type(value).__name__


def _equal(left: Any, right: Any, decimal: str = ".") -> bool:
    a, b = _coerced(left, right, decimal)
    return bool(a == b)


class SafeExpression:
    """A parsed expression, evaluated against a name resolver.

    Args:
        constants: Names with fixed values (checked before `names`).
        allow_names: Whether other names are allowed (resolved at evaluation).
        allow_comparisons: Whether comparisons, `and`/`or`/`not` and `in` are allowed.
        allow_strings: Whether string, boolean and `None` literals are allowed.
        decimal: Decimal separator assumed for ambiguous numeric text (see `as_number`).
        strict: Fail closed instead of guessing: ordering a missing value, comparing or computing
            with a value that is not a finite number, or with two-way numeric text (`5.000`),
            raises.

    Raises:
        ToolException: If the expression is too long, malformed, or uses anything outside the
            allowlist.
    """

    def __init__(
        self,
        expression: str,
        *,
        functions: Mapping[str, Callable[..., Any]],
        constants: Mapping[str, Any] | None = None,
        allow_names: bool = True,
        allow_comparisons: bool = True,
        allow_strings: bool = True,
        decimal: str = ".",
        strict: bool = False,
    ) -> None:
        if len(expression) > MAX_EXPRESSION_CHARS:
            raise ToolException(f"expression is longer than {MAX_EXPRESSION_CHARS} characters")
        try:
            tree = ast.parse(expression.strip(), mode="eval")
        except SyntaxError as exc:
            raise ToolException(f"invalid expression: {exc.msg}") from None
        self._functions = dict(functions)
        self._constants = dict(constants or {})
        self._allow_names = allow_names
        self._allow_comparisons = allow_comparisons
        self._allow_strings = allow_strings
        self._decimal = decimal
        self._strict = strict
        nodes = list(ast.walk(tree))
        if len(nodes) > _MAX_NODES:
            raise ToolException("expression is too complex")
        for node in nodes:
            self._validate(node)
        self._tree = tree
        called = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
        self._names = frozenset(
            node.id
            for node in nodes
            if isinstance(node, ast.Name)
            and id(node) not in called
            and node.id not in self._constants
        )

    @property
    def names(self) -> frozenset[str]:
        """The names the expression reads (neither called functions nor constants)."""

        return self._names

    def _validate(self, node: ast.AST) -> None:
        allowed: tuple[type[ast.AST], ...] = (
            ast.Expression,
            ast.Constant,
            ast.BinOp,
            ast.UnaryOp,
            ast.UAdd,
            ast.USub,
            ast.Call,
            ast.Name,
            ast.Load,
            *_ARITHMETIC,
        )
        if self._allow_comparisons:
            allowed += (
                ast.Compare,
                ast.BoolOp,
                ast.And,
                ast.Or,
                ast.Not,
                ast.In,
                ast.NotIn,
                ast.List,
                ast.Tuple,
                *_COMPARISONS,
            )
        if not isinstance(node, allowed):
            raise ToolException(f"{type(node).__name__} is not allowed in expressions")
        if isinstance(node, ast.Constant):
            value = node.value
            is_number = isinstance(value, int | float) and not isinstance(value, bool)
            if not is_number and not (
                self._allow_strings and (isinstance(value, str | bool) or value is None)
            ):
                raise ToolException(f"literal {value!r} is not allowed in expressions")
            if is_number:
                self._bounded(value)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in self._functions:
                name = node.func.id if isinstance(node.func, ast.Name) else "this call"
                known = ", ".join(sorted(self._functions))
                raise ToolException(f"unknown function {name!r} (known: {known})")
            if node.keywords:
                raise ToolException("keyword arguments are not allowed in expressions")
        elif isinstance(node, ast.Name):
            if node.id.startswith("__"):
                raise ToolException(f"name {node.id!r} is not allowed")
            if (
                not self._allow_names
                and node.id not in self._constants
                and node.id not in self._functions
            ):
                raise ToolException(f"unknown name {node.id!r}")

    def evaluate(self, names: Callable[[str], Any] | None = None) -> Any:
        """Evaluate the expression; `names` resolves every non-constant name.

        Raises:
            ToolException: If the evaluation fails (division by zero, an unknown name, a result that is
                too large).
        """

        try:
            return self._eval(self._tree.body, names)
        except ToolException:
            raise
        except (ArithmeticError, ValueError, TypeError) as exc:
            raise ToolException(f"cannot evaluate the expression: {exc}") from None

    def _eval(self, node: ast.AST, names: Callable[[str], Any] | None) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self._constants:
                return self._constants[node.id]
            if names is None:
                raise ToolException(f"unknown name {node.id!r}")
            return names(node.id)
        if isinstance(node, ast.UnaryOp):
            operand = self._eval(node.operand, names)
            if isinstance(node.op, ast.Not):
                return not operand
            number = self._number(operand)
            return -number if isinstance(node.op, ast.USub) else +number
        if isinstance(node, ast.BinOp):
            left = self._number(self._eval(node.left, names))
            right = self._number(self._eval(node.right, names))
            return self._arithmetic(node.op, left, right)
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(self._eval(value, names) for value in node.values)
            return any(self._eval(value, names) for value in node.values)
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, names)
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                right = self._eval(comparator, names)
                if not self._compare(op, left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.List | ast.Tuple):
            return [self._eval(element, names) for element in node.elts]
        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name)
            args = [self._eval(arg, names) for arg in node.args]
            if self._strict and node.func.id in _NUMERIC_FUNCTIONS:
                for arg in args:
                    _refuse_non_number(arg)
            return self._bounded(self._functions[node.func.id](*args))
        raise ToolException(f"{type(node).__name__} is not allowed in expressions")

    def _number(self, value: Any) -> Any:
        if self._strict:
            _refuse_non_number(value)
        number = as_number(value, self._decimal)
        if number is None:
            raise ToolException(f"{value!r} is not a number")
        return number

    def _arithmetic(self, op: ast.operator, left: Any, right: Any) -> Any:
        if isinstance(op, ast.Pow) and isinstance(left, int) and isinstance(right, int):
            if right < 0:
                return self._bounded(float(left) ** right)
            if abs(left) > 1 and right * max(left.bit_length(), 1) > _MAX_INT_BITS:
                raise ToolException("the result is too large")
        return self._bounded(_ARITHMETIC[type(op)](left, right))

    @staticmethod
    def _bounded(value: Any) -> Any:
        if isinstance(value, int) and not isinstance(value, bool):
            if value.bit_length() > _MAX_INT_BITS:
                raise ToolException("the result is too large")
        elif isinstance(value, float) and not math.isfinite(value):
            raise ToolException("the result is not a finite number")
        elif isinstance(value, complex):
            raise ToolException("the result is not a real number")
        return value

    def _compare(self, op: ast.cmpop, left: Any, right: Any) -> bool:
        decimal = self._decimal
        if self._strict:
            _refuse_guess(op, left, right)
        if isinstance(op, ast.In | ast.NotIn):
            if isinstance(right, list):
                if self._strict:  # each comparison with a list item fails closed like `==`
                    for item in right:
                        _refuse_guess(ast.Eq(), left, item)
                found = any(_equal(left, item, decimal) for item in right)
            elif isinstance(right, str):
                found = _text(left) in right
            else:
                raise ToolException("'in' needs a list or a text on its right side")
            return found if isinstance(op, ast.In) else not found
        if isinstance(op, ast.Eq):
            return _equal(left, right, decimal)
        if isinstance(op, ast.NotEq):
            return not _equal(left, right, decimal)
        a, b = _coerced(left, right, decimal)
        if a is None or b is None:
            return False
        try:
            return bool(_COMPARISONS[type(op)](a, b))
        except TypeError:
            return False


def calculate(expression: str) -> int | float:
    """Evaluate an arithmetic expression safely.

    Raises:
        ToolException: If the expression is not plain arithmetic or cannot be evaluated.
    """

    result = SafeExpression(
        expression,
        functions=MATH_FUNCTIONS,
        constants=MATH_CONSTANTS,
        allow_names=False,
        allow_comparisons=False,
        allow_strings=False,
    ).evaluate()
    if isinstance(result, bool) or not isinstance(result, int | float):
        raise ToolException("the expression does not evaluate to a number")
    return result


def predicate(expression: str, *, decimal: str = ".", strict: bool = False) -> SafeExpression:
    """Parse a filter expression (comparisons, `and`/`or`/`not`, `in`, filter functions).

    `decimal` and `strict` work as for `SafeExpression`.

    Raises:
        ToolException: If the expression is malformed or not allowed.
    """

    return SafeExpression(
        expression,
        functions=FILTER_FUNCTIONS if decimal == "." else filter_functions(decimal),
        constants={"true": True, "false": False, "null": None},
        decimal=decimal,
        strict=strict,
    )


__all__ = [
    "FILTER_FUNCTIONS",
    "MATH_CONSTANTS",
    "MATH_FUNCTIONS",
    "MAX_EXPRESSION_CHARS",
    "SafeExpression",
    "as_number",
    "calculate",
    "detect_decimal",
    "filter_functions",
    "predicate",
]
