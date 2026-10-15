"""Every `PeopleDirectory` runs the same suite: Entra ID (against FakeGraph) and a local CSV list."""

from __future__ import annotations

import csv
from collections.abc import Iterator, Sequence

import pytest

from _helpers.fakegraph import people as fake_graph_people
from _helpers.fakegraph.server import FakeGraph
from awa.connectors import _engine
from awa.people import EntraPeople, LocalPeople, PeopleDirectory, PersonRef
from awa.testing.contracts import Place
from awa.testing.doubles import FakeClock
from awa.testing.people_contract import PeopleDirectoryContract, Someone, write_roster_csv


class TestEntraPeople(PeopleDirectoryContract):
    adapter = EntraPeople

    @pytest.fixture(autouse=True)
    def _graph(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        monkeypatch.setattr(_engine, "_sleep", lambda seconds: None)
        with FakeGraph() as graph:
            graph.use(fake_graph_people)
            self.graph = graph
            yield

    def directory(
        self, place: Place, roster: Sequence[Someone], clock: FakeClock
    ) -> PeopleDirectory:
        store = self.graph.store("people")
        for person in roster:
            store.add(person.name, person.mail, guest=person.guest)
        app = self.graph.add_app("people-app", "people-secret", roles=("User.Read.All",))
        return EntraPeople(
            env=self.graph.env(app), dotenv=None, options=self.graph.connector_options, clock=clock
        )

    def remove(self, place: Place, person: PersonRef) -> None:
        self.graph.store("people").delete(str(person.object_id))

    def change_mail(self, place: Place, person: PersonRef, mail: str) -> None:
        self.graph.store("people").update(str(person.object_id), mail=mail)


class TestLocalPeople(PeopleDirectoryContract):
    adapter = LocalPeople

    def directory(
        self, place: Place, roster: Sequence[Someone], clock: FakeClock
    ) -> PeopleDirectory:
        return LocalPeople(write_roster_csv(place.path / "people.csv", roster), clock=clock)

    def _rows(self, place: Place) -> list[dict[str, str]]:
        with (place.path / "people.csv").open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def _write(self, place: Place, rows: list[dict[str, str]]) -> None:
        with (place.path / "people.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, ["name", "mail", "guest"])
            writer.writeheader()
            writer.writerows(rows)

    def remove(self, place: Place, person: PersonRef) -> None:
        self._write(place, [r for r in self._rows(place) if r["name"] != person.name])

    def change_mail(self, place: Place, person: PersonRef, mail: str) -> None:
        rows = self._rows(place)
        for row in rows:
            if row["name"] == person.name:
                row["mail"] = mail
        self._write(place, rows)
