"""Tools for the employee-onboarding recipe, backed by small in-memory sample systems.

The tools stand in for the HR information system (new hires, buddies, payroll), the
identity provider (accounts and groups), procurement in the ERP (hardware purchase
requests), the facilities system (desks and badges) and the calendar. Replace each body
with a call to your system; keep the names, signatures and docstrings, which the agents
rely on.

Steps that commit money or personal data do not act directly: `order_hardware` raises a
purchase request that procurement approves in the ERP, and `create_payroll_record` creates
a draft that a payroll administrator signs off in the HR system (four-eyes principle).
"""

from __future__ import annotations

import json
from typing import Any, Literal

_HIRES: dict[str, dict[str, Any]] = {
    "H-2001": {
        "name": "Maria Sample",
        "role": "Software Engineer",
        "department": "Engineering",
        "manager": "Chris Lead",
        "start_date": "2026-10-05",
        "location": "Berlin office",
        "employment_type": "employee",
        "equipment_profile": "engineering",
    },
    "H-2002": {
        "name": "Tom Example",
        "role": "Account Executive",
        "department": "Sales",
        "manager": "Pat Manager",
        "start_date": "2026-10-12",
        "location": "remote",
        "employment_type": "employee",
        "equipment_profile": "standard",
        "shipping_address": "Musterweg 5, 20095 Hamburg",
    },
    "H-2003": {
        "name": "Ava Placeholder",
        "role": "Data Analyst (contractor)",
        "department": "Finance",
        "manager": "Sam Controller",
        "start_date": "2026-10-05",
        "location": "Berlin office",
        "employment_type": "contractor",
        "equipment_profile": "standard",
    },
}

_GROUPS: dict[str, list[str]] = {
    "Engineering": ["all-staff", "engineering", "code-repositories", "on-call-tools"],
    "Sales": ["all-staff", "sales", "crm-users"],
    "Finance": ["all-staff", "finance-readonly"],
}
_BUDDIES: dict[str, str] = {
    "Engineering": "Lena Mentor",
    "Sales": "Omar Mentor",
    "Finance": "Iris Mentor",
}
_EQUIPMENT: dict[str, str] = {
    "engineering": "16-inch developer laptop, 2 monitors, headset",
    "standard": "14-inch laptop, 1 monitor, headset",
}

ACCOUNTS: list[dict[str, Any]] = []
PURCHASE_REQUESTS: list[dict[str, Any]] = []
PAYROLL_DRAFTS: list[dict[str, Any]] = []
BUDDIES: list[dict[str, str]] = []
DESKS: list[dict[str, str]] = []
BADGES: list[dict[str, str]] = []
CALENDAR: list[dict[str, Any]] = []
MANAGER_NOTES: list[dict[str, str]] = []


def reset_sample_data() -> None:
    """Restore the sample systems to their initial state (used by tests)."""

    for records in (
        ACCOUNTS,
        PURCHASE_REQUESTS,
        PAYROLL_DRAFTS,
        BUDDIES,
        DESKS,
        BADGES,
        CALENDAR,
        MANAGER_NOTES,
    ):
        records.clear()


def _hire(hire_id: str) -> dict[str, Any] | None:
    return _HIRES.get(hire_id.strip().upper())


def _number(hire_id: str) -> str:
    # Sample references derive from the hire id, so repeated or concurrent runs agree.
    return hire_id.strip().upper().removeprefix("H-")


def get_new_hire(hire_id: str) -> str:
    """Return a new hire's record: name, role, department, manager, start date, location,
    employment type (employee or contractor) and equipment profile.

    In production: read the hire from the HR information system.
    """

    hire = _hire(hire_id)
    return json.dumps({"hire_id": hire_id, **hire}) if hire else f"No new hire {hire_id!r}."


def create_accounts(hire_id: str) -> str:
    """Create the work email, single sign-on account and department groups for a new hire.

    Accounts stay disabled until the start date. In production: call your identity provider.
    """

    hire = _hire(hire_id)
    if hire is None:
        return f"No new hire {hire_id!r}."
    first, last = hire["name"].lower().split()
    email = f"{first}.{last}@company.example"
    groups = _GROUPS[hire["department"]]
    ACCOUNTS.append({"hire_id": hire_id, "email": email, "groups": groups})
    return f"Created {email} (enabled on {hire['start_date']}) with groups {', '.join(groups)}."


