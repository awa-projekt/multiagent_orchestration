"""Tools for the incident-postmortem use case.

`log_events` condenses a log file (thousands of repeating lines become a short list of event
groups with counts and first/last times), which no model could read line by line. The drafts and
the published postmortems are kept in `out/`; replace `publish_postmortem` by a call to your
wiki's API (see README), the approval stays. Tickets and deployments are read with
the `incident_data` connector declared in `blueprint.yaml`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from awa.runtime import case_dir

HERE = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """The sample data: the eval case's copy inside `awa eval`, else the folder here."""

    return case_dir(HERE) / "data"


def _out_dir() -> Path:
    """Where the process writes: the eval case's copy inside `awa eval`, else out/ here."""

    return case_dir(HERE) / "out"


LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
MAX_GROUPS = 60
_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:]{8})Z (DEBUG|INFO|WARN|ERROR) (.*)$")
_INCIDENT = re.compile(r"[A-Za-z0-9-]{3,40}")


def log_events(log_file: str, start: str, end: str, min_level: str = "WARN") -> str:
    """Summarize a log file between two UTC times as event groups.

    `log_file` is a file name in the data/logs folder, `start` and `end` are ISO times
    (2026-10-02T09:00:00). Lines at or above `min_level` (DEBUG, INFO, WARN, ERROR) are grouped
    when their text is the same apart from numbers; each group has the first and last time, the
    count and an example. Groups come in time order; at most 60 are returned.
    """

    path = (_data_dir() / "logs" / log_file).resolve()
    if not path.is_relative_to((_data_dir() / "logs").resolve()) or not path.is_file():
        return f"No log file {log_file!r} in the logs folder."
    floor = LEVELS.get(min_level.upper(), 2)
    start, end = start.rstrip("Z"), end.rstrip("Z")
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = _LINE.match(raw)
        if not match or not start <= match[1] <= end or LEVELS[match[2]] < floor:
            continue
        key = (match[2], re.sub(r"\d+", "#", match[3]))
        group = groups.setdefault(
            key,
            {
                "level": match[2],
                "first": match[1],
                "last": match[1],
                "count": 0,
                "example": match[3],
            },
        )
        group["last"], group["count"] = match[1], group["count"] + 1
    ordered = sorted(groups.values(), key=lambda g: g["first"])
    return json.dumps({"groups": ordered[:MAX_GROUPS], "total_groups": len(ordered)})


def save_postmortem_draft(incident_id: str, markdown: str) -> str:
    """Save the postmortem draft for review as `out/drafts/<incident id>.md`.

    A draft is not published; publishing is a separate step that waits for approval.
    """

    if not _INCIDENT.fullmatch(incident_id):
        return f"Not saved: {incident_id!r} is not a plain incident id."
    (_out_dir() / "drafts").mkdir(parents=True, exist_ok=True)
    (_out_dir() / "drafts" / f"{incident_id}.md").write_text(
        markdown, encoding="utf-8", newline="\n"
    )
    return f"Saved the draft of {incident_id}."


def publish_postmortem(incident_id: str) -> str:
    """Publish the saved draft of an incident to the engineering wiki. Waits for approval.

    In production: create the page through your wiki's API (the `openapi` or `rest` connector).
    """

    draft = _out_dir() / "drafts" / f"{incident_id}.md"
    if not _INCIDENT.fullmatch(incident_id) or not draft.is_file():
        return f"Not published: there is no saved draft for {incident_id!r}."
    (_out_dir() / "published").mkdir(exist_ok=True)
    (_out_dir() / "published" / draft.name).write_text(
        draft.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return f"Published the postmortem of {incident_id}."


__all__ = ["log_events", "publish_postmortem", "save_postmortem_draft"]
