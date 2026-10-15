"""`PeopleFile`: `config/people.yaml`, the people a project pinned and the roles they hold."""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from awa._internal.serialize import dump_yaml, load_yaml
from awa.core.dotenv import restrict_to_owner
from awa.people._book import PeopleBook
from awa.people._ref import PeopleError, PersonRef, check_key, check_role, is_guid, stamp

SCHEMA = "awa.people/v1"
"""The format version of `config/people.yaml`."""
PEOPLE_FILE = "config/people.yaml"
"""Where a project keeps its pinned people, relative to the project folder."""
STORED_FIELDS = ("source", "entra", "name", "mail", "kind", "state", "checked_at")
"""The seven fields stored per person (data minimization: nothing more)."""
_MAX_FILE = 2_000_000


class PeopleFile:
    """Reads and writes one project's `config/people.yaml` (schema `awa.people/v1`).

    It keeps the least personal data that works: per person the directory id, name, mail, source,
    guest flag, state and last check. It is written atomically, readable by the owner only, and kept
    out of git (`ensure_gitignored`); unpinning deletes the entry. A missing file reads as empty.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def __repr__(self) -> str:
        return f"PeopleFile({str(self.path)!r})"

    @classmethod
    def of_project(cls, root: str | Path) -> PeopleFile:
        """The people file of the project in `root`."""

        return cls(Path(root) / PEOPLE_FILE)

    def load(self) -> PeopleBook:
        """The book in the file (empty when there is no file).

        Raises:
            PeopleError: If the file is too large, is not `awa.people/v1`, or an entry is invalid.
        """

        if not self.path.exists():
            return PeopleBook()
        if self.path.is_symlink() or not self.path.is_file():
            raise PeopleError(f"{self.path} is a link or a folder, not a people file.")
        if self.path.stat().st_size > _MAX_FILE:
            raise PeopleError(f"{self.path} is larger than {_MAX_FILE} bytes.")
        try:
            data = load_yaml(self.path.read_text(encoding="utf-8"), what="people file") or {}
        except ValueError as exc:
            raise PeopleError(f"{self.path} is not valid YAML: {exc}") from None
        if not isinstance(data, Mapping) or data.get("schema") != SCHEMA:
            raise PeopleError(f"{self.path} must start with schema: {SCHEMA}.")
        people_raw = data.get("people") or {}
        assign_raw = data.get("assign") or {}
        if not isinstance(people_raw, Mapping) or not isinstance(assign_raw, Mapping):
            raise PeopleError(f"{self.path}: people and assign must be mappings.")
        people = {str(k): _person(str(k), v, self.path) for k, v in people_raw.items()}
        assign: dict[str, list[str]] = {}
        for role, keys in assign_raw.items():
            check_role(str(role))
            if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                raise PeopleError(f"{self.path}: assign.{role} must be a list of person keys.")
            missing = [k for k in keys if k not in people]
            if missing:
                raise PeopleError(
                    f"{self.path}: assign.{role} names {', '.join(missing)}, who is not pinned."
                )
            assign[str(role)] = list(dict.fromkeys(keys))
        return PeopleBook(people, assign)

    def save(self, book: PeopleBook) -> None:
        """Write `book` atomically, readable by the owner only (an empty book removes the file)."""

        if not book.people:
            self.path.unlink(missing_ok=True)
            return
        document = {
            "schema": SCHEMA,
            "people": {key: _entry(p) for key, p in sorted(book.people.items())},
            "assign": {role: list(keys) for role, keys in sorted(book.assign.items()) if keys},
        }
        if self.path.is_symlink() or self.path.is_dir():
            raise PeopleError(f"{self.path} is a link or a folder, not a people file.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A fresh, unpredictable name created exclusively: a planted link is never followed and
        # two writers never share a temporary file; the last complete write wins.
        descriptor, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(dump_yaml(document))
            restrict_to_owner(temporary)
            for attempt in range(5):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:  # Windows: another process has the file open
                    if attempt == 4:
                        raise PeopleError(f"{self.path} is in use; try again.") from None
                    time.sleep(0.05 * (attempt + 1))
        finally:
            temporary.unlink(missing_ok=True)


def ensure_gitignored(root: str | Path, entry: str = PEOPLE_FILE) -> bool:
    """Add `entry` to the project's `.gitignore` unless covered; whether the file changed."""

    ignore = Path(root) / ".gitignore"
    text = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
    covering = {entry, f"/{entry}", entry.split("/")[0] + "/", "/" + entry.split("/")[0] + "/"}
    if any(line.strip() in covering for line in text.splitlines()):
        return False
    prefix = "" if not text or text.endswith("\n") else "\n"
    note = "# pinned people (personal data): never commit\n"
    ignore.write_text(f"{text}{prefix}{note}{entry}\n", encoding="utf-8", newline="\n")
    return True


def _entry(person: PersonRef) -> dict[str, Any]:
    entry: dict[str, Any] = {"source": person.source}
    if person.source == "entra":
        entry["entra"] = f"{person.tenant_id}/{person.object_id}"
    entry.update(name=person.name, mail=person.mail, kind=person.kind, state=person.state)
    entry["checked_at"] = person.checked_at
    return entry


def _person(key: str, raw: Any, where: Path) -> PersonRef:
    if not isinstance(raw, Mapping):
        raise PeopleError(
            f"{where}: the entry {key!r} must be a mapping of {', '.join(STORED_FIELDS)}."
        )
    unknown = set(raw) - set(STORED_FIELDS)
    if unknown:
        raise PeopleError(
            f"{where}: the entry {key!r} has fields awa does not store: {', '.join(sorted(map(str, unknown)))}."
        )
    data: dict[str, Any] = {k: raw[k] for k in STORED_FIELDS if k in raw and k != "entra"}
    if raw.get("source") == "entra":
        tenant, _, oid = str(raw.get("entra") or "").partition("/")
        if not (is_guid(tenant) and is_guid(oid)):
            raise PeopleError(f"{where}: {key!r} needs entra: <tenant id>/<object id> (two GUIDs).")
        data.update(tenant_id=tenant.lower(), object_id=oid.lower())
    checked = data.get("checked_at")
    if isinstance(checked, datetime):  # written by hand without quotes
        moment = checked if checked.tzinfo else checked.replace(tzinfo=UTC)
        data["checked_at"] = stamp(moment.astimezone(UTC))
    elif checked is not None:
        data["checked_at"] = str(checked)
    try:
        return PersonRef(key=check_key(key), **data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise PeopleError(f"{where}: the entry {key!r} is not valid ({problems}).") from None
