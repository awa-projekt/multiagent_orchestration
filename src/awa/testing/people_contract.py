"""The contract of a `PeopleDirectory`: what every people source must do the same way.

A subclass fills a directory with `ROSTER` and can remove a person or change their mail address.

```python
class TestLocalPeople(PeopleDirectoryContract):
    adapter = LocalPeople

    def directory(self, place, roster, clock):
        return LocalPeople(write_roster_csv(place.path / "people.csv", roster), clock=clock)
```
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from awa.people import MAX_RESULTS, PeopleDirectory, PeopleError, PersonRef
from awa.testing.contracts._base import AdapterContract, Place
from awa.testing.doubles import FakeClock


@dataclass(frozen=True)
class Someone:
    """One person of the test roster (invented names, `example` domains)."""

    name: str
    mail: str | None
    guest: bool = False


ROSTER: tuple[Someone, ...] = (
    Someone("Carla Meier", "carla.meier@contoso.example"),
    Someone("Carl Meyer", "carl.meyer@contoso.example"),
    Someone("Dieter Vogt", "dieter.vogt@contoso.example"),
    Someone("David Li", "david.li@contoso.example"),
    Someone("Anna-Lena Schmidt", "anna-lena.schmidt@contoso.example"),
    Someone("Gail Gast", "gail.gast@fabrikam.example", guest=True),
)
"""Who every directory under test holds."""


def write_roster_csv(path: Path, roster: Sequence[Someone]) -> Path:
    """`roster` as a CSV file `LocalPeople` reads (`name`, `mail`, `guest`)."""

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "mail", "guest"])
        for person in roster:
            writer.writerow([person.name, person.mail or "", "yes" if person.guest else ""])
    return path


def _names(found: Sequence[PersonRef]) -> list[str]:
    return sorted(p.name for p in found)


class PeopleDirectoryContract(AdapterContract):
    """Run this against a `PeopleDirectory`: implement `directory`, `remove` and `change_mail`."""

    def directory(
        self, place: Place, roster: Sequence[Someone], clock: FakeClock
    ) -> PeopleDirectory:
        """A directory holding `roster`."""

        raise NotImplementedError

    def remove(self, place: Place, person: PersonRef) -> None:
        """Delete `person` from the directory made on `place`."""

        raise NotImplementedError

    def change_mail(self, place: Place, person: PersonRef, mail: str) -> None:
        """Give `person` a new mail address in the directory made on `place`."""

        raise NotImplementedError

    @pytest.fixture
    def people(self, place: Place, clock: FakeClock) -> PeopleDirectory:
        return self.directory(place, ROSTER, clock)

    def test_a_word_beginning_finds_everyone_whose_name_starts_so(
        self, people: PeopleDirectory
    ) -> None:
        assert _names(people.search("carl")) == ["Carl Meyer", "Carla Meier"]
        assert _names(people.search("dieter")) == ["Dieter Vogt"]

    def test_words_match_in_any_order_and_any_case(self, people: PeopleDirectory) -> None:
        assert _names(people.search("MEIER carla")) == ["Carla Meier"]

    def test_a_short_family_name_is_a_word_of_its_own(self, people: PeopleDirectory) -> None:
        assert _names(people.search("li")) == ["David Li"]

    def test_the_middle_of_a_word_does_not_match(self, people: PeopleDirectory) -> None:
        assert people.search("arla") == []

    def test_a_mail_address_beginning_finds_the_person(self, people: PeopleDirectory) -> None:
        assert _names(people.search("dieter.vogt")) == ["Dieter Vogt"]

    def test_guests_are_hidden_unless_asked_for(self, people: PeopleDirectory) -> None:
        assert people.search("gail") == []
        found = people.search("gail", guests=True)
        assert _names(found) == ["Gail Gast"] and found[0].kind == "guest"

    def test_members_are_marked_as_members(self, people: PeopleDirectory) -> None:
        assert [p.kind for p in people.search("dieter")] == ["member"]

    def test_results_are_capped_by_the_limit_and_by_ten(self, people: PeopleDirectory) -> None:
        assert len(people.search("carl", limit=1)) == 1
        assert len(people.search("carl", limit=50)) <= MAX_RESULTS

    @pytest.mark.parametrize(
        "text", ["a", " b ", "x" * 65, "carla\x00", "carla\nmeier", "**", "-. "]
    )
    def test_too_short_too_long_or_control_text_is_refused(
        self, people: PeopleDirectory, text: str
    ) -> None:
        with pytest.raises(PeopleError):
            people.search(text)

    @pytest.mark.parametrize(
        "text", ['carla" OR "mail:', 'x\\" OR "displayName:', "carla' or '1'='1", "c&d"]
    )
    def test_query_syntax_in_the_text_is_plain_text(
        self, people: PeopleDirectory, text: str
    ) -> None:
        """Quotes, backslashes and operators are searched for, never obeyed: nobody shows."""

        assert people.search(text) == []

    def test_a_result_says_where_it_comes_from_and_when_it_was_checked(
        self, people: PeopleDirectory, clock: FakeClock
    ) -> None:
        (carla,) = people.search("carla")
        assert carla.source == people.source
        assert carla.state in ("active", "unknown")  # a search need not read the account state
        assert carla.mail == "carla.meier@contoso.example"
        assert carla.checked_at == clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        (again,) = people.search("meier carla")
        assert again.identity == carla.identity

    def test_get_returns_the_person_as_the_directory_has_them(
        self, people: PeopleDirectory
    ) -> None:
        (dieter,) = people.search("dieter")
        fresh = people.get(dieter)
        assert fresh.identity == dieter.identity and fresh.key == dieter.key
        assert fresh.state == "active" and fresh.name == "Dieter Vogt"

    def test_a_removed_person_comes_back_as_deleted(
        self, people: PeopleDirectory, place: Place
    ) -> None:
        (dieter,) = people.search("dieter")
        self.remove(place, dieter)
        gone = people.get(dieter)
        assert gone.state == "deleted" and not gone.usable
        assert gone.identity == dieter.identity

    def test_a_changed_mail_address_shows_in_get(
        self, people: PeopleDirectory, place: Place
    ) -> None:
        (david,) = people.search("david")
        self.change_mail(place, david, "david.li@new.contoso.example")
        assert people.get(david).mail == "david.li@new.contoso.example"

    def test_check_answers_for_each_person_in_order(
        self, people: PeopleDirectory, place: Place
    ) -> None:
        carla, dieter = people.search("carla")[0], people.search("dieter")[0]
        self.remove(place, carla)
        checked = people.check([carla, dieter])
        assert [p.state for p in checked] == ["deleted", "active"]
        assert [p.identity for p in checked] == [carla.identity, dieter.identity]


__all__ = ["ROSTER", "PeopleDirectoryContract", "Someone", "write_roster_csv"]
