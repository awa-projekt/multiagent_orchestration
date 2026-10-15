"""Tools of the meeting-notes-to-tasks use case: dates, owners, the task board and the follow-up.

Notes, the team list and the meeting index are read by the `workspace` connector
(`connector:workspace.*`, see `blueprint.yaml`); this file holds the real logic. A model is bad at
calendar arithmetic and at guessing who "Ben" is, so `resolve_due_date` turns "next Wednesday" or
"EOW" into a date relative to the meeting, `add_task` resolves the owner against the team list
(refusing unknown and ambiguous names) and never adds a duplicate, and `send_follow_up` mails only
the owners of the meeting's tasks. In production the board is Jira, Asana or Planner (see the
README).
"""

from __future__ import annotations

import calendar
import csv
import difflib
import re
import threading
from datetime import date, timedelta
from pathlib import Path

from awa.runtime import case_dir

HERE = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """The sample data: the eval case's copy inside `awa eval`, else the folder here."""

    return case_dir(HERE) / "data"


def _outbox_dir() -> Path:
    return _data_dir() / "outbox"


def _board_file() -> Path:
    return _outbox_dir() / "tasks.csv"


_LOCK = threading.Lock()
_FIELDS = ["task_id", "title", "owner", "due_date", "status", "meeting_id"]
_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _meeting(meeting_id: str) -> dict[str, str] | None:
    wanted = meeting_id.strip().upper()
    return next((m for m in _rows(_data_dir() / "meetings.csv") if m["meeting_id"] == wanted), None)


def _board() -> list[dict[str, str]]:
    return [*_rows(_data_dir() / "tasks.csv"), *_rows(_board_file())]


def _first_after(day: date, weekday: int) -> date:
    return day + timedelta(days=(weekday - day.weekday() - 1) % 7 + 1)


def _due(text: str, meeting: date) -> date | None:
    text = text.strip().lower().rstrip(".")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)
    day_month = re.search(rf"\b(\d{{1,2}})\.? ?({'|'.join(_MONTHS)})[a-z]*", text)
    month_day = re.search(rf"\b({'|'.join(_MONTHS)})[a-z]* (\d{{1,2}})\b", text)
    pair = None
    if day_month:
        pair = (int(day_month[1]), day_month[2])
    elif month_day:
        pair = (int(month_day[2]), month_day[1])
    if pair:
        try:
            result = date(meeting.year, _MONTHS.index(pair[1]) + 1, pair[0])
        except ValueError:
            return None
        return result if result >= meeting else result.replace(year=meeting.year + 1)
    friday = meeting + timedelta(days=4 - meeting.weekday())
    if text in ("today", "now"):
        return meeting
    if text == "tomorrow":
        return meeting + timedelta(days=1)
    if text in ("eow", "end of week", "end of the week"):
        return friday if meeting.weekday() < 4 else friday + timedelta(days=7)
    if text in ("end of next week", "eonw"):
        return friday + timedelta(days=7)
    if text in ("eom", "end of month", "end of the month"):
        return meeting.replace(day=calendar.monthrange(meeting.year, meeting.month)[1])
    span = re.fullmatch(r"in (\d+) (day|week)s?", text)
    if span:
        return meeting + timedelta(days=int(span[1]) * (7 if span[2] == "week" else 1))
    weekday = re.fullmatch(r"(?:by |on |next |this )*(\w+)", text)
    names = [d for d in _DAYS if weekday and (d == weekday[1] or d[:3] == weekday[1])]
    return _first_after(meeting, _DAYS.index(names[0])) if names else None


def _means(member: str, tokens: list[str]) -> bool:
    """Whether the tokens of a name ("ben", "ben v", "ben vogt") point at the team member."""

    parts = member.lower().split()
    if parts[: len(tokens)] == tokens:
        return True
    initial = len(tokens) == 2 and len(tokens[1]) == 1
    return initial and parts[0] == tokens[0] and parts[-1].startswith(tokens[1])


def _owner(name: str) -> tuple[dict[str, str] | None, str]:
    """The team member `name` means (full name, first name or `Ben V.`), or why none."""

    tokens = re.sub(r"[.,]", " ", name).lower().split()
    if not tokens:
        return None, "no owner given"
    found = [m for m in _rows(_data_dir() / "team.csv") if _means(m["name"], tokens)]
    if len(found) == 1:
        return found[0], ""
    if found:
        return (
            None,
            f"{name!r} is ambiguous: {', '.join(m['name'] for m in found)}; ask who is meant",
        )
    return None, f"{name!r} is not in the team list"


