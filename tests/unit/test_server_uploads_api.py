"""`POST /v1/uploads`, `GET`/`DELETE /v1/uploads/{id}` and `POST /v1/data/profile`: behavior and security."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from _helpers.data_http import CSV, KEYS, auth, client_for, csv_file, make_app, upload
from awa.server import RateLimit

REPO = Path(__file__).resolve().parents[2]
INBOX = REPO / "examples" / "benchmarks" / "core" / "04_document_intake" / "data" / "inbox"
H = auth("k-alice")


def error_of(response: httpx.Response) -> dict[str, Any]:
    body = response.json()["error"]
    assert set(body) >= {"type", "code", "message", "retryable", "request_id"}
    return dict(body)


def files_below(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# ---------------------------------------------------------------------- uploading


async def test_an_upload_is_accepted_described_and_kept_under_the_tenant(tmp_path: Path) -> None:
    async with client_for(make_app(tmp_path)) as client:
        response = await client.post(
            "/v1/uploads",
            headers=H,
            files=[
                ("files", ("orders.csv", CSV, "application/x-whatever")),
                ("files", ("notes.txt", b"hello", "text/plain")),
            ],
        )

        assert response.status_code == 201
        body = response.json()
        assert response.headers["location"] == f"/v1/uploads/{body['upload_id']}"
        assert [
            (f["id"], f["name"], f["kind"], f["sniffed_type"], f["status"]) for f in body["files"]
        ] == [
            ("f1", "notes.txt", "text", "text/plain", "ok"),
            ("f2", "orders.csv", "csv", "text/csv", "ok"),  # the client's content type is ignored
        ]
        assert body["total_bytes"] == 5 + len(CSV)
        assert body["files"][1]["sha256"] and body["expires_at"] > body["created_at"]
        assert (await client.get(f"/v1/uploads/{body['upload_id']}", headers=H)).json() == body
        assert any(path.endswith("files/orders.csv") for path in files_below(tmp_path))


async def test_an_app_may_serve_only_the_data_endpoints_but_not_nothing_at_all() -> None:
    from awa.server import ApiKeyAuthenticator, DataOptions, create_app

    only_data = create_app(
        [], authenticator=ApiKeyAuthenticator({"k": KEYS["k-alice"]}), data=DataOptions()
    )
    async with client_for(only_data) as client:
        listed = await client.get("/v1/systems", headers=auth("k"))
        uploaded = await client.post("/v1/uploads", headers=auth("k"), files=csv_file())

    assert listed.json() == {"systems": []} and uploaded.status_code == 201
    with pytest.raises(ValueError, match="at least one system"):
        create_app([])


async def test_the_file_ids_of_an_upload_are_the_ids_of_its_profile() -> None:
    async with client_for(make_app()) as client:
        response = await client.post(
            "/v1/uploads", headers=H, files=csv_file("b.csv") + csv_file("a.csv", b"x;y\n1;2\n")
        )
        uploaded = response.json()
        profiled = await client.post(
            "/v1/data/profile", headers=H, json={"upload_id": uploaded["upload_id"]}
        )

        assert [(f["id"], f["name"]) for f in uploaded["files"]] == [
            ("f1", "a.csv"),
            ("f2", "b.csv"),
        ]
        assert [(f["id"], f["path"]) for f in profiled.json()["profile"]["files"]] == [
            ("f1", "a.csv"),
            ("f2", "b.csv"),
        ]


async def test_an_upload_can_be_deleted_and_is_then_gone(tmp_path: Path) -> None:
    async with client_for(make_app(tmp_path)) as client:
        upload_id = await upload(client)

        deleted = await client.delete(f"/v1/uploads/{upload_id}", headers=H)

        assert deleted.json() == {"upload_id": upload_id, "deleted": True}
        gone = await client.get(f"/v1/uploads/{upload_id}", headers=H)
        assert gone.status_code == 404 and error_of(gone)["code"] == "awa.data.upload_not_found"
        assert files_below(tmp_path) == []


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("../../etc/passwd.csv", "passwd.csv"),
        ("..\\..\\windows\\system32\\evil.csv", "evil.csv"),
        ("/abs/path/data.csv", "data.csv"),
        ("C:\\work\\x\\data.csv", "data.csv"),
        ("CON.txt", "_CON.txt"),
        (".hidden.csv", "hidden.csv"),
        ("a\x00b.csv", "a%00b.csv"),  # the client library escapes the NUL itself
        ("report: 2026?.csv", "report_ 2026_.csv"),
    ],
)
async def test_a_file_name_the_client_sends_never_becomes_a_path(
    tmp_path: Path, sent: str, stored: str
) -> None:
    async with client_for(make_app(tmp_path)) as client:
        response = await client.post("/v1/uploads", headers=H, files=csv_file(sent))

        assert response.status_code == 201
        assert [f["name"] for f in response.json()["files"]] == [stored]
        inside = [p for p in files_below(tmp_path) if "/files/" in p]
        assert inside and all(p.split("/files/")[1] == stored for p in inside)


async def test_a_name_with_umlauts_is_kept_in_its_normalised_form() -> None:
    decomposed = "Rechnung_März.csv"  # "a" followed by a combining diaeresis
    async with client_for(make_app()) as client:
        response = await client.post("/v1/uploads", headers=H, files=csv_file(decomposed))

        assert response.status_code == 201
        assert [f["name"] for f in response.json()["files"]] == ["Rechnung_März.csv"]


async def test_equal_names_in_one_upload_are_kept_apart() -> None:
    async with client_for(make_app()) as client:
        response = await client.post(
            "/v1/uploads",
            headers=H,
            files=csv_file("a.csv") + csv_file("A.CSV") + csv_file("a.csv"),
        )

        assert [f["name"] for f in response.json()["files"]] == [
            "a-3.csv",
            "A-2.CSV",
            "a.csv",
        ] or sorted(f["name"] for f in response.json()["files"]) == ["A-2.CSV", "a-3.csv", "a.csv"]


async def test_two_files_with_a_long_equal_name_are_both_kept() -> None:
    long = "r" * 96 + ".csv"  # 100 characters: the longest safe name
    async with client_for(make_app()) as client:
        response = await client.post(
            "/v1/uploads", headers=H, files=csv_file(long) + csv_file(long)
        )

        assert response.status_code == 201
        names = sorted(f["name"] for f in response.json()["files"])
        assert names[1] == long and names[0].endswith("-2.csv") and len(names[0]) == 100


async def test_a_zip_is_unpacked_and_zip_slip_is_refused(tmp_path: Path) -> None:
    good, evil, links = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with zipfile.ZipFile(good, "w") as archive:
        archive.writestr("march/orders.csv", CSV)
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../../outside.csv", CSV)
    with zipfile.ZipFile(links, "w") as archive:
        info = zipfile.ZipInfo("link.csv")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "/etc/passwd")
    async with client_for(make_app(tmp_path)) as client:
        ok = await client.post(
            "/v1/uploads", headers=H, files=csv_file("data.zip", good.getvalue())
        )
        refused = [
            await client.post(
                "/v1/uploads", headers=H, files=csv_file("evil.zip", evil.getvalue())
            ),
            await client.post(
                "/v1/uploads", headers=H, files=csv_file("links.zip", links.getvalue())
            ),
        ]

        assert [f["name"] for f in ok.json()["files"]] == ["data/march/orders.csv"]
        assert [r.status_code for r in refused] == [422, 422]
        assert {error_of(r)["code"] for r in refused} == {"awa.data.zip_unsafe"}
        assert not any("outside" in path for path in files_below(tmp_path))


async def test_a_zip_bomb_is_refused_before_it_is_unpacked() -> None:
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("zeros.csv", b"0" * 5_000_000)
    async with client_for(make_app(options={"max_file_bytes": 4_000_000})) as client:
        response = await client.post(
            "/v1/uploads", headers=H, files=csv_file("bomb.zip", bomb.getvalue())
        )

        assert response.status_code in (413, 422)
        assert error_of(response)["code"] in ("awa.data.zip_unsafe", "awa.data.too_large")


async def test_the_type_is_decided_by_content_not_by_name_or_header() -> None:
    async with client_for(make_app()) as client:
        lie = await client.post("/v1/uploads", headers=H, files=csv_file("scan.pdf", b"plain text"))
        png = await client.post(
            "/v1/uploads", headers=H, files=csv_file("photo.png", b"\x89PNG\r\n\x1a\nxxxx")
        )
        exe = await client.post(
            "/v1/uploads", headers=H, files=csv_file("tool.exe", b"MZ\x90\x00\x03")
        )

        assert (lie.status_code, error_of(lie)["code"]) == (415, "awa.data.type_mismatch")
        assert (png.status_code, error_of(png)["code"]) == (415, "awa.data.unsupported_type")
        assert (exe.status_code, error_of(exe)["code"]) == (415, "awa.data.unsupported_type")


async def test_the_real_inbox_files_of_the_document_intake_example_are_accepted() -> None:
    names = sorted(p.name for p in INBOX.iterdir())
    async with client_for(make_app()) as client:
        response = await client.post(
            "/v1/uploads",
            headers=H,
            files=[
                ("files", (name, (INBOX / name).read_bytes(), "application/octet-stream"))
                for name in names
            ],
        )

        assert response.status_code == 201, response.text
        kinds = {f["name"]: f["kind"] for f in response.json()["files"]}
        assert kinds == {
            "2024-0412_mueller.pdf": "pdf",
            "88231_zuercher.docx": "docx",
            "ln-2024-17_meyer.xlsx": "xlsx",
            "scan_0001.pdf": "pdf",
        }


# ---------------------------------------------------------------------- what the request must look like


@pytest.mark.parametrize(
    ("headers", "content"),
    [
        ({"content-type": "application/json"}, b'{"files": []}'),
        ({"content-type": "text/plain"}, b"hello"),
        ({"content-type": "application/octet-stream"}, CSV),
        ({"content-type": "multipart/form-data"}, b"no boundary"),
        ({"content-type": "multipart/mixed; boundary=x"}, b"--x--"),
    ],
)
async def test_other_content_types_than_multipart_form_data_are_refused(
    headers: dict[str, str], content: bytes
) -> None:
    async with client_for(make_app()) as client:
        response = await client.post("/v1/uploads", headers={**H, **headers}, content=content)

        assert response.status_code == 415
        assert error_of(response)["code"] == "awa.data.unsupported_type"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"--B--\r\n",
        b'--B\r\nContent-Disposition: form-data; name="note"\r\n\r\njust a field\r\n--B--\r\n',
        b'--B\r\nContent-Disposition: form-data; name="files"; filename="a.csv"\r\n\r\nid;x\r\n',  # cut off
        b"garbage without any boundary at all",
    ],
)
async def test_an_empty_truncated_or_malformed_body_is_a_clean_400(body: bytes) -> None:
    async with client_for(make_app()) as client:
        response = await client.post(
            "/v1/uploads",
            headers={**H, "content-type": "multipart/form-data; boundary=B"},
            content=body,
        )

        assert response.status_code == 400
        assert error_of(response)["type"] == "InvalidRequestError"


@pytest.mark.parametrize("field", ["file", "upload", "data", ""])
async def test_files_are_taken_only_from_the_field_named_files(tmp_path: Path, field: str) -> None:
    """A client that names its file field anything else gets the fix, and nothing is kept."""

    async with client_for(make_app(tmp_path)) as client:
        response = await client.post(
            "/v1/uploads", headers=H, files=[(field, ("orders.csv", CSV, "text/csv"))]
        )

        assert response.status_code == 400
        error = error_of(response)
        assert error["type"] == "InvalidRequestError"
        assert "form field named `files`" in error["message"] and f"{field!r}" in error["message"]
    assert files_below(tmp_path) == []


async def test_a_plain_form_field_next_to_the_files_is_still_ignored(tmp_path: Path) -> None:
    async with client_for(make_app(tmp_path)) as client:
        response = await client.post(
            "/v1/uploads", headers=H, data={"note": "march"}, files=csv_file()
        )

        assert response.status_code in (200, 201)
        assert [f["name"] for f in response.json()["files"]] == ["orders.csv"]


async def test_the_data_endpoints_need_a_known_caller_and_the_builder_role() -> None:
    async with client_for(make_app(options={"roles": {"builder"}})) as client:
        anonymous = await client.post("/v1/uploads", files=csv_file())
        wrong = await client.post("/v1/uploads", headers=auth("nope"), files=csv_file())
        viewer = await client.post("/v1/uploads", headers=auth("k-vera"), files=csv_file())
        builder = await client.post("/v1/uploads", headers=H, files=csv_file())

        assert (anonymous.status_code, wrong.status_code) == (401, 401)
        assert viewer.status_code == 403 and "builder" in error_of(viewer)["message"]
        assert builder.status_code == 201
        for path in ("/v1/uploads/up_1", "/v1/builds", "/v1/builds/bld_1", "/v1/data/profile"):
            assert (await client.request("GET", path)).status_code in (401, 405)


# ---------------------------------------------------------------------- limits, enforced while streaming


async def chunks(
    total: int, size: int = 64 * 1024, *, counter: list[int] | None = None
) -> AsyncIterator[bytes]:
    """A body that yields `total` bytes of a CSV in pieces and counts how many pieces were taken."""

    head = (
        b'--B\r\nContent-Disposition: form-data; name="files"; filename="big.csv"\r\n\r\nid;x\r\n'
    )
    yield head
    sent = 0
    while sent < total:
        piece = (b"1;2\r\n" * (size // 5))[:size]
        sent += len(piece)
        if counter is not None:
            counter.append(1)
        yield piece
    yield b"\r\n--B--\r\n"


async def test_a_file_over_the_limit_is_cut_off_while_it_streams(tmp_path: Path) -> None:
    taken: list[int] = []
    async with client_for(make_app(tmp_path, options={"max_file_bytes": 256 * 1024})) as client:
        response = await client.post(
            "/v1/uploads",
            headers={**H, "content-type": "multipart/form-data; boundary=B"},
            content=chunks(50 * 1024 * 1024, counter=taken),
        )

        assert response.status_code == 413
        assert error_of(response)["code"] == "awa.data.too_large"
        assert len(taken) < 12  # not the 800 chunks of the whole body: reading stopped at the limit
        assert files_below(tmp_path) == []


async def test_a_request_over_the_limit_is_refused_by_its_declared_length_at_once() -> None:
    async with client_for(
        make_app(options={"max_request_bytes": 1024 * 1024, "max_file_bytes": 512 * 1024})
    ) as client:
        response = await client.post(
            "/v1/uploads",
            headers={
                **H,
                "content-type": "multipart/form-data; boundary=B",
                "content-length": str(50 * 1024 * 1024),
            },
            content=b"--B--",
        )

        assert response.status_code == 413


async def test_uploads_may_exceed_the_small_body_limit_of_the_json_endpoints_but_nothing_else_may() -> (
    None
):
    big = b"id;x\n" + b"1;2\n" * 300_000  # about 1.2 MB, above the default 1 MiB body limit
    async with client_for(make_app()) as client:
        accepted = await client.post("/v1/uploads", headers=H, files=csv_file("big.csv", big))
        refused = await client.post(
            "/v1/builds", headers=H, json={"description": "x" * 2_000_000, "upload_id": "up_1"}
        )

        assert accepted.status_code == 201
        assert refused.status_code == 413
        assert error_of(refused)["code"] == "awa.payload_too_large"


async def test_more_files_than_allowed_are_refused() -> None:
    async with client_for(make_app(options={"max_files": 3})) as client:
        response = await client.post(
            "/v1/uploads", headers=H, files=[csv_file(f"{n}.csv")[0] for n in range(4)]
        )

        assert response.status_code == 413
        assert error_of(response)["code"] == "awa.data.too_many_files"


async def test_a_body_of_nothing_but_empty_parts_is_refused_early() -> None:
    part = b'--B\r\nContent-Disposition: form-data; name="x"\r\n\r\n\r\n'
    async with client_for(make_app(options={"max_files": 3})) as client:
        response = await client.post(
            "/v1/uploads",
            headers={**H, "content-type": "multipart/form-data; boundary=B"},
            content=part * 500 + b"--B--\r\n",
        )

        assert response.status_code == 413
        assert error_of(response)["code"] == "awa.data.too_many_files"


async def test_the_storage_quota_of_a_tenant_is_enforced_per_chunk_and_per_tenant(
    tmp_path: Path,
) -> None:
    async with client_for(make_app(tmp_path, options={"max_tenant_bytes": 100})) as client:
        first = await client.post("/v1/uploads", headers=H, files=csv_file("a.csv", b"id;x\n" * 12))
        second = await client.post(
            "/v1/uploads", headers=H, files=csv_file("b.csv", b"id;x\n" * 12)
        )
        other_tenant = await client.post(
            "/v1/uploads", headers=auth("k-gus"), files=csv_file("a.csv", b"id;x\n" * 12)
        )

        assert first.status_code == 201
        assert second.status_code == 429
        assert error_of(second)["code"] == "awa.data.quota_exceeded"
        assert int(second.headers["retry-after"]) >= 1
        assert other_tenant.status_code == 201
        assert len([p for p in files_below(tmp_path) if p.endswith("upload.json")]) == 2


async def test_a_client_that_stalls_is_stopped_and_leaves_nothing_behind(tmp_path: Path) -> None:
    async def slow() -> AsyncIterator[bytes]:
        yield b'--B\r\nContent-Disposition: form-data; name="files"; filename="a.csv"\r\n\r\nid;x\r\n'
        await asyncio.sleep(5)
        yield b"\r\n--B--\r\n"  # pragma: no cover - the server gives up first

    async with client_for(make_app(tmp_path, options={"idle_timeout": 0.1})) as client:
        response = await client.post(
            "/v1/uploads",
            headers={**H, "content-type": "multipart/form-data; boundary=B"},
            content=slow(),
        )

        assert response.status_code == 408
        assert error_of(response)["code"] == "awa.data.upload_timeout"
        assert error_of(response)["retryable"] is True
        assert files_below(tmp_path) == []


async def test_an_upload_that_takes_too_long_in_total_is_stopped(tmp_path: Path) -> None:
    async def trickle() -> AsyncIterator[bytes]:
        yield b'--B\r\nContent-Disposition: form-data; name="files"; filename="a.csv"\r\n\r\n'
        for _ in range(100):
            await asyncio.sleep(0.05)
            yield b"1;2\r\n"

    async with client_for(
        make_app(tmp_path, options={"idle_timeout": 1, "upload_timeout": 0.2})
    ) as client:
        response = await client.post(
            "/v1/uploads",
            headers={**H, "content-type": "multipart/form-data; boundary=B"},
            content=trickle(),
        )

        assert response.status_code == 408
        assert files_below(tmp_path) == []


async def test_only_a_few_uploads_of_one_tenant_are_received_at_once(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def held() -> AsyncIterator[bytes]:
        yield b'--B\r\nContent-Disposition: form-data; name="files"; filename="a.csv"\r\n\r\nid;x\r\n'
        await release.wait()
        yield b"\r\n--B--\r\n"

    async with client_for(make_app(tmp_path, options={"idle_timeout": 30})) as client:
        headers = {**H, "content-type": "multipart/form-data; boundary=B"}
        pending = [
            asyncio.create_task(client.post("/v1/uploads", headers=headers, content=held()))
            for _ in range(4)
        ]
        await asyncio.sleep(0.3)
        fifth = await client.post("/v1/uploads", headers=H, files=csv_file())
        other_tenant = await client.post("/v1/uploads", headers=auth("k-gus"), files=csv_file())
        release.set()
        finished = await asyncio.gather(*pending)

        assert fifth.status_code == 429 and error_of(fifth)["code"] == "awa.data.quota_exceeded"
        assert other_tenant.status_code == 201
        assert [r.status_code for r in finished] == [201, 201, 201, 201]


async def test_the_data_endpoints_are_rate_limited_per_principal() -> None:
    async with client_for(make_app(options={"rate_limit": RateLimit(2, per=60)})) as client:
        results = [await client.post("/v1/uploads", headers=H, files=csv_file()) for _ in range(3)]
        bob = await client.post("/v1/uploads", headers=auth("k-bob"), files=csv_file())

        assert [r.status_code for r in results] == [201, 201, 429]
        assert error_of(results[2])["type"] == "RateLimitExceededError"
        assert int(results[2].headers["retry-after"]) >= 1
        assert bob.status_code == 201


# ---------------------------------------------------------------------- tenants and principals


async def test_another_tenant_gets_the_same_404_as_for_an_id_that_does_not_exist() -> None:
    async with client_for(make_app()) as client:
        upload_id = await upload(client, "k-alice")

        for method, path, kwargs in (
            ("GET", f"/v1/uploads/{upload_id}", {}),
            ("DELETE", f"/v1/uploads/{upload_id}", {}),
            ("POST", "/v1/data/profile", {"json": {"upload_id": upload_id}}),
        ):
            stranger = await client.request(method, path, headers=auth("k-gus"), **kwargs)
            unknown = await client.request(
                method,
                path.replace(upload_id, "up_000000000000000000000000"),
                headers=auth("k-gus"),
                **({"json": {"upload_id": "up_000000000000000000000000"}} if kwargs else {}),
            )
            assert (stranger.status_code, unknown.status_code) == (404, 404)
            assert (
                error_of(stranger)["code"]
                == error_of(unknown)["code"]
                == "awa.data.upload_not_found"
            )
            assert error_of(stranger)["message"].replace(upload_id, "X") == error_of(unknown)[
                "message"
            ].replace("up_000000000000000000000000", "X")
        assert (
            await client.get(f"/v1/uploads/{upload_id}", headers=auth("k-bob"))
        ).status_code == 200


async def test_with_principal_scope_only_the_owner_sees_an_upload() -> None:
    async with client_for(make_app(thread_scope="principal")) as client:
        upload_id = await upload(client, "k-alice")

        assert (
            await client.get(f"/v1/uploads/{upload_id}", headers=auth("k-bob"))
        ).status_code == 404
        assert (await client.get(f"/v1/uploads/{upload_id}", headers=H)).status_code == 200


@pytest.mark.parametrize(
    "bad", ["..", "..%2F..%2Fetc", "a%2Fb", "up_x/../../y", "%2e%2e", "up_1;ls", "x" * 100]
)
async def test_a_malformed_upload_id_never_reaches_the_file_system(bad: str) -> None:
    async with client_for(make_app()) as client:
        response = await client.get(f"/v1/uploads/{bad}", headers=H)

        assert response.status_code in (404, 422)
        assert "etc" not in response.text.lower()


# ---------------------------------------------------------------------- idempotent uploads


async def test_a_replayed_upload_with_the_same_key_gives_the_same_upload_and_keeps_one(
    tmp_path: Path,
) -> None:
    async with client_for(make_app(tmp_path)) as client:
        headers = {**H, "Idempotency-Key": "send-orders-1"}
        first = await client.post("/v1/uploads", headers=headers, files=csv_file())
        again = await client.post("/v1/uploads", headers=headers, files=csv_file())

        assert first.status_code == again.status_code == 201
        assert again.json() == first.json()
        assert again.headers["idempotent-replayed"] == "true"
        assert "idempotent-replayed" not in first.headers
        assert again.headers["location"] == first.headers["location"]
        assert len([p for p in files_below(tmp_path) if p.endswith("upload.json")]) == 1


async def test_the_same_key_with_other_files_is_refused_and_keeps_nothing_new(
    tmp_path: Path,
) -> None:
    async with client_for(make_app(tmp_path)) as client:
        headers = {**H, "Idempotency-Key": "k1"}
        await client.post("/v1/uploads", headers=headers, files=csv_file())
        different = await client.post(
            "/v1/uploads", headers=headers, files=csv_file("other.csv", b"a;b\n")
        )
        new_key = await client.post(
            "/v1/uploads", headers={**H, "Idempotency-Key": "k2"}, files=csv_file()
        )
        other_caller = await client.post(
            "/v1/uploads",
            headers={**auth("k-bob"), "Idempotency-Key": "k1"},
            files=csv_file("other.csv", b"a;b\n"),
        )

        assert different.status_code == 422
        assert error_of(different)["code"] == "awa.idempotency_mismatch"
        assert new_key.status_code == 201 and other_caller.status_code == 201  # keys are per caller
        assert len([p for p in files_below(tmp_path) if p.endswith("upload.json")]) == 3


async def test_a_replay_gives_the_original_answer_even_after_the_upload_was_deleted() -> None:
    # An idempotency key stands for the first answer for as long as the server remembers it;
    # whoever deletes the upload and wants it again sends it with a new key.
    async with client_for(make_app()) as client:
        headers = {**H, "Idempotency-Key": "k1"}
        first = (await client.post("/v1/uploads", headers=headers, files=csv_file())).json()
        await client.delete(f"/v1/uploads/{first['upload_id']}", headers=H)

        again = await client.post("/v1/uploads", headers=headers, files=csv_file())
        fresh = await client.post(
            "/v1/uploads", headers={**H, "Idempotency-Key": "k2"}, files=csv_file()
        )

        assert again.json() == first and again.headers["idempotent-replayed"] == "true"
        assert (await client.get(f"/v1/uploads/{first['upload_id']}", headers=H)).status_code == 404
        assert fresh.status_code == 201 and fresh.json()["upload_id"] != first["upload_id"]


async def test_an_idempotency_key_must_be_well_formed() -> None:
    async with client_for(make_app()) as client:
        response = await client.post(
            "/v1/uploads", headers={**H, "Idempotency-Key": "has space"}, files=csv_file()
        )

        assert response.status_code == 422


# ---------------------------------------------------------------------- profiles


async def test_a_profile_describes_the_files_and_hides_where_the_server_keeps_them(
    tmp_path: Path,
) -> None:
    async with client_for(make_app(tmp_path)) as client:
        upload_id = await upload(client, content="id;betrag\n1;4.250,00\n2;3,50\n".encode("cp1252"))

        response = await client.post("/v1/data/profile", headers=H, json={"upload_id": upload_id})

        assert response.status_code == 200
        body = response.json()
        assert body["upload_id"] == upload_id and "model_view" not in body
        profile = body["profile"]
        assert profile["schema"] == "awa.data/profile/v1" and profile["summary"]["files"] == 1
        file = profile["files"][0]
        assert (file["id"], file["path"], file["kind"], file["status"]) == (
            "f1",
            "orders.csv",
            "csv",
            "ok",
        )
        assert file["source"] is None
        assert str(tmp_path) not in response.text and tmp_path.name not in response.text
        assert file["tables"][0]["sample_rows"]  # the user's own data is shown to the user


async def test_mask_drop_removes_every_sample_value_from_the_profile() -> None:
    async with client_for(make_app()) as client:
        upload_id = await upload(client, content=b"name;iban\nAnna Meyer;DE89370400440532013000\n")

        response = await client.post(
            "/v1/data/profile", headers=H, json={"upload_id": upload_id, "mask": "drop"}
        )

        assert response.status_code == 200
        assert "Anna" not in response.text and "DE89" not in response.text
        assert response.json()["profile"]["files"][0]["tables"][0]["sample_rows"] == []


async def test_a_model_view_comes_with_mask_or_budget_and_uses_the_given_privacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    def render(
        profile: Any, budget_tokens: int = 6000, *, privacy: Any = None, focus: Any = None
    ) -> str:
        calls.append((len(profile.files), budget_tokens, privacy.mask))
        return f"<<<DATA profile>>> {budget_tokens} {privacy.mask}"

    import awa.data

    monkeypatch.setattr(awa.data, "render_for_model", render, raising=False)
    async with client_for(make_app()) as client:
        upload_id = await upload(client)

        plain = await client.post("/v1/data/profile", headers=H, json={"upload_id": upload_id})
        masked = await client.post(
            "/v1/data/profile", headers=H, json={"upload_id": upload_id, "mask": "placeholders"}
        )
        sized = await client.post(
            "/v1/data/profile", headers=H, json={"upload_id": upload_id, "budget_tokens": 800}
        )

        assert "model_view" not in plain.json()
        assert masked.json()["model_view"] == "<<<DATA profile>>> 6000 placeholders"
        assert sized.json()["model_view"] == "<<<DATA profile>>> 800 none"
        assert calls == [(1, 6000, "placeholders"), (1, 800, "none")]


async def test_a_profile_can_be_limited_to_some_files_that_keep_their_ids() -> None:
    async with client_for(make_app()) as client:
        response = await client.post(
            "/v1/uploads",
            headers=H,
            files=csv_file("a.csv") + csv_file("b.csv") + csv_file("c.csv"),
        )
        upload_id = response.json()["upload_id"]

        some = await client.post(
            "/v1/data/profile", headers=H, json={"upload_id": upload_id, "files": ["f3", "f1"]}
        )
        unknown = await client.post(
            "/v1/data/profile", headers=H, json={"upload_id": upload_id, "files": ["f9"]}
        )

        assert [(f["id"], f["path"]) for f in some.json()["profile"]["files"]] == [
            ("f1", "a.csv"),
            ("f3", "c.csv"),
        ]
        assert some.json()["profile"]["summary"]["files"] == 2
        assert unknown.status_code == 400 and "f9" in error_of(unknown)["message"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"upload_id": "x/../y"},
        {"upload_id": "up_1", "mask": "everything"},
        {"upload_id": "up_1", "budget_tokens": 5},
        {"upload_id": "up_1", "budget_tokens": 99999},
        {"upload_id": "up_1", "files": ["../x"]},
        {"upload_id": "up_1", "extra": 1},
    ],
)
async def test_an_invalid_profile_request_is_a_422_with_details(body: dict[str, Any]) -> None:
    async with client_for(make_app()) as client:
        response = await client.post("/v1/data/profile", headers=H, json=body)

        assert response.status_code == 422
        assert error_of(response)["code"] == "awa.validation"


async def test_a_profile_request_with_another_content_type_or_a_huge_body_is_refused() -> None:
    async with client_for(make_app()) as client:
        text = await client.post(
            "/v1/data/profile", headers={**H, "content-type": "text/plain"}, content=b"x"
        )
        huge = await client.post(
            "/v1/data/profile",
            headers=H,
            json={"upload_id": "up_1", "files": ["f1"] * 20, "pad": "x" * 70_000},
        )
        broken = await client.post(
            "/v1/data/profile", headers={**H, "content-type": "application/json"}, content=b"{nope"
        )

        assert text.status_code == 415
        assert huge.status_code == 400
        assert broken.status_code == 422


async def test_files_can_be_uploaded_and_profiled_in_one_call() -> None:
    async with client_for(make_app()) as client:
        response = await client.post(
            "/v1/data/profile?mask=drop&keep=true", headers=H, files=csv_file("orders.csv")
        )

        assert response.status_code == 200
        body = response.json()
        assert body["profile"]["files"][0]["path"] == "orders.csv"
        kept = await client.get(f"/v1/uploads/{body['upload_id']}", headers=H)
        assert kept.status_code == 200  # the upload stays, a build can use it
        bad = await client.post("/v1/data/profile?mask=nonsense", headers=H, files=csv_file())
        assert bad.status_code == 422


async def test_a_file_that_cannot_be_read_is_in_the_profile_and_not_an_http_error() -> None:
    broken = io.BytesIO()
    with zipfile.ZipFile(broken, "w") as archive:  # looks like a workbook, is not one
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "not xml at all <<<")
    async with client_for(make_app()) as client:
        response = await client.post(
            "/v1/uploads", headers=H, files=csv_file("broken.xlsx", broken.getvalue()) + csv_file()
        )
        profiled = await client.post(
            "/v1/data/profile", headers=H, json={"upload_id": response.json()["upload_id"]}
        )

        assert profiled.status_code == 200
        files = {f["path"]: f for f in profiled.json()["profile"]["files"]}
        # an unreadable file is a fact about the file ("error", or "unsupported" until a reader
        # for it is installed), with a flag that says why; the other files are profiled as usual
        assert files["broken.xlsx"]["status"] in ("error", "unsupported")
        assert files["broken.xlsx"]["flags"]
        assert files["orders.csv"]["status"] == "ok"
        assert profiled.json()["profile"]["summary"]["files"] == 2


async def test_the_real_example_files_profile_through_http(tmp_path: Path) -> None:
    async with client_for(make_app(tmp_path)) as client:
        response = await client.post(
            "/v1/uploads",
            headers=H,
            files=[
                ("files", (p.name, p.read_bytes(), "application/octet-stream"))
                for p in INBOX.iterdir()
            ],
        )
        upload_id = response.json()["upload_id"]

        profiled = await client.post("/v1/data/profile", headers=H, json={"upload_id": upload_id})

        assert profiled.status_code == 200
        profile = profiled.json()["profile"]
        assert profile["summary"]["files"] == 4
        assert {f["path"]: f["kind"] for f in profile["files"]} == {
            "2024-0412_mueller.pdf": "pdf",
            "88231_zuercher.docx": "docx",
            "ln-2024-17_meyer.xlsx": "xlsx",
            "scan_0001.pdf": "pdf",
        }
        assert all(f["source"] is None for f in profile["files"])
        assert str(tmp_path) not in profiled.text


# ---------------------------------------------------------------------- nothing secret in the answers


async def test_errors_and_answers_never_carry_keys_headers_or_storage_paths(tmp_path: Path) -> None:
    async with client_for(make_app(tmp_path)) as client:
        responses = [
            await client.post(
                "/v1/uploads", headers=auth("k-alice"), files=csv_file("x.pdf", b"nope")
            ),
            await client.get("/v1/uploads/up_000000000000000000000000", headers=auth("k-alice")),
            await client.post("/v1/uploads", headers=auth("wrong-key-123"), files=csv_file()),
            await client.post(
                "/v1/data/profile", headers=auth("k-alice"), json={"upload_id": "up_1", "oops": 1}
            ),
        ]

        text = " ".join(r.text for r in responses)
        for secret in (*KEYS, "wrong-key-123", str(tmp_path), "Bearer"):
            assert secret not in text


async def test_audit_records_who_uploaded_profiled_and_deleted(tmp_path: Path) -> None:
    records: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    class Log:
        def __call__(self, event: Any) -> None: ...

        def record(self, event_type: str, data: dict[str, Any], **identity: Any) -> None:
            records.append((event_type, data, identity))

    async with client_for(make_app(tmp_path, audit_log=Log())) as client:
        upload_id = await upload(client)
        await client.post("/v1/data/profile", headers=H, json={"upload_id": upload_id})
        await client.delete(f"/v1/uploads/{upload_id}", headers=H)

    assert [name for name, _, _ in records] == [
        "upload_created",
        "profile_requested",
        "upload_deleted",
    ]
    assert records[0][1]["upload_id"] == upload_id and records[0][1]["files"] == 1
    assert {identity["actor"] for _, _, identity in records} == {"alice"}
    assert {identity["tenant"] for _, _, identity in records} == {"acme"}
    assert all(identity["request_id"] for _, _, identity in records)
    assert "orders.csv" not in json.dumps(records, default=str)  # ids and counts, not content


async def test_a_part_with_oversized_headers_is_a_clean_400() -> None:
    # Each line and the number of lines stay within the parser's own limits: 12 KB in all.
    padding = b"".join(b"X-Pad-%d: %s\r\n" % (n, b"a" * 2000) for n in range(6))
    part = (
        b'--B\r\nContent-Disposition: form-data; name="files"; filename="a.csv"\r\n'
        + padding
        + b"\r\nid;x\n1;2\n\r\n--B--\r\n"
    )
    async with client_for(make_app()) as client:
        response = await client.post(
            "/v1/uploads",
            headers={**H, "content-type": "multipart/form-data; boundary=B"},
            content=part,
        )

        assert response.status_code == 400
        assert "headers of a part" in error_of(response)["message"]


async def test_files_sent_to_the_profile_endpoint_are_profiled_and_not_kept(tmp_path: Path) -> None:
    async with client_for(make_app(tmp_path, options={"max_tenant_bytes": 100})) as client:
        profiled = [
            await client.post(
                "/v1/data/profile", headers=H, files=csv_file("a.csv", b"id;x\n" * 12)
            )
            for _ in range(3)
        ]
        kept = await client.post("/v1/uploads", headers=H, files=csv_file("b.csv", b"id;x\n" * 12))

    assert [answer.status_code for answer in profiled] == [200, 200, 200]
    assert profiled[0].json()["upload_id"] is None
    assert profiled[0].json()["profile"]["files"][0]["path"] == "a.csv"
    assert kept.status_code == 201  # the profiled files hold no quota
    assert len([p for p in files_below(tmp_path) if p.endswith("upload.json")]) == 1
