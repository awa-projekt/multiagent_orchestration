"""`config/people.yaml` and the `awa people` commands."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path

import pytest

from _helpers.fakegraph import people as fake_graph_people
from _helpers.fakegraph.server import FakeGraph
from awa.cli import _people as people_cli
from awa.cli import main
from awa.connectors import _engine
from awa.people import (
    EntraPeople,
    PeopleBook,
    PeopleDirectory,
    PeopleError,
    PeopleFile,
    PersonRef,
    ensure_gitignored,
)
from awa.people._file import STORED_FIELDS
from awa.people._ref import make_key, matches, words
from awa.testing.people_contract import ROSTER, write_roster_csv

TID = "6c2e3d1a-5b7f-4c9e-8a10-2f4d6e8b0c13"
OID = "9f0b1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d"


def carla(**changes: object) -> PersonRef:
    data: dict[str, object] = {
        "key": "carla_meier",
        "source": "entra",
        "tenant_id": TID,
        "object_id": OID,
        "name": "Carla Meier",
        "mail": "carla.meier@contoso.example",
        "kind": "member",
        "state": "active",
        "checked_at": "2026-10-07T09:12:00Z",
    }
    return PersonRef(**{**data, **changes})  # type: ignore[arg-type]


def test_the_file_round_trips_and_holds_only_seven_fields(tmp_path: Path) -> None:
    file = PeopleFile.of_project(tmp_path)
    book = PeopleBook()
    book.pin(carla(), ["controller", "finance"])
    file.save(book)
    text = file.path.read_text(encoding="utf-8")
    assert text.startswith("schema: awa.people/v1\n")
    assert f"entra: {TID}/{OID}" in text
    loaded = file.load()
    assert loaded.people == {"carla_meier": carla()}
    assert loaded.assign == {"controller": ["carla_meier"], "finance": ["carla_meier"]}
    assert len(STORED_FIELDS) == 7


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("people: {}\n", "schema: awa.people/v1"),
        ("schema: awa.people/v2\n", "schema: awa.people/v1"),
        ("schema: awa.people/v1\npeople:\n  x: {source: entra, name: X}\n", "two GUIDs"),
        (
            "schema: awa.people/v1\npeople:\n  x: {source: local, name: X, phone: '1'}\n",
            "does not store: phone",
        ),
        (
            "schema: awa.people/v1\npeople:\n  Bad Key: {source: local, name: X}\n",
            "not a person key",
        ),
        (
            "schema: awa.people/v1\npeople:\n  x: {source: local, name: X}\nassign:\n  r: [y]\n",
            "who is not pinned",
        ),
        ("schema: awa.people/v1\nassign:\n  Big Role: []\n", "not a role name"),
        (
            "schema: awa.people/v1\npeople:\n  x: {source: local, name: X, state: gone}\n",
            "not valid",
        ),
    ],
)
def test_a_damaged_file_is_refused_with_the_fix(tmp_path: Path, body: str, problem: str) -> None:
    file = PeopleFile.of_project(tmp_path)
    file.path.parent.mkdir(parents=True)
    file.path.write_text(body, encoding="utf-8")
    with pytest.raises(PeopleError, match=problem):
        file.load()


def test_an_unquoted_timestamp_written_by_hand_is_read(tmp_path: Path) -> None:
    file = PeopleFile.of_project(tmp_path)
    file.path.parent.mkdir(parents=True)
    file.path.write_text(
        "schema: awa.people/v1\npeople:\n  x: {source: local, name: X, checked_at: 2026-10-07T09:12:00Z}\n",
        encoding="utf-8",
    )
    assert file.load().people["x"].checked_at == "2026-10-07T09:12:00Z"


def test_pinning_the_same_person_again_keeps_one_entry() -> None:
    book = PeopleBook()
    first = book.pin(carla(), ["controller"])
    again = book.pin(carla(key="other", name="Carla Meier-Lang"), ["approver"])
    assert first == again == "carla_meier" and book.people[first].name == "Carla Meier-Lang"
    namesake = book.pin(carla(object_id="00000000-0000-0000-0000-00000000000a"))
    assert namesake == "carla_meier_2"


def test_a_local_key_already_taken_is_refused() -> None:
    book = PeopleBook()
    book.pin(carla())
    with pytest.raises(PeopleError, match="already pinned"):
        book.pin(PersonRef(key="carla_meier", source="local", name="Carla M."))


def test_unpin_removes_the_person_from_every_role() -> None:
    book = PeopleBook()
    book.pin(carla(), ["a", "b"])
    book.unpin("carla_meier")
    assert book.people == {} and book.assign == {}


def test_a_recheck_keeps_an_unpin_made_while_the_directory_was_asked(tmp_path: Path) -> None:
    file = PeopleFile.of_project(tmp_path)
    book = PeopleBook()
    book.pin(carla(checked_at=None), ["finance"])
    book.pin(PersonRef(key="dana", source="local", name="Dana"), ["finance"])
    file.save(book)

    class Directory:
        def get(self, person: PersonRef) -> PersonRef:
            meanwhile = file.load()
            meanwhile.unpin("dana")
            file.save(meanwhile)
            return person.model_copy(update={"mail": "carla@contoso.example"})

    from awa.people._recipients import RoleRecipients

    RoleRecipients(file, {"entra": Directory()}).people("finance")  # type: ignore[dict-item]

    saved = file.load()
    assert set(saved.people) == {"carla_meier"}
    assert saved.people["carla_meier"].mail == "carla@contoso.example"


def test_an_entra_person_without_ids_cannot_be_pinned() -> None:
    with pytest.raises(PeopleError, match="no Entra tenant"):
        PeopleBook().pin(carla(object_id=None))


def test_an_empty_book_removes_the_file(tmp_path: Path) -> None:
    file = PeopleFile.of_project(tmp_path)
    book = PeopleBook()
    book.pin(carla())
    file.save(book)
    book.unpin("carla_meier")
    file.save(book)
    assert not file.path.exists()


def test_gitignore_gets_the_line_unless_something_covers_it(tmp_path: Path) -> None:
    assert ensure_gitignored(tmp_path) is True
    assert ensure_gitignored(tmp_path) is False
    other = tmp_path / "other"
    other.mkdir()
    (other / ".gitignore").write_text("config/", encoding="utf-8")
    assert ensure_gitignored(other) is False
    third = tmp_path / "third"
    third.mkdir()
    (third / ".gitignore").write_text(".env", encoding="utf-8")  # no final newline
    ensure_gitignored(third)
    assert (third / ".gitignore").read_text(encoding="utf-8").splitlines()[
        -1
    ] == "config/people.yaml"
    assert ".env" in (third / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_words_and_keys() -> None:
    assert words("Anna-Lena O'Brien") == ["anna", "lena", "o", "brien"]
    assert words("ZoëCastaño") == ["zoe", "castano"]
    assert matches("zo cas", "ZoëCastaño") and not matches("oe", "Zoë")
    assert make_key("Anna-Lena Schmidt") == "anna_lena_schmidt"
    assert make_key("42", {"person_42"}) == "person_42_2"


def test_the_people_file_never_leaves_the_machine_in_an_export(tmp_path: Path) -> None:
    from awa.people import PEOPLE_FILE
    from awa.server._builds import export_files

    for name in (PEOPLE_FILE, "config/other.yaml", "blueprint.yaml", "data/orders.db"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x", encoding="utf-8")

    exported = [path.as_posix() for path in export_files(tmp_path)]

    assert exported == ["blueprint.yaml", "config/other.yaml", "data/orders.db"]


def test_save_refuses_a_link_in_place_of_the_file(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.yaml"
    target.write_text("keep", encoding="utf-8")
    file = PeopleFile.of_project(tmp_path)
    file.path.parent.mkdir(parents=True)
    try:
        file.path.symlink_to(target)
    except OSError:
        pytest.skip("this system does not let the test create a link")
    book = PeopleBook()
    book.pin(carla())
    with pytest.raises(PeopleError, match="link or a folder"):
        file.save(book)
    assert target.read_text(encoding="utf-8") == "keep"


def test_save_leaves_no_temporary_file(tmp_path: Path) -> None:
    file = PeopleFile.of_project(tmp_path)
    book = PeopleBook()
    book.pin(carla())
    file.save(book)
    file.save(book)
    assert [p.name for p in file.path.parent.iterdir()] == ["people.yaml"]


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGraph]:
    monkeypatch.setattr(_engine, "_sleep", lambda seconds: None)
    with FakeGraph() as server:
        server.use(fake_graph_people)
        for person in ROSTER:
            server.store("people").add(person.name, person.mail, guest=person.guest)
        app = server.add_app("people-app", "people-secret", roles=("User.Read.All",))

        def entra(args: argparse.Namespace) -> PeopleDirectory:
            return EntraPeople(env=server.env(app), options=server.connector_options)

        monkeypatch.setattr(people_cli, "_entra", entra)
        yield server


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    return tmp_path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(["people", *argv])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_search_prints_a_numbered_list_and_stores_nothing(
    graph: FakeGraph, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(capsys, "search", "carl", "--project", str(project))
    assert code == 0
    assert "1  Carl Meyer" in out and "2  Carla Meier" in out
    assert "Nothing was stored" in out
    assert not (project / "config").exists()


def test_pin_stores_the_person_with_roles_and_ignores_the_file_in_git(
    graph: FakeGraph, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(capsys, "pin", "carl", "--project", str(project))
    assert code == 1 and "2 people match" in out  # ambiguous: nothing pinned
    assert not PeopleFile.of_project(project).path.exists()
    code, out = run(
        capsys, "pin", "carl", "--pick", "2", "--role", "controller", "--project", str(project)
    )
    assert code == 0 and "Pinned Carla Meier as carla_meier (roles: controller)" in out
    assert "config/people.yaml" in (project / ".gitignore").read_text(encoding="utf-8")
    text = PeopleFile.of_project(project).path.read_text(encoding="utf-8")
    assert "schema: awa.people/v1" in text and "carla.meier@contoso.example" in text
    assert "userPrincipalName" not in text and "#EXT#" not in text  # only id, name and mail
    code, _ = run(capsys, "pin", "meier carla", "--role", "approver", "--project", str(project))
    book = PeopleFile.of_project(project).load()
    assert list(book.people) == ["carla_meier"] and book.roles_of("carla_meier") == [
        "approver",
        "controller",
    ]


def test_pin_by_user_principal_name_and_guests_only_on_request(
    graph: FakeGraph, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    upn = next(
        p for p in graph.store("people").people.values() if p["displayName"] == "Dieter Vogt"
    )["userPrincipalName"]
    code, out = run(capsys, "pin", upn, "--role", "finance", "--project", str(project))
    assert code == 0 and "Dieter Vogt as dieter_vogt" in out
    code, out = run(capsys, "pin", "gail", "--project", str(project))
    assert code == 1 and "Nobody matches" in out
    code, out = run(capsys, "pin", "gail", "--guests", "--project", str(project))
    assert code == 0 and "Gail Gast" in out


def test_an_old_project_gets_the_gitignore_line_once(
    graph: FakeGraph, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "pin", "dieter", "--project", str(tmp_path))
    run(capsys, "pin", "david", "--project", str(tmp_path))
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines.count("config/people.yaml") == 1


def test_list_shows_people_and_roles(
    graph: FakeGraph, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "Nobody is pinned" in run(capsys, "list", "--project", str(project))[1]
    run(capsys, "pin", "dieter", "--role", "finance", "--project", str(project))
    code, out = run(capsys, "list", "--project", str(project))
    assert code == 0 and "dieter_vogt" in out and "finance" in out and "unknown" in out


def test_check_flags_a_deleted_person_and_takes_over_a_new_mail(
    graph: FakeGraph, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "pin", "dieter", "--role", "finance", "--project", str(project))
    run(capsys, "pin", "david", "--role", "finance", "--project", str(project))
    book = PeopleFile.of_project(project).load()
    store = graph.store("people")
    store.delete(str(book.people["dieter_vogt"].object_id))
    store.update(str(book.people["david_li"].object_id), mail="david@new.contoso.example")
    code, out = run(capsys, "check", "--project", str(project))
    assert code == 1
    assert "Dieter Vogt no longer exists in the directory" in out
    assert "mail address changed; updated" in out
    after = PeopleFile.of_project(project).load()
    assert after.people["dieter_vogt"].state == "deleted"
    assert after.people["david_li"].mail == "david@new.contoso.example"


def test_unpin_deletes_the_entry_and_its_roles(
    graph: FakeGraph, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "pin", "dieter", "--role", "finance", "--project", str(project))
    run(capsys, "pin", "david", "--role", "finance", "--project", str(project))
    code, out = run(capsys, "unpin", "dieter_vogt", "--project", str(project))
    assert code == 0 and "Unpinned Dieter Vogt" in out
    book = PeopleFile.of_project(project).load()
    assert list(book.people) == ["david_li"] and book.assign == {"finance": ["david_li"]}
    run(capsys, "unpin", "david_li", "--project", str(project))
    assert not PeopleFile.of_project(project).path.exists()
    code, out = run(capsys, "unpin", "nobody", "--project", str(project))
    assert code == 1 and "Nobody is pinned as 'nobody'" in out


def test_local_people_work_without_entra(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "config").mkdir()
    write_roster_csv(project / "config" / "people.csv", ROSTER)
    code, out = run(capsys, "search", "anna", "--local", "--project", str(project))
    assert code == 0 and "Anna-Lena Schmidt" in out
    code, out = run(capsys, "pin", "anna", "--role", "hr", "--local", "--project", str(project))
    assert code == 0 and "anna_lena_schmidt" in out
    rows = (project / "config" / "people.csv").read_text(encoding="utf-8").splitlines()
    (project / "config" / "people.csv").write_text(
        "\n".join(r for r in rows if "Anna" not in r) + "\n", encoding="utf-8"
    )
    code, out = run(capsys, "check", "--project", str(project))
    assert code == 0 and "add --local" in out  # local people are checked against the list only
    code, out = run(capsys, "check", "--local", "--project", str(project))
    assert code == 1 and "Anna-Lena Schmidt no longer exists" in out


def test_bad_input_is_refused_in_plain_words(
    graph: FakeGraph, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(capsys, "search", "a", "--project", str(project))
    assert code == 1 and "at least 2" in out
    code, out = run(capsys, "pin", "dieter", "--role", "Big Boss", "--project", str(project))
    assert code == 1 and "is not a role name" in out
    code, out = run(capsys, "pin", "carl", "--pick", "9", "--project", str(project))
    assert code == 1 and "--pick must be between 1 and 2" in out


def test_the_global_project_option_names_the_folder_people_are_pinned_in(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "config").mkdir()
    write_roster_csv(project / "config" / "people.csv", ROSTER)
    elsewhere = project / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    code = main(["--project", str(project), "people", "pin", "anna", "--role", "hr", "--local"])

    assert code == 0, capsys.readouterr()
    assert PeopleFile.of_project(project).path.exists()
    assert not (elsewhere / "config").exists()
