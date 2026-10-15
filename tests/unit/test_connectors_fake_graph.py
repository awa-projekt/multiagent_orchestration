"""The connector engine against FakeGraph: real HTTP, the production code path, nothing patched.

The connector reaches the fake only through `MS_GRAPH_URL` / `AZURE_AUTHORITY_HOST` and the
explicit options `allow_http` / `allow_private_networks` (`graph.env`, `graph.connector_options`).
These tests prove what a `MockTransport` cannot: redirects without credentials, upload sessions,
Retry-After over the wire, token expiry on a clock the server controls, refresh-token rotation
through a `TokenProvider`.
"""

from __future__ import annotations

import hashlib
import os
import urllib.parse
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from _helpers.fakegraph.core import decode_jwt
from _helpers.fakegraph.files import CHUNK, FilesStore
from _helpers.fakegraph.identity import DEVICE_GRANT
from _helpers.fakegraph.server import V1, FakeGraph
from _helpers.polling import poll
from awa._internal.upstream.tools import ToolException
from awa.connectors import (
    ConnectorSpec,
    EnvVar,
    Operation,
    Param,
    _engine,
    connector_tools,
    get_connector,
)
from awa.connectors._catalog import connector_context
from awa.connectors._engine import RetryAfter, UncertainOutcome, call_operation, send
from awa.connectors._saas_microsoft import _AUTHORITY, _GRAPH_URL, _entra
from awa.connectors._spec import TokenAuth, UploadSession
from awa.connectors._transfer import FILE_OPTIONS

