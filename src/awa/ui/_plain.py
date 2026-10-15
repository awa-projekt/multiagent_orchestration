"""Plain language for the web app: tool calls, values, approval conditions and output as words
(pure functions of a tool's metadata and a call's arguments)."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

_MAX_TEXT = 300
_MAX_ROWS = 50
_MAX_DEPTH = 3
_CURRENCIES = frozenset(
    {"EUR", "USD", "GBP", "CHF", "JPY", "CNY", "SEK", "NOK", "DKK", "PLN", "CZK"}
)
_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP"}
_MONEY_WORDS = frozenset(
    {"amount", "total", "price", "sum", "fee", "cost", "balance", "payment", "salary", "revenue"}
    | {"budget", "tax", "vat", "subtotal", "betrag", "preis", "summe", "gesamt"}
)
_ISO_DATE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
_HIDDEN_ARGUMENTS = frozenset({"query", "sql", "statement", "code", "script", "body", "html"})
_SPOKEN = frozenset({"parameters", "params"})  # arguments that read better as `for INV-2042`
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_WHOLE_CENTS = re.compile(r"\.00(?!\d)")
_SPLIT_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")

# standard operations of the local workspace and of databases, in a beginner's words
_OPERATION_LABELS = {
    "schema": "Check how the {system} is organized",
    "query": "Look something up in the {system}",
    "execute": "Change data in the {system}",
    "list_files": "List the files in the {system}",
    "read_file": "Read a file from the {system}",
    "read_csv": "Read a table from the {system}",
    "query_csv": "Look something up in a table of the {system}",
    "read_excel": "Read a spreadsheet from the {system}",
    "query_excel": "Look something up in a spreadsheet of the {system}",
    "read_pdf": "Read a PDF document from the {system}",
    "read_pdfs": "Read several PDF documents from the {system}",
    "read_docx": "Read a Word document from the {system}",
    "search": "Search the documents of the {system}",
    "write_file": "Save a file in the {system}",
    "append_file": "Add text to a file in the {system}",
    "write_csv": "Save rows in a table of the {system}",
    "fill_excel": "Add columns to a spreadsheet and save it in the {system}",
}  # fmt: skip
_COMPARE_WORDS: dict[type[ast.cmpop], tuple[str, str]] = {
    ast.GtE: ("{v} or more", "less than {v}"),
    ast.Gt: ("more than {v}", "{v} or less"),
    ast.LtE: ("{v} or less", "more than {v}"),
    ast.Lt: ("less than {v}", "{v} or more"),
    ast.Eq: ("{v}", "not {v}"),
    ast.NotEq: ("not {v}", "{v}"),
    ast.In: ("one of {v}", "not one of {v}"),
    ast.NotIn: ("not one of {v}", "one of {v}"),
}
_TEXT_TESTS = {
    "contains": ("contains", "does not contain"),
    "startswith": ("starts with", "does not start with"),
    "endswith": ("ends with", "does not end with"),
}


def humanize(name: str) -> str:
    """`invoice_id` as `Invoice id`, `totalEUR` as `Total EUR`; a currency suffix gets brackets.

    Example:
        ```python
        humanize("total_eur")  # 'Total (EUR)'
        ```
    """

    words = _SPLIT_WORDS.findall(name.replace("-", " ").replace(".", " ").replace("_", " "))
    if not words:
        return name
    if len(words) > 1 and words[-1].upper() in _CURRENCIES:
        words[-1] = f"({words[-1].upper()})"
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def as_text(value: Any) -> str:
    """`value` as one line of text: a string as it is, anything else as compact JSON."""

    return value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)


def first_sentence(text: str) -> str:
    """The first sentence of `text` without its final full stop (`""` for no text)."""

    flat = " ".join((text or "").split())
    match = re.match(r"(.+?[.!?])(?:\s|$)", flat)
    return (match.group(1) if match else flat).rstrip(".!? ")


_GROUP_SPACES = "  ' "
_SPACE_GROUPED = re.compile(rf"\d{{1,3}}(?:[{_GROUP_SPACES}]\d{{3}})+(?:[.,]\d+)?")
_MARKED_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _amount_of(text: str) -> float | None:
    """A number written as text (`1,234.50`, `4.250,00`) as a float; `None` when ambiguous.

    `1,234` or `1.234` stay text, so nobody decides about money on a wrong figure.
    """

    text = text.strip()
    sign = -1.0 if text.startswith("-") else 1.0
    body = text.lstrip("+-") if text[:1] in "+-" else text
    if _SPACE_GROUPED.fullmatch(body):
        body = re.sub(f"[{_GROUP_SPACES}]", "", body)
    if _MARKED_NUMBER.fullmatch(body) is None:
        return None
    marks = [c for c in body if c in ".,"]
    if not marks:
        return sign * float(body)
    last = marks[-1]
    if len(set(marks)) == 2 or marks.count(last) == 1:
        group = {".": ",", ",": "."}[last] if len(set(marks)) == 2 else ""
        if marks.count(last) != 1:
            return None
        whole, fraction = body.rsplit(last, 1)
        if not group and len(fraction) == 3 and whole != "0":
            return None
    else:
        group, whole, fraction = last, body, ""
    if group or not fraction:
        parts = whole.split(group)
        if not (len(parts[0]) <= 3 and all(len(part) == 3 for part in parts[1:])):
            return None
        whole = "".join(parts)
    return sign * float(f"{whole}.{fraction}" if fraction else whole)


def money(value: Any, currency: str | None = None, *, fraction: bool = True) -> str | None:
    """`value` as an amount: `7,450.00 EUR` (`None` when it is not a number)."""

    if isinstance(value, bool):
        return None
    try:
        number = _amount_of(value) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    if number is None or number != number or number in (float("inf"), float("-inf")):
        return None
    shown = f"{number:,.2f}" if fraction or number != int(number) else f"{number:,.0f}"
    return f"{shown} {currency}" if currency else shown


def date_text(value: Any) -> str | None:
    """An ISO date or date-time as `5 Oct 2026` (`5 Oct 2026, 14:30`); `None` for anything else."""

    match = _ISO_DATE.match(value) if isinstance(value, str) else None
    if match is None:
        return None
    year, month, day, hour, minute = match.groups()
    try:
        day_text = datetime(int(year), int(month), int(day)).strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return None
    return f"{day_text}, {hour}:{minute}" if hour is not None else day_text


def _currency(name: str, description: str, siblings: Mapping[str, Any]) -> str | None:
    """The currency of the amount `name`: a sibling field, a suffix of its name, its description."""

    for key in ("currency", "currency_code", "waehrung"):
        found = siblings.get(key)
        if isinstance(found, str) and found.strip():
            return found.strip().upper()[:3]
    suffix = name.rsplit("_", 1)[-1].upper()
    if suffix in _CURRENCIES and "_" in name:
        return suffix
    for word in re.findall(r"[A-Z]{3}", description):
        if word in _CURRENCIES:
            return str(word)
    for symbol, code in _SYMBOLS.items():
        if symbol in description:
            return code
    return None


def _is_money(name: str) -> bool:
    tokens = {word.lower() for word in _SPLIT_WORDS.findall(name)}
    return bool(tokens & _MONEY_WORDS) or bool(
        len(tokens) > 1 and tokens & {c.lower() for c in _CURRENCIES}
    )


def format_value(
    name: str, value: Any, *, description: str = "", siblings: Mapping[str, Any] | None = None
) -> str:
    """`value` of the field `name` the way a person reads it.

    Amounts get separators and their currency (sibling field, suffix or description), dates
    `5 Oct 2026`, booleans `Yes`/`No`, lists one line; long text is cut.

    Example:
        ```python
        format_value("total_eur", 7450)  # '7,450.00 EUR'
        format_value("due", "2026-10-05")  # '5 Oct 2026'
        ```
    """

    if value is None or value == "":
        return "(empty)"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int | float) and _is_money(name):
        return money(value, _currency(name, description, siblings or {})) or str(value)
    if isinstance(value, str):
        if (when := date_text(value)) is not None:
            return when
        if _is_money(name) and (
            shown := money(value, _currency(name, description, siblings or {}))
        ):
            return shown
        return value if len(value) <= _MAX_TEXT else value[: _MAX_TEXT - 1] + "…"
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return ", ".join(format_value(name, item, siblings=None) for item in value[:20])
    if isinstance(value, Mapping):
        return "; ".join(f"{humanize(str(k))}: {format_value(str(k), v)}" for k, v in value.items())
    return str(value)


def tool_label(
    name: str, description: str, metadata: Mapping[str, Any], instances: Mapping[str, str]
) -> str:
    """A short label for a tool: its `label`, a standard operation wording, its description's
    first sentence, else its name in words. `instances` titles connector instances by name.
    """

    explicit = metadata.get("label")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    sentence = first_sentence(description)
    reference = metadata.get("awa_connector")
    if isinstance(reference, str) and "." in reference:
        instance, operation = reference.split(".", 1)
        system = instances.get(instance) or humanize(instance).lower()
        if operation in _OPERATION_LABELS:
            return _OPERATION_LABELS[operation].format(system=system)
        if sentence:
            return f"{sentence} ({system})"
    if sentence and len(sentence) <= 90:
        return sentence
    return humanize(name)


def _filled(label: str, args: Mapping[str, Any], fields: Mapping[str, Mapping[str, Any]]) -> str:
    """`label` with `{argument}` placeholders replaced by the formatted argument values."""

    def fill(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in args:
            return match.group(0)
        meta = fields.get(key, {})
        return format_value(key, args[key], description=meta.get("description", ""), siblings=args)

    return _PLACEHOLDER.sub(fill, label)


def _shown_arguments(
    args: Mapping[str, Any], fields: Mapping[str, Mapping[str, Any]], used: set[str]
) -> list[str]:
    """`Title: value` for the short arguments a sentence can mention (long or technical ones stay
    in the details)."""

    parts: list[str] = []
    for key, value in args.items():
        if key in used or key in _HIDDEN_ARGUMENTS or value in (None, ""):
            continue
        shown = format_value(
            key, value, description=fields.get(key, {}).get("description", ""), siblings=args
        )
        if len(shown) > 60 or isinstance(value, Mapping):
            continue
        title = str(fields.get(key, {}).get("title") or humanize(key))
        parts.append(f"for {shown}" if key in _SPOKEN else f"{title}: {shown}")
    return parts


def describe_call(
    label: str,
    args: Mapping[str, Any],
    fields: Mapping[str, Mapping[str, Any]],
    *,
    listed: bool = True,
) -> str:
    """One sentence for a tool call: `Look up an invoice in the ledger (Invoice id: INV-2042)`.

    `{argument}` placeholders are filled; with `listed` the other short arguments follow.
    """

    text = _filled(label, args, fields)
    extras = _shown_arguments(args, fields, set(_PLACEHOLDER.findall(label))) if listed else []
    return f"{text} ({', '.join(extras)})" if extras else text


class _Unsupported(Exception):
    """The expression uses something `condition_words` cannot say in words."""


def condition_words(expression: str, fields: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    """An approval condition (`amount >= 5000`) as a clause: `the amount is 5,000 or more`.

    What this cannot phrase comes back as `the rule "<expression>" applies`, never an error.

    Example:
        ```python
        condition_words("total_eur >= 5000")  # 'the total is 5,000 EUR or more'
        ```
    """

    fields = fields or {}
    try:
        tree = ast.parse(" ".join(expression.split()), mode="eval")
        return _clause(tree.body, fields, negate=False)
    except (SyntaxError, _Unsupported, RecursionError):
        return f'the rule "{" ".join(expression.split())}" applies'


def _subject(node: ast.expr, fields: Mapping[str, Mapping[str, Any]]) -> tuple[str, str | None]:
    """`(words, field name)` of an expression that names a value: `the total`."""

    if isinstance(node, ast.Name):
        title = str(fields.get(node.id, {}).get("title") or humanize(node.id))
        title = re.sub(r"\s*\([A-Z]{3}\)$", "", title)
        return f"the {title.lower()}", node.id
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) == 1:
        inner, name = _subject(node.args[0], fields)
        if node.func.id in {"lower", "upper", "number", "abs", "round"}:
            return inner, name
        if node.func.id == "len":
            return f"the length of {inner}", None
    raise _Unsupported


def _constant(node: ast.expr, name: str | None, fields: Mapping[str, Mapping[str, Any]]) -> str:
    """A literal (or a list of literals) as text, formatted like the field `name`."""

    description = str(fields.get(name or "", {}).get("description", ""))
    if isinstance(node, ast.Constant):
        return _WHOLE_CENTS.sub("", format_value(name or "", node.value, description=description))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return "-" + _constant(node.operand, name, fields)
    if isinstance(node, ast.List | ast.Tuple):
        return ", ".join(_constant(item, name, fields) for item in node.elts)
    raise _Unsupported


def _clause(node: ast.expr, fields: Mapping[str, Mapping[str, Any]], *, negate: bool) -> str:
    if isinstance(node, ast.BoolOp):
        joiner = " and " if isinstance(node.op, ast.And) ^ negate else " or "
        return joiner.join(_clause(value, fields, negate=negate) for value in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _clause(node.operand, fields, negate=not negate)
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        words = _COMPARE_WORDS.get(type(node.ops[0]))
        if words is None:
            raise _Unsupported
        subject, name = _subject(node.left, fields)
        value = _constant(node.comparators[0], name, fields)
        return f"{subject} is {words[1 if negate else 0].format(v=value)}"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        function = node.func.id
        if function in _TEXT_TESTS and len(node.args) == 2:
            subject, name = _subject(node.args[0], fields)
            return (
                f"{subject} {_TEXT_TESTS[function][negate]} {_constant(node.args[1], name, fields)}"
            )
        if function == "empty" and len(node.args) == 1:
            subject, _ = _subject(node.args[0], fields)
            return f"{subject} is {'filled in' if negate else 'empty'}"
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return "this always applies" if node.value is not negate else "this never applies"
    raise _Unsupported


def view_of(
    value: Any, name: str = "", siblings: Mapping[str, Any] | None = None, depth: int = 0
) -> Any:
    """Structured output as a tree the page draws: a string, or `facts`, `table` or `list`.

    Example:
        ```python
        view_of({"invoice_id": "INV-1", "total_eur": 5000})
        # {'type': 'facts', 'items': [{'title': 'Invoice id', 'value': 'INV-1'},
        #                             {'title': 'Total (EUR)', 'value': '5,000.00 EUR'}]}
        ```
    """

    if isinstance(value, Mapping):
        if depth >= _MAX_DEPTH:
            return format_value(name, value)
        items = [
            {"title": humanize(str(key)), "value": view_of(item, str(key), value, depth + 1)}
            for key, item in list(value.items())[:_MAX_ROWS]
        ]
        return {"type": "facts", "items": items}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        rows = list(value)[:_MAX_ROWS]
        if rows and all(isinstance(row, Mapping) for row in rows):
            keys = list(dict.fromkeys(key for row in rows for key in row))
            if depth < _MAX_DEPTH - 1 and all(
                not isinstance(row.get(key), Mapping | list | tuple) for row in rows for key in keys
            ):
                return {
                    "type": "table",
                    "columns": [humanize(str(key)) for key in keys],
                    "rows": [
                        [format_value(str(key), row.get(key), siblings=row) for key in keys]
                        for row in rows
                    ],
                    "more": max(0, len(value) - len(rows)),
                }
        if depth < _MAX_DEPTH:
            return {"type": "list", "items": [view_of(row, name, None, depth + 1) for row in rows]}
    return format_value(name, value, siblings=siblings)
