"""Edit a blueprint file in place, comments kept; and its `components:` section, the one persistent
part switch."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from awa._internal.serialize import load_yaml
from awa.blueprints.schema import Blueprint
from awa.errors import ConfigurationError
from awa.plugins.switches import split_key


def find_blueprint(directory: Path, explicit: str | os.PathLike[str] | None = None) -> Path:
    """The blueprint file to edit: `explicit`, else the only `blueprint.y*ml` in `directory`.

    Raises:
        ConfigurationError: If there is none, or several and none is named.
    """

    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigurationError(f"blueprint {path} does not exist")
        return path
    found = sorted(
        {
            *directory.glob("blueprint*.yaml"),
            *directory.glob("blueprint*.yml"),
            *directory.glob("blueprint*.json"),
        }
    )
    if not found:
        raise ConfigurationError(
            "there is no blueprint here to hold the switch: create blueprint.yaml or pass "
            "--blueprint PATH"
        )
    if len(found) > 1:
        names = ", ".join(path.name for path in found)
        raise ConfigurationError(f"several blueprints here ({names}): pass --blueprint PATH")
    return found[0]


def edit_blueprint(
    path: str | os.PathLike[str],
    change: Callable[[dict[str, Any]], None],
    *,
    what: str = "change",
    dry_run: bool = False,
    check: Callable[[Blueprint], None] | None = None,
) -> str:
    """Apply `change` to the data of the blueprint file `path` and return the new text.

    YAML keeps its comments, key order and indentation; JSON is rewritten. The file is written
    only when the result is valid and `check` accepts it (`dry_run` writes nothing).

    Raises:
        ConfigurationError: If the file is not a blueprint mapping, or the result is invalid.
    """

    target = Path(path)
    original = target.read_text(encoding="utf-8-sig")
    as_json = target.suffix.lower() == ".json"
    handler = None
    if as_json:
        data = load_yaml(original.encode(), what="blueprint")
    else:
        from ruamel.yaml import YAML
        from ruamel.yaml.error import YAMLError
        from ruamel.yaml.util import load_yaml_guess_indent

        handler = YAML(typ="rt")
        handler.preserve_quotes = True
        handler.width = 4096  # never fold a line the person wrote
        try:
            data, indent, offset = load_yaml_guess_indent(original, yaml=handler)
        except YAMLError as exc:
            raise ConfigurationError(f"{target} is not valid YAML: {exc}") from None
        handler.indent(mapping=2, sequence=indent or 2, offset=offset or 0)
    if not isinstance(data, dict):
        raise ConfigurationError(f"{target} is not a blueprint mapping")
    change(data)
    if handler is None:
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    else:
        out = io.StringIO()
        handler.dump(data, out)
        text = out.getvalue()
    changed = Blueprint.from_yaml_text(text)
    errors = [issue for issue in changed.check() if issue.severity == "error"]
    if errors:
        raise ConfigurationError(f"The {what} makes {target} invalid: {errors[0]}")
    if check is not None:
        check(changed)
    if not dry_run:
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
    return text


def set_component(
    path: str | os.PathLike[str],
    key: str,
    state: Literal["on", "off", "default"],
    *,
    dry_run: bool = False,
) -> str:
    """Set `key` (`KIND.NAME`) to `on`, `off` or its default in the blueprint file; return the new text.

    Raises:
        ComponentError: If `key` is not `KIND.NAME`.
        ConfigurationError: If the file is not a blueprint mapping or the result is invalid.
    """

    prefix, name = split_key(key)

    def switch(data: dict[str, Any]) -> None:
        section = data.get("components") or {}
        kind = section[prefix] = section.get(prefix) or {}
        if state == "default":
            kind.pop(name, None)
            if not kind:
                section.pop(prefix)
        else:
            kind[name] = state == "on"
        if section:
            data["components"] = section
        else:
            data.pop("components", None)

    return edit_blueprint(path, switch, what="switch", dry_run=dry_run)


__all__ = ["edit_blueprint", "find_blueprint", "set_component"]
