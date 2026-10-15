"""Writes the sample data of the email-inbox-triage use case into `data/` (deterministic)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

CUSTOMERS = [
    (
        "anna.keller@brauhaus-keller.example",
        "Anna Keller",
        "Brauhaus Keller GmbH",
        "business",
        "billing",
    ),
    (
        "t.novak@novak-logistik.example",
        "Tomas Novak",
        "Novak Logistik s.r.o.",
        "enterprise",
        "support",
    ),
    ("m.rossi@rossi-design.example", "Marta Rossi", "Rossi Design", "starter", "support"),
    (
        "purchasing@nordwind-energie.example",
        "Purchasing",
        "Nordwind Energie AG",
        "enterprise",
        "billing",
    ),
    ("j.weber@weber-bau.example", "Jonas Weber", "Weber Bau GmbH", "business", "support"),
]
TEAMS = {
    "support": {
        "sla_hours": {"urgent": 2, "normal": 24, "low": 72},
        "lead": "support-lead@example.com",
    },
    "billing": {
        "sla_hours": {"urgent": 4, "normal": 24, "low": 72},
        "lead": "billing-lead@example.com",
    },
    "sales": {
        "sla_hours": {"urgent": 8, "normal": 48, "low": 96},
        "lead": "sales-lead@example.com",
    },
    "security": {
        "sla_hours": {"urgent": 1, "normal": 8, "low": 24},
        "lead": "security@example.com",
    },
}
KNOWLEDGE = {
    "invoicing.md": """# Invoicing FAQ

- Invoice copies: we send a PDF copy of any invoice of the last 24 months on request. Ask for the
  invoice number or the period. Copies go only to the billing contact on file.
- Payment terms are 30 days net. A reminder follows after 14 days past due.
- Changed bank details are never accepted by e-mail. We confirm them by phone with the contact on file.
""",
    "support-hours.md": """# Support hours and escalation

- Support answers Monday to Friday, 08:00 to 18:00 (Berlin). Urgent outages: phone +49 30 23125 000.
- Business and enterprise plans: first reply within 4 working hours; starter plans within 1 working day.
- Threats of legal action or the press go to the support lead the same day. Do not promise
  compensation in a first reply.
""",
    "returns.md": """# Returns and cancellations

- Hardware can be returned within 30 days in the original packaging; the customer gets a prepaid label.
- Subscriptions end at the end of the paid month; cancellations need the account owner's request.
""",
}


SAMPLE_MAIL = {
    "message_id": "<demo-1@example.com>",
    "from": "anna.keller@brauhaus-keller.example",
    "date": "2026-10-05",
    "subject": "Copy of invoice 2026-0412",
    "text": "Hello, could you send me a copy of invoice 2026-0412? Thanks, Anna",
}


def generate(directory: Path) -> None:
    """Write customers, teams and the knowledge base into `directory`."""

    (directory / "kb").mkdir(parents=True, exist_ok=True)
    with (directory / "customers.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["email", "name", "company", "plan", "owner_team"])
        writer.writerows(CUSTOMERS)
    for name, value in (("teams.json", TEAMS), ("sample_mail.json", SAMPLE_MAIL)):
        text = json.dumps(value, indent=2) + "\n"
        (directory / name).write_text(text, encoding="utf-8", newline="\n")
    for name, text in KNOWLEDGE.items():
        (directory / "kb" / name).write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    generate(Path(__file__).resolve().parent / "data")
