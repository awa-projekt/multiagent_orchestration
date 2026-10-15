"""The Data step: files dropped into the web app are copied safely into the project, analysed with
`awa.data`, explained in plain words, and kept with the user's notes."""

from __future__ import annotations

import asyncio
import io
import json
import os
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

import httpx
from fastapi.testclient import TestClient

from _helpers.data_fixtures import ORDERS_CSV, full_profile
from awa import blueprints
from awa.data import DataProfile, Limits
from awa.errors import ConfigurationError
from awa.ui import Project, create_ui_app
from awa.ui._data_cards import file_card, pending_card

TOKEN = "data-token"
BASE = "http://127.0.0.1:8765"
SAFE = {"Origin": BASE}
CSV = "id;amount\n1;4.250,00\n2;5.980,50\n".encode("cp1252")
PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def events(body: str) -> list[tuple[str, dict[str, Any]]]:
    frames = [dict(line.split(": ", 1) for line in f.splitlines()) for f in body.split("\n\n") if f]
    return [(frame["event"], json.loads(frame["data"])) for frame in frames if "event" in frame]


def zipped(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def make(limits: Limits | None = None) -> TestClient:
        project = Project(tmp_path, environ={}, data_limits=limits)
        app = create_ui_app(project, token=TOKEN, port=8765)
        client = TestClient(app, base_url=BASE).__enter__()
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def put(client: TestClient, name: str, data: bytes, headers: dict[str, str] | None = None) -> Any:
    return client.put(
        "/api/data/files", params={"name": name}, content=data, headers=headers or SAFE
    )


def files_below(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


# ---------------------------------------------------------------------- the happy path


def test_a_dropped_file_is_copied_analysed_and_explained(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()
    original = ORDERS_CSV.read_bytes()

    stored = put(client, "orders.csv", original)
    waiting = client.get("/api/data").json()["files"][0]
    analysed = events(client.post("/api/data/analyse", json={}, headers=SAFE).text)
    state = client.get("/api/data").json()

    assert stored.status_code == 201 and stored.json()["stored"][0]["name"] == "orders.csv"
    assert (tmp_path / "data" / "files" / "orders.csv").read_bytes() == original  # a copy
    assert waiting["status"] == "new" and waiting["headline"] == "Not analyzed yet."
    assert ("progress", {"message": "Reading orders.csv"}) in analysed
    assert analysed[-1][0] == "done" and analysed[-1][1]["analysed"] is True
    card = state["files"][0]
    assert card["status"] in {"ok", "partial"} and card["kind_label"] == "Table (CSV)"
    assert card["headline"].startswith("Table with ") and "rows" in card["headline"]
    columns = {c["name"]: c for c in card["tables"][0]["columns"]}
    assert columns["Betrag_EUR"]["examples"] and "decimal comma" in columns["Betrag_EUR"]["notes"]
    assert card["tables"][0]["sample"]["rows"][0][0] == "B-1001"
    profile = DataProfile.from_json((tmp_path / "data" / "PROFILE.json").read_text("utf-8"))
    assert [f.path for f in profile.files] == ["orders.csv"]


def test_a_changed_file_needs_a_new_analysis(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()
    put(client, "a.csv", CSV)
    client.post("/api/data/analyse", json={}, headers=SAFE)
    (tmp_path / "data" / "files" / "a.csv").write_bytes(CSV + b"3;1,00\n")

    state = client.get("/api/data").json()

    assert state["analysed"] is False and state["files"][0]["status"] == "new"


def test_folders_are_kept_and_a_taken_name_is_never_overwritten(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()

    put(client, "invoices/2024/a.csv", CSV)
    second = put(client, "invoices/2024/a.csv", CSV + b"3;1,00\n")
    backslash = put(client, "scans\\b.csv", CSV)

    names = files_below(tmp_path / "data" / "files")
    assert names == ["invoices/2024/a-2.csv", "invoices/2024/a.csv", "scans/b.csv"]
    assert second.json()["stored"][0]["name"] == "invoices/2024/a-2.csv"
    assert backslash.status_code == 201
    assert (tmp_path / "data" / "files" / "invoices/2024/a.csv").read_bytes() == CSV


def test_a_zip_of_files_is_unpacked_into_a_folder(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()

    done = put(client, "batch.zip", zipped({"one.csv": CSV, "sub/two.csv": CSV}))

    assert done.status_code == 201
    assert files_below(tmp_path / "data" / "files") == ["batch/one.csv", "batch/sub/two.csv"]


# ---------------------------------------------------------------------- hostile names and types


@pytest.mark.parametrize(
    "name",
    [
        "../evil.csv",
        "a/../../evil.csv",
        "..\\..\\evil.csv",
        "/etc/passwd",
        "\\\\server\\share\\x.csv",
        "C:\\Windows\\x.csv",
        "c:x.csv",
        "a/" * 9 + "x.csv",
        "..",
        "./.",
    ],
)
def test_names_that_leave_the_folder_are_refused(
    make_client: Callable[..., TestClient], tmp_path: Path, name: str
) -> None:
    client = make_client()

    response = put(client, name, CSV)

    assert response.status_code == 400 and response.json()["error"]["fix"]
    assert files_below(tmp_path) == []  # nothing anywhere, not even a scratch file
    assert not (tmp_path.parent / "evil.csv").exists()


@pytest.mark.parametrize(
    ("name", "reserved"),
    [("CON.csv", "_CON.csv"), ("a?b*.csv", "a_b_.csv"), (".hidden.csv", "hidden.csv")],
)
def test_awkward_names_are_made_safe(
    make_client: Callable[..., TestClient], tmp_path: Path, name: str, reserved: str
) -> None:
    client = make_client()

    put(client, name, CSV)

    assert files_below(tmp_path / "data" / "files") == [reserved]


@pytest.mark.parametrize(
    ("name", "data", "status", "code"),
    [
        ("program.exe", b"MZ\x90\x00" + b"\x00" * 100, 415, "awa.data.unsupported_type"),
        ("notes.pdf", b"id;amount\n1;2\n", 415, "awa.data.type_mismatch"),
        ("report.csv", PDF, 415, "awa.data.type_mismatch"),
        ("sheet.xlsx", b"not a workbook at all", 415, "awa.data.type_mismatch"),
        ("empty.csv", b"", 400, "awa.data"),
    ],
)
def test_the_content_decides_the_type_not_the_name(
    make_client: Callable[..., TestClient],
    tmp_path: Path,
    name: str,
    data: bytes,
    status: int,
    code: str,
) -> None:
    client = make_client()

    response = put(client, name, data)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code and response.json()["error"]["fix"]
    assert files_below(tmp_path) == []


def test_the_type_sent_by_the_browser_is_not_trusted(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()

    response = put(client, "x.csv", PDF, {**SAFE, "Content-Type": "text/csv"})

    assert response.status_code == 415 and files_below(tmp_path) == []


@pytest.mark.parametrize(
    "entries",
    [
        {"../evil.csv": CSV},
        {"/abs.csv": CSV},
        {"a/../../evil.csv": CSV},
        {"C:/evil.csv": CSV},
        {"inner.zip": zipped({"a.csv": CSV})},
    ],
)
def test_an_unsafe_archive_is_refused_as_a_whole(
    make_client: Callable[..., TestClient], tmp_path: Path, entries: dict[str, bytes]
) -> None:
    client = make_client()

    response = put(client, "bad.zip", zipped({"fine.csv": CSV, **entries}))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "awa.data.zip_unsafe"
    assert files_below(tmp_path) == [] and not (tmp_path.parent / "evil.csv").exists()


def test_an_archive_that_expands_enormously_is_refused(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()
    bomb = zipped({"zeros.csv": b"0," * 5_000_000})

    response = put(client, "bomb.zip", bomb)

    assert response.status_code == 422 and files_below(tmp_path) == []


# ---------------------------------------------------------------------- size limits


def test_a_file_over_the_limit_is_cut_off_while_it_arrives(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client(Limits(max_file_bytes=1_000))

    response = put(client, "big.csv", b"a;b\n" + b"1;2\n" * 500)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "awa.data.too_large"
    assert "1 KB" in response.json()["error"]["message"]
    assert files_below(tmp_path) == []


def test_a_declared_size_far_over_the_limit_is_refused_before_it_is_read(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client(Limits(max_file_bytes=1_000))

    response = put(client, "huge.csv", b"x" * 2_500_000)

    assert response.status_code == 413 and files_below(tmp_path) == []


def test_the_files_together_may_not_exceed_the_total(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client(Limits(max_file_bytes=600, max_total_bytes=1_000))
    row = b"a;b\n" + b"1;2\n" * 149  # 600 bytes: fits alone, not twice in 1,000

    first = put(client, "one.csv", row)
    second = put(client, "two.csv", row)

    assert first.status_code == 201 and second.status_code == 413
    assert files_below(tmp_path / "data" / "files") == ["one.csv"]


def test_an_archive_counts_toward_the_total_with_its_unpacked_size(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client(Limits(max_file_bytes=600, max_total_bytes=1_000))
    row = b"a;b\n" + b"1;2\n" * 149
    put(client, "one.csv", row)

    response = put(client, "z.zip", zipped({"a.csv": row[:300], "b.csv": row[:300]}))

    assert response.status_code == 413
    assert files_below(tmp_path / "data" / "files") == ["one.csv"]


def test_the_number_of_files_is_limited(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client(Limits(max_files=2))

    codes = [put(client, f"f{i}.csv", CSV).status_code for i in range(3)]
    zipped_in = put(client, "z.zip", zipped({"a.csv": CSV, "b.csv": CSV}))

    assert codes == [201, 201, 413] and zipped_in.status_code == 413
    assert files_below(tmp_path / "data" / "files") == ["f0.csv", "f1.csv"]


def test_a_body_without_a_declared_size_is_cut_off_too(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client(Limits(max_file_bytes=1_000))

    def chunks() -> Iterator[bytes]:
        for _ in range(30):
            yield b"x" * 100_000  # no Content-Length: the size is only known by counting

    response = client.put(
        "/api/data/files", params={"name": "stream.csv"}, content=chunks(), headers=SAFE
    )

    assert response.status_code == 413 and files_below(tmp_path) == []


def test_direction_marks_cannot_disguise_a_name(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()

    put(client, "evil\u202efdp.csv", CSV)

    assert files_below(tmp_path / "data" / "files") == ["evilfdp.csv"]


def test_the_page_shows_the_limits(make_client: Callable[..., TestClient]) -> None:
    limits = make_client().get("/api/data").json()["limits"]

    assert limits["file"] == "50.0 MB" and limits["total"] == "500.0 MB" and limits["files"] == 200
    assert {"csv", "xlsx", "pdf", "docx", "mail", "json", "sqlite"} <= set(limits["kinds"])


def test_nothing_is_left_in_the_scratch_folder(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()

    put(client, "ok.csv", CSV)
    put(client, "bad.pdf", CSV)
    put(client, "../x.csv", CSV)

    assert list((tmp_path / ".awa" / "uploads").glob("*")) == []


def test_what_an_interrupted_upload_left_behind_is_swept_at_start(tmp_path: Path) -> None:
    leftover = tmp_path / ".awa" / "uploads" / "abc"
    leftover.mkdir(parents=True)
    (leftover / "half.csv").write_bytes(b"a;b")
    project = Project(tmp_path, environ={})

    project.data.store.sweep()  # type: ignore[attr-defined]

    assert not (tmp_path / ".awa" / "uploads").exists()


async def test_many_uploads_at_once_all_arrive_and_no_name_is_lost(tmp_path: Path) -> None:
    app = create_ui_app(Project(tmp_path, environ={}), token=TOKEN, port=8765)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        answers = await asyncio.gather(
            *(
                client.put(
                    "/api/data/files", params={"name": "same.csv"}, content=CSV, headers=SAFE
                )
                for _ in range(6)
            ),
            *(
                client.put(
                    "/api/data/files", params={"name": f"n{i}.csv"}, content=CSV, headers=SAFE
                )
                for i in range(4)
            ),
        )

    assert [a.status_code for a in answers] == [201] * 10
    names = files_below(tmp_path / "data" / "files")
    assert len(names) == 10 and len(set(names)) == 10
    assert sorted(n for n in names if n.startswith("same")) == sorted(
        ["same.csv"] + [f"same-{i}.csv" for i in range(2, 7)]
    )


# ---------------------------------------------------------------------- removing, notes, masking


def test_a_file_is_removed_with_its_note_and_its_profile_entry(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()
    put(client, "a.csv", CSV)
    put(client, "b.csv", CSV)
    client.post("/api/data/analyse", json={}, headers=SAFE)
    client.post("/api/data/note", json={"name": "a.csv", "text": "Match to invoices"}, headers=SAFE)

    state = client.post("/api/data/remove", json={"name": "a.csv"}, headers=SAFE).json()

    assert [f["name"] for f in state["files"]] == ["b.csv"]
    assert files_below(tmp_path / "data" / "files") == ["b.csv"]
    profile = DataProfile.from_json((tmp_path / "data" / "PROFILE.json").read_text("utf-8"))
    assert [f.path for f in profile.files] == ["b.csv"]
    assert json.loads((tmp_path / ".awa" / "data.json").read_text("utf-8"))["notes"] == {}


def test_the_last_file_removed_leaves_no_profile_and_no_empty_folders(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()
    put(client, "deep/er/a.csv", CSV)
    client.post("/api/data/analyse", json={}, headers=SAFE)

    client.post("/api/data/remove", json={"name": "deep/er/a.csv"}, headers=SAFE)

    assert not (tmp_path / "data" / "PROFILE.json").exists()
    assert not (tmp_path / "data" / "files" / "deep").exists()


@pytest.mark.parametrize("name", ["../../secret.txt", "missing.csv", "/etc/passwd", ".."])
def test_only_stored_files_can_be_removed(
    make_client: Callable[..., TestClient], tmp_path: Path, name: str
) -> None:
    (tmp_path.parent / "secret.txt").write_text("keep")
    client = make_client()
    put(client, "a.csv", CSV)

    response = client.post("/api/data/remove", json={"name": name}, headers=SAFE)

    assert response.status_code == 400
    assert (tmp_path.parent / "secret.txt").read_text() == "keep"
    assert files_below(tmp_path / "data" / "files") == ["a.csv"]


def test_a_planted_link_is_neither_listed_nor_removed(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    outside = tmp_path.parent / "outside-data.csv"
    outside.write_bytes(CSV)
    client = make_client()
    put(client, "a.csv", CSV)
    link = tmp_path / "data" / "files" / "link.csv"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("no right to create links here")

    names = [f["name"] for f in client.get("/api/data").json()["files"]]
    refused = client.post("/api/data/remove", json={"name": "link.csv"}, headers=SAFE)

    assert names == ["a.csv"] and refused.status_code == 400 and outside.exists()


def test_notes_and_the_masking_choice_are_kept_in_the_project(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()
    put(client, "bank.csv", CSV)

    saved = client.post(
        "/api/data/note",
        json={"name": "bank.csv", "text": " Match each line to an invoice. "},
        headers=SAFE,
    )
    masked = client.post("/api/data/mask", json={"mask": "placeholders"}, headers=SAFE)
    state = client.get("/api/data").json()

    assert saved.status_code == 200 and masked.json() == {"mask": "placeholders"}
    assert state["files"][0]["note"] == "Match each line to an invoice."
    assert state["mask"] == "placeholders"
    kept = json.loads((tmp_path / ".awa" / "data.json").read_text("utf-8"))
    assert kept == {
        "format": "awa.ui.data/v1",
        "mask": "placeholders",
        "notes": {"bank.csv": "Match each line to an invoice."},
    }


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/data/note", {"name": "nothere.csv", "text": "x"}),
        ("/api/data/note", {"name": "a.csv", "text": "x" * 1500}),
        ("/api/data/mask", {"mask": "everything"}),
    ],
)
def test_a_bad_note_or_mask_is_explained(
    make_client: Callable[..., TestClient], path: str, body: dict[str, str]
) -> None:
    client = make_client()
    put(client, "a.csv", CSV)

    response = client.post(path, json=body, headers=SAFE)

    assert response.status_code in (400, 422) and "x" * 100 not in response.text


def test_a_settings_file_of_another_version_is_refused_not_overwritten(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()
    put(client, "a.csv", CSV)
    settings = tmp_path / ".awa" / "data.json"
    settings.write_text(json.dumps({"format": "awa.ui.data/v9", "notes": {}}), encoding="utf-8")

    response = client.post("/api/data/mask", json={"mask": "drop"}, headers=SAFE)

    assert response.status_code == 400 and "different awa version" in response.text
    assert "v9" in settings.read_text(encoding="utf-8")


def test_notes_are_keyed_so_a_build_finds_the_file_wherever_it_lies(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client()
    for name in ("a/x/f.csv", "a/y/f.csv", "a/y/g.csv"):
        put(client, name, CSV)
    for name in ("a/x/f.csv", "a/y/g.csv"):
        client.post("/api/data/note", json={"name": name, "text": f"note {name}"}, headers=SAFE)

    request = client.app.state.project.data.request()  # type: ignore[attr-defined]

    assert request.notes == {"*a/x/f.csv": "note a/x/f.csv", "g.csv": "note a/y/g.csv"}


def test_a_build_takes_the_files_the_notes_and_the_mask(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()
    assert client.app.state.project.data.request() is None  # type: ignore[attr-defined]
    put(client, "a.csv", CSV)
    put(client, "gone.csv", CSV)
    client.post("/api/data/note", json={"name": "a.csv", "text": "Check totals"}, headers=SAFE)
    client.post("/api/data/note", json={"name": "gone.csv", "text": "x"}, headers=SAFE)
    client.post("/api/data/remove", json={"name": "gone.csv"}, headers=SAFE)
    client.post("/api/data/mask", json={"mask": "placeholders"}, headers=SAFE)

    request = client.app.state.project.data.request()  # type: ignore[attr-defined]

    assert request.paths == [tmp_path / "data" / "files" / "a.csv"]
    assert request.notes == {"a.csv": "Check totals"} and request.mask == "placeholders"


# ---------------------------------------------------------------------- the Describe step uses the files


def test_describe_hands_the_files_the_notes_and_the_mask_to_the_build(
    make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    class Report:
        passed, pass_rate = True, 1.0

        def summary(self) -> str:
            return "Ready."

    async def build(process: str, *, out_dir: Path, data: Any = None, **options: Any) -> Report:
        seen.update(data=data, notes=options.get("notes"), mask=options.get("mask"))
        return Report()

    monkeypatch.setattr(blueprints, "build_project", build)
    client = make_client()
    put(client, "a.csv", CSV)
    client.post("/api/data/note", json={"name": "a.csv", "text": "Sum it"}, headers=SAFE)
    client.post("/api/data/mask", json={"mask": "placeholders"}, headers=SAFE)

    got = events(client.post("/api/build", json={"process": "Sum the file."}, headers=SAFE).text)

    assert seen == {
        "data": [tmp_path / "data" / "files" / "a.csv"],
        "notes": {"a.csv": "Sum it"},
        "mask": "placeholders",
    }
    assert ("progress", {"message": "Using your 1 data file(s)"}) in got


# ---------------------------------------------------------------------- the access model


ENDPOINTS: list[tuple[str, str, dict[str, Any]]] = [
    ("put", "/api/data/files?name=a.csv", {"content": CSV}),
    ("post", "/api/data/analyse", {"json": {}}),
    ("post", "/api/data/remove", {"json": {"name": "a.csv"}}),
    ("post", "/api/data/note", {"json": {"name": "a.csv", "text": "x"}}),
    ("post", "/api/data/mask", {"json": {"mask": "drop"}}),
]


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 403),
        ({"Origin": "http://attacker.example"}, 403),
        ({"Origin": BASE, "Authorization": "Bearer forged"}, 401),
        ({"Origin": BASE, "Host": "attacker.example"}, 421),
    ],
)
def test_the_data_endpoints_keep_the_token_origin_and_host_checks(
    make_client: Callable[..., TestClient],
    tmp_path: Path,
    method: str,
    path: str,
    body: dict[str, Any],
    headers: dict[str, str],
    status: int,
) -> None:
    client = make_client()

    response = getattr(client, method)(path, headers=headers, **body)

    assert response.status_code == status
    assert files_below(tmp_path) == []


@pytest.mark.parametrize(("method", "path", "body"), [*ENDPOINTS, ("get", "/api/data", {})])
def test_without_the_session_the_data_endpoints_are_closed(
    tmp_path: Path, method: str, path: str, body: dict[str, Any]
) -> None:
    app = create_ui_app(Project(tmp_path, environ={}), token=TOKEN, port=8765)
    with TestClient(app, base_url=BASE) as client:
        response = getattr(client, method)(path, headers=SAFE, **body)

    assert response.status_code == 401 and files_below(tmp_path) == []


def test_other_methods_cannot_reach_the_upload(make_client: Callable[..., TestClient]) -> None:
    client = make_client()

    posted = client.post("/api/data/files?name=a.csv", content=CSV, headers=SAFE)
    got = client.get("/api/data/files?name=a.csv")

    assert posted.status_code == 405 and got.status_code == 405


# ---------------------------------------------------------------------- the plain-words cards


def test_every_kind_of_file_gets_a_sentence_and_what_to_watch() -> None:
    profile = full_profile()

    cards = {card["kind"]: card for card in (file_card(f) for f in profile.files)}

    assert cards["csv"]["headline"] == "Table with 412 rows, 1 column."
    csv_column = cards["csv"]["tables"][0]["columns"][0]
    assert csv_column["type"] == "numbers with decimals"
    assert "decimal comma" in csv_column["notes"]
    assert "personal data: IBANs" in csv_column["notes"] and cards["csv"]["personal"] == ["IBANs"]
    assert cards["csv"]["flags"] == [
        {
            "severity": "warning",
            "code": "DF-W-DECIMAL-COMMA",
            "text": "German number format found (decimal comma, for example 4.250,00): "
            "awa reads it as a German number; check a few values.",
        }
    ]
    assert cards["xlsx"]["headline"] == "Excel workbook with 1 sheet and 0 rows of data."
    assert cards["xlsx"]["tables"][0]["title"] == "Sheet Rechnungen"
    assert cards["pdf"]["headline"] == "PDF document with 1 page; its text can be read."
    assert cards["pdf"]["excerpt"] == "Rechnung 2024-0412"
    assert cards["docx"]["headline"] == "Word document with 12 paragraphs and 1 table."
    assert cards["mail"]["headline"] == "1 email message." and cards["mail"]["attachments"] == [
        "r.pdf"
    ]
    assert cards["json"]["headline"] == "JSON data: a list with 200 records."
    assert cards["sqlite"]["headline"] == "Database file with 1 table."


@pytest.mark.parametrize(
    ("code", "text"),
    [
        ("DF-W-SCANNED", "Looks like a scan"),
        ("DF-W-TOTALS-ROW", "A totals row was found"),
        ("DF-W-INJECTION-SUSPECTED", "instructions aimed at an AI: it is ignored"),
        ("DF-E-ENCRYPTED", "password protected"),
    ],
)
def test_the_important_flags_have_plain_sentences(code: str, text: str) -> None:
    from awa.data import make_flag

    file = full_profile().files[0].model_copy(update={"flags": [make_flag(code, "raw message")]})

    assert text in file_card(file)["flags"][0]["text"]


def test_a_flag_about_one_column_names_the_column() -> None:
    from awa.data import make_flag

    file = full_profile().files[0]
    file = file.model_copy(
        update={"flags": [make_flag("DF-W-TOTALS-ROW", "x", "f1.t1.betrag_eur")]}
    )

    assert file_card(file)["flags"][0]["text"] == (
        "Column Betrag_EUR: A totals row was found: it is not counted as data."
    )


def test_an_unknown_flag_keeps_the_message_the_reader_wrote_with_a_capital() -> None:
    from awa.data import make_flag

    file = (
        full_profile()
        .files[0]
        .model_copy(update={"flags": [make_flag("DF-W-NEW-THING", "the new thing happened")]})
    )

    assert file_card(file)["flags"][0]["text"] == "The new thing happened"


def test_an_unreadable_file_says_so_and_a_new_one_waits() -> None:
    broken = full_profile().files[0].model_copy(update={"status": "error", "tables": []})

    assert file_card(broken)["headline"] == "awa could not read this file."
    assert pending_card("x.pdf", 1_500_000, "pdf")["size_text"] == "1.5 MB"


def test_a_note_travels_with_the_card() -> None:
    assert file_card(full_profile().files[0], "Pay by Friday")["note"] == "Pay by Friday"


def test_a_settings_file_that_is_not_json_is_explained(tmp_path: Path) -> None:
    (tmp_path / ".awa").mkdir()
    (tmp_path / ".awa" / "data.json").write_text("{not json", encoding="utf-8")
    project = Project(tmp_path, environ={})

    with pytest.raises(ConfigurationError, match="not valid JSON"):
        project.data.state()
    assert os.path.exists(tmp_path / ".awa" / "data.json")
