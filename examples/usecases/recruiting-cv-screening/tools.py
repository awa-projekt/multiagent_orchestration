"""Tools of the recruiting-cv-screening use case: the explainable score and the interview invite.

CVs are read by the `workspace` connector (`connector:workspace.read_pdf`, `.read_docx`); this
file holds the real logic. `score_candidate` scores a structured profile against the criteria
of `data/role_profile.yaml`, point by point with a reason for each. It accepts job-related
fields only (skills, years of experience, languages), so protected attributes cannot enter the
score. `send_interview_invite` only invites candidates whose saved score is a shortlist score.
In production the invitation goes through your applicant tracking system or mail (see the README).
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import yaml

from awa.runtime import case_dir

HERE = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """The sample data: the eval case's copy inside `awa eval`, else the folder here."""

    return case_dir(HERE) / "data"


def _outbox_dir() -> Path:
    return _data_dir() / "outbox"


_ID = re.compile(r"[A-Za-z0-9_-]{1,40}")
_ADDRESS = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")


def _role(role_id: str) -> dict[str, Any] | None:
    role = yaml.safe_load((_data_dir() / "role_profile.yaml").read_text(encoding="utf-8"))
    return role if role["role_id"] == role_id.strip().upper() else None


def _score_file(candidate_id: str) -> Path:
    return _outbox_dir() / "scores" / f"{candidate_id.strip().upper()}.json"


def score_candidate(
    role_id: str,
    candidate_id: str,
    email: str,
    skills: list[str],
    years_experience: float,
    languages: list[str],
) -> str:
    """Score a candidate's profile against a role's criteria and save the result.

    Pass only what the CV states about skills, years of professional experience in the field
    and languages (without levels). Returns the points per criterion with the reason, the total
    out of 100, the required criteria that are missing and whether the candidate is shortlisted
    (total at or above the role's limit and every required criterion met).

    In production: keep the criteria in your applicant tracking system or in the role profile.
    """

    if not _ID.fullmatch(candidate_id):
        return f"Refused: {candidate_id!r} is not a valid candidate id."
    if not _ADDRESS.fullmatch(email.strip()):
        return f"Refused: {email!r} is not a single email address."
    role = _role(role_id)
    if role is None:
        return f"Unknown role {role_id!r}."
    skill_text = " ".join(skills).lower()
    language_text = " ".join(languages).lower()
    criteria, missing = [], []
    for item in role["criteria"]:
        points, reason = 0.0, ""
        if item["type"] == "years":
            if years_experience >= item["min_years"]:
                points = item["points"] * min(1.0, years_experience / item["full_years"])
            reason = (
                f"{years_experience:g} years; {item['min_years']} needed, "
                f"full points from {item['full_years']}"
            )
        else:
            text = skill_text if item["type"] == "skill" else language_text
            found = [
                a for a in item["aliases"] if re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", text)
            ]
            points = item["points"] if found else 0.0
            reason = f"found {', '.join(found)}" if found else "not stated in the CV"
        if item.get("required") and points == 0:
            missing.append(item["label"])
        criteria.append(
            {
                "id": item["id"],
                "label": item["label"],
                "points": round(points, 1),
                "max": item["points"],
                "reason": reason,
            }
        )
    total = round(sum(c["points"] for c in criteria), 1)
    result = {
        "role_id": role["role_id"],
        "candidate_id": candidate_id.strip().upper(),
        "email": email.strip(),
        "total": total,
        "shortlisted": total >= role["shortlist_from"] and not missing,
        "missing_required": missing,
        "criteria": criteria,
    }
    _score_file(candidate_id).parent.mkdir(parents=True, exist_ok=True)
    _score_file(candidate_id).write_text(json.dumps(result, indent=1), encoding="utf-8")
    return json.dumps(result)


def _taken_slots(invites: Path) -> set[str]:
    return {
        line.removeprefix("Slot: ")
        for file in invites.glob("*.txt")
        for line in file.read_text(encoding="utf-8").split("\n\n", 1)[0].splitlines()
        if line.startswith("Slot: ")
    }


def send_interview_invite(candidate_id: str, slot_id: str, message: str) -> str:
    """Invite a shortlisted candidate to an interview slot (see interview_slots.csv). Refused for
    a candidate who is not shortlisted by score_candidate and for a slot that is taken. A recruiter
    approves every invitation before it is sent.

    In production: send the invitation through your applicant tracking system or mail.
    """

    if not _ID.fullmatch(candidate_id):
        return f"Refused: {candidate_id!r} is not a valid candidate id."
    path = _score_file(candidate_id)
    score = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    if score is None or not score["shortlisted"]:
        return f"Refused: {candidate_id} is not shortlisted; score the CV first."
    with (_data_dir() / "interview_slots.csv").open(encoding="utf-8", newline="") as handle:
        slot = next((r for r in csv.DictReader(handle) if r["slot_id"] == slot_id), None)
    if slot is None:
        return f"Refused: unknown slot {slot_id!r}."
    invites = _outbox_dir() / "invites"
    mine = invites / f"{score['candidate_id']}.txt"
    if mine.is_file():
        return f"Already queued: {mine.name}."
    taken = _taken_slots(invites)
    if slot_id in taken:
        return f"Refused: slot {slot_id} is taken."
    invites.mkdir(parents=True, exist_ok=True)
    head = (
        f"To: {score['email']}\nSubject: Interview invitation, {slot['start']}\nSlot: {slot_id}\n"
    )
    mine.write_text(f"{head}Interviewer: {slot['interviewer']}\n\n{message}\n", encoding="utf-8")
    return (
        f"Queued invitation for {score['candidate_id']} to {score['email']}, "
        f"{slot['start']} with {slot['interviewer']}."
    )


__all__ = ["score_candidate", "send_interview_invite"]
