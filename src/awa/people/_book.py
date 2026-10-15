"""`PeopleBook`: the pinned people of one project and the roles they hold (pure, no files)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from awa.people._ref import PeopleError, PersonRef, check_key, check_role, make_key


@dataclass
class PeopleBook:
    """What `config/people.yaml` holds: pinned people by key, and role -> their keys (`assign`)."""

    people: dict[str, PersonRef] = field(default_factory=dict)
    assign: dict[str, list[str]] = field(default_factory=dict)

    def find(self, person: PersonRef) -> str | None:
        """The key under which the same person (same identity) is pinned, or `None`."""

        return next((k for k, p in self.people.items() if p.identity == person.identity), None)

    def pin(self, person: PersonRef, roles: Iterable[str] = ()) -> str:
        """Pin `person` (or refresh the same person's entry) with `roles`; returns the key.

        Raises:
            PeopleError: If a role name is invalid, or an Entra person has no ids.
        """

        wanted = [check_role(r) for r in roles]
        if person.source == "entra" and not (person.tenant_id and person.object_id):
            raise PeopleError(
                f"{person.name} has no Entra tenant and object id; pin from a search."
            )
        key = self.find(person)
        if key is None and person.source == "local":
            # A local person's key is their identity in the list: it cannot be renamed here.
            key = check_key(person.key)
            if key in self.people:
                raise PeopleError(
                    f"The key {key!r} is already pinned for {self.people[key].name}; unpin it "
                    "first or give the person another key in the local list."
                )
        if key is None:
            free = person.key if person.key and person.key not in self.people else None
            key = check_key(free) if free else make_key(person.name, set(self.people))
        self.people[key] = person.model_copy(update={"key": key})
        for role in wanted:
            members = self.assign.setdefault(role, [])
            if key not in members:
                members.append(key)
        return key

    def unpin(self, key: str) -> PersonRef:
        """Remove the person and every role they hold.

        Raises:
            PeopleError: If nobody is pinned under `key`.
        """

        if key not in self.people:
            raise PeopleError(f"Nobody is pinned as {key!r}; awa people list shows the keys.")
        person = self.people.pop(key)
        for role in list(self.assign):
            self.assign[role] = [k for k in self.assign[role] if k != key]
            if not self.assign[role]:
                del self.assign[role]
        return person

    def roles_of(self, key: str) -> list[str]:
        """The roles `key` holds, sorted."""

        return sorted(role for role, keys in self.assign.items() if key in keys)

    def members(self, role: str) -> list[PersonRef]:
        """The people assigned to `role`, in order (deleted and disabled ones included).

        Raises:
            PeopleError: If no one holds `role` in this project.
        """

        keys = self.assign.get(check_role(role))
        if not keys:
            raise PeopleError(
                f"No one holds the role {role!r} in config/people.yaml. Pin someone with "
                f'awa people pin "<name>" --role {role}.'
            )
        return [self.people[k] for k in keys if k in self.people]

    def replace(self, person: PersonRef) -> None:
        """Store a refreshed copy of a pinned person (same key)."""

        if person.key not in self.people:
            raise PeopleError(f"Nobody is pinned as {person.key!r}.")
        self.people[person.key] = person
