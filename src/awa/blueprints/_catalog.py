"""A compact catalog of the use-case packages and recipes, and a word-overlap retriever for design briefs.

`_catalog.json` is generated from the examples with `python -m awa.blueprints._catalog examples`.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

from awa._internal.serialize import load_yaml
from awa._internal.text import STOPWORDS, terms

_FILE = Path(__file__).with_name("_catalog.json")
_FOLDERS = (("usecases", "use case"), ("recipes", "recipe"))
_STOP = STOPWORDS | frozenset(
    (  # noqa: SIM905
        "after all also ask before between check create during each every get give make more most "
        "new not only out over per send should than under up without"
    ).split()
)
_TITLE_WEIGHT, _DESCRIPTION_WEIGHT, _PROCESS_WEIGHT = 3.0, 2.0, 1.0
_SOURCES = ("schedule", "mailbox", "file_drop", "webhook", "queue")


def build(examples: str | Path) -> list[dict[str, Any]]:
    """The catalog of the packages under `examples` (`usecases/*` and `recipes/*`)."""

    entries: list[dict[str, Any]] = []
    for folder, kind in _FOLDERS:
        for directory in sorted((Path(examples) / folder).iterdir()):
            readme = directory / "README.md"
            if readme.is_file() and (directory / "blueprint.yaml").is_file():
                entries.append(_entry(directory, kind))
    return entries


def _entry(directory: Path, kind: str) -> dict[str, Any]:
    data = load_yaml((directory / "blueprint.yaml").read_bytes(), what="blueprint")
    readme = (directory / "README.md").read_text(encoding="utf-8")
    title = readme.splitlines()[0].partition(":")[2].strip() or directory.name
    process = re.search(r"\*\*Process\.\*\*(.*?)(?:\n\n|\Z)", readme, re.S)
    summary = " ".join(process.group(1).split()) if process else ""
    return {
        "id": directory.name,
        "kind": kind,
        "title": title,
        "description": str(data.get("description", "")),
        "summary": summary,
        "structure": _structure(data),
    }


def _structure(data: Mapping[str, Any]) -> str:
    agents = [
        f"{a['name']}({', '.join(str(t).removeprefix('connector:') for t in a.get('tools', []))})"
        for a in data.get("agents", [])
        if isinstance(a, Mapping)
    ]
    team, workflow = data.get("team"), data.get("workflow")
    if isinstance(team, Mapping):
        shape = (
            f"team: {team.get('orchestrator')} delegates to {', '.join(team.get('members', []))}"
        )
    elif isinstance(workflow, Mapping):
        shape = "workflow: " + " -> ".join(_flat(step) for step in workflow.get("sequential", []))
    else:
        shape = "single agent"
    lines = [shape, "agents(tools): " + "; ".join(agents)]
    approvals = [
        str(a.get("tool")) + (f" when {a['when']}" if a.get("when") else "")
        for a in data.get("approvals", [])
        if isinstance(a, Mapping)
    ]
    if approvals:
        lines.append("approvals: " + ", ".join(approvals))
    triggers = [_trigger(t) for t in _triggers(data) if isinstance(t, Mapping)]
    if triggers:
        lines.append("triggers: " + "; ".join(triggers))
    return "\n".join(lines)


def _flat(step: Any) -> str:
    if isinstance(step, Mapping):
        return "(" + " | ".join(str(x) for x in step.get("parallel", [])) + ")"
    return str(step)


def _triggers(data: Mapping[str, Any]) -> list[Any]:
    section = data.get("automation")
    nested = section.get("triggers") if isinstance(section, Mapping) else None
    return list(nested or data.get("triggers") or [])


def _trigger(item: Mapping[str, Any]) -> str:
    kind = next(k for k in _SOURCES if k in item)
    source = item.get(kind)
    detail = ""
    if isinstance(source, Mapping):
        detail = str(source.get("cron") or source.get("every") or source.get("directory") or "")
    return (
        f"{item.get('name')} = {kind}{f' {detail}' if detail else ''}, input {item.get('input')!r}"
    )


@cache
def catalog() -> tuple[Mapping[str, Any], ...]:
    """The shipped catalog."""

    return tuple(json.loads(_FILE.read_text(encoding="utf-8")))


def closest(
    text: str, limit: int = 2, *, entries: Sequence[Mapping[str, Any]] | None = None
) -> list[Mapping[str, Any]]:
    """The up to `limit` entries most like `text`, best first; a second one only at 60 % of the best score."""

    pool = list(entries) if entries is not None else list(catalog())
    fields = [
        (
            Counter(terms(e["title"], _STOP)),
            Counter(terms(e["description"], _STOP)),
            Counter(terms(e["summary"], _STOP)),
        )
        for e in pool
    ]
    frequency = Counter(word for parts in fields for word in set().union(*parts))
    wanted = set(terms(text, _STOP))
    scored: list[tuple[float, int]] = []
    for index, (title, description, summary) in enumerate(fields):
        score = sum(
            math.log(1 + len(pool) / frequency[word])
            * (
                _TITLE_WEIGHT * (word in title)
                + _DESCRIPTION_WEIGHT * (word in description)
                + _PROCESS_WEIGHT * (word in summary)
            )
            for word in wanted
            if word in frequency
        )
        scored.append((score, index))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    best = scored[0][0] if scored else 0.0
    return [pool[index] for score, index in scored[:limit] if score > 0 and score >= 0.6 * best]


def reference_section(text: str, *, limit: int = 2) -> str:
    """The brief section for the packages closest to `text`; empty when none is close."""

    picked = closest(text, limit)
    if not picked:
        return ""
    blocks = [f"### {e['title']} ({e['kind']})\n{e['summary']}\n{e['structure']}" for e in picked]
    return (
        "## Similar packages (style reference only: adapt, do not copy; ignore whatever "
        "does not fit this process)\n" + "\n\n".join(blocks)
    )


def write(examples: str | Path, target: Path = _FILE) -> None:
    """Regenerate the shipped catalog from `examples`."""

    target.write_text(
        json.dumps(build(examples), indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    write(sys.argv[1] if len(sys.argv) > 1 else "examples")
