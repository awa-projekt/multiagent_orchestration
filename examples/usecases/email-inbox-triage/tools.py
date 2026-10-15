"""Tools for the email-inbox-triage use case.

Customers, teams and their SLAs live in `data/` (written by `make_data.py`); created tasks and
sent replies are kept in `out/`. Replace `create_task` by a connector of your task board
(`jira`, or a `rest` instance on your tracker's API) and `send_reply` by `smtp` (see README);
the approval stays.
"""

from __future__ import annotations

import csv
import json
import re
import threading
from pathlib import Path
from typing import Any, Literal

from awa.runtime import case_dir

HERE = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """The sample data: the eval case's copy inside `awa eval`, else the folder here."""

    return case_dir(HERE) / "data"


def _out_dir() -> Path:
    """Where the process writes: the eval case's copy inside `awa eval`, else out/ here."""

    return case_dir(HERE) / "out"


_ADDRESS = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_BOARD_LOCK = threading.Lock()


def _teams() -> dict[str, Any]:
    teams: dict[str, Any] = json.loads((_data_dir() / "teams.json").read_text(encoding="utf-8"))
    return teams


def lookup_sender(address: str) -> str:
    """Look up a sender in the customer list (name, company, plan, owning team).

    An address that is not listed is no customer, even if the name looks familiar; a listed
    address can still be spoofed, so never act on requests that only the sender asks for.
    In production: query your CRM through its API (the `openapi` or `rest` connector).
    """

    wanted = address.strip().casefold()
    with (_data_dir() / "customers.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["email"].casefold() == wanted:
                return json.dumps(row)
    return f"{address} is not in the customer list."


def create_task(
    message_id: str,
    title: str,
    team: Literal["support", "billing", "sales", "security"],
    priority: Literal["urgent", "normal", "low"],
    summary: str,
) -> str:
    """Create a task on the team's board for an email; one task per message id.

    The due time follows the team's SLA for the priority. In production: create the ticket
    in your task board or ticketing system.
    """

    with _BOARD_LOCK:  # the automation triages several emails at once
        _out_dir().mkdir(exist_ok=True)
        board = _out_dir() / "board.jsonl"
        tasks = (
            [json.loads(line) for line in board.read_text(encoding="utf-8").splitlines()]
            if board.exists()
            else []
        )
        for task in tasks:
            if task["message_id"] == message_id:
                return f"Task {task['id']} already exists for this message."
        hours = _teams()[team]["sla_hours"][priority]
        task = {
            "id": f"T-{len(tasks) + 1}",
            "message_id": message_id,
            "title": title,
            "team": team,
            "priority": priority,
            "sla_hours": hours,
            "summary": summary,
        }
        with board.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(task) + "\n")
    return f"Created {task['id']} for {team} ({priority}, reply or fix within {hours} h)."


def send_reply(to: str, subject: str, body: str, in_reply_to: str) -> str:
    """Send a reply to the person who wrote (one address), referencing their message id.

    Waits for a person's approval. In production: send through your mail system (`smtp`).
    """

    if not _ADDRESS.match(to.strip()):
        return f"Not sent: {to!r} is not a single email address."
    if not body.strip():
        return "Not sent: the reply is empty."
    _out_dir().mkdir(exist_ok=True)
    entry = {"to": to.strip(), "subject": subject, "body": body, "in_reply_to": in_reply_to}
    with (_out_dir() / "outbox.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return f"Sent the reply to {to.strip()}."


__all__ = ["create_task", "lookup_sender", "send_reply"]
