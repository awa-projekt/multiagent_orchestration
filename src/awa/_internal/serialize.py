"""YAML and JSON as awa writes and reads them; errors are plain `ValueError`s for callers to wrap."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from functools import cache
from typing import Any

MAX_YAML_VALUES = 100_000
"""Most values `load_yaml` accepts by default (aliases count at every use)."""

MAX_YAML_DEPTH = 64
"""Deepest nesting `load_yaml` accepts by default."""

FRONTMATTER_DELIMITER = "---"


@cache
def _block_dumper() -> type[Any]:
    import yaml

    class BlockDumper(yaml.SafeDumper):
        pass

    def represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
        style = "|" if "\n" in value else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    BlockDumper.add_representer(str, represent_str)
    return BlockDumper


def dump_yaml(data: Any, *, width: int = 100) -> str:
    """Return `data` as safe YAML: keys in their order, Unicode kept, multi-line text in block style."""

    import yaml

    text: str = yaml.dump(
        data, Dumper=_block_dumper(), sort_keys=False, allow_unicode=True, width=width
    )
    return text


def load_yaml(
    text: str | bytes,
    *,
    max_bytes: int | None = None,
    max_values: int = MAX_YAML_VALUES,
    max_depth: int = MAX_YAML_DEPTH,
    what: str = "document",
) -> Any:
    """Parse safe YAML (`yaml.safe_load`) within size bounds; `None` for an empty document.

    Args:
        text: The YAML text; bytes are decoded as UTF-8 (a BOM is dropped).
        max_bytes: Largest accepted size of the UTF-8 text; `None` for no limit.
        max_values: Most values, counting an aliased value every time it is used.
        what: The kind of document, used in error messages (`"blueprint"`).
    """

    import yaml

    if max_bytes is not None:
        size = len(text) if isinstance(text, bytes) else len(text.encode("utf-8"))
        if size > max_bytes:
            raise ValueError(f"The {what} is larger than {max_bytes:,} bytes")
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"The {what} is not valid UTF-8: {exc.reason}") from None
    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        # PyYAML's constructors raise ValueError for impossible values such as
        # the timestamp 2001-13-01.
        raise ValueError(f"Malformed {what} YAML: {exc}") from None
    except RecursionError:
        raise ValueError(f"The {what} nests deeper than {max_depth} levels") from None
    check_yaml_size(data, max_values=max_values, max_depth=max_depth, what=what)
    return data


def check_yaml_size(
    data: Any,
    *,
    max_values: int = MAX_YAML_VALUES,
    max_depth: int = MAX_YAML_DEPTH,
    what: str = "document",
) -> None:
    """Bound parsed YAML by `max_values` and `max_depth`, counting aliased values every time."""

    pending: list[tuple[Any, int]] = [(data, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > max_values:
            raise ValueError(
                f"The {what} has more than {max_values:,} values "
                "(a YAML alias counts every time it is used)"
            )
        if depth > max_depth:
            raise ValueError(f"The {what} nests deeper than {max_depth} levels")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)


# Markdown and YAML line breaks; `str.splitlines` would also split on U+0085, U+2028, ...
_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split a Markdown text into its `---` frontmatter block (unparsed) and the body.

    Without an opening `---` line the result is `(None, text)`. Lines end at `\n`, `\r\n` or `\r`
    only, so Unicode line separators inside a YAML value stay part of it.
    """

    lines = _LINE_BREAK.split(text.lstrip("\ufeff"))
    if not lines[-1]:
        lines.pop()
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        return None, text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONTMATTER_DELIMITER:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise ValueError("The frontmatter block is not closed with '---'")


def to_json(
    value: Any,
    *,
    compact: bool = True,
    ensure_ascii: bool = False,
    allow_nan: bool = False,
    default: Callable[[Any], Any] | None = None,
) -> str:
    """Serialize `value` as JSON text: by default strict (no NaN or Infinity), compact, Unicode kept."""

    return json.dumps(
        value,
        separators=(",", ":") if compact else None,
        ensure_ascii=ensure_ascii,
        allow_nan=allow_nan,
        default=default,
    )
