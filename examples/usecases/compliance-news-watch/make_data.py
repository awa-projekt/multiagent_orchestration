"""Writes the sample data of the compliance-news-watch use case into `data/` (deterministic)."""

from __future__ import annotations

import json
from pathlib import Path

WATCHLIST = {
    "recipients": ["compliance-team@example.com"],
    "topics": [
        {
            "id": "gdpr",
            "query": "GDPR data protection authority fine enforcement",
            "why": "Fines and guidance change how we handle customer data.",
        },
        {
            "id": "nis2",
            "query": "NIS2 directive national implementation cybersecurity obligations",
            "why": "Our IT services fall under the essential entities.",
        },
        {
            "id": "csrd",
            "query": "CSRD sustainability reporting omnibus delay",
            "why": "Reporting deadlines drive the finance close calendar.",
        },
    ],
}

PREVIOUS_DIGEST = {
    "date": "2026-10-03",
    "items": [
        {
            "topic": "gdpr",
            "title": "Data protection authority fines a retailer for unlawful tracking",
            "url": "https://news.example.org/gdpr/retailer-tracking-fine",
            "summary": "Fine of 1.2 million EUR for cookies set before consent.",
        },
        {
            "topic": "nis2",
            "title": "Germany passes its NIS2 implementation act",
            "url": "https://news.example.org/nis2/germany-implementation",
            "summary": "Registration with the BSI is required within three months.",
        },
        {
            "topic": "csrd",
            "title": "Omnibus package: first CSRD reporting year postponed",
            "url": "https://news.example.org/csrd/first-reporting-year-postponed",
            "summary": "Companies of the second group report one year later.",
        },
    ],
}


def generate(directory: Path) -> None:
    """Write the watchlist and the previous digest into `directory`."""

    directory.mkdir(parents=True, exist_ok=True)
    for name, content in (("watchlist.json", WATCHLIST), ("previous_digest.json", PREVIOUS_DIGEST)):
        text = json.dumps(content, indent=2, ensure_ascii=False) + "\n"
        (directory / name).write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    generate(Path(__file__).resolve().parent / "data")
