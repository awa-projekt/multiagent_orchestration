"""Where awa keeps state: checkpoints, stores, artifacts, run records, and what erasure deletes.

Entry points: `open_checkpointer` and `StateInMemorySaver`, `open_store`,
`open_artifact_repository`, `RunLog` with `Scope`, `erase_subject` and `apply_retention`, and
`register_state_type`; everything else lives in its module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute

if TYPE_CHECKING:
    from awa._internal.state_types import register_state_type
    from awa.persistence.artifacts import ArtifactRepository, open_artifact_repository
    from awa.persistence.checkpointer import StateInMemorySaver, open_checkpointer
    from awa.persistence.erasure import ErasureReport, erase_subject
    from awa.persistence.retention import RetentionPolicy, SubjectIndex, apply_retention
    from awa.persistence.runlog import RunLog, Scope
    from awa.persistence.sqlite import SqliteCheckpointer
    from awa.persistence.store import open_store

_HOMES = homes(
    {
        "awa.persistence.artifacts": "ArtifactRepository open_artifact_repository",
        "awa.persistence.erasure": "ErasureReport erase_subject",
        "awa.persistence.retention": "RetentionPolicy SubjectIndex apply_retention",
        "awa.persistence.runlog": "RunLog Scope",
        "awa.persistence.sqlite": "SqliteCheckpointer",
        "awa.persistence.checkpointer": "StateInMemorySaver open_checkpointer",
        "awa.persistence.store": "open_store",
        "awa._internal.state_types": "register_state_type",
    }
)

__all__ = [
    "ArtifactRepository",
    "ErasureReport",
    "RetentionPolicy",
    "RunLog",
    "Scope",
    "SqliteCheckpointer",
    "StateInMemorySaver",
    "SubjectIndex",
    "apply_retention",
    "erase_subject",
    "open_artifact_repository",
    "open_checkpointer",
    "open_store",
    "register_state_type",
]


def __getattr__(name: str) -> Any:
    # Submodules such as `.cache` must load without the checkpoint stack behind these names.
    return lazy_attribute(globals(), _HOMES, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
