"""How awa is installed here, and the exact command that adds an extra to it (`install_hint`)."""

from __future__ import annotations

import importlib
import json
import os
import shlex
import sys
from dataclasses import dataclass
from importlib import metadata
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Self
from urllib.parse import unquote, urlsplit

from awa.errors import OptionalDependencyError

DISTRIBUTION = "awa-agents"
"""The distribution name on the package index and in the wheel."""


@dataclass(frozen=True)
class Install:
    """What `importlib.metadata` and `direct_url.json` say about the running awa."""

    version: str | None
    location: Path | None
    editable_root: Path | None
    installed_at: Path | None
    has_pip: bool
    source: Path | None = None  # the local awa folder or wheel a non-editable install came from

    @classmethod
    def detect(cls) -> Self:
        spec = safe_find_spec("awa")
        location = Path(spec.origin).parent if spec is not None and spec.origin else None
        has_pip = safe_find_spec("pip") is not None
        try:
            dist = metadata.distribution(DISTRIBUTION)
        except metadata.PackageNotFoundError:
            return cls(None, location, None, None, has_pip)
        direct_url = dist.read_text("direct_url.json")
        editable_root = editable_root_of(direct_url)
        installed_at = None if editable_root else Path(str(dist.locate_file("awa")))
        source = None if editable_root else local_source_of(direct_url)
        if source is not None and not source.exists():
            source = None
        return cls(dist.version, location, editable_root, installed_at, has_pip, source)

    def command(self, target: str) -> str:
        """The exact command that installs `target` into the running interpreter."""

        if self.has_pip:
            return f"{shell_path(sys.executable)} -m pip install {target}"
        return f"uv pip install --python {quoted_path(sys.executable)} {target}"

    def extra_target(self, extra: str | None = None) -> str:
        """awa itself (with `extra`) from the local copy it came from, never a package index."""

        suffix = f"[{extra}]" if extra else ""
        if self.editable_root is not None:
            return f'-e "{self.editable_root}{suffix}"'
        if self.source is not None:
            return f'"{self.source}{suffix}"'
        return f'-e ".{suffix}"'

    def awa_command(self, extra: str | None = None, options: str = "") -> str:
        """The command that (re)installs awa with `extra` from its local folder or wheel."""

        command = self.command(options + self.extra_target(extra))
        if self.editable_root is None and self.source is None:
            command += " (in the awa folder)"
        return command


def install_hint(extra: str) -> str:
    """The command that installs the awa extra `extra` for this install kind."""

    return Install.detect().awa_command(extra)


def editable_root_of(direct_url: str | None) -> Path | None:
    info = _direct_url(direct_url)
    if info is None or not info.get("dir_info", {}).get("editable"):
        return None
    return _file_path(info)


def local_source_of(direct_url: str | None) -> Path | None:
    """The local folder or wheel of a non-editable install (PEP 610 `direct_url.json`)."""

    info = _direct_url(direct_url)
    if info is None or info.get("dir_info", {}).get("editable"):
        return None
    return _file_path(info)


def _direct_url(direct_url: str | None) -> dict[str, Any] | None:
    if not direct_url:
        return None
    try:
        info = json.loads(direct_url)
    except ValueError:
        return None
    return info if isinstance(info, dict) else None


def _file_path(info: dict[str, Any]) -> Path | None:
    url = urlsplit(str(info.get("url", "")))
    if url.scheme != "file":
        return None
    path = unquote(url.path)
    if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    return Path(path)


def shell_path(path: str) -> str:
    """`path` as the command word of a shell line (PowerShell needs `&` before a quoted program)."""

    quoted = quoted_path(path)
    return f"& {quoted}" if os.name == "nt" and quoted != path else quoted


def quoted_path(path: str) -> str:
    """`path` as one argument of a shell line."""

    if " " not in path:
        return path
    return f'"{path}"' if os.name == "nt" else shlex.quote(path)


def safe_find_spec(name: str) -> Any:
    """The import spec of `name`, or `None` when it cannot be found (never raises)."""

    try:
        return find_spec(name)
    except (ImportError, ValueError):
        return None


def require_module(module: str, package: str, *, feature: str) -> Any:
    """Import an optional dependency, or explain how to install it.

    Raises:
        OptionalDependencyError: If `module` is not installed.
    """

    try:
        return importlib.import_module(module)
    except ImportError:
        raise OptionalDependencyError(
            f"{feature} needs {package}; install it with {install_hint('files')}"
        ) from None
