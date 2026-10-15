"""Classes whose instances a checkpoint may revive (no LangGraph import: agents register on build)."""

from __future__ import annotations

STATE_TYPES: set[tuple[str, str]] = set()
"""`(module, class name)` of every registered class: what a checkpoint may revive as such."""


def register_state_type(cls: type) -> type:
    """Allow checkpoints to revive instances of `cls` (a model or dataclass); usable as a decorator."""

    STATE_TYPES.add((cls.__module__, cls.__name__))
    return cls
