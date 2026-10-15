"""Format constants and text/file helpers for reading blueprints."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from awa.blueprints.errors import BlueprintError

BLUEPRINT_FORMAT_VERSION = "awa/v1"
"""The blueprint file format this version of awa reads and writes (`format_version`)."""

MAX_BLUEPRINT_BYTES = 1_000_000
"""Largest blueprint, as a file or as text, that `Blueprint.from_yaml_file` and
`Blueprint.from_yaml_text` accept, in bytes (UTF-8)."""

_SUPPORTED_FORMAT_VERSIONS = frozenset({BLUEPRINT_FORMAT_VERSION})
_FORMAT_VERSION_PATTERN = re.compile(r"awa/v[1-9][0-9]*")
# A single line that is YAML text rather than a path: flow style or `key: value`.
INLINE_YAML = re.compile(r"\s*(\{|[A-Za-z_][\w.-]*\s*:(\s|$))")


def agent_entry(name: Any, value: Any) -> Any:
    """One entry of an `agents:` mapping as an agent entry: a task text or agent fields."""

    if value is None:
        return {"name": name}
    if isinstance(value, str):
        return {"name": name, "description": value}
    if isinstance(value, Mapping):
        if "name" in value and value["name"] != name:
            raise ValueError(f"agent {name!r} sets a different name: {value['name']!r}")
        return {"name": name, **value}
    return value


def issue_path(loc: Sequence[int | str]) -> str:
    """A validation error location as a blueprint path, e.g. `agents[1].tools[0]`."""

    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else part
    return path or "$"


def raw_top_level_scalar(text: str, key: str) -> str:
    """Return the source text of a top-level scalar (so `1.10` is not read as `1.1`)."""

    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if isinstance(root, yaml.MappingNode):
        for key_node, value_node in root.value:
            if key_node.value == key and isinstance(value_node, yaml.ScalarNode):
                return str(value_node.value)
    raise KeyError(key)


def format_version_problem(value: object) -> str | None:
    """Why `value` is not a `format_version` this awa reads, or `None`."""

    if value in _SUPPORTED_FORMAT_VERSIONS:
        return None
    supported = ", ".join(repr(version) for version in sorted(_SUPPORTED_FORMAT_VERSIONS))
    if isinstance(value, str) and _FORMAT_VERSION_PATTERN.fullmatch(value):
        return (
            f"Unsupported blueprint format_version {value!r}: this version of awa reads "
            f"{supported}; upgrade awa to load this blueprint"
        )
    return f"Invalid blueprint format_version {value!r}: expected {supported}"


def read_blueprint_text(path: Path, *, hint: str = "") -> str:
    """The UTF-8 text of a blueprint file of at most `MAX_BLUEPRINT_BYTES`; `hint` ends a not-found message."""

    shown = f"'{path}'"
    if not path.is_absolute():
        shown += f" (resolved to '{path.resolve()}')"
    if path.is_dir():
        raise BlueprintError(
            f"Blueprint path {shown} is a directory without a blueprint.yaml; pass "
            "blueprint.yaml or the project folder that contains it"
        )
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BLUEPRINT_BYTES + 1)
    except FileNotFoundError as exc:
        raise BlueprintError(f"Blueprint file not found: {shown}{hint}") from exc
    except OSError as exc:
        raise BlueprintError(f"Cannot read blueprint file {shown}: {exc.strerror or exc}") from exc
    if len(raw) > MAX_BLUEPRINT_BYTES:
        raise BlueprintError(
            f"Blueprint file {shown} is larger than MAX_BLUEPRINT_BYTES "
            f"({MAX_BLUEPRINT_BYTES:,} bytes)"
        )
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BlueprintError(f"Blueprint file {shown} is not valid UTF-8: {exc.reason}") from None
