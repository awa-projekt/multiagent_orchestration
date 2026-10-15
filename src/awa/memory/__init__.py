"""Memory (extension): long-term agent memory and context compaction.

Entry points: `AgentMemory` (`remember` / `recall` / `forget` tools on a LangGraph store, scoped
per agent, user, team or globally) and `Compaction` (summarize older turns over a token budget).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute

if TYPE_CHECKING:
    from awa.errors import MemoryScopeError
    from awa.memory.compaction import Compaction, CompactionMiddleware
    from awa.memory.long_term import (
        MAX_MEMORY_CHARS,
        MAX_RECALL_LIMIT,
        MEMORY_TOOL_NAMES,
        RECALLED_MEMORIES_ID_PREFIX,
        AgentMemory,
        MemoryItem,
        MemoryScope,
        MemoryScopeKind,
        UserIdResolver,
        recalled_memories_message,
        render_memories,
        user_id_from_deps,
        with_recalled_memories,
    )

_HOMES = homes(
    {
        "awa.errors": "MemoryScopeError",
        "awa.memory.compaction": "Compaction CompactionMiddleware",
        "awa.memory.long_term": (
            "MAX_MEMORY_CHARS MAX_RECALL_LIMIT MEMORY_TOOL_NAMES RECALLED_MEMORIES_ID_PREFIX"
            " AgentMemory MemoryItem MemoryScope MemoryScopeKind UserIdResolver"
            " recalled_memories_message render_memories user_id_from_deps with_recalled_memories"
        ),
    }
)

__all__ = [
    "MAX_MEMORY_CHARS",
    "MAX_RECALL_LIMIT",
    "MEMORY_TOOL_NAMES",
    "RECALLED_MEMORIES_ID_PREFIX",
    "AgentMemory",
    "Compaction",
    "CompactionMiddleware",
    "MemoryItem",
    "MemoryScope",
    "MemoryScopeError",
    "MemoryScopeKind",
    "UserIdResolver",
    "recalled_memories_message",
    "render_memories",
    "user_id_from_deps",
    "with_recalled_memories",
]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _HOMES, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
