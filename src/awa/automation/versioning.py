"""Versions of a process: digests that pin runs, and the migration of in-flight runs.

By default (`migration="pin"`) a worker only continues runs of versions it knows; with
`"at_wait"` a waiting run moves to the current version when it resumes from its checkpoint.
`plan_migration` (`awa runs migrate --dry-run`) shows what would move.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from awa.automation.models import MigrationPolicy, ProcessRun

MigrationAction = Literal["migrate", "pinned", "orphaned"]
"""What happens to a run of another version (see `MigrationItem.action`)."""


def blueprint_digest(blueprint: Any, *, extra: Any = None) -> str:
    """The version digest of a blueprint; formatting, key order and comments do not change it.

    `extra` is more data that defines the version (for example tool module digests).
    """

    data = blueprint.to_dict() if hasattr(blueprint, "to_dict") else blueprint
    return _digest({"blueprint": data, "extra": extra})


def system_digest(system: Any) -> str:
    """The version digest of a system: type, card, tools and members, not private instructions."""

    return _digest(_describe(system, depth=0))


class MigrationItem(BaseModel):
    """What happens to one run of another version.

    Attributes:
        action: `"migrate"` (moves to the current version), `"pinned"` (stays on a version a worker
            knows) or `"orphaned"` (no worker knows its version: deploy it or cancel the run).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    status: str
    version: str | None
    action: MigrationAction
    reason: str


def plan_migration(
    runs: Iterable[ProcessRun],
    *,
    current: str,
    policy: MigrationPolicy,
    known: Collection[str] = (),
) -> list[MigrationItem]:
    """Decide for each unfinished run of another version whether it moves to `current`.

    Args:
        known: Older versions some worker still runs.
    """

    plan: list[MigrationItem] = []
    for run in runs:
        if run.is_finished or run.version is None or run.version == current:
            continue
        action, reason = _migration(run, policy, known)
        plan.append(
            MigrationItem(
                run_id=run.id, status=run.status, version=run.version, action=action, reason=reason
            )
        )
    return plan


def _migration(
    run: ProcessRun, policy: MigrationPolicy, known: Collection[str]
) -> tuple[MigrationAction, str]:
    if policy == "at_wait" and run.status == "waiting":
        return "migrate", "waiting; moves to the current version when it resumes"
    if policy == "at_wait" and run.status == "queued" and run.resuming:
        return "migrate", "answered wait; resumes on the current version"
    if run.version in known:
        return "pinned", "a worker runs its version"
    where = "mid-run, not at a wait" if policy == "at_wait" else "migration policy is 'pin'"
    return "orphaned", f"{where}; no worker knows its version"


def _digest(data: Any) -> str:
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _describe(system: Any, *, depth: int) -> dict[str, Any]:
    description: dict[str, Any] = {
        "type": f"{type(system).__module__}.{type(system).__qualname__}",
        "name": getattr(system, "name", None),
    }
    card = getattr(system, "card", None)
    if isinstance(card, BaseModel):
        description["card"] = card.model_dump(mode="json")
    tools = getattr(system, "tools", None)
    if isinstance(tools, Iterable) and not isinstance(tools, str | bytes):
        described = []
        for tool in tools:
            schema = getattr(tool, "args", None)
            described.append(
                {
                    "name": getattr(tool, "name", repr(tool)),
                    "description": getattr(tool, "description", ""),
                    "args": schema if isinstance(schema, dict) else None,
                }
            )
        description["tools"] = described
    members = getattr(system, "members", None)
    if depth < 5 and isinstance(members, Iterable) and not isinstance(members, str | bytes):
        items = members.values() if isinstance(members, dict) else members
        description["members"] = [_describe(member, depth=depth + 1) for member in items]
    return description


__all__ = [
    "MigrationAction",
    "MigrationItem",
    "blueprint_digest",
    "plan_migration",
    "system_digest",
]