def resolve_due_date(meeting_id: str, expression: str) -> str:
    """Turn a due date as people say it into a date, relative to the meeting date.

    Understands 2026-10-09, "7 Oct", "Oct 7", "Wed 7 Oct", "tomorrow", "Friday" and "next
    Wednesday" (the first such weekday after the meeting), "EOW" (that week's Friday, the next
    Friday when the meeting is on a Friday or later), "end of next week", "end of month" and
    "in 3 days". Returns the date as YYYY-MM-DD, or says that it cannot be resolved.

    In production: keep it; it needs only the meeting date from your calendar.
    """

    meeting = _meeting(meeting_id)
    if meeting is None:
        return f"Unknown meeting {meeting_id!r}."
    result = _due(expression, date.fromisoformat(meeting["date"]))
    return result.isoformat() if result else f"Cannot resolve {expression!r}; ask for a date."


def add_task(meeting_id: str, title: str, owner: str, due_date: str) -> str:
    """Add an action item to the task board. The owner (full name, first name or "Ben V.") must
    be one person of the team list and the due date a YYYY-MM-DD date on or after the meeting.
    An open task of the same owner with a similar title is not added twice.

    In production: create the task in your task tool (Jira, Asana, Planner).
    """

    meeting = _meeting(meeting_id)
    if meeting is None:
        return f"Unknown meeting {meeting_id!r}."
    person, problem = _owner(owner)
    if person is None:
        return f"Refused: {problem}."
    try:
        due = date.fromisoformat(due_date)
    except ValueError:
        return f"Refused: due date {due_date!r} is not YYYY-MM-DD; use resolve_due_date."
    if due < date.fromisoformat(meeting["date"]):
        return f"Refused: due date {due_date} is before the meeting."
    title = " ".join(title.split())
    with _LOCK:  # the number and the duplicate check must not race
        for task in _board():
            same = (
                difflib.SequenceMatcher(None, task["title"].lower(), title.lower()).ratio() > 0.75
            )
            if task["owner"] == person["name"] and task["status"] == "open" and same:
                return (
                    f"Already on the board: {task['task_id']} {task['title']} "
                    f"({task['owner']}, due {task['due_date']})."
                )
        number = f"TASK-{101 + len(_board())}"
        _board_file().parent.mkdir(parents=True, exist_ok=True)
        new_file = not _board_file().is_file()
        with _board_file().open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(_FIELDS)
            writer.writerow(
                [number, title, person["name"], due.isoformat(), "open", meeting["meeting_id"]]
            )
        return f"Created {number}: {title} ({person['name']}, due {due.isoformat()})."


def send_follow_up(meeting_id: str, subject: str, body: str) -> str:
    """Queue the follow-up mail of a meeting. The recipients are chosen here, not by the caller:
    the owners of the tasks created in this meeting. Refused when the meeting has no tasks.
    A person approves the mail before it goes out.

    In production: send it through your mail system.
    """

    meeting = _meeting(meeting_id)
    if meeting is None:
        return f"Unknown meeting {meeting_id!r}."
    owners = {t["owner"] for t in _rows(_board_file()) if t["meeting_id"] == meeting["meeting_id"]}
    emails = sorted(m["email"] for m in _rows(_data_dir() / "team.csv") if m["name"] in owners)
    if not emails:
        return (
            f"Refused: no tasks were created in {meeting['meeting_id']}, "
            "so there is nobody to write to."
        )
    mail = _outbox_dir() / "follow-ups" / f"{meeting['meeting_id']}.txt"
    if mail.is_file():
        return f"Already queued: {mail.name}."
    mail.parent.mkdir(parents=True, exist_ok=True)
    subject = " ".join(subject.split())  # one header line, whatever the caller passes
    mail.write_text(f"To: {', '.join(emails)}\nSubject: {subject}\n\n{body}\n", encoding="utf-8")
    return f"Queued the follow-up of {meeting['meeting_id']} to {', '.join(emails)}."


__all__ = ["add_task", "resolve_due_date", "send_follow_up"]
