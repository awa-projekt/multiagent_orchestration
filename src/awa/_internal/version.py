"""The installed version of awa, read once from the package metadata."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("awa-agents")
except PackageNotFoundError:  # pragma: no cover - running from an uninstalled checkout
    __version__ = "0.0.0+unknown"
