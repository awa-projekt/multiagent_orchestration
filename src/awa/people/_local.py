"""`LocalPeople`: a people directory from a local CSV or YAML list, for work without Entra ID."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from awa._internal.serialize import load_yaml
from awa.people._ref import (
    MAX_RESULTS,
    PeopleError,
    PersonRef,
    check_key,
    clean_search_text,
    make_key,
    matches,
    stamp,
)
from awa.people.ports import PeopleDirectory
from awa.runtime.clock import Clock, as_clock

MAX_FILE = 5_000_000
"""The largest list read (bytes)."""
_NAME = ("name", "display_name", "displayname")
_MAIL = ("mail", "email", "e-mail")
_STATES = ("active", "disabled")


class LocalPeople(PeopleDirectory):
    """A `PeopleDirectory` over a CSV or YAML file of people, reread on every call.

    CSV: a header with `name` (or `display_name`) and `mail` (or `email`), optionally `key`, `state`
    (`active` or `disabled`) and `guest` (`yes`). YAML: a list of such mappings, or `people:`
    holding a list or a key-to-mapping map. A missing `key` is made from the name. Every typed word
    must start a word of the name or mail, in any order, ignoring accents and case.

    Raises:
        PeopleError: When a call finds the file missing, too large or malformed.
    """

    source = "local"

    def __init__(self, path: str | Path, *, clock: Clock | None = None) -> None:
        self.path = Path(path)
        self._clock = as_clock(clock)

    def __repr__(self) -> str:
        return f"LocalPeople({str(self.path)!r})"

    def _stamp(self) -> str:
        return stamp(self._clock.now())

    def _rows(self) -> Iterator[Mapping[str, Any]]:
        if not self.path.is_file():
            raise PeopleError(f"The people list {self.path} does not exist.")
        if self.path.stat().st_size > MAX_FILE:
            raise PeopleError(f"The people list {self.path} is larger than {MAX_FILE} bytes.")
        text = self.path.read_text(encoding="utf-8-sig")
        if self.path.suffix.lower() == ".csv":
            yield from csv.DictReader(io.StringIO(text))
            return
        try:
            data = load_yaml(text, what="people list")
        except ValueError as exc:
            raise PeopleError(f"The people list {self.path} is not valid YAML: {exc}") from None
        if isinstance(data, Mapping):
            data = data.get("people")
        if isinstance(data, Mapping):
            data = [{**(v or {}), "key": k} for k, v in data.items()]
        if not isinstance(data, list) or not all(isinstance(r, Mapping) for r in data):
            raise PeopleError(
                f"The people list {self.path} must be a list of people (name, mail) or "
                "people: with such a list."
            )
        yield from data

    def _people(self) -> list[PersonRef]:
        found: list[PersonRef] = []
        taken: set[str] = set()
        checked_at = self._stamp()
        for number, row in enumerate(self._rows(), start=1):
            name = _pick(row, _NAME)
            if name is None:
                raise PeopleError(f"{self.path}: entry {number} has no name.")
            key = _pick(row, ("key",))
            key = check_key(key) if key else make_key(name, taken)
            if key in taken:
                raise PeopleError(f"{self.path}: the key {key!r} appears twice.")
            taken.add(key)
            state = (_pick(row, ("state",)) or "active").lower()
            if state not in _STATES:
                raise PeopleError(
                    f"{self.path}: {key} has state {state!r}; use active or disabled."
                )
            guest = (_pick(row, ("guest", "kind")) or "").lower() in ("yes", "true", "1", "guest")
            found.append(
                PersonRef(
                    key=key,
                    source="local",
                    name=name,
                    mail=_pick(row, _MAIL),
                    kind="guest" if guest else "member",
                    state="active" if state == "active" else "disabled",
                    checked_at=checked_at,
                )
            )
        return found

    def search(self, text: str, *, limit: int = 10, guests: bool = False) -> list[PersonRef]:
        """People whose name or mail words start with the words of `text` (see the class)."""

        query = clean_search_text(text)
        limit = max(1, min(int(limit), MAX_RESULTS))
        hits = [
            p
            for p in self._people()
            if (guests or p.kind != "guest")
            and matches(query, p.name, (p.mail or "").partition("@")[0])
        ]
        return sorted(hits, key=lambda p: (p.name.casefold(), p.key))[:limit]

    def get(self, ref: PersonRef) -> PersonRef:
        """The entry with `ref.key` from the file; `state="deleted"` when it is not there."""

        for person in self._people():
            if person.key == ref.key:
                return person
        return ref.model_copy(update={"state": "deleted", "checked_at": self._stamp()})

    def check(self, refs: Sequence[PersonRef]) -> list[PersonRef]:
        """`get` for each of `refs` (the file is read once)."""

        people = {p.key: p for p in self._people()}
        checked_at = self._stamp()
        return [
            people.get(r.key) or r.model_copy(update={"state": "deleted", "checked_at": checked_at})
            for r in refs
        ]


def _pick(row: Mapping[str, Any], names: Sequence[str]) -> str | None:
    lowered = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    for name in names:
        value = lowered.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