CLIENT = "11111111-2222-3333-4444-555555555555"
SECRET = "client-secret-for-the-fake"
PUBLIC = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def graph() -> Iterator[FakeGraph]:
    with FakeGraph() as server:
        yield server


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The engine's waits are recorded, not slept (the server's clock is separate)."""

    slept: list[float] = []
    monkeypatch.setattr(_engine, "_sleep", slept.append)
    return slept


def tools(
    graph: FakeGraph, app: Any, spec: ConnectorSpec | str = "microsoft_graph", **config: Any
) -> dict[str, Any]:
    options = {**graph.connector_options, "requires_approval": False, **config}
    built = connector_tools(spec, env=graph.env(app), config=options)
    prefix = spec if isinstance(spec, str) else spec.name
    return {t.name.removeprefix(f"{prefix}_"): t for t in built}


def content(result: str) -> Any:
    import json

    assert result.startswith("<tool_output"), result
    text = json.loads(result.split(">", 1)[1].rsplit("</tool_output>", 1)[0])["content"]
    try:
        return json.loads(text)
    except ValueError:
        return text


# --------------------------------------------------------------------------- the real connector


def test_microsoft_graph_reaches_the_fake_through_variables_and_explicit_options(
    graph: FakeGraph,
) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Mail.Read",))
    ada = graph.add_user("ada@contoso.test")
    for number in range(7):
        graph.store("mail").add_message(ada, subject=f"mail {number}")
        graph.clock.advance(1)
    listed = content(
        tools(graph, app)["list_messages"].invoke({"user": "ada@contoso.test", "limit": 5})
    )
    assert [m["subject"] for m in listed["items"]] == [f"mail {n}" for n in (6, 5, 4, 3, 2)]
    assert listed["has_more"] is True
    token_request, graph_request = graph.requests[:2]
    assert token_request.path == f"/{graph.tenant_id}/oauth2/v2.0/token"
    form = token_request.form()
    assert form["grant_type"] == "client_credentials" and form["client_secret"] == SECRET
    assert form["scope"] == "https://graph.microsoft.com/.default"
    assert graph_request.path == f"{V1}/users/ada@contoso.test/mailFolders/inbox/messages"
    assert graph_request.header("user-agent").startswith("NONISV|awa|awa/")
    assert decode_jwt(graph_request.header("authorization").split()[1])["roles"] == ["Mail.Read"]


def test_a_localhost_server_is_refused_unless_private_networks_are_allowed(
    graph: FakeGraph,
) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Mail.Read",))
    graph.add_user("ada@contoso.test")
    for options in ({}, {"allow_http": True}):  # production defaults: both off
        built = connector_tools("microsoft_graph", env=graph.env(app), config=options)
        result = next(t for t in built if t.name.endswith("list_messages")).invoke(
            {"user": "ada@contoso.test"}
        )
        assert "private or reserved address" in result, result
        assert graph.requests == []  # the engine refused before reaching the server at all
    only = {"allow_private_networks": True}  # plain http to the loopback host needs no option
    built = connector_tools("microsoft_graph", env=graph.env(app), config=only)
    result = next(t for t in built if t.name.endswith("list_messages")).invoke(
        {"user": "ada@contoso.test"}
    )
    assert result.startswith("<tool_output")


def test_paging_follows_the_absolute_next_link_of_the_fake(graph: FakeGraph) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Files.Read.All",))
    files: FilesStore = graph.store("files")
    site = files.add_site("contoso.sharepoint.com", "sites/awa-files")
    for number in range(250):
        files.put_file(site, f"many/f{number:03d}.txt", b"x")
    folder = files.folder_path(files.drives[site.drive_id], "many")
    arguments = {"site_id": site.id, "item_id": folder["id"], "limit": 150}
    listed = content(tools(graph, app, max_output_chars=500_000)["list_files"].invoke(arguments))
    assert listed["count"] == 150 and listed["has_more"] is True
    first, second = graph.calls("GET", "*/children")
    assert first.query["$top"] == ["100"] and "$skiptoken" not in first.query
    assert second.query["$top"] == ["100"] and second.query["$skiptoken"][0]  # the link as it came
    assert [f["name"] for f in listed["items"]][-1] == "f149.txt"
    rest = content(
        tools(graph, app, max_output_chars=500_000)["list_files"].invoke(
            {**arguments, "page_token": listed["next_page"]}
        )
    )
    assert rest["items"][0]["name"] == "f150.txt"


def test_the_engine_refreshes_a_token_the_server_expired_and_retries_once(graph: FakeGraph) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Mail.Read",))
    ada = graph.add_user("ada@contoso.test")
    graph.store("mail").add_message(ada, subject="hello")
    ops = tools(graph, app)
    assert "hello" in ops["list_messages"].invoke({"user": "ada@contoso.test"})
    graph.clock.advance(3600)  # the server's clock: the cached token is expired there
    assert "hello" in ops["list_messages"].invoke({"user": "ada@contoso.test"})
    statuses = [(r.path.rsplit("/", 1)[-1], r.status) for r in graph.requests]
    assert statuses == [
        ("token", 200),
        ("messages", 200),
        ("messages", 401),
        ("token", 200),
        ("messages", 200),
    ]


def test_a_wrong_secret_is_explained_without_echoing_it(graph: FakeGraph) -> None:
    graph.add_app(CLIENT, SECRET, roles=("Mail.Read",))
    graph.add_user("ada@contoso.test")
    wrong = graph.add_app("00000000-1111-2222-3333-444444444444", "x", roles=())
    env = graph.env(wrong) | {"AZURE_CLIENT_SECRET": "not-the-secret-value"}
    tools = connector_tools("microsoft_graph", env=env, config=graph.connector_options)
    listing = next(t for t in tools if t.name == "microsoft_graph_list_messages")
    result = listing.invoke({"user": "ada@contoso.test"})
    assert "no access token" in result and "AADSTS7000215" in result
    assert "not-the-secret-value" not in result


# --------------------------------------------------------------------------- needs


def teams_world(graph: FakeGraph) -> tuple[Any, Any, Any]:
    ada = graph.add_user("ada@contoso.test", name="Ada")
    teams = graph.store("teams")
    team = teams.add_team("Ops", ada)
    channel = teams.add_channel(team, "General")
    return ada, team, channel


def test_posting_to_a_channel_as_the_app_is_refused_by_the_engine_not_by_microsoft(
    graph: FakeGraph,
) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Teamwork.Migrate.All", "ChannelMessage.Read.All"))
    _, team, channel = teams_world(graph)
    arguments = {"team_id": team.id, "channel_id": channel.id, "text": "hello"}
    result = tools(graph, app)["post_channel_message"].invoke(arguments)
    assert "acts as a signed-in person" in result and "MS_GRAPH_TOKEN" in result
    assert graph.requests == []  # the engine said no before any request, even a token request
    # without the marker, the same call reaches Microsoft, which answers what the marker predicts
    spec = get_connector("microsoft_graph")
    unmarked = replace(
        spec,
        operations=tuple(replace(op, needs="any") for op in spec.operations),
        name="msgraph_unmarked",
    )
    answer = tools(graph, app, unmarked)["post_channel_message"].invoke(arguments)
    assert "denied access (HTTP 403)" in answer
    assert graph.calls("POST", "/v1.0/teams/*")[0].status == 403


def test_the_same_operation_works_with_a_delegated_token(graph: FakeGraph) -> None:
    app = graph.add_app(PUBLIC, public_client=True, scopes=("ChannelMessage.Send",))
    ada, team, channel = teams_world(graph)
    token = graph.issue_token(app, kind="user", scopes=["ChannelMessage.Send"], user=ada)
    env = {"MS_GRAPH_URL": graph.graph_url, "MS_GRAPH_TOKEN": token}
    built = connector_tools(
        "microsoft_graph",
        env=env,
        config={
            **graph.connector_options,
            "include": ["post_channel_message"],
            "requires_approval": False,
        },
    )
    posted = content(built[0].invoke({"team_id": team.id, "channel_id": channel.id, "text": "hi"}))
    assert posted["from"]["user"]["displayName"] == "Ada"
    assert channel.messages[0]["body"]["content"] == "hi"


# --------------------------------------------------------------------------- waits and writes


def test_a_throttled_read_waits_for_retry_after_then_succeeds(
    graph: FakeGraph, sleeps: list[float]
) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Mail.Read",))
    ada = graph.add_user("ada@contoso.test")
    graph.store("mail").add_message(ada, subject="hello")
    graph.throttle("/users/*", retry_after=2)
    assert "hello" in tools(graph, app)["list_messages"].invoke({"user": "ada@contoso.test"})
    assert sleeps == [2.0]
    assert [r.status for r in graph.calls("GET", "/v1.0/users/*")] == [429, 200]


def test_a_throttle_without_retry_after_backs_off_and_a_long_one_is_surfaced(
    graph: FakeGraph, sleeps: list[float]
) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Mail.Read",))
    ada = graph.add_user("ada@contoso.test")
    graph.store("mail").add_message(ada, subject="hello")
    graph.throttle("/users/*", retry_after=None)
    assert "hello" in tools(graph, app)["list_messages"].invoke({"user": "ada@contoso.test"})
    assert sleeps == [0.5]
    graph.clear_faults()
    graph.clear_requests()
    sleeps.clear()
    graph.throttle("/users/*", retry_after=120, times=None)
    ctx = connector_context("microsoft_graph", env=graph.env(app), config=graph.connector_options)
    operation = ctx.spec.operation("list_messages")
    with pytest.raises(RetryAfter) as caught:
        call_operation(ctx, operation, {"user": "ada@contoso.test"})
    assert caught.value.seconds == 120 and sleeps == []
    assert len(graph.calls("GET", "/v1.0/users/*")) == 1  # not waited for, not hammered


def test_a_503_on_a_read_is_retried_and_on_a_write_never(
    graph: FakeGraph, sleeps: list[float]
) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Mail.Read", "Mail.Send"))
    ada = graph.add_user("ada@contoso.test")
    graph.store("mail").add_message(ada, subject="hello")
    graph.fail("GET", "/users/*", status=503)
    ops = tools(graph, app)
    assert "hello" in ops["list_messages"].invoke({"user": "ada@contoso.test"})
    assert [r.status for r in graph.calls("GET", "/v1.0/users/*")] == [503, 200]
    graph.fail("POST", "/users/*/sendMail", status=503, times=None)
    mail = {"user": "ada@contoso.test", "to": ["bob@contoso.test"], "subject": "S", "body": "B"}
    result = ops["send_mail"].invoke(mail)
    assert "may or may not have been made" in result and "HTTP 503" in result
    assert len(graph.calls("POST", "/v1.0/users/*/sendMail")) == 1  # one request, no repeat
    ctx = connector_context("microsoft_graph", env=graph.env(app), config=graph.connector_options)
    with pytest.raises(UncertainOutcome):
        call_operation(ctx, ctx.spec.operation("send_mail"), mail)


def test_a_429_on_a_write_is_retried_because_nothing_was_processed(
    graph: FakeGraph, sleeps: list[float]
) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Mail.Send",))
    ada = graph.add_user("ada@contoso.test")
    graph.add_user("bob@contoso.test")
    graph.throttle("/users/*/sendMail", retry_after=1)
    mail = {"user": "ada@contoso.test", "to": ["bob@contoso.test"], "subject": "S", "body": "B"}
    assert tools(graph, app)["send_mail"].invoke(mail).startswith("<tool_output")
    assert [r.status for r in graph.calls("POST", "/v1.0/users/*/sendMail")] == [429, 202]
    assert len(graph.store("mail").box(ada.id)["sentitems"]) == 1  # sent once, not twice


def test_a_write_that_times_out_is_uncertain_even_though_the_server_did_it(
    graph: FakeGraph,
) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Mail.Send",))
    ada = graph.add_user("ada@contoso.test")
    graph.delay("/users/*/sendMail", 1.0)
    mail = {"user": "ada@contoso.test", "to": ["x@contoso.test"], "subject": "S", "body": "B"}
    result = tools(graph, app, timeout=0.3)["send_mail"].invoke(mail)
    assert "timed out" in result and "may or may not have been processed" in result
    poll(lambda: graph.calls("POST", "/v1.0/users/*/sendMail"), timeout=10)
    poll(lambda: graph.store("mail").box(ada.id)["sentitems"], timeout=10)  # it was sent after all
    assert len(graph.calls("POST", "/v1.0/users/*/sendMail")) == 1


def test_the_localhost_guard_refuses_without_leaving_an_uncertain_write(graph: FakeGraph) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Mail.Send",))
    graph.add_user("ada@contoso.test")
    options = {"allow_http": True, "requires_approval": False}  # private networks stay refused
    built = connector_tools("microsoft_graph", env=graph.env(app), config=options)
    mail = {"user": "ada@contoso.test", "to": ["x@contoso.test"], "subject": "S", "body": "B"}
    result = next(t for t in built if t.name.endswith("send_mail")).invoke(mail)
    assert "private or reserved address" in result and "may or may not" not in result


# --------------------------------------------------------------------------- files

STORE_PATH = "/sites/{site}/drive/root:/{path}"


def drive_spec() -> ConnectorSpec:
    site = Param("site", "Site id.", location="path", required=True)
    path = Param("path", "Path in the library.", location="path", required=True, safe="/")
    conflict = Param(
        "conflict",
        "fail, rename or replace.",
        location="query",
        key="@microsoft.graph.conflictBehavior",
    )
    return ConnectorSpec(
        name="drive",
        title="Graph drive",
        category="files",
        summary="Files of a site.",
        base_url="{MS_GRAPH_URL}/v1.0",
        env=(_GRAPH_URL,),
        auth=(_entra("https://graph.microsoft.com/.default"),),
        options=FILE_OPTIONS,
        operations=(
            Operation(
                "download",
                "Save a file to the workspace.",
                path=f"{STORE_PATH}:/content",
                params=(
                    site,
                    path,
                    Param("save_to", "Workspace path.", location="local", required=True),
                ),
                response="file",
                file_param="save_to",
            ),
            Operation(
                "upload",
                "Upload a workspace file.",
                method="PUT",
                path=f"{STORE_PATH}:/content",
                params=(
                    site,
                    path,
                    conflict,
                    Param("local_path", "Workspace path.", location="local", required=True),
                ),
                body_format="file",
                file_param="local_path",
                write=True,
                session=UploadSession(
                    f"{STORE_PATH}:/createUploadSession",
                    simple_limit=4 * 1024 * 1024,
                    fragment=3 * CHUNK,
                    body_builder=lambda args, size, name: {
                        "item": {
                            "@microsoft.graph.conflictBehavior": args.get("conflict") or "replace"
                        }
                    },
                ),
            ),
        ),
    )


class Library:
    def __init__(self, graph: FakeGraph, tmp_path: Path, **config: Any) -> None:
        self.graph = graph
        self.workspace = tmp_path / "ws"
        self.workspace.mkdir()
        self.files: FilesStore = graph.store("files")
        self.site = self.files.add_site("contoso.sharepoint.com", "sites/awa-files")
        app = graph.add_app(CLIENT, SECRET, roles=("Sites.ReadWrite.All", "Files.ReadWrite.All"))
        self.ops = tools(graph, app, drive_spec(), root=str(self.workspace), **config)

    def download(self, path: str, save_to: str) -> str:
        result: str = self.ops["download"].invoke(
            {"site": self.site.id, "path": path, "save_to": save_to}
        )
        return result

    def upload(self, local: str, path: str, **extra: Any) -> str:
        result: str = self.ops["upload"].invoke(
            {"site": self.site.id, "path": path, "local_path": local, **extra}
        )
        return result


@pytest.fixture
def library(graph: FakeGraph, tmp_path: Path) -> Library:
    return Library(graph, tmp_path)


def test_a_download_follows_the_302_without_credentials_and_saves_atomically(
    library: Library,
) -> None:
    body = os.urandom(10_000)
    library.files.put_file(library.site, "Reports/q3.bin", body)
    result = content(library.download("Reports/q3.bin", "inbox/q3.bin"))
    assert result == {
        "saved_to": "inbox/q3.bin",
        "size": 10_000,
        "sha256": hashlib.sha256(body).hexdigest(),
        "content_type": "application/octet-stream",
    }
    assert (library.workspace / "inbox" / "q3.bin").read_bytes() == body
    first, second = (
        library.graph.calls("GET", "*content")[0],
        library.graph.calls("GET", "/_download/*")[0],
    )
    assert first.header("authorization").startswith("Bearer ")
    assert second.header("authorization") is None and second.status == 200
    leftovers = [p.name for p in (library.workspace / "inbox").iterdir()]
    assert leftovers == ["q3.bin"]  # no temporary file


def test_the_download_url_is_a_credential_and_never_shown(library: Library) -> None:
    library.files.put_file(library.site, "a.bin", b"x")
    library.files.simple_put_limit = 1
    location = library.graph.call(
        "GET",
        f"{V1}/sites/{library.site.id}/drive/root:/a.bin:/content",
        token=library.graph.app_token(library.graph.apps[CLIENT]),
    ).headers["location"]
    assert "/_download/" in location
    shown = library.download("a.bin", "a.bin")
    assert content(shown)["size"] == 1
    assert location not in shown and "/_download/" not in shown
    result = library.download("missing.bin", "m.bin")  # a 404 on the first hop
    assert result.startswith("Error") and "found nothing there" in result
    assert not (library.workspace / "m.bin").exists()


def test_a_redirect_to_another_host_name_needs_that_host_to_be_allowed(
    graph: FakeGraph, tmp_path: Path
) -> None:
    graph.store("files").download_host = "localhost"
    blocked = Library(graph, tmp_path)
    blocked.files.put_file(blocked.site, "a.bin", b"payload")
    result = blocked.download("a.bin", "a.bin")
    assert "host 'localhost' is not in the allowed hosts" in result
    other = tmp_path / "other"
    other.mkdir()
    allowed = Library(graph, other, allowed_hosts=["localhost"])
    allowed.files.put_file(allowed.site, "b.bin", b"payload")
    assert content(allowed.download("b.bin", "b.bin"))["size"] == 7
    last = graph.calls("GET", "/_download/*")[-1]
    assert last.header("authorization") is None and last.header("host").startswith("localhost:")


def test_downloads_refuse_existing_files_escapes_and_oversized_answers(
    graph: FakeGraph, tmp_path: Path
) -> None:
    library = Library(graph, tmp_path, max_file_bytes=1000)
    library.files.put_file(library.site, "big.bin", b"x" * 1001)
    library.files.put_file(library.site, "ok.bin", b"x" * 1000)
    big = library.download("big.bin", "big.bin")
    assert "larger than 1000 bytes" in big and not (library.workspace / "big.bin").exists()
    assert content(library.download("ok.bin", "ok.bin"))["size"] == 1000
    again = library.download("ok.bin", "ok.bin")
    assert "already exists in the workspace" in again
    assert "must not contain '..'" in library.download("ok.bin", "../escape.bin")
    assert "must be relative" in library.download("ok.bin", "/etc/passwd")
    assert "hidden" in library.download("ok.bin", ".secret")
    assert not (tmp_path / "escape.bin").exists()


def test_a_missing_workspace_folder_is_explained(graph: FakeGraph, tmp_path: Path) -> None:
    library = Library(graph, tmp_path)
    library.workspace.rmdir()
    answer = library.download("a.bin", "a.bin")
    assert "workspace folder" in answer and "does not exist" in answer


def test_a_small_file_is_one_put_with_the_conflict_behavior(library: Library) -> None:
    (library.workspace / "report.txt").write_bytes(b"a,b\n1,2\n")
    created = content(library.upload("report.txt", "Reports/report.txt", conflict="fail"))
    assert created["name"] == "report.txt" and created["size"] == 8
    put = library.graph.calls("PUT", "*content")[0]
    assert put.header("content-type") == "text/plain" and put.body == b"a,b\n1,2\n"
    assert put.query == {"@microsoft.graph.conflictBehavior": ["fail"]}
    assert library.files.content(library.site, "Reports/report.txt") == b"a,b\n1,2\n"
    again = library.upload("report.txt", "Reports/report.txt", conflict="fail")
    assert "rejected the request (HTTP 409)" in again


def test_a_large_file_goes_through_an_upload_session_in_fragments_without_credentials(
    library: Library,
) -> None:
    body = os.urandom(4 * 1024 * 1024 + 5000)
    (library.workspace / "video.bin").write_bytes(body)
    result = content(library.upload("video.bin", "Media/video.bin"))
    assert result["size"] == len(body) and result["name"] == "video.bin"
    stored = library.files.content(library.site, "Media/video.bin")
    assert hashlib.sha256(stored).hexdigest() == hashlib.sha256(body).hexdigest()
    opened = library.graph.calls("POST", "*createUploadSession")
    assert len(opened) == 1 and opened[0].json() == {
        "item": {"@microsoft.graph.conflictBehavior": "replace"}
    }
    assert opened[0].header("authorization").startswith("Bearer ")
    fragments = library.graph.calls("PUT", "/_upload/*")
    assert len(fragments) == 5  # 4 MiB + 5000 bytes in 3 x 320 KiB pieces
    assert all(f.header("authorization") is None for f in fragments)
    ranges = [f.header("content-range") for f in fragments]
    total = len(body)
    assert ranges[0] == f"bytes 0-{3 * CHUNK - 1}/{total}" and ranges[-1].endswith(
        f"-{total - 1}/{total}"
    )
    assert [f.status for f in fragments] == [202, 202, 202, 202, 201]
    assert not library.graph.calls("PUT", "*content")  # no simple PUT for the big file


def test_a_fragment_that_gets_a_503_is_sent_again(library: Library) -> None:
    body = os.urandom(5 * CHUNK)
    (library.workspace / "f.bin").write_bytes(body)
    library.files.simple_put_limit = None
    spec_ops = drive_spec()
    library.graph.fail("PUT", "/_upload/*", status=503, after=1, times=1)
    ops = tools(
        library.graph,
        library.graph.apps[CLIENT],
        replace(
            spec_ops,
            operations=(
                spec_ops.operations[0],
                replace(
                    spec_ops.operations[1],
                    session=replace(spec_ops.operations[1].session, simple_limit=10),
                ),
            ),
        ),
        root=str(library.workspace),
    )
    result = ops["upload"].invoke({"site": library.site.id, "path": "f.bin", "local_path": "f.bin"})
    assert content(result)["size"] == len(body)
    statuses = [f.status for f in library.graph.calls("PUT", "/_upload/*")]
    assert statuses == [202, 503, 201]
    assert library.files.content(library.site, "f.bin") == body


def test_a_failed_session_is_cancelled_and_leaves_the_target_alone(library: Library) -> None:
    library.files.put_file(library.site, "keep.bin", b"original")
    body = os.urandom(4 * 1024 * 1024 + 10)
    (library.workspace / "keep.bin").write_bytes(body)
    library.graph.fail("PUT", "/_upload/*", status=503, after=1, times=None)
    result = library.upload("keep.bin", "keep.bin")
    assert "server error (HTTP 503)" in result
    assert library.files.content(library.site, "keep.bin") == b"original"
    assert [r.method for r in library.graph.calls(path="/_upload/*")][-1] == "DELETE"


def test_a_session_conflict_surfaces_on_the_last_fragment(library: Library) -> None:
    library.files.put_file(library.site, "taken.bin", b"original")
    (library.workspace / "taken.bin").write_bytes(os.urandom(4 * 1024 * 1024 + 10))
    result = library.upload("taken.bin", "taken.bin", conflict="fail")
    assert "rejected the request (HTTP 409)" in result
    assert library.files.content(library.site, "taken.bin") == b"original"


def test_uploads_refuse_missing_files_directories_escapes_and_oversized_files(
    graph: FakeGraph, tmp_path: Path
) -> None:
    library = Library(graph, tmp_path, max_file_bytes=100)
    (library.workspace / "dir").mkdir()
    (library.workspace / "big.bin").write_bytes(b"x" * 101)
    assert "does not exist" in library.upload("nothing.bin", "x.bin")
    assert "is not a file" in library.upload("dir", "x.bin")
    assert "more than the max_file_bytes option" in library.upload("big.bin", "x.bin")
    assert "must not contain" in library.upload("../x.bin", "x.bin")
    assert graph.calls("PUT") == [] and graph.calls("POST", "*createUploadSession") == []


def test_an_empty_file_is_uploaded_with_the_simple_request(library: Library) -> None:
    (library.workspace / "empty.txt").write_bytes(b"")
    assert content(library.upload("empty.txt", "empty.txt"))["size"] == 0
    assert library.graph.calls("POST", "*createUploadSession") == []


def test_upload_urls_are_redacted_from_errors(library: Library) -> None:
    (library.workspace / "v.bin").write_bytes(os.urandom(4 * 1024 * 1024 + 10))
    library.graph.fail("PUT", "/_upload/*", status=503, after=1, times=None, message="see http://x")
    result = library.upload("v.bin", "v.bin")
    session = library.graph.calls("PUT", "/_upload/*")[0].path.rsplit("/", 1)[-1]
    assert session not in result


# --------------------------------------------------------------------------- a token provider over real HTTP


class RefreshTokenProvider:
    """What a user-mode implementation looks like: a refresh-token grant against the token
    endpoint that stores the NEW refresh token every time and caches the access token."""

    kind = "user"

    def __init__(self, ctx: Any, refresh_token: str) -> None:
        self.refresh_token = refresh_token
        self.token_value: str | None = None
        self.expires = 0.0
        self.grants = 0

    def token(self, ctx: Any) -> str:
        import time

        if self.token_value and time.monotonic() < self.expires:
            return self.token_value
        url = ctx.value("AZURE_AUTHORITY_HOST")
        tenant = ctx.require("AZURE_TENANT_ID")
        form = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": ctx.require("AZURE_CLIENT_ID"),
                "refresh_token": self.refresh_token,
            }
        ).encode()
        response = send(
            ctx,
            "POST",
            f"{url}/{tenant}/oauth2/v2.0/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=form,
            authenticate=False,
        )
        data = response.json()
        if response.status != 200:
            raise ToolException(f"sign in again: {data['error_description'].splitlines()[0]}")
        self.grants += 1
        self.refresh_token = data["refresh_token"]
        self.token_value = data["access_token"]
        self.expires = time.monotonic() + 3000
        return self.token_value

    def invalidate(self, ctx: Any) -> None:
        self.token_value = None


def user_spec(holder: list[RefreshTokenProvider], refresh_token: str) -> ConnectorSpec:
    def build(ctx: Any) -> RefreshTokenProvider:
        provider = RefreshTokenProvider(ctx, refresh_token)
        holder.append(provider)
        return provider

    team = Param("team", "Team id.", location="path", required=True)
    ids = (
        EnvVar("AZURE_CLIENT_ID", "Client.", secret=False),
        EnvVar("AZURE_TENANT_ID", "Tenant.", secret=False),
    )
    return ConnectorSpec(
        name="msuser",
        title="Graph (user)",
        category="collaboration",
        summary="As a person.",
        base_url="{MS_GRAPH_URL}/v1.0",
        env=(_GRAPH_URL, _AUTHORITY),
        auth=(
            TokenAuth(
                build,
                kind="user",
                env=ids,
                token_urls=("{AZURE_AUTHORITY_HOST}/{AZURE_TENANT_ID}/oauth2/v2.0/token",),
            ),
        ),
        operations=(
            Operation("joined", "My teams.", path="/me/joinedTeams", items="value"),
            Operation(
                "channels",
                "Channels.",
                path="/teams/{team}/channels",
                params=(team,),
                items="value",
            ),
        ),
    )


def sign_in(graph: FakeGraph) -> tuple[Any, str]:
    app = graph.add_app(PUBLIC, public_client=True, scopes=("User.Read", "Channel.ReadBasic.All"))
    ada = graph.add_user("ada@contoso.test", name="Ada")
    graph.identity.device_pending_polls = 0
    start = graph.call(
        "POST",
        f"/{graph.tenant_id}/oauth2/v2.0/devicecode",
        form={"client_id": PUBLIC, "scope": "User.Read Channel.ReadBasic.All offline_access"},
    ).json()
    done = graph.call(
        "POST",
        f"/{graph.tenant_id}/oauth2/v2.0/token",
        form={"grant_type": DEVICE_GRANT, "client_id": PUBLIC, "device_code": start["device_code"]},
    ).json()
    assert ada.upn
    return app, str(done["refresh_token"])


def test_a_refresh_token_provider_plugs_into_the_engine_and_rotates_the_token(
    graph: FakeGraph,
) -> None:
    _, refresh = sign_in(graph)
    ada_team = graph.store("teams").add_team("Ops", graph.find_user("ada@contoso.test"))
    graph.store("teams").add_channel(ada_team, "General")
    holder: list[RefreshTokenProvider] = []
    env = {
        "AZURE_TENANT_ID": graph.tenant_id,
        "AZURE_CLIENT_ID": PUBLIC,
        "AZURE_AUTHORITY_HOST": graph.authority,
        "MS_GRAPH_URL": graph.graph_url,
    }
    built = connector_tools(user_spec(holder, refresh), env=env, config=graph.connector_options)
    ops = {t.name.removeprefix("msuser_"): t for t in built}
    channels = content(ops["channels"].invoke({"team": ada_team.id}))
    assert [c["displayName"] for c in channels["items"]] == ["General"]
    teams = content(ops["joined"].invoke({}))
    assert [t["displayName"] for t in teams["items"]] == ["Ops"]
    grant = graph.calls("POST", "/*/oauth2/v2.0/token")[-1].form()
    assert grant["grant_type"] == "refresh_token" and grant["refresh_token"] == refresh
    provider = holder[0]
    assert provider.refresh_token != refresh  # the new refresh token was kept
    assert "client_secret" not in grant  # a public client sends none
    claims = decode_jwt(graph.calls("GET", "/v1.0/teams/*")[-1].header("authorization").split()[1])
    assert claims["upn"] == "ada@contoso.test" and "roles" not in claims
    assert provider.grants == 1  # cached: one grant for the two calls


def test_when_the_server_expires_the_token_the_provider_is_asked_again(graph: FakeGraph) -> None:
    _, refresh = sign_in(graph)
    graph.store("teams").add_team("Ops", graph.find_user("ada@contoso.test"))
    holder: list[RefreshTokenProvider] = []
    env = {
        "AZURE_TENANT_ID": graph.tenant_id,
        "AZURE_CLIENT_ID": PUBLIC,
        "MS_GRAPH_URL": graph.graph_url,
        "AZURE_AUTHORITY_HOST": graph.authority,
    }
    ops = {
        t.name.removeprefix("msuser_"): t
        for t in connector_tools(
            user_spec(holder, refresh), env=env, config=graph.connector_options
        )
    }
    team = next(iter(graph.store("teams").teams))
    ops["channels"].invoke({"team": team})
    graph.clock.advance(3600)
    ops["channels"].invoke({"team": team})
    assert holder[0].grants == 2  # the 401 made the engine call invalidate() and ask again
    assert [r.status for r in graph.calls("GET", "/v1.0/teams/*")] == [200, 401, 200]


def test_a_revoked_refresh_token_becomes_a_plain_error_naming_the_next_step(
    graph: FakeGraph,
) -> None:
    _, refresh = sign_in(graph)
    graph.revoke_refresh_tokens()
    env = {
        "AZURE_TENANT_ID": graph.tenant_id,
        "AZURE_CLIENT_ID": PUBLIC,
        "MS_GRAPH_URL": graph.graph_url,
        "AZURE_AUTHORITY_HOST": graph.authority,
    }
    built = connector_tools(user_spec([], refresh), env=env, config=graph.connector_options)
    result = next(t for t in built if t.name.endswith("_joined")).invoke({})
    assert result.startswith("Error: sign in again: AADSTS50173")
    assert graph.calls("GET", "/v1.0/*") == []
