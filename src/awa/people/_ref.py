"""`PersonRef`: one person of a directory, and the pure rules every backend shares."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from awa.errors import AwaError

Source = Literal["entra", "local"]
Kind = Literal["member", "guest", "unknown"]
State = Literal["active", "disabled", "deleted", "unknown"]

MIN_SEARCH = 2
"""The shortest search text (a single letter would list half the company)."""
MAX_SEARCH = 64
"""The longest search text."""
MAX_RESULTS = 10
"""At most this many people come back from one search."""

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class PeopleError(AwaError):
    """A people directory or people file problem, worded for the person who fixes it."""

    code = "awa.people"


class PersonRef(BaseModel):
    """One person as a directory knows them; identity is the Entra ids or `key`, never name or mail.

    Attributes:
        key: Short name inside the project (`carla_meier`), unique in `config/people.yaml`.
        source: `entra` (Microsoft Entra ID) or `local` (a local list).
        tenant_id: The Entra tenant (`tid`); Entra only.
        object_id: The Entra object id (`oid`); Entra only.
        mail: Email address for sending; `None` when the person has no mailbox.
        kind: `member`, `guest` (B2B) or `unknown`.
        state: `active`, `disabled`, `deleted` (gone from the directory) or `unknown`.
        checked_at: When the directory last confirmed the entry (ISO 8601, UTC).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    source: Source
    tenant_id: str | None = None
    object_id: str | None = None
    name: str
    mail: str | None = None
    kind: Kind = "unknown"
    state: State = "unknown"
    checked_at: str | None = None

    @property
    def identity(self) -> tuple[str, ...]:
        """What makes two entries the same person: `("entra", tid, oid)` or `("local", key)`."""

        if self.source == "entra":
            return ("entra", (self.tenant_id or "").lower(), (self.object_id or "").lower())
        return ("local", self.key)

    @property
    def usable(self) -> bool:
        """Whether the person may hold a role now (deleted and disabled people may not)."""

        return self.state not in ("deleted", "disabled")


def clean_search_text(text: str) -> str:
    """`text` ready to search with: normalized (NFC), trimmed, inner spaces collapsed.

    Raises:
        PeopleError: If it has control characters, or fewer than 2 or more than 64 characters.
    """

    if not isinstance(text, str) or _CONTROL.search(text):
        raise PeopleError("The search text must be plain text without control characters.")
    cleaned = " ".join(unicodedata.normalize("NFC", text).split())
    if len(cleaned) < MIN_SEARCH:
        raise PeopleError(f"Type at least {MIN_SEARCH} characters of the name.")
    if len(cleaned) > MAX_SEARCH:
        raise PeopleError(f"The search text is longer than {MAX_SEARCH} characters.")
    if not words(cleaned):
        raise PeopleError("Type letters or digits of the name, not only symbols.")
    return cleaned


def words(text: str) -> list[str]:
    """The words of a name for matching: split at spaces, symbols, digits and lower-to-upper case
    changes, accents removed, case folded (`ZoëCastaño` gives `zoe`, `castano`)."""

    split = "".join(
        f" {c}" if i and c.isupper() and text[i - 1].islower() else c for i, c in enumerate(text)
    )
    plain = "".join(
        c for c in unicodedata.normalize("NFKD", split) if not unicodedata.combining(c)
    ).casefold()
    return [w for w in re.split(r"[^\w]+|_|(?<=\d)(?=\D)|(?<=\D)(?=\d)", plain) if w]


def matches(query: str, *texts: str | None) -> bool:
    """Whether every word of `query` starts a word of one of `texts` (any order)."""

    have = [w for t in texts if t for w in words(t)]
    return all(any(h.startswith(q) for h in have) for q in words(query))


def make_key(name: str, taken: set[str] | frozenset[str] = frozenset()) -> str:
    """A key for `name` that is not in `taken`: `Carla Meier` gives `carla_meier`, then
    `carla_meier_2`."""

    base = "_".join(words(name))[:50].strip("_")
    base = re.sub(r"[^a-z0-9_]", "", base)
    if not base or not base[0].isalpha():
        base = f"person_{base}".rstrip("_")
    key, n = base, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    return key


def check_key(key: str) -> str:
    """`key` if it is a valid person key (lowercase letters, digits, `_`; a letter first)."""

    return _checked(key, "a person key", "carla_meier")


def check_role(role: str) -> str:
    """`role` if it is a valid role name (same form as a key: `finance_recipients`)."""

    return _checked(role, "a role name", "controller")


def _checked(value: str, what: str, example: str) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise PeopleError(
            f"{value!r} is not {what}: use lowercase letters, digits and _ ({example})."
        )
    return value


def stamp(moment: datetime) -> str:
    """`moment` (UTC) as a `checked_at` value: ISO 8601 to the second."""

    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_guid(text: str | None) -> bool:
    """Whether `text` is a GUID such as an Entra object or tenant id."""

    return bool(text) and bool(_GUID.fullmatch(str(text)))
