"""Safe text templates that turn a trigger event into a run's input, thread id or dedup key."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from awa.errors import TemplateError

MAX_TEMPLATE_CHARS = 20_000
"""Longest template accepted."""

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
# Literal braces, a placeholder (quotes may hold braces), or a stray brace.
_TOKEN = re.compile(r"""\{\{|\}\}|\{((?:[^{}"']|"[^"]*"|'[^']*')*)\}|[{}]""")
_STEP = re.compile(r"""\.([A-Za-z_][A-Za-z0-9_-]*)|\[\s*(?:(-?[0-9]+)|(["'])(.*?)\3)\s*\]""")
_SLUG = re.compile(r"[^a-z0-9]+")
_MISSING = object()


@dataclass(frozen=True)
class _Placeholder:
    path: tuple[str | int, ...]
    filters: tuple[str, ...]
    fallback: str | None
    source: str


class Template:
    """A compiled template: plain text with `{...}` placeholders, never passed to `eval`.

    Placeholders: `{payload.subject}`, `{payload.lines[0].sku}`, `{payload["invoice-no"]}`,
    `{payload}` (JSON), `{event.id}`, `{event.source}`, `{event.dedup_key}`, `{event.received_at}`,
    `{trigger}`, `{now}`, `{today}`. Filters: `|lower`, `|upper`, `|trim`, `|json`, `|slug` and a
    quoted fallback `{payload.priority|"normal"}`. `{{` and `}}` are literal braces. A missing field
    without a fallback raises, so a typo never yields an empty dedup key.

    Raises:
        TemplateError: If the template is malformed.

    Example:
        ```python
        template = Template("inv-{payload.invoice_no|slug}")
        template.render({"payload": {"invoice_no": "RE 2026/17"}})  # "inv-re-2026-17"
        ```
    """

    __slots__ = ("_parts", "text")

    def __init__(self, text: str) -> None:
        if not isinstance(text, str):
            raise TemplateError(f"A template must be a string, got {type(text).__name__}")
        if len(text) > MAX_TEMPLATE_CHARS:
            raise TemplateError(f"A template must be at most {MAX_TEMPLATE_CHARS} characters")
        self.text = text
        self._parts = _compile(text)

    def __repr__(self) -> str:
        return f"Template({self.text!r})"

    @property
    def fields(self) -> tuple[str, ...]:
        """The paths the template reads, for example `("payload.subject", "trigger")`."""

        return tuple(
            part.source.split("|", 1)[0].strip()
            for part in self._parts
            if isinstance(part, _Placeholder)
        )

    def render(self, context: Mapping[str, Any]) -> str:
        """Render the template against `context` (`payload`, `event`, `trigger`, `now`, ...).

        Raises:
            TemplateError: If a placeholder names a missing field and has no fallback.
        """

        out: list[str] = []
        for part in self._parts:
            if isinstance(part, str):
                out.append(part)
                continue
            value = _lookup(context, part.path)
            empty = value is _MISSING or value is None or value == ""
            if empty and part.fallback is not None:
                out.append(part.fallback)
                continue
            if value is _MISSING:
                raise TemplateError(
                    f"Template field {{{part.source}}} is missing from the event; "
                    "add a fallback such as {" + part.source + '|"unknown"}'
                )
            if empty:
                value = ""
            text = _json(value) if "json" in part.filters else _text(value)
            for name in part.filters:
                text = _FILTER_FUNCTIONS[name](text)
            out.append(text)
        return "".join(out)


TemplateLike = str | Template | Callable[..., Any]
"""A template text, a compiled `Template`, or a function of the `TriggerEvent`."""


def render(template: TemplateLike, context: Mapping[str, Any], *event: Any) -> Any:
    """Render a template with `context`, or call a function with `event`."""

    if isinstance(template, Template):
        return template.render(context)
    if isinstance(template, str):
        return Template(template).render(context)
    return template(*event)


def _compile(text: str) -> list[str | _Placeholder]:
    parts: list[str | _Placeholder] = []
    index = 0
    for match in _TOKEN.finditer(text):
        parts.append(text[index : match.start()])
        token = match.group(0)
        if token in ("{{", "}}"):
            parts.append(token[0])
        elif token == "{":
            raise TemplateError(f"Unclosed '{{' at position {match.start()} of template {text!r}")
        elif token == "}":
            raise TemplateError(f"Unmatched '}}' at position {match.start()} of template {text!r}")
        else:
            parts.append(_placeholder(match.group(1)))
        index = match.end()
    parts.append(text[index:])
    return [part for part in parts if part != ""]


def _placeholder(source: str) -> _Placeholder:
    expression, *pipes = _split_pipes(source)
    path = _path(expression.strip(), source)
    filters: list[str] = []
    fallback: str | None = None
    for pipe in pipes:
        item = pipe.strip()
        if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'":
            fallback = item[1:-1]
        elif item in _FILTER_FUNCTIONS:
            filters.append(item)
        else:
            raise TemplateError(
                f"Unknown filter {item!r} in {{{source}}}; use one of {sorted(_FILTER_FUNCTIONS)} or a "
                'quoted fallback such as "unknown"'
            )
    return _Placeholder(tuple(path), tuple(filters), fallback, source.strip())


def _split_pipes(source: str) -> list[str]:
    """`source` split at the pipes outside quotes and brackets, in one pass; unsplit when a
    quote or bracket is left open."""

    parts: list[str] = []
    start, quote, bracket = 0, "", False
    for index, char in enumerate(source):
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "[":
            bracket = True
        elif char == "]":
            bracket = False
        elif char == "|" and not bracket:
            parts.append(source[start:index])
            start = index + 1
    if quote or bracket:
        return [source]
    parts.append(source[start:])
    return parts


def _path(expression: str, source: str) -> list[str | int]:
    if not expression:
        raise TemplateError("Empty placeholder {} in template; write {{}} for literal braces")
    match = _IDENT.match(expression)
    if match is None:
        raise TemplateError(f"Invalid placeholder {{{source}}}: it must start with a name")
    path: list[str | int] = [match.group(0)]
    index = match.end()
    while index < len(expression):
        step = _STEP.match(expression, index)
        if step is None:
            raise TemplateError(
                f"Invalid placeholder {{{source}}}: unexpected {expression[index:]!r}; "
                'use .name, [n] or ["key"]'
            )
        name, number, _quote, key = step.groups()
        path.append(int(number) if number is not None else name or key)
        index = step.end()
    return path


def _lookup(context: Mapping[str, Any], path: Sequence[str | int]) -> Any:
    current: Any = context
    for key in path:
        if isinstance(key, int):
            if isinstance(current, Sequence) and not isinstance(current, str | bytes):
                try:
                    current = current[key]
                except IndexError:
                    return _MISSING
                continue
            return _MISSING
        if isinstance(current, Mapping):
            if key not in current:
                return _MISSING
            current = current[key]
        elif hasattr(current, "model_dump") and key in type(current).model_fields:
            current = getattr(current, key)
        else:
            return _MISSING
    return current


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return _json(value)


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, default=str)


_FILTER_FUNCTIONS: dict[str, Callable[[str], str]] = {
    "lower": str.lower,
    "upper": str.upper,
    "trim": str.strip,
    "slug": lambda text: _SLUG.sub("-", text.lower()).strip("-"),
    "json": lambda text: text,  # applied while converting the value
}


__all__ = ["MAX_TEMPLATE_CHARS", "Template", "TemplateLike", "render"]
