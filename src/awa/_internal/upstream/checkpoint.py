"""Checkpointers and stores (`langgraph.checkpoint`, `langgraph.store`).

`state_serde` is always strict: it revives only LangGraph's safe types, awa's state types and
classes of `register_state_type`, so a tampered store cannot call a function in the process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.store.base import BaseStore, SearchItem
from langgraph.store.memory import InMemoryStore

from awa._internal.state_types import STATE_TYPES, register_state_type

if TYPE_CHECKING:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore


class _StateSerde(JsonPlusSerializer):
    """LangGraph's strict serializer, allowing the classes registered by the time a checkpoint loads."""

    _view: tuple[int, JsonPlusSerializer] | None = None

    def __init__(self) -> None:
        super().__init__(allowed_msgpack_modules=None)

    def with_msgpack_allowlist(self, extra_allowlist: Any) -> JsonPlusSerializer:
        clone = super().with_msgpack_allowlist(extra_allowlist)
        if clone is not self and isinstance(clone, _StateSerde):
            clone._view = None
        return clone

    def loads_typed(self, data: Any) -> Any:
        if self._view is None or self._view[0] != len(STATE_TYPES):
            self._view = (len(STATE_TYPES), super().with_msgpack_allowlist(STATE_TYPES))
        return JsonPlusSerializer.loads_typed(self._view[1], data)


def state_serde() -> JsonPlusSerializer:
    """A new strict serializer for a checkpointer: safe types plus the registered state types."""

    return _StateSerde()


class StateInMemorySaver(InMemorySaver):
    """`InMemorySaver` with `state_serde` as its default serializer: what awa uses itself, so
    registered state types (structured outputs) survive a pause and resume."""

    def __init__(self, *, serde: Any = None, **options: Any) -> None:
        super().__init__(serde=serde or state_serde(), **options)


def postgres_saver_class() -> type[AsyncPostgresSaver]:
    """Return `AsyncPostgresSaver`, importing the optional package on demand.

    Raises:
        ImportError: If `langgraph-checkpoint-postgres` is not installed.
    """

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    return AsyncPostgresSaver


def postgres_store_class() -> type[AsyncPostgresStore]:
    """Return `AsyncPostgresStore`, importing the optional package on demand.

    Raises:
        ImportError: If `langgraph-checkpoint-postgres` is not installed.
    """

    from langgraph.store.postgres.aio import AsyncPostgresStore

    return AsyncPostgresStore


__all__ = [
    "WRITES_IDX_MAP",
    "BaseCheckpointSaver",
    "BaseStore",
    "CheckpointTuple",
    "InMemorySaver",
    "InMemoryStore",
    "SearchItem",
    "StateInMemorySaver",
    "get_checkpoint_id",
    "get_checkpoint_metadata",
    "postgres_saver_class",
    "postgres_store_class",
    "register_state_type",
    "state_serde",
]
