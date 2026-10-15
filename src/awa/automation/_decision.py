"""The resume value of an approval, as `awa runs decide` and the web app's Approvals page send it."""

from __future__ import annotations

from typing import Any

from awa.errors import ConfigurationError


def approval_decision(
    approve: bool, note: str | None = None, edit_args: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The value `Agent.resume` takes: approve, reject (with a note the agent reads) or edit.

    Raises:
        ConfigurationError: If `edit_args` is given with a rejection.
    """

    if not approve:
        if edit_args is not None:
            raise ConfigurationError("--edit-args goes with --approve, not --reject")
        return {"action": "reject", "note": note or "Rejected by the operator."}
    decision: dict[str, Any] = (
        {"action": "edit", "args": edit_args} if edit_args is not None else {"action": "approve"}
    )
    if note:
        decision["note"] = note
    return decision
