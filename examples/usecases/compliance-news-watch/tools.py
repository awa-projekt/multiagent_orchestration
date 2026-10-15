"""Tools for the compliance-news-watch use case.

The watchlist and the previous digest live in `data/` (written by `make_data.py`); the sent
digests and the digest to compare with next time are kept in `out/`. Web search and page
reading come from the `web` connector, so no tool here touches the internet. Replace
`send_digest` by the `smtp` connector (see README) to send real mail; the approval stays.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from typing_extensions import TypedDict

from awa.runtime import case_dir

HERE = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """The sample data: the eval case's copy inside `awa eval`, else the folder here."""

    return case_dir(HERE) / "data"


def _out_dir() -> Path:
    """Where the process writes: the eval case's copy inside `awa eval`, else out/ here."""

    return case_dir(HERE) / "out"


class Story(TypedDict):
    """One story found for the digest, as the researcher reports it."""

    topic: str
    title: str
    url: str
    summary: str


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


_TRACKING = frozenset({"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid"})


def _key(url: str) -> str:
    """Compare links without tracking parameters, fragments, case of host and trailing slash."""

    parts = urlsplit(url.strip())
    query = sorted(
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not name.lower().startswith("utm_") and name.lower() not in _TRACKING
    )
    kept = f"?{urlencode(query)}" if query else ""
    return f"{parts.netloc.lower().removeprefix('www.')}{parts.path.rstrip('/')}{kept}"


def _previous() -> dict[str, Any]:
    kept = _out_dir() / "last_digest.json"
    result: dict[str, Any] = _read(kept if kept.is_file() else _data_dir() / "previous_digest.json")
    return result


def get_watchlist() -> str:
    """Return the watched topics (id, search query, why it matters) and the digest recipients.

    In production: read your own compliance watchlist (a file, a wiki page or a database).
    """

    return json.dumps(_read(_data_dir() / "watchlist.json"))


def compare_with_previous(items: list[Story]) -> str:
    """Compare today's findings with the previous digest and say what is new, changed or gone.

    Each item needs `topic`, `title`, `url` and `summary`. Items with the same link are the
    same story: it is `changed` when title or summary differ, else `unchanged`. Only new and
    changed items belong in the digest.
    """

    previous = _previous()
    known = {_key(item["url"]): item for item in previous["items"]}
    seen: set[str] = set()
    new, changed, unchanged = [], [], []
    for item in items:
        key = _key(item["url"])
        seen.add(key)
        old = known.get(key)
        if old is None:
            new.append(item)
        elif (old["title"], old["summary"]) != (item["title"], item["summary"]):
            changed.append({**item, "previous_summary": old["summary"]})
        else:
            unchanged.append(item)
    gone = [item["url"] for key, item in known.items() if key not in seen]
    return json.dumps(
        {
            "previous_digest": previous["date"],
            "new": new,
            "changed": changed,
            "unchanged": unchanged,
            "no_longer_found": gone,
        }
    )


def send_digest(date: str, subject: str, body: str, items: list[Story]) -> str:
    """Email the digest to the compliance team on the watchlist and remember it for tomorrow.

    `date` is the day of the run (YYYY-MM-DD). `items` are all the stories the digest covers
    (topic, title, url, summary); `body` must cite the link of every one of them. Recipients
    come from the watchlist, never from arguments. In production: send through your mail system
    (the `smtp` connector).
    """

    if not items:
        return "Not sent: the digest has no items."
    missing = [item["url"] for item in items if item["url"] not in body]
    if missing:
        return f"Not sent: the body does not cite {', '.join(missing)}."
    if any(urlsplit(item["url"]).scheme != "https" for item in items):
        return "Not sent: only https sources may be cited."
    recipients = _read(_data_dir() / "watchlist.json")["recipients"]
    _out_dir().mkdir(exist_ok=True)
    entry = {
        "to": recipients,
        "subject": subject,
        "body": body,
        "sources": [i["url"] for i in items],
    }
    with (_out_dir() / "outbox.jsonl").open("a", encoding="utf-8") as outbox:
        outbox.write(json.dumps(entry) + "\n")
    (_out_dir() / "last_digest.json").write_text(
        json.dumps({"date": date, "items": items}), encoding="utf-8"
    )
    return f"Sent {subject!r} to {', '.join(recipients)} citing {len(items)} source(s)."


__all__ = ["compare_with_previous", "get_watchlist", "send_digest"]
