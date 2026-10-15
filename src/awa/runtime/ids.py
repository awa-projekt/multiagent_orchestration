"""Identifiers: the id port (`IdSource`) and the frozen ids of one run (`RunIds`).

Code that creates ids takes an `IdSource` and defaults to `UuidIds`; a test passes
`awa.testing.FixedIds`. A nested run gets new `RunIds` derived from its parent's.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Protocol

from awa._internal.ids import new_id

DEFAULT_TENANT = "local"
"""The tenant of runs that name none."""

SESSION_PREFIX = "ses_"
"""Prefix of the session ids awa generates."""

MAX_SESSION_ID_CHARS = 200
"""Longest session id a caller may supply."""


class IdSource(Protocol):
    """Makes identifiers of the form `"<prefix>_<hex>"`."""

    def new_id(self, prefix: str) -> str:
        """Return a new identifier for an object of the kind `prefix` (for example `"evt"`)."""
        ...


class UuidIds:
    """Random identifiers: `awa.ext.new_id`."""

    def new_id(self, prefix: str) -> str:
        """Return a new random identifier."""

        return new_id(prefix)


class Uuid7Ids:
    """Time-ordered identifiers `"<prefix>_<UUIDv7 hex>"` that sort in creation order, also as text."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ms = -1
        self._counter = 0

    def new_id(self, prefix: str) -> str:
        """Return a new time-ordered identifier."""

        return f"{prefix}_{self.new_hex()}"

    def new_hex(self) -> str:
        """Return a new time-ordered UUIDv7 as 32 hex digits."""

        with self._lock:
            now = time.time_ns() // 1_000_000
            if now <= self._last_ms:
                self._counter += 1
                if self._counter >= 1 << 12:  # the counter ran out: borrow the next millisecond
                    self._last_ms += 1
                    self._counter = 0
            else:
                self._last_ms, self._counter = now, 0
            unix_ms, counter = self._last_ms, self._counter
        random_bits = int.from_bytes(os.urandom(8), "big") >> 2
        value = (unix_ms << 80) | (0x7 << 76) | (counter << 64) | (0b10 << 62) | random_bits
        return f"{value:032x}"


_UUID7 = Uuid7Ids()


def uuid7() -> uuid.UUID:
    """A time-ordered UUID (version 7, RFC 9562): later calls sort after earlier ones."""

    return uuid.UUID(_UUID7.new_hex())


def new_session_id() -> str:
    """A new session id: `"ses_"` and 32 hex characters."""

    return f"{SESSION_PREFIX}{uuid.uuid4().hex}"


def new_agent_call_id() -> str:
    """A new id for one call of an agent by another: `"call_"` and a time-ordered UUID."""

    return f"call_{uuid7().hex}"


def check_session_id(value: str) -> str:
    """`value` if it is non-empty, at most 200 characters and printable US-ASCII."""

    if not value or len(value) > MAX_SESSION_ID_CHARS:
        raise ValueError(
            f"A session id must have 1 to {MAX_SESSION_ID_CHARS} characters, got {len(value)}"
        )
    if not all(" " < char <= "~" for char in value):
        raise ValueError(
            "A session id may contain printable US-ASCII characters only (no spaces); "
            f"got {value[:40]!r}"
        )
    return value


def brief_hash(text: str) -> str:
    """A short digest of what an agent was asked, used to recognize a repeated call."""

    normalized = " ".join(text.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class AgentKey:
    """One link of a run's call chain: which agent was called, where, and with what.

    Attributes:
        scope: The team or network of the call; `""` for an agent called as a tool.
        brief_hash: `brief_hash` of the brief or task the agent received; `""` when unknown.
    """

    agent: str
    scope: str = ""
    brief_hash: str = ""

    def label(self) -> str:
        """`scope.agent`, or just `agent` outside a team."""

        return f"{self.scope}.{self.agent}" if self.scope else self.agent


@dataclass(frozen=True, slots=True)
class RunIds:
    """The frozen identifiers of one run.

    Attributes:
        tenant: Who the run tree works for (`awa.tenant`, else `"local"`); fixed at the top level.
        actor: Who started the run tree (`awa.actor`), if known; fixed like `tenant`.
        session_id: The conversation: the caller's `awa.session_id`, else the top-level thread,
            else inherited, else new; shared by every nested run.
        agent_call_id: Id of the call that started this run; `None` for a top-level run.
        tool_call_id: The provider's id of the tool call that started this run; kept apart from
            `agent_call_id` because providers reuse such ids.
        chain: The agent calls from the top-level run down to this one.
    """

    tenant: str = DEFAULT_TENANT
    actor: str | None = None
    session_id: str = ""
    agent_call_id: str | None = None
    tool_call_id: str | None = None
    chain: tuple[AgentKey, ...] = field(default=())

    @property
    def branch(self) -> str:
        """The chain rendered for display, for example `"desk.lead/desk.writer"` (never parse it)."""

        return "/".join(key.label() for key in self.chain)

    def for_child(
        self,
        link: AgentKey,
        *,
        agent_call_id: str | None = None,
        tool_call_id: str | None = None,
        session_id: str | None = None,
    ) -> RunIds:
        """The ids of a run nested in this one: same tenant, actor and session, one link more."""

        return replace(
            self,
            session_id=session_id or self.session_id,
            agent_call_id=agent_call_id or new_agent_call_id(),
            tool_call_id=tool_call_id,
            chain=(*self.chain, link),
        )


def actor_id(value: object) -> str | None:
    """The actor id in an `awa.actor` value: the string, or the `id` of a mapping or object."""

    if value is None or value == "":
        return None
    if isinstance(value, str | int):
        return str(value)
    found = value.get("id") if isinstance(value, dict) else getattr(value, "id", None)
    return None if found is None else str(found)


__all__ = [
    "DEFAULT_TENANT",
    "AgentKey",
    "IdSource",
    "RunIds",
    "Uuid7Ids",
    "UuidIds",
    "actor_id",
    "brief_hash",
    "check_session_id",
    "new_agent_call_id",
    "new_session_id",
    "uuid7",
]