def order_hardware(hire_id: str, ship_to: Literal["office", "home"]) -> str:
    """Raise a purchase request for the hire's standard equipment; procurement approves it.

    `ship_to` "home" ships to the address in the hire's record (remote hires). In production:
    create a purchase request in the ERP's procurement module.
    """

    hire = _hire(hire_id)
    if hire is None:
        return f"No new hire {hire_id!r}."
    if ship_to == "home" and "shipping_address" not in hire:
        return "Not requested: the hire's record has no shipping address."
    request = f"PR-{_number(hire_id)}"
    PURCHASE_REQUESTS.append(
        {
            "hire_id": hire_id,
            "request": request,
            "ship_to": ship_to,
            "items": _EQUIPMENT[hire["equipment_profile"]],
        }
    )
    return (
        f"Purchase request {request} ({_EQUIPMENT[hire['equipment_profile']]}, ship to "
        f"{ship_to}) is waiting for procurement approval; delivery 5 business days after it."
    )


def create_payroll_record(hire_id: str) -> str:
    """Create a draft payroll record for an employee from the signed contract data.

    A payroll administrator signs it off in the HR system; contractors are not on payroll.
    In production: create the draft through your HR or payroll system's API.
    """

    hire = _hire(hire_id)
    if hire is None:
        return f"No new hire {hire_id!r}."
    if hire["employment_type"] != "employee":
        return "Not created: contractors are paid through accounts payable, not payroll."
    PAYROLL_DRAFTS.append({"hire_id": hire_id, "status": "awaiting approval"})
    return (
        f"Draft payroll record for {hire['name']} is awaiting the payroll administrator's approval."
    )


def assign_buddy(hire_id: str) -> str:
    """Assign an onboarding buddy from the hire's department and inform them.

    In production: pick from the buddy pool in the HR system and send the buddy a note.
    """

    hire = _hire(hire_id)
    if hire is None:
        return f"No new hire {hire_id!r}."
    buddy = _BUDDIES[hire["department"]]
    BUDDIES.append({"hire_id": hire_id, "buddy": buddy})
    return f"{buddy} is the onboarding buddy of {hire['name']}."


def assign_desk(hire_id: str) -> str:
    """Reserve a desk near the hire's team in their office (office-based hires only).

    In production: book the desk in your facilities or workplace system.
    """

    hire = _hire(hire_id)
    if hire is None:
        return f"No new hire {hire_id!r}."
    if hire["location"] == "remote":
        return "Not assigned: the hire works remotely."
    desk = f"B-3.{_number(hire_id)[-2:]}"
    DESKS.append({"hire_id": hire_id, "desk": desk})
    return f"Desk {desk} (floor 3, next to {hire['department']}) is reserved from {hire['start_date']}."


def issue_badge(hire_id: str) -> str:
    """Request an access badge for the hire's office and desk floor; needs a reserved desk.

    In production: create the badge request in your access-control system.
    """

    hire = _hire(hire_id)
    if hire is None:
        return f"No new hire {hire_id!r}."
    if not any(_number(desk["hire_id"]) == _number(hire_id) for desk in DESKS):
        return "Not requested: reserve a desk first, the badge grants access to its floor."
    badge = f"BD-{_number(hire_id)}"
    BADGES.append({"hire_id": hire_id, "badge": badge})
    return f"Badge {badge} is ready for pick-up at reception on {hire['start_date']}."


def schedule_first_week(hire_id: str, sessions: list[str]) -> str:
    """Put the first-week sessions (one line each: day, time, session, with whom) into the
    calendars of the hire, the manager and the buddy.

    In production: create the invitations through your calendar system.
    """

    hire = _hire(hire_id)
    if hire is None:
        return f"No new hire {hire_id!r}."
    CALENDAR.append({"hire_id": hire_id, "sessions": list(sessions)})
    return f"Scheduled {len(sessions)} first-week sessions for {hire['name']}."


def send_manager_summary(hire_id: str, summary: str) -> str:
    """Email the hiring manager the onboarding status: what is done and what is pending.

    Internal message. In production: send through your email or chat system.
    """

    hire = _hire(hire_id)
    if hire is None:
        return f"No new hire {hire_id!r}."
    MANAGER_NOTES.append({"hire_id": hire_id, "to": hire["manager"], "summary": summary})
    return f"Sent the onboarding summary to {hire['manager']}."


__all__ = [
    "assign_buddy",
    "assign_desk",
    "create_accounts",
    "create_payroll_record",
    "get_new_hire",
    "issue_badge",
    "order_hardware",
    "schedule_first_week",
    "send_manager_summary",
]
