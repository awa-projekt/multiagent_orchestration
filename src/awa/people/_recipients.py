"""`RoleRecipients`: `role:<name>` recipients, resolved when a message is sent."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from awa._internal.filelock import locked_file
from awa._internal.logging import get_logger
from awa.people._ref import PeopleError, PersonRef, check_role
from awa.people.ports import PeopleDirectory
from awa.runtime.clock import Clock, as_clock

if TYPE_CHECKING:
    from awa.people._file import PeopleFile

ROLE_PREFIX = "role:"
"""How a recipient names a role instead of an address."""
FRESH_SECONDS = 900.0
"""A person checked within this many seconds is not asked for again (15 minutes)."""

logger = get_logger("people")


def role_name(value: str) -> str | None:
    """The role `role:<name>` names, or `None` for anything else (an address).

    Raises:
        PeopleError: If it starts with `role:` but the name is not a valid role name.
    """

    text = str(value).strip()
    if not text.lower().startswith(ROLE_PREFIX):
        return None
    return check_role(text[len(ROLE_PREFIX) :].strip())


def allowed_roles(option: object) -> tuple[str, ...] | None:
    """The roles of a connector's `recipients` option (`role:x` or a list), or `None` when unset.

    Raises:
        PeopleError: If an entry is not `role:<name>`.
    """

    if option is None:
        return None
    if not isinstance(option, str | list | tuple):
        raise PeopleError("recipients takes a role (role:finance_recipients) or a list of roles.")
    values = [option] if isinstance(option, str) else list(option)
    roles: list[str] = []
    for value in values:
        found = role_name(str(value)) if isinstance(value, str) else None
        if found is None:
            raise PeopleError(
                f"recipients takes roles such as role:finance_recipients, not {value!r}."
            )
        roles.append(found)
    if not roles:
        raise PeopleError("recipients names no role; remove the option or name a role.")
    return tuple(dict.fromkeys(roles))


class RoleRecipients:
    """Resolves roles to the people (and addresses) that hold them now.

    People are rechecked against their directory at most every 15 minutes: a changed address is
    saved, a gone or disabled person is dropped, and with nobody left nothing is sent (fail closed).
    The roles are also the exact allow-list of addresses.

    Args:
        directories: The directory per `PersonRef.source` used to recheck people; people of a source
            without one are used as stored.
    """

    def __init__(
        self,
        file: PeopleFile,
        directories: dict[str, PeopleDirectory] | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.file = file
        self.directories = dict(directories or {})
        self._clock = as_clock(clock)

    def _stale(self, person: PersonRef) -> bool:
        if not person.checked_at:
            return True
        try:
            checked = datetime.fromisoformat(person.checked_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        checked = checked if checked.tzinfo else checked.replace(tzinfo=UTC)
        age = (self._clock.now() - checked).total_seconds()
        return age >= FRESH_SECONDS or age < -60  # a check dated in the future is not trusted

    def _refresh(self, people: Sequence[PersonRef]) -> list[PersonRef]:
        fresh: list[PersonRef] = []
        checked: list[PersonRef] = []
        for person in people:
            directory = self.directories.get(person.source)
            if directory is None or not self._stale(person):
                fresh.append(person)
                continue
            try:
                found = directory.get(person)
            except PeopleError as exc:
                raise PeopleError(
                    f"Could not check {person.name} in the directory, so nothing was sent: {exc}"
                ) from None
            if found.mail != person.mail:
                logger.info(
                    "people: the mail address of %s changed; the entry is updated", person.key
                )
            if found.state != person.state:
                logger.info("people: %s is now %s", person.key, found.state)
            checked.append(found)
            fresh.append(found)
        if checked:
            self._save(checked)
        return fresh

    def _save(self, checked: Sequence[PersonRef]) -> None:
        """Store the rechecked people in the current file, so a pin or unpin made meanwhile stays."""

        with locked_file(self.file.path):
            book = self.file.load()
            for person in checked:
                pinned = book.people.get(person.key)
                if pinned is not None and pinned.object_id == person.object_id:
                    book.replace(person)
            self.file.save(book)

    def people(self, role: str) -> list[PersonRef]:
        """The people who hold `role` and may receive messages now (checked, fail closed).

        Raises:
            PeopleError: If no one holds the role, the directory cannot be asked, or every holder is
                deleted or disabled.
        """

        holders = self._refresh(self.file.load().members(role))
        usable = [p for p in holders if p.usable]
        if not usable:
            gone = ", ".join(f"{p.name} ({p.state})" for p in holders) or "nobody"
            raise PeopleError(
                f"Nobody of the role {role!r} can receive messages now: {gone}. Pin someone else "
                f'with awa people pin "<name>" --role {role}, or run awa people check.'
            )
        return usable

    def addresses(self, role: str) -> list[str]:
        """The current mail addresses of the people of `role`.

        Raises:
            PeopleError: As `people`, and when one of them has no mail address.
        """

        people = self.people(role)
        missing = [p.name for p in people if not p.mail]
        if missing:
            raise PeopleError(
                f"{', '.join(missing)} of the role {role!r} has no mail address in the directory."
            )
        return [str(p.mail) for p in people]

    def expand(self, values: Iterable[str], allowed: tuple[str, ...] | None) -> list[str]:
        """`values` with each `role:<name>` replaced by its addresses, without duplicates.

        With `allowed`, only those roles may be named and every address must belong to one of them.

        Raises:
            PeopleError: For a role outside `allowed`, an address outside the allow-list, or any
                problem resolving a role.
        """

        out: list[str] = []
        for value in values:
            role = role_name(value)
            if role is None:
                out.append(str(value).strip())
                continue
            if allowed is not None and role not in allowed:
                raise PeopleError(
                    f"not sent: the role {role!r} is not among the allowed recipients "
                    f"({', '.join('role:' + r for r in allowed)})."
                )
            out.extend(self.addresses(role))
        first: dict[str, str] = {}
        for address in out:
            first.setdefault(address.lower(), address)
        unique = list(first.values())
        if allowed is not None:
            self.require_allowed(unique, allowed)
        return unique

    def require_allowed(self, addresses: Iterable[str], allowed: tuple[str, ...]) -> None:
        """Refuse any of `addresses` that is not the current address of a person in `allowed`.

        Raises:
            PeopleError: Naming how many addresses are outside (not the addresses themselves).
        """

        permitted: set[str] = set()
        problem: PeopleError | None = None
        for role in allowed:
            try:
                permitted.update(a.lower() for a in self.addresses(role) if a.isascii())
            except PeopleError as exc:  # one role without usable people does not block the others
                problem = problem or exc
        if not permitted and problem is not None:
            raise problem
        # Only ASCII addresses compare: a look-alike such as the Kelvin sign lowercases to k.
        outside = [a for a in addresses if not a.isascii() or a.lower() not in permitted]
        if outside:
            raise PeopleError(
                f"not sent: {len(outside)} recipient(s) are not people of "
                f"{', '.join('role:' + r for r in allowed)} (the exact list of allowed recipients)."
            )
