"""`PeopleDirectory`: the protocol every people source implements (Entra ID, a local list, yours)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from awa.people._ref import PersonRef


@runtime_checkable
class PeopleDirectory(Protocol):
    """Finds people by name and tells whether a known person still exists.

    Search results stay in memory; a backend writes nothing anywhere.
    """

    source: str
    """`entra` or `local`: the `PersonRef.source` of what this directory returns."""

    def search(self, text: str, *, limit: int = 10, guests: bool = False) -> list[PersonRef]:
        """People whose name or mail words start with the words of `text`, at most `limit` (max 10).

        Raises:
            PeopleError: If `text` is shorter than 2 or longer than 64 characters, has control
                characters, or the directory cannot be asked.
        """
        ...

    def get(self, ref: PersonRef) -> PersonRef:
        """A fresh copy of `ref`; `state="deleted"` when the directory no longer has the person.

        Raises:
            PeopleError: If the directory cannot be asked (a person who is gone is not an error).
        """
        ...

    def check(self, refs: Sequence[PersonRef]) -> list[PersonRef]:
        """`get` for each of `refs`, in order."""
        ...


__all__ = ["PeopleDirectory"]
