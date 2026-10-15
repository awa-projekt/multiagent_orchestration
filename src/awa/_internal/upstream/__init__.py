"""The seam between awa and LangChain and LangGraph, the only place awa imports them.

One module per upstream area (`agents`, `messages`, `tools`, `graph`, `checkpoint`, `models`,
...); the `TID251` rule in `src/ruff.toml` enforces the seam. Private: names may change in any
release.
"""

from __future__ import annotations

from importlib import metadata

UPSTREAM_DISTRIBUTIONS: tuple[str, ...] = (
    "langchain",
    "langchain-core",
    "langgraph",
    "langgraph-checkpoint",
    "langgraph-prebuilt",
    "langchain-mcp-adapters",
    "mcp",
    "langgraph-checkpoint-postgres",
    "langchain-anthropic",
    "opentelemetry-api",
)
"""The upstream distributions awa's seam covers."""


def upstream_versions() -> dict[str, str | None]:
    """The installed version of every upstream distribution (`None` if absent)."""

    versions: dict[str, str | None] = {}
    for name in UPSTREAM_DISTRIBUTIONS:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


__all__ = ["UPSTREAM_DISTRIBUTIONS", "upstream_versions"]
