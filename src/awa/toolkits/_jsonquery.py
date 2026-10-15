"""A small path language for JSON documents.

`orders[0].id`, projections `orders[*].id` / `config.*`, filters `orders[?total > 100].id` (the
`query_csv` expression language; `item` is the element), quoted names `"a b"`, and a final
function `| sum` (`length keys values sum min max avg sort unique first last`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from awa._internal.upstream.tools import ToolException
from awa.kernel.conditions import SafeExpression, as_number, predicate

_NAME = re.compile(r"[A-Za-z_$][\w$-]*")
_INDEX = re.compile(r"-?\d+")


def _numbers(value: Any) -> list[float | int]:
    if not isinstance(value, list):
        raise ToolException("this function needs a list")
    numbers = [as_number(item) for item in value]
    return [number for number in numbers if number is not None]


def _sorted(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ToolException("sort needs a list")
    try:
        return sorted(value)
    except TypeError:
        return sorted(value, key=lambda item: str(item))


def _unique(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ToolException("unique needs a list")
    kept: list[Any] = []
    seen: set[tuple[bool, Any]] = set()
    for item in value:
        # Lists and objects compare by their JSON text, everything else by value: one pass.
        nested = isinstance(item, list | dict)
        key = (nested, json.dumps(item, sort_keys=True, default=str) if nested else item)
        if key not in seen:
            seen.add(key)
            kept.append(item)
    return kept


def _length(value: Any) -> int:
    if isinstance(value, list | dict | str):
        return len(value)
    raise ToolException("length needs a list, an object or a text")


def _keys(value: Any) -> list[str]:
    if not isinstance(value, dict):
        raise ToolException("keys needs an object")
    return list(value)


def _values(value: Any) -> list[Any]:
    if not isinstance(value, dict):
        raise ToolException("values needs an object")
    return list(value.values())


def _avg(value: Any) -> float | None:
    numbers = _numbers(value)
    return sum(numbers) / len(numbers) if numbers else None


def _pick(index: int) -> Callable[[Any], Any]:
    def pick(value: Any) -> Any:
        if not isinstance(value, list):
            raise ToolException("first/last need a list")
        return value[index] if value else None

    return pick


PIPE_FUNCTIONS: dict[str, Callable[[Any], Any]] = {
    "length": _length,
    "keys": _keys,
    "values": _values,
    "sum": lambda value: sum(_numbers(value)),
    "min": lambda value: min(_numbers(value), default=None),
    "max": lambda value: max(_numbers(value), default=None),
    "avg": _avg,
    "sort": _sorted,
    "unique": _unique,
    "first": _pick(0),
    "last": _pick(-1),
}
"""Functions that may end a query after `|`."""

_Step = tuple[str, Any]


def _parse(query: str) -> tuple[list[_Step], list[str]]:
    path, *pipes = _split_pipes(query)
    functions = [pipe.strip() for pipe in pipes]
    for function in functions:
        if function not in PIPE_FUNCTIONS:
            raise ToolException(
                f"unknown function {function!r} (known: {', '.join(sorted(PIPE_FUNCTIONS))})"
            )
    text = path.strip()
    if text in ("", "@", "$"):
        return [], functions
    if text[0] in "@$":
        text = text[1:]
    steps: list[_Step] = []
    position = 0
    while position < len(text):
        char = text[position]
        if char == ".":
            position += 1
            if position < len(text) and text[position] == "*":
                steps.append(("values", None))
                position += 1
                continue
            if position >= len(text):
                raise ToolException("the query ends with '.'")
            char = text[position]
        if char == '"':
            end = text.find('"', position + 1)
            if end < 0:
                raise ToolException("unterminated quoted field name")
            steps.append(("field", text[position + 1 : end]))
            position = end + 1
        elif char == "[":
            end = _closing(text, position)
            inner = text[position + 1 : end].strip()
            if inner in ("", "*"):
                steps.append(("project", None))
            elif inner.startswith("?"):
                steps.append(("filter", predicate(inner[1:])))
            elif _INDEX.fullmatch(inner):
                steps.append(("index", int(inner)))
            elif len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
                steps.append(("field", inner[1:-1]))
            else:
                raise ToolException(f"invalid bracket expression [{inner}]")
            position = end + 1
        else:
            match = _NAME.match(text, position)
            if match is None:
                raise ToolException(f"unexpected {char!r} at position {position} of the query")
            steps.append(("field", match.group()))
            position = match.end()
    return steps, functions


def _split_pipes(query: str) -> list[str]:
    """Split at `|` outside quotes and brackets."""

    parts: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    for index, char in enumerate(query):
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == "|" and depth == 0:
            parts.append(query[start:index])
            start = index + 1
    parts.append(query[start:])
    return parts


def _closing(text: str, start: int) -> int:
    depth = 0
    quote: str | None = None
    for index in range(start, len(text)):
        char = text[index]
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
    raise ToolException("unbalanced '[' in the query")


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else None


def _matches(expression: SafeExpression, item: Any) -> bool:
    def names(name: str) -> Any:
        if isinstance(item, dict) and name in item:
            return item[name]
        if name == "item":
            return item
        return None

    return bool(expression.evaluate(names))


def _apply(value: Any, steps: list[_Step]) -> Any:
    for position, (kind, argument) in enumerate(steps):
        if kind == "field":
            value = _field(value, argument)
        elif kind == "index":
            if not isinstance(value, list):
                return None
            value = value[argument] if -len(value) <= argument < len(value) else None
        else:
            if kind == "values":
                items = list(value.values()) if isinstance(value, dict) else None
            elif kind == "project":
                items = value if isinstance(value, list) else None
            else:
                items = (
                    [item for item in value if _matches(argument, item)]
                    if isinstance(value, list)
                    else None
                )
            if items is None:
                return None
            rest = steps[position + 1 :]
            if not rest:
                return items
            projected = [_apply(item, rest) for item in items]
            return [item for item in projected if item is not None]
        if value is None:
            return None
    return value


def json_query(data: Any, query: str) -> Any:
    """Evaluate `query` against parsed JSON `data`; missing paths give `None`.

    `"orders[?total > 100].id"` filters, `"orders[*].total | sum"` aggregates.
    """

    if len(query) > 2_000:
        raise ToolException("the query is longer than 2000 characters")
    steps, functions = _parse(query)
    result = _apply(data, steps)
    for function in functions:
        result = PIPE_FUNCTIONS[function](result)
    return result
