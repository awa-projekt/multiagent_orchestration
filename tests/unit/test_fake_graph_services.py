"""FakeGraph service plug-ins: files (SharePoint/OneDrive), mail, Teams and To Do.

Each test proves rules of the plug-in (`proves(...)`): the fake must refuse what Microsoft refuses,
because the connector tests that use it can only be as honest as these rules.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import pytest

from _helpers.fake_graph_proof import proves
from _helpers.fakegraph import files as fake_graph_files
from _helpers.fakegraph import teams as fake_graph_teams
from _helpers.fakegraph import todo as fake_graph_todo
from _helpers.fakegraph.server import V1, FakeGraph

CLIENT = "11111111-2222-3333-4444-555555555555"
SELECTED_APP = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CHUNK = fake_graph_files.CHUNK


@pytest.fixture
def graph() -> Iterator[FakeGraph]:
    with FakeGraph() as server:
        yield server


# --------------------------------------------------------------------------- SharePoint / files


class Site:
    """A site, its store and two apps: a tenant-wide one and a Sites.Selected one."""

    def __init__(self, graph: FakeGraph) -> None:
        self.graph = graph
        self.files: fake_graph_files.FilesStore = graph.store("files")
        self.site = self.files.add_site("contoso.sharepoint.com", "sites/awa-files")
        self.drive = self.files.drives[self.site.drive_id]
        wide = graph.add_app(CLIENT, "secret", roles=("Sites.ReadWrite.All", "Files.ReadWrite.All"))
        selected = graph.add_app(SELECTED_APP, "secret", roles=("Sites.Selected",))
        full = graph.add_app(
            "ffffffff-0000-0000-0000-000000000000", "secret", roles=("Sites.FullControl.All",)
        )
        self.wide = graph.app_token(wide)
        self.selected = graph.app_token(selected)
        self.full = graph.app_token(full)
        self.base = f"{V1}/sites/{self.site.id}/drive"

    def call(self, method: str, path: str, token: str | None = None, **kwargs: Any) -> Any:
        return self.graph.call(method, path, token=token or self.wide, **kwargs)


@pytest.fixture
def site(graph: FakeGraph) -> Site:
    return Site(graph)


@proves("sharepoint.sites_selected_cannot_search")
def test_search_needs_more_than_sites_selected(site: Site) -> None:
    site.files.add_site("contoso.sharepoint.com", "sites/other")
    ok = site.call("GET", f"{V1}/sites?search=awa")
    assert ok.status == 200 and [s["name"] for s in ok.json()["value"]] == ["awa-files"]
    denied = site.call("GET", f"{V1}/sites?search=awa", site.selected)
    assert denied.status == 403 and denied.error()[0] == "accessDenied"


@proves("sharepoint.sites_selected_needs_grant")
def test_a_sites_selected_app_reaches_only_the_site_it_was_granted(site: Site) -> None:
    url = f"{V1}/sites/contoso.sharepoint.com:/sites/awa-files"
    assert site.call("GET", url, site.selected).status == 403  # not granted yet
    site.files.grant(site.site, SELECTED_APP, "read")
    got = site.call("GET", url, site.selected)
    assert got.status == 200
    assert got.json()["id"] == site.site.id and got.json()["name"] == "awa-files"
    other = site.files.add_site("contoso.sharepoint.com", "sites/other")
    assert (
        site.call("GET", f"{V1}/sites/contoso.sharepoint.com:/sites/other", site.selected).status
        == 403
    )
    assert (
        site.call("GET", f"{V1}/sites/{other.id}/drive/root/children", site.selected).status == 403
    )
    assert (
        site.call("GET", f"{V1}/sites/{site.site.id}/drive/root/children", site.selected).status
        == 200
    )
    missing = site.call("GET", f"{V1}/sites/contoso.sharepoint.com:/sites/nothing", site.wide)
    assert missing.status == 404


@proves("sharepoint.sites_selected_needs_grant")
def test_a_read_grant_cannot_write_but_a_write_grant_can(site: Site) -> None:
    site.files.grant(site.site, SELECTED_APP, "read")
    put = f"{site.base}/root:/note.txt:/content"
    assert site.call("PUT", put, site.selected, data=b"x").status == 403
    site.files.grant(site.site, SELECTED_APP, "write")
    assert site.call("PUT", put, site.selected, data=b"x").status == 201


@proves("sharepoint.grant_input_is_grantedToIdentities")
def test_the_admin_grant_call_and_its_input_rules(site: Site) -> None:
    url = f"{V1}/sites/{site.site.id}/permissions"
    body = {
        "roles": ["write"],
        "grantedToIdentities": [{"application": {"id": SELECTED_APP, "displayName": "awa"}}],
    }
    assert site.call("POST", url, site.selected, json_body=body).status == 403  # not an admin
    # .All is not FullControl
    assert site.call("POST", url, site.wide, json_body=body).status == 403
    v2 = {"roles": ["write"], "grantedToIdentitiesV2": body["grantedToIdentities"]}
    assert site.call("POST", url, site.full, json_body=v2).status == 400
    for bad in (
        {"roles": ["manage"], "grantedToIdentities": body["grantedToIdentities"]},  # older name
        {"roles": ["read"]},
        {"roles": [], "grantedToIdentities": body["grantedToIdentities"]},
        {"roles": ["read"], "grantedToIdentities": [{"application": {}}]},
        {"roles": ["read"], "grantedToIdentities": [*body["grantedToIdentities"], {}]},
    ):
        assert site.call("POST", url, site.full, json_body=bad).status == 400
    assert site.files.grants == {}  # a refused call grants nothing
    created = site.call("POST", url, site.full, json_body=body)
    assert created.status == 201
    answer = created.json()
    assert answer["roles"] == ["write"]
    assert answer["grantedToIdentitiesV2"][0]["application"]["id"] == SELECTED_APP
    assert (
        site.call("PUT", f"{site.base}/root:/n.txt:/content", site.selected, data=b"1").status
        == 201
    )


@proves("files.content_redirects_without_credentials")
def test_content_is_a_302_to_a_url_that_refuses_an_authorization_header(site: Site) -> None:
    site.files.put_file(site.site, "docs/report.csv", b"a,b\n1,2\n")
    redirect = site.call("GET", f"{site.base}/root:/docs/report.csv:/content")
    assert redirect.status == 302
    location = redirect.headers["location"]
    assert location.startswith(site.graph.url + "/_download/")
    assert site.graph.call("GET", location, token=site.wide).status == 401
    plain = site.graph.call("GET", location)
    assert plain.status == 200 and plain.body == b"a,b\n1,2\n"
    item = site.call("GET", f"{site.base}/root:/docs/report.csv").json()
    assert item["@microsoft.graph.downloadUrl"].startswith(site.graph.url + "/_download/")
    assert site.graph.call("GET", item["@microsoft.graph.downloadUrl"]).body == b"a,b\n1,2\n"
    by_id = site.call("GET", f"{V1}/drives/{site.drive.id}/items/{item['id']}/content")
    assert by_id.status == 302


@proves("files.content_redirects_without_credentials")
def test_a_download_url_expires_and_a_folder_has_no_content(site: Site) -> None:
    site.files.put_file(site.site, "a.txt", b"x")
    location = site.call("GET", f"{site.base}/root:/a.txt:/content").headers["location"]
    folder = site.files.add_folder(site.site, "docs")
    assert site.call("GET", f"{site.base}/items/{folder['id']}/content").status == 404
    site.graph.clock.advance(3601)
    assert site.graph.call("GET", location).status == 401
    assert site.graph.call("GET", site.graph.url + "/_download/unknown").status == 401


def test_the_download_url_can_live_on_another_host_name(site: Site) -> None:
    site.files.download_host = "localhost"
    site.files.put_file(site.site, "a.txt", b"x")
    location = site.call("GET", f"{site.base}/root:/a.txt:/content").headers["location"]
    assert location.startswith("http://localhost:")


@proves("files.conflict_behavior", "files.if_match_etag")
def test_simple_put_creates_replaces_and_follows_the_conflict_behavior(site: Site) -> None:
    put = f"{site.base}/root:/Reports/q3.txt:/content"
    created = site.call("PUT", put, data=b"one")
    assert created.status == 201
    item = created.json()
    assert item["name"] == "q3.txt" and item["size"] == 3
    assert item["eTag"].startswith('"{') and item["file"]["mimeType"]
    replaced = site.call("PUT", put, data=b"three")
    assert replaced.status == 200 and replaced.json()["id"] == item["id"]
    assert replaced.json()["eTag"] != item["eTag"] and replaced.json()["size"] == 5
    failed = site.call("PUT", put + "?@microsoft.graph.conflictBehavior=fail", data=b"x")
    assert failed.status == 409 and failed.error()[0] == "nameAlreadyExists"
    renamed = site.call("PUT", put + "?@microsoft.graph.conflictBehavior=rename", data=b"x")
    assert renamed.status == 201 and renamed.json()["name"] == "q3 1.txt"
    bad = site.call("PUT", put + "?@microsoft.graph.conflictBehavior=nope", data=b"x")
    assert bad.status == 400
    assert site.files.content(site.site, "Reports/q3.txt") == b"three"
    on_root = site.call("PUT", f"{site.base}/root/content", data=b"x")
    assert on_root.status == 400 and on_root.error()[0] == "invalidRequest"
    below_a_file = site.call("PUT", f"{site.base}/root:/Reports/q3.txt/x.txt:/content", data=b"x")
    assert below_a_file.status == 409 and below_a_file.error()[0] == "nameAlreadyExists"


@proves("files.if_match_etag")
def test_if_match_protects_against_a_concurrent_edit_and_a_lock_is_423(site: Site) -> None:
    put = f"{site.base}/root:/book.xlsx:/content"
    first = site.call("PUT", put, data=b"v1").json()
    ok = site.call("PUT", put, data=b"v2", headers={"If-Match": first["eTag"]})
    assert ok.status == 200
    stale = site.call("PUT", put, data=b"v3", headers={"If-Match": first["eTag"]})
    assert stale.status == 412 and stale.error()[0] == "preconditionFailed"
    assert site.files.content(site.site, "book.xlsx") == b"v2"
    site.files.lock(first["id"])
    locked = site.call("PUT", put, data=b"v4")
    assert locked.status == 423 and locked.error()[0] == "resourceLocked"
    site.files.unlock(first["id"])
    assert site.call("PUT", put, data=b"v4").status == 200


@proves("files.simple_put_conservative_limit")
def test_a_simple_put_above_four_mib_is_refused_unless_the_limit_is_lifted(site: Site) -> None:
    put = f"{site.base}/root:/big.bin:/content"
    too_big = site.call("PUT", put, data=b"x" * (4 * 1024 * 1024 + 1))
    assert too_big.status == 413 and too_big.error()[0] == "requestEntityTooLarge"
    assert site.call("PUT", put, data=b"x" * (4 * 1024 * 1024)).status == 201
    site.files.simple_put_limit = None
    assert site.call("PUT", put, data=b"x" * (5 * 1024 * 1024)).status == 200


@proves("files.invalid_names")
def test_names_with_forbidden_characters_are_refused(site: Site) -> None:
    for name in ("a*b.txt", "what?.txt", "a|b", "a<b", 'q".txt'):
        reply = site.call("PUT", f"{site.base}/root:/{quote(name)}:/content", data=b"x")
        assert reply.status == 400, name
    folder = site.call(
        "POST", f"{site.base}/root/children", json_body={"name": "a:b", "folder": {}}
    )
    assert folder.status == 400 and folder.error()[0] == "invalidRequest"


@proves("files.conflict_behavior")
def test_create_folder_follows_the_conflict_behavior(site: Site) -> None:
    url = f"{site.base}/root/children"
    made = site.call("POST", url, json_body={"name": "inbox", "folder": {}})
    assert made.status == 201 and "folder" in made.json()
    again = site.call("POST", url, json_body={"name": "inbox", "folder": {}})
    assert again.status == 409 and again.error()[0] == "nameAlreadyExists"
    renamed = site.call(
        "POST",
        url,
        json_body={"name": "inbox", "folder": {}, "@microsoft.graph.conflictBehavior": "rename"},
    )
    assert renamed.json()["name"] == "inbox 1"
    no_facet = site.call("POST", url, json_body={"name": "x"})
    assert no_facet.status == 400


@proves("files.children_top_capped_at_200")
def test_children_are_listed_by_name_capped_at_200_and_paged(site: Site) -> None:
    for number in range(205):
        site.files.put_file(site.site, f"many/f{number:03d}.txt", b"x")
    folder = site.files.folder_path(site.drive, "many")
    url = f"{site.base}/items/{folder['id']}/children"
    assert len(site.call("GET", url).json()["value"]) == 200
    capped = site.call("GET", url + "?$top=999").json()
    assert len(capped["value"]) == 200
    assert capped["@odata.nextLink"].startswith(site.graph.url + V1)
    rest = site.call("GET", capped["@odata.nextLink"]).json()
    assert [i["name"] for i in rest["value"]] == [f"f{n:03d}.txt" for n in range(200, 205)]
    small = site.call("GET", url + "?$top=3&$select=name,size").json()
    assert [set(i) for i in small["value"]] == [{"id", "name", "size"}] * 3
    unknown = site.call("GET", url + "?$select=nope")
    assert unknown.status == 400
    assert site.call("GET", f"{site.base}/items/root/children").status == 200


def start_session(
    site: Site, path: str = "root:/video.bin:/createUploadSession", **body: Any
) -> Any:
    return site.call("POST", f"{site.base}/{path}", json_body=body or {"item": {}})


def fragment(site: Site, url: str, data: bytes, first: int, total: int, **kwargs: Any) -> Any:
    last = first + len(data) - 1
    headers = {"Content-Range": f"bytes {first}-{last}/{total}", **kwargs.pop("headers", {})}
    return site.graph.call("PUT", url, data=data, headers=headers, **kwargs)


@proves("files.upload_fragment_rules", "files.upload_url_rejects_authorization")
def test_an_upload_session_takes_320_kib_fragments_and_assembles_the_file(site: Site) -> None:
    size = CHUNK * 2 + 1000  # a bit over 2 chunks
    content = (bytes(range(256)) * (size // 256 + 1))[:size]
    created = start_session(site, item={"@microsoft.graph.conflictBehavior": "replace"})
    assert created.status == 200
    body = created.json()
    assert body["uploadUrl"].startswith(site.graph.url + "/_upload/")
    assert body["nextExpectedRanges"] == ["0-"] and body["expirationDateTime"]
    url = body["uploadUrl"]
    first = fragment(site, url, content[:CHUNK], 0, len(content))
    assert first.status == 202 and first.json()["nextExpectedRanges"] == [f"{CHUNK}-"]
    assert site.graph.call("GET", url).json()["nextExpectedRanges"] == [f"{CHUNK}-"]
    second = fragment(site, url, content[CHUNK : 2 * CHUNK], CHUNK, len(content))
    assert second.status == 202
    last = fragment(site, url, content[2 * CHUNK :], 2 * CHUNK, len(content))
    assert last.status == 201
    item = last.json()
    assert item["name"] == "video.bin" and item["size"] == len(content)
    stored = site.files.content(site.site, "video.bin")
    assert hashlib.sha256(stored).hexdigest() == hashlib.sha256(content).hexdigest()
    assert fragment(site, url, b"x", 0, 1).status == 404  # the session is over


@proves("files.upload_url_rejects_authorization")
def test_the_upload_url_refuses_an_authorization_header(site: Site) -> None:
    url = start_session(site).json()["uploadUrl"]
    data = b"x" * 100
    refused = fragment(site, url, data, 0, 100, token=site.wide)
    assert refused.status == 401 and refused.error()[0] == "InvalidAuthenticationToken"
    assert fragment(site, url, data, 0, 100).status == 201  # the same fragment without it


@proves("files.upload_fragment_rules")
def test_fragment_rules_content_range_order_multiples_and_size(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    total = CHUNK * 3
    url = start_session(site).json()["uploadUrl"]
    no_range = site.graph.call("PUT", url, data=b"x" * CHUNK)
    assert no_range.status == 400 and no_range.error()[0] == "invalidRequest"
    odd = fragment(site, url, b"x" * (CHUNK - 1), 0, total)
    assert odd.status == 400 and "320 KiB" in odd.error()[1]
    mismatch = site.graph.call(
        "PUT", url, data=b"x" * 10, headers={"Content-Range": f"bytes 0-{CHUNK - 1}/{total}"}
    )
    assert mismatch.status == 400
    assert fragment(site, url, b"x" * CHUNK, 0, total).status == 202
    skipped = fragment(site, url, b"x" * CHUNK, 2 * CHUNK, total)
    assert skipped.status == 416 and skipped.error()[0] == "invalidRange"
    assert skipped.json()["error"]["nextExpectedRanges"] == [f"{CHUNK}-"]
    changed_total = fragment(site, url, b"x" * CHUNK, CHUNK, total + 1)
    assert changed_total.status == 416
    monkeypatch.setattr(fake_graph_files, "MAX_FRAGMENT", CHUNK)
    too_big = fragment(site, url, b"x" * (2 * CHUNK), CHUNK, total)
    assert too_big.status == 413


@proves("files.upload_fragment_rules")
def test_an_upload_session_expires_on_the_clock_and_can_be_cancelled(site: Site) -> None:
    url = start_session(site).json()["uploadUrl"]
    assert site.graph.call("DELETE", url).status == 204
    assert site.graph.call("GET", url).status == 404
    other = start_session(site).json()["uploadUrl"]
    site.graph.clock.advance(86_401)
    assert fragment(site, other, b"x", 0, 1).status == 404


@proves("files.conflict_behavior")
def test_the_conflict_behavior_of_a_session_applies_when_the_last_fragment_arrives(
    site: Site,
) -> None:
    site.files.put_file(site.site, "video.bin", b"old")
    url = start_session(site, item={"@microsoft.graph.conflictBehavior": "fail"}).json()[
        "uploadUrl"
    ]
    failed = fragment(site, url, b"new", 0, 3)
    assert failed.status == 409
    assert site.files.content(site.site, "video.bin") == b"old"
    url = start_session(site, item={"@microsoft.graph.conflictBehavior": "rename"}).json()[
        "uploadUrl"
    ]
    renamed = fragment(site, url, b"new", 0, 3)
    assert renamed.status == 201 and renamed.json()["name"] == "video 1.bin"
    url = start_session(site).json()["uploadUrl"]  # default: replace
    replaced = fragment(site, url, b"newer", 0, 5)
    assert replaced.status == 200 and site.files.content(site.site, "video.bin") == b"newer"


@proves("files.upload_fragment_rules")
def test_a_session_needs_write_access_and_valid_names(site: Site) -> None:
    site.files.grant(site.site, SELECTED_APP, "read")
    denied = site.call(
        "POST",
        f"{site.base}/root:/x.bin:/createUploadSession",
        site.selected,
        json_body={"item": {}},
    )
    assert denied.status == 403
    bad = site.call("POST", f"{site.base}/root:/a*b:/createUploadSession", json_body={"item": {}})
    assert bad.status == 400


@proves("files.drive_search_excludes_sites_selected")
def test_drive_search_matches_names_and_is_closed_to_an_app_with_only_sites_selected(
    site: Site,
) -> None:
    site.files.put_file(site.site, "Reports/Invoice 2026.xlsx", b"x")
    site.files.put_file(site.site, "Reports/it's mine.txt", b"x")
    found = site.call("GET", f"{site.base}/root/search(q='invoice')")
    assert found.status == 200
    assert [i["name"] for i in found.json()["value"]] == ["Invoice 2026.xlsx"]
    assert "searchResult" in found.json()["value"][0]
    quoted = site.call("GET", f"{site.base}/root/search(q='it''s')")
    assert [i["name"] for i in quoted.json()["value"]] == ["it's mine.txt"]
    site.files.grant(site.site, SELECTED_APP, "write")  # even a granted site: not supported
    denied = site.call("GET", f"{site.base}/root/search(q='invoice')", site.selected)
    assert denied.status == 403 and denied.error()[0] == "accessDenied"


@proves("files.session_honours_if_match")
def test_a_session_checks_if_match_and_if_none_match_before_any_byte(site: Site) -> None:
    put = f"{site.base}/root:/book.xlsx:/content"
    etag = site.call("PUT", put, data=b"v1").json()["eTag"]
    opening = f"{site.base}/root:/book.xlsx:/createUploadSession"
    body = {"item": {"@microsoft.graph.conflictBehavior": "replace"}}
    ok = site.call("POST", opening, json_body=body, headers={"If-Match": etag})
    assert ok.status == 200 and ok.json()["uploadUrl"]
    stale = site.call("POST", opening, json_body=body, headers={"If-Match": '"{nope},1"'})
    assert stale.status == 412 and stale.error()[0] == "preconditionFailed"
    same = site.call("POST", opening, json_body=body, headers={"If-None-Match": etag})
    assert same.status == 412
    other = site.call("POST", opening, json_body=body, headers={"If-None-Match": '"{other},1"'})
    assert other.status == 200
    missing = site.call(
        "POST",
        f"{site.base}/root:/new.xlsx:/createUploadSession",
        json_body=body,
        headers={"If-Match": etag},
    )
    assert missing.status == 412  # nothing there to match


# --------------------------------------------------------------------------- mail


class Mailbox:
    def __init__(self, graph: FakeGraph) -> None:
        self.graph = graph
        self.mail = graph.store("mail")
        self.ada = graph.add_user("ada@contoso.test", name="Ada")
        self.bob = graph.add_user("bob@contoso.test", name="Bob")
        self.read = graph.add_app(CLIENT, "s", roles=("Mail.Read",))
        self.rw = graph.add_app("rw", "s", roles=("Mail.ReadWrite", "Mail.Send"))
        self.client = graph.add_app(
            "delegated", public_client=True, scopes=("Mail.Read", "Mail.ReadWrite")
        )

    def get(self, path: str, app: Any = None) -> Any:
        return self.graph.call("GET", f"{V1}{path}", token=self.graph.app_token(app or self.read))


@pytest.fixture
def mailbox(graph: FakeGraph) -> Mailbox:
    return Mailbox(graph)


@proves("graph.me_needs_delegated_token")
def test_me_needs_a_delegated_token(mailbox: Mailbox) -> None:
    mailbox.mail.add_message(mailbox.ada, subject="hi")
    app_call = mailbox.get("/me/mailFolders/inbox/messages")
    assert app_call.status == 400
    assert app_call.error() == (
        "BadRequest",
        "/me request is only valid with delegated authentication flow.",
    )
    token = mailbox.graph.issue_token(
        mailbox.client, kind="user", scopes=["Mail.Read"], user=mailbox.ada
    )
    ok = mailbox.graph.call("GET", f"{V1}/me/mailFolders/inbox/messages", token=token)
    assert ok.status == 200 and ok.json()["value"][0]["subject"] == "hi"


@proves("outlook.other_mailbox_needs_shared_scope")
def test_a_user_token_reaches_another_mailbox_only_with_a_shared_scope(mailbox: Mailbox) -> None:
    url = f"{V1}/users/bob@contoso.test/mailFolders/inbox/messages"
    own = mailbox.graph.issue_token(
        mailbox.client, kind="user", scopes=["Mail.Read"], user=mailbox.ada
    )
    assert mailbox.graph.call("GET", url, token=own).status == 403
    shared = mailbox.graph.issue_token(
        mailbox.client, kind="user", scopes=["Mail.Read", "Mail.Read.Shared"], user=mailbox.ada
    )
    # Mail.Read.Shared is not in the route's list, so Mail.Read passes and the scope decides
    assert mailbox.graph.call("GET", url, token=shared).status == 200


@proves("outlook.unknown_mailbox")
def test_an_unknown_mailbox_is_404_error_invalid_user(mailbox: Mailbox) -> None:
    reply = mailbox.get("/users/nobody@contoso.test/mailFolders/inbox/messages")
    assert reply.status == 404 and reply.error()[0] == "ErrorInvalidUser"
    assert mailbox.get("/users/ada@contoso.test/mailFolders/spam/messages").status == 404


@proves("outlook.rbac_roles_not_in_token")
def test_exchange_rbac_scopes_a_role_to_one_mailbox_and_never_shows_in_the_token(
    mailbox: Mailbox,
) -> None:
    mailbox.mail.add_message(mailbox.ada, subject="scoped")
    mailbox.mail.add_message(mailbox.bob, subject="other")
    scoped = mailbox.graph.add_app("rbac", "s")  # no Mail.* role consented in Entra
    scoped.rbac["Application Mail.Read"] = {"ada@contoso.test"}
    token = mailbox.graph.app_token(scoped)
    from _helpers.fakegraph.core import decode_jwt

    assert "roles" not in decode_jwt(token)
    ada = mailbox.graph.call(
        "GET", f"{V1}/users/ada@contoso.test/mailFolders/inbox/messages", token=token
    )
    assert ada.status == 200 and ada.json()["value"][0]["subject"] == "scoped"
    bob = mailbox.graph.call(
        "GET", f"{V1}/users/bob@contoso.test/mailFolders/inbox/messages", token=token
    )
    assert bob.status == 403 and bob.error()[0] == "ErrorAccessDenied"
    write = mailbox.graph.call(
        "PATCH",
        f"{V1}/users/ada@contoso.test/messages/{mailbox.mail.box(mailbox.ada.id)['inbox'][0]['id']}",
        token=token,
        json_body={"isRead": True},
    )
    assert write.status == 403  # only Application Mail.Read was assigned


@proves("outlook.prefer_body_content_type")
def test_prefer_text_converts_an_html_body(mailbox: Mailbox) -> None:
    message = mailbox.mail.add_message(
        mailbox.ada, subject="html", body="<p>Hello <b>world</b></p>", html=True
    )
    path = f"{V1}/users/ada@contoso.test/messages/{message['id']}"
    token = mailbox.graph.app_token(mailbox.read)
    html = mailbox.graph.call("GET", path, token=token).json()
    assert html["body"]["contentType"] == "html"
    text = mailbox.graph.call(
        "GET", path, token=token, headers={"Prefer": 'outlook.body-content-type="text"'}
    )
    assert text.json()["body"] == {"contentType": "text", "content": "Hello world"}
    assert text.headers["preference-applied"] == 'outlook.body-content-type="text"'
    listed = mailbox.graph.call(
        "GET",
        f"{V1}/users/ada@contoso.test/mailFolders/inbox/messages",
        token=token,
        headers={"Prefer": 'outlook.body-content-type="text"'},
    ).json()["value"][0]
    assert listed["body"]["contentType"] == "text"


@proves("outlook.sendmail_shape")
def test_send_mail_is_202_empty_validates_recipients_and_delivers_to_known_mailboxes(
    mailbox: Mailbox,
) -> None:
    token = mailbox.graph.app_token(mailbox.rw)
    url = f"{V1}/users/ada@contoso.test/sendMail"
    message = {
        "subject": "Report",
        "body": {"contentType": "Text", "content": "numbers"},
        "toRecipients": [{"emailAddress": {"address": "bob@contoso.test"}}],
    }
    sent = mailbox.graph.call(
        "POST", url, token=token, json_body={"message": message, "saveToSentItems": True}
    )
    assert sent.status == 202 and sent.body == b""
    assert mailbox.mail.box(mailbox.ada.id)["sentitems"][0]["subject"] == "Report"
    delivered = mailbox.mail.box(mailbox.bob.id)["inbox"][0]
    assert (
        delivered["subject"] == "Report"
        and delivered["from"]["emailAddress"]["address"] == "ada@contoso.test"
    )
    mailbox.graph.call(
        "POST", url, token=token, json_body={"message": message, "saveToSentItems": False}
    )
    assert len(mailbox.mail.box(mailbox.ada.id)["sentitems"]) == 1
    for bad in (
        {"message": {"subject": "x"}},
        {"message": {"toRecipients": []}},
        {"message": {"toRecipients": [{"emailAddress": {"name": "no address"}}]}},
        {"message": {"toRecipients": [{"emailAddress": {"address": "not-an-address"}}]}},
        {"toRecipients": [{"emailAddress": {"address": "a@b.test"}}]},
    ):
        reply = mailbox.graph.call("POST", url, token=token, json_body=bad)
        assert reply.status == 400, bad
    assert reply.error()[0] in ("ErrorInvalidRequest", "ErrorInvalidRecipients")
    big = base64.b64encode(b"x" * (3 * 1024 * 1024 + 10)).decode()
    message["attachments"] = [
        {"@odata.type": "#microsoft.graph.fileAttachment", "name": "a.bin", "contentBytes": big}
    ]
    too_big = mailbox.graph.call("POST", url, token=token, json_body={"message": message})
    assert too_big.status == 413 and too_big.error()[0] == "ErrorMessageSizeExceeded"
    small = base64.b64encode(b"hello").decode()
    message["attachments"] = [
        {"@odata.type": "#microsoft.graph.fileAttachment", "name": "a.txt", "contentBytes": small}
    ]
    assert (
        mailbox.graph.call("POST", url, token=token, json_body={"message": message}).status == 202
    )
    for copy in (
        mailbox.mail.box(mailbox.ada.id)["sentitems"][0],
        mailbox.mail.box(mailbox.bob.id)["inbox"][0],
    ):
        assert copy["hasAttachments"] is True
        assert [(a["name"], a["_bytes"]) for a in copy["_attachments"]] == [("a.txt", b"hello")]


@proves("outlook.sendmail_shape")
def test_send_mail_needs_the_send_role(mailbox: Mailbox) -> None:
    reply = mailbox.graph.call(
        "POST",
        f"{V1}/users/ada@contoso.test/sendMail",
        token=mailbox.graph.app_token(mailbox.read),
        json_body={"message": {"toRecipients": [{"emailAddress": {"address": "a@b.test"}}]}},
    )
    assert reply.status == 403 and reply.error()[0] == "ErrorAccessDenied"


@proves("outlook.mark_read_needs_readwrite")
def test_marking_a_message_read_needs_mail_readwrite(mailbox: Mailbox) -> None:
    message = mailbox.mail.add_message(mailbox.ada, subject="unread")
    url = f"{V1}/users/ada@contoso.test/messages/{message['id']}"
    refused = mailbox.graph.call(
        "PATCH", url, token=mailbox.graph.app_token(mailbox.read), json_body={"isRead": True}
    )
    assert refused.status == 403 and refused.error()[0] == "ErrorAccessDenied"
    assert message["isRead"] is False
    done = mailbox.graph.call(
        "PATCH", url, token=mailbox.graph.app_token(mailbox.rw), json_body={"isRead": True}
    )
    assert done.status == 200 and done.json()["isRead"] is True and message["isRead"] is True
    missing = mailbox.graph.call(
        "PATCH",
        f"{V1}/users/ada@contoso.test/messages/AAMkNOPE",
        token=mailbox.graph.app_token(mailbox.rw),
        json_body={"isRead": True},
    )
    assert missing.status == 404 and missing.error()[0] == "ErrorItemNotFound"


def test_a_folder_reports_its_counts(mailbox: Mailbox) -> None:
    mailbox.mail.add_message(mailbox.ada, subject="a")
    mailbox.mail.add_message(mailbox.ada, subject="b", is_read=True)
    info = mailbox.get("/users/ada@contoso.test/mailFolders/inbox?$select=totalItemCount").json()
    assert info["totalItemCount"] == 2 and "unreadItemCount" not in info


# --------------------------------------------------------------------------- Teams


class Teams:
    def __init__(self, graph: FakeGraph) -> None:
        self.graph = graph
        self.store: fake_graph_teams.TeamsStore = graph.store("teams")
        self.ada = graph.add_user("ada@contoso.test", name="Ada")
        self.eve = graph.add_user("eve@contoso.test", name="Eve")
        self.team = self.store.add_team("Ops", self.ada)
        self.channel = self.store.add_channel(self.team, "General")
        self.app = graph.add_app(
            CLIENT,
            "s",
            roles=("ChannelMessage.Read.All", "Teamwork.Migrate.All", "Channel.ReadBasic.All"),
        )
        self.delegated = graph.add_app(
            "bot",
            public_client=True,
            scopes=(
                "ChannelMessage.Send",
                "ChannelMessage.Read.All",
                "Team.ReadBasic.All",
                "Channel.ReadBasic.All",
            ),
        )
        self.url = f"{V1}/teams/{self.team.id}/channels/{self.channel.id}/messages"

    def user_token(self, user: Any = None, *scopes: str) -> str:
        return self.graph.issue_token(
            self.delegated,
            kind="user",
            user=user or self.ada,
            scopes=list(scopes or ("ChannelMessage.Send", "ChannelMessage.Read.All")),
        )


@pytest.fixture
def teams(graph: FakeGraph) -> Teams:
    return Teams(graph)


def test_a_repeated_team_or_channel_name_is_refused_instead_of_replacing_it(teams: Teams) -> None:
    with pytest.raises(ValueError, match="Ops"):
        teams.store.add_team("Ops")
    with pytest.raises(ValueError, match="General"):
        teams.store.add_channel(teams.team, "General")
    assert list(teams.team.channels) == [teams.channel.id]


def test_the_teams_plug_in_wraps_the_token_route_whatever_the_plug_in_order() -> None:
    graph = FakeGraph(plugins=[fake_graph_teams])
    token_routes = [
        route
        for route in graph.routes
        if route.plugin == "identity"
        and route.method == "POST"
        and route.template.endswith("/token")
    ]
    assert token_routes
    assert all(route.handler.__module__ == fake_graph_teams.__name__ for route in token_routes)


def card(text: str = "hello") -> dict[str, Any]:
    content = {
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": [{"type": "TextBlock", "text": text}],
    }
    return {
        "body": {"contentType": "html", "content": '<attachment id="1"></attachment>'},
        "attachments": [
            {"id": "1", "contentType": fake_graph_teams.ADAPTIVE, "content": json.dumps(content)}
        ],
    }


@proves("teams.app_only_post_forbidden")
def test_an_app_token_cannot_post_a_channel_message_even_with_the_migration_role(
    teams: Teams,
) -> None:
    token = teams.graph.app_token(teams.app)
    reply = teams.graph.call("POST", teams.url, token=token, json_body={"body": {"content": "hi"}})
    assert reply.status == 403
    code, message = reply.error()
    assert code == "Forbidden" and message.startswith(
        "Message POST is allowed in application-only context"
    )
    assert teams.channel.messages == []
    reply = teams.graph.call(
        "POST",
        teams.url + f"/{teams.store.add_message(teams.channel, 'x')['id']}/replies",
        token=token,
        json_body={"body": {"content": "hi"}},
    )
    assert reply.status == 403


@proves("teams.app_only_post_forbidden")
def test_a_delegated_post_needs_the_scope_and_membership(teams: Teams) -> None:
    body = {"body": {"content": "hello team"}}
    created = teams.graph.call("POST", teams.url, token=teams.user_token(), json_body=body)
    assert created.status == 201
    message = created.json()
    assert (
        message["from"]["user"]["displayName"] == "Ada" and message["from"]["application"] is None
    )
    assert (
        message["body"]["content"] == "hello team"
        and message["channelIdentity"]["channelId"] == teams.channel.id
    )
    stranger = teams.graph.call(
        "POST", teams.url, token=teams.user_token(teams.eve), json_body=body
    )
    assert stranger.status == 403 and stranger.error()[0] == "Forbidden"
    read_only = teams.graph.call(
        "POST",
        teams.url,
        token=teams.user_token(teams.ada, "ChannelMessage.Read.All"),
        json_body=body,
    )
    assert read_only.status == 403 and "ChannelMessage.Send" in read_only.error()[1]
    unknown = teams.graph.call(
        "POST", f"{V1}/teams/nope/channels/x/messages", token=teams.user_token(), json_body=body
    )
    assert unknown.status == 404


@proves("teams.adaptive_card_attachment_shape")
def test_an_adaptive_card_needs_a_string_content_and_a_matching_attachment_tag(
    teams: Teams,
) -> None:
    token = teams.user_token()
    ok = teams.graph.call("POST", teams.url, token=token, json_body=card())
    assert ok.status == 201 and ok.json()["attachments"][0]["id"] == "1"
    as_object = card()
    as_object["attachments"][0]["content"] = json.loads(as_object["attachments"][0]["content"])
    wrong_type = card()
    wrong_type["attachments"][0]["content"] = json.dumps({"type": "Other"})
    broken = card()
    broken["attachments"][0]["content"] = "{not json"
    no_tag = card()
    no_tag["body"]["content"] = "no placeholder"
    stray_tag = card()
    stray_tag["body"]["content"] += '<attachment id="2"></attachment>'
    no_id = card()
    del no_id["attachments"][0]["id"]
    empty = {"body": {"content": "  "}}
    bad_type = {"body": {"contentType": "markdown", "content": "x"}}
    mention_missing = {"body": {"contentType": "html", "content": '<at id="0">Eve</at> hi'}}
    for bad in (
        as_object,
        wrong_type,
        broken,
        no_tag,
        stray_tag,
        no_id,
        empty,
        bad_type,
        mention_missing,
    ):
        reply = teams.graph.call("POST", teams.url, token=token, json_body=bad)
        assert reply.status == 400 and reply.error()[0] == "BadRequest", bad
    mention = {
        "body": {"contentType": "html", "content": '<at id="0">Eve</at> hi'},
        "mentions": [{"id": 0, "mentionText": "Eve", "mentioned": {"user": {"id": teams.eve.id}}}],
    }
    assert teams.graph.call("POST", teams.url, token=token, json_body=mention).status == 201
    assert len(teams.channel.messages) == 2


@proves("teams.app_only_read_is_protected_api")
def test_app_only_reading_needs_the_protected_api_approval(teams: Teams) -> None:
    teams.store.add_message(teams.channel, "first", sender=teams.ada)
    token = teams.graph.app_token(teams.app)
    refused = teams.graph.call("GET", teams.url, token=token)
    assert refused.status == 403 and "protected API" in refused.error()[1]
    teams.store.approve_protected_api()
    assert teams.graph.call("GET", teams.url, token=token).status == 200
    delegated = teams.graph.call("GET", teams.url, token=teams.user_token())
    assert delegated.status == 200  # a delegated read never needed the approval


@proves("teams.channel_messages_top_max_50")
def test_channel_messages_reject_top_above_50_and_filter_orderby_and_page_newest_first(
    teams: Teams,
) -> None:
    for number in range(55):
        teams.store.add_message(teams.channel, f"m{number}", sender=teams.ada)
        teams.graph.clock.advance(1)
    token = teams.user_token()
    default = teams.graph.call("GET", teams.url, token=token).json()
    assert len(default["value"]) == 20 and default["value"][0]["body"]["content"] == "m54"
    top = teams.graph.call("GET", teams.url + "?$top=51", token=token)
    assert top.status == 400 and "between 1 and 50" in top.error()[1]
    fifty = teams.graph.call("GET", teams.url + "?$top=50", token=token).json()
    assert len(fifty["value"]) == 50
    rest = teams.graph.call("GET", fifty["@odata.nextLink"], token=token).json()
    assert [m["body"]["content"] for m in rest["value"]] == [f"m{n}" for n in range(4, -1, -1)]
    for option in ("$filter=createdDateTime gt 2026-01-01T00:00:00Z", "$orderby=createdDateTime"):
        assert teams.graph.call("GET", teams.url + "?" + option, token=token).status == 400
    assert teams.graph.call("GET", teams.url + "?$expand=attachments", token=token).status == 400


def test_replies_come_from_the_replies_route_or_expand(teams: Teams) -> None:
    root = teams.store.add_message(teams.channel, "question", sender=teams.ada)
    teams.store.add_message(teams.channel, "answer", sender=teams.ada, in_reply_to=root["id"])
    token = teams.user_token()
    plain = teams.graph.call("GET", teams.url, token=token).json()["value"][0]
    assert "replies" not in plain
    answer = teams.graph.call("GET", teams.url + "?$expand=replies", token=token)
    expanded = answer.json()["value"][0]
    assert [r["body"]["content"] for r in expanded["replies"]] == ["answer"]
    listed = teams.graph.call("GET", f"{teams.url}/{root['id']}/replies", token=token).json()
    assert [r["replyToId"] for r in listed["value"]] == [root["id"]]
    posted = teams.graph.call(
        "POST",
        f"{teams.url}/{root['id']}/replies",
        token=token,
        json_body={"body": {"content": "thanks"}},
    )
    assert posted.status == 201 and posted.json()["replyToId"] == root["id"]
    assert (
        teams.graph.call(
            "POST", f"{teams.url}/999/replies", token=token, json_body={"body": {"content": "x"}}
        ).status
        == 404
    )


def test_joined_teams_and_channels_follow_membership(teams: Teams) -> None:
    mine = teams.graph.call(
        "GET", f"{V1}/me/joinedTeams", token=teams.user_token(teams.ada, "Team.ReadBasic.All")
    )
    assert [t["displayName"] for t in mine.json()["value"]] == ["Ops"]
    none = teams.graph.call(
        "GET", f"{V1}/me/joinedTeams", token=teams.user_token(teams.eve, "Team.ReadBasic.All")
    )
    assert none.json()["value"] == []
    channels = teams.graph.call(
        "GET",
        f"{V1}/teams/{teams.team.id}/channels",
        token=teams.user_token(teams.ada, "Channel.ReadBasic.All"),
    )
    assert [c["displayName"] for c in channels.json()["value"]] == ["General"]
    forbidden = teams.graph.call(
        "GET",
        f"{V1}/teams/{teams.team.id}/channels",
        token=teams.user_token(teams.eve, "Channel.ReadBasic.All"),
    )
    assert forbidden.status == 403


@proves("teams.channel_message_rate_limits")
def test_the_documented_teams_limits_are_one_post_per_second_per_channel_and_user(
    teams: Teams,
) -> None:
    fake_graph_teams.apply_limits(teams.graph)
    token = teams.user_token()
    body = {"body": {"content": "burst"}}
    first = teams.graph.call("POST", teams.url, token=token, json_body=body)
    second = teams.graph.call("POST", teams.url, token=token, json_body=body)
    assert (first.status, second.status) == (201, 429)
    assert second.headers["retry-after"] == "1"
    other = teams.store.add_channel(teams.team, "Random")
    elsewhere = teams.graph.call(
        "POST",
        f"{V1}/teams/{teams.team.id}/channels/{other.id}/messages",
        token=token,
        json_body=body,
    )
    assert elsewhere.status == 429  # still the same user within the second
    teams.graph.clock.advance(1)
    assert teams.graph.call("POST", teams.url, token=token, json_body=body).status == 201
    reads = [teams.graph.call("GET", teams.url, token=token).status for _ in range(2)]
    assert reads == [200, 429]


@proves("teams.workflows_envelope")
def test_the_workflows_webhook_validates_the_envelope_and_answers_202(teams: Teams) -> None:
    content = json.loads(card()["attachments"][0]["content"])
    envelope = {
        "type": "message",
        "attachments": [
            {"contentType": fake_graph_teams.ADAPTIVE, "contentUrl": None, "content": content}
        ],
    }
    ok = teams.graph.call("POST", "/workflows/flow-1?api-version=1&sig=abc", json_body=envelope)
    assert ok.status == 202 and ok.body == b""
    assert teams.store.workflow_posts["flow-1"] == [envelope]
    bad_cases = [
        {"type": "message"},
        {"attachments": envelope["attachments"]},
        {"type": "message", "attachments": []},
        {"type": "message", "attachments": [{"contentType": "text/plain", "content": content}]},
        {
            "type": "message",
            "attachments": [
                {"contentType": fake_graph_teams.ADAPTIVE, "content": json.dumps(content)}
            ],
        },
        {
            "type": "message",
            "attachments": [
                {"contentType": fake_graph_teams.ADAPTIVE, "content": {"type": "AdaptiveCard"}}
            ],
        },
        {
            "type": "message",
            "attachments": [
                {"contentType": fake_graph_teams.ADAPTIVE, "content": {**content, "body": "x"}}
            ],
        },
    ]
    for bad in bad_cases:
        reply = teams.graph.call("POST", "/workflows/flow-1", json_body=bad)
        assert reply.status == 400 and reply.error()[0] == "InvalidRequest", bad
    huge = {
        **envelope,
        "attachments": [
            {
                **envelope["attachments"][0],
                "content": {**content, "body": [{"type": "TextBlock", "text": "x" * 30_000}]},
            }
        ],
    }
    assert teams.graph.call("POST", "/workflows/flow-1", json_body=huge).status == 413
    assert teams.graph.call("POST", "/workflows/flow-1", data=b"not json").status == 400
    assert len(teams.store.workflow_posts["flow-1"]) == 1


@proves("teams.workflows_envelope")
def test_the_workflows_url_secret_is_checked_when_configured(teams: Teams) -> None:
    teams.store.workflow_sig = "secret-sig"
    content = json.loads(card()["attachments"][0]["content"])
    envelope = {
        "type": "message",
        "attachments": [{"contentType": fake_graph_teams.ADAPTIVE, "content": content}],
    }
    assert teams.graph.call("POST", "/workflows/f?sig=wrong", json_body=envelope).status == 401
    assert teams.graph.call("POST", "/workflows/f?sig=secret-sig", json_body=envelope).status == 202


# --------------------------------------------------------------------------- To Do


class Todo:
    def __init__(self, graph: FakeGraph) -> None:
        self.graph = graph
        self.store: fake_graph_todo.TodoStore = graph.store("todo")
        self.ada = graph.add_user("ada@contoso.test", name="Ada")
        self.bob = graph.add_user("bob@contoso.test", name="Bob")
        self.list = self.store.add_list(self.ada, "Tasks")
        self.app = graph.add_app(CLIENT, "s", roles=("Tasks.ReadWrite.All", "Tasks.Read.All"))
        self.delegated = graph.add_app(
            "todo-user", public_client=True, scopes=("Tasks.Read", "Tasks.ReadWrite")
        )

    def token(self, user: Any, *scopes: str) -> str:
        return self.graph.issue_token(self.delegated, kind="user", user=user, scopes=list(scopes))


@pytest.fixture
def todo(graph: FakeGraph) -> Todo:
    return Todo(graph)


def test_a_repeated_list_name_is_refused_instead_of_replacing_the_list(todo: Todo) -> None:
    with pytest.raises(ValueError, match="Tasks"):
        todo.store.add_list(todo.ada, "Tasks")
    assert todo.store.add_list(todo.bob, "Tasks").id != todo.list.id


@proves("todo.application_permissions_not_supported")
def test_to_do_refuses_an_app_token_on_every_route_whatever_roles_it_has(todo: Todo) -> None:
    token = todo.graph.app_token(todo.app)
    base = f"{V1}/users/ada@contoso.test/todo/lists"
    for method, path, body in (
        ("GET", base, None),
        ("GET", f"{base}/{todo.list.id}/tasks", None),
        ("POST", f"{base}/{todo.list.id}/tasks", {"title": "x"}),
        ("PATCH", f"{base}/{todo.list.id}/tasks/abc", {"status": "completed"}),
    ):
        reply = todo.graph.call(method, path, token=token, json_body=body)
        assert reply.status == 403, (method, path)
        assert reply.error() == (
            "Forbidden",
            "This API is not supported for application permissions.",
        )
    assert todo.list.tasks == []


@proves("todo.application_permissions_not_supported")
def test_to_do_works_for_the_signed_in_person_with_the_right_scope(todo: Todo) -> None:
    reader = todo.token(todo.ada, "Tasks.Read")
    writer = todo.token(todo.ada, "Tasks.ReadWrite")
    lists = todo.graph.call("GET", f"{V1}/me/todo/lists", token=reader).json()["value"]
    assert [item["displayName"] for item in lists] == ["Tasks"]
    path = f"{V1}/me/todo/lists/{todo.list.id}/tasks"
    refused = todo.graph.call("POST", path, token=reader, json_body={"title": "x"})
    assert refused.status == 403 and "Tasks.ReadWrite" in refused.error()[1]
    created = todo.graph.call("POST", path, token=writer, json_body={"title": "Call ACME"})
    assert created.status == 201 and created.json()["status"] == "notStarted"
    task_path = f"{path}/{created.json()['id']}"
    done = todo.graph.call("PATCH", task_path, token=writer, json_body={"status": "completed"})
    assert done.status == 200 and done.json()["status"] == "completed"
    assert todo.graph.call("GET", path, token=reader).json()["value"][0]["status"] == "completed"
    for bad in ({"title": " "}, {}, {"title": "x", "status": "finished"}):
        assert todo.graph.call("POST", path, token=writer, json_body=bad).status == 400
    odd = todo.graph.call("PATCH", task_path, token=writer, json_body={"status": "x"})
    assert odd.status == 400
    filtered = todo.graph.call("GET", path + "?$filter=status ne 'completed'", token=reader)
    assert filtered.status == 501


@proves("todo.application_permissions_not_supported")
def test_a_persons_lists_belong_to_that_person(todo: Todo) -> None:
    other = todo.token(todo.bob, "Tasks.ReadWrite")
    mine = todo.graph.call("GET", f"{V1}/me/todo/lists", token=other).json()["value"]
    assert mine == []
    own_path = f"{V1}/me/todo/lists/{todo.list.id}/tasks"
    assert todo.graph.call("GET", own_path, token=other).status == 404
    ada = todo.token(todo.ada, "Tasks.ReadWrite")
    foreign = todo.graph.call("GET", f"{V1}/users/bob@contoso.test/todo/lists", token=ada)
    assert foreign.status == 403 and foreign.error()[0] == "Forbidden"
