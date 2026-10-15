"""FakeGraph core: Entra token endpoint, bearer checks, faults, paging, delta, request log, the loopback rule and the answers `awa check microsoft` relies on."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from _helpers.fake_graph_proof import COVERED_RULES, proves
from _helpers.fakegraph import directory as directory_plugin
from _helpers.fakegraph import files as fake_graph_files
from _helpers.fakegraph import mail as fake_graph_mail
from _helpers.fakegraph.core import GRAPH_AUDIENCE, Ctx, Plugin, Reply, decode_jwt
from _helpers.fakegraph.identity import DEVICE_GRANT
from _helpers.fakegraph.server import V1, FakeGraph, HttpReply

SECRET = "s3cr3t-value-for-tests"
CLIENT = "11111111-2222-3333-4444-555555555555"
PUBLIC = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def graph() -> Iterator[FakeGraph]:
    with FakeGraph() as server:
        yield server


def token_call(graph: FakeGraph, **form: str) -> HttpReply:
    return graph.call("POST", f"/{graph.tenant_id}/oauth2/v2.0/token", form=form)


def credentials(
    app: str = CLIENT, secret: str = SECRET, scope: str | None = None
) -> dict[str, str]:
    return {
        "grant_type": "client_credentials",
        "client_id": app,
        "client_secret": secret,
        "scope": scope or f"{GRAPH_AUDIENCE}/.default",
    }


def error_of(reply: HttpReply) -> tuple[str, str]:
    body = reply.json()
    return body["error"], body["error_description"]


def messages_url(user: str = "ada@contoso.test") -> str:
    return f"{V1}/users/{user}/mailFolders/inbox/messages"


# --------------------------------------------------------------------------- token endpoint


@proves("entra.token_request_is_a_form")
def test_client_credentials_issue_a_jwt_with_the_claims_of_a_real_graph_token(
    graph: FakeGraph,
) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Mail.Read", "Mail.Send"), name="awa")
    reply = token_call(graph, **credentials())
    assert reply.status == 200
    body = reply.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == body["ext_expires_in"] == 3599
    parts = body["access_token"].split(".")
    assert len(parts) == 3
    claims = decode_jwt(body["access_token"])
    assert claims["aud"] == GRAPH_AUDIENCE
    assert claims["appid"] == app.client_id
    assert claims["tid"] == graph.tenant_id
    assert claims["iss"] == f"https://sts.windows.net/{graph.tenant_id}/"
    assert claims["roles"] == ["Mail.Read", "Mail.Send"]
    assert claims["idtyp"] == "app"
    assert claims["exp"] - claims["iat"] == 3599
    assert claims["iat"] == int(graph.clock.time())


@proves("entra.token_request_is_a_form")
def test_the_token_endpoint_reads_form_fields_and_only_answers_post(graph: FakeGraph) -> None:
    graph.add_app(CLIENT, SECRET)
    no_grant = token_call(graph)
    assert no_grant.status == 400
    assert error_of(no_grant)[0] == "invalid_request"
    assert "AADSTS900144" in error_of(no_grant)[1] and "'grant_type'" in error_of(no_grant)[1]
    no_scope = token_call(graph, grant_type="client_credentials", client_id=CLIENT)
    assert "'scope'" in error_of(no_scope)[1]
    got = graph.call("GET", f"/{graph.tenant_id}/oauth2/v2.0/token")
    assert got.status == 405 and "AADSTS900561" in error_of(got)[1]
    unknown = token_call(graph, grant_type="password", client_id=CLIENT)
    assert error_of(unknown)[0] == "unsupported_grant_type"


@proves("entra.client_secret_wrong")
def test_a_wrong_client_secret_is_aadsts7000215(graph: FakeGraph) -> None:
    graph.add_app(CLIENT, SECRET)
    reply = token_call(graph, **credentials(secret="not-the-secret"))
    assert reply.status == 401
    error, description = error_of(reply)
    assert error == "invalid_client"
    assert description.startswith("AADSTS7000215: Invalid client secret provided.")
    assert "Trace ID:" in description and "Correlation ID:" in description
    assert reply.json()["error_codes"] == [7000215]
    assert SECRET not in reply.text


@proves("entra.client_secret_expired")
def test_an_expired_client_secret_is_aadsts7000222_on_the_fake_clock(graph: FakeGraph) -> None:
    graph.add_app(CLIENT, SECRET, secret_lifetime_days=30)
    assert token_call(graph, **credentials()).status == 200
    graph.clock.advance(31 * 86400)
    reply = token_call(graph, **credentials())
    assert reply.status == 401
    assert "AADSTS7000222" in error_of(reply)[1]


@proves("entra.client_credentials_need_default_scope")
def test_client_credentials_need_the_default_scope(graph: FakeGraph) -> None:
    graph.add_app(CLIENT, SECRET)
    for scope in (
        "https://graph.microsoft.com/Mail.Read",
        "Mail.Read",
        "https://graph.microsoft.com/.default Mail.Read",
    ):
        reply = token_call(graph, **credentials(scope=scope))
        assert reply.status == 400
        assert error_of(reply)[0] == "invalid_scope"
        assert "AADSTS1002012" in error_of(reply)[1]


@proves("entra.unknown_tenant", "entra.unknown_resource")
def test_unknown_tenant_client_and_resource_have_their_own_aadsts_codes(graph: FakeGraph) -> None:
    graph.add_app(CLIENT, SECRET)
    wrong_tenant = graph.call("POST", "/nonexistent/oauth2/v2.0/token", form=credentials())
    assert wrong_tenant.status == 400 and "AADSTS90002" in error_of(wrong_tenant)[1]
    by_domain = graph.call("POST", f"/{graph.tenant_domain}/oauth2/v2.0/token", form=credentials())
    assert by_domain.status == 200
    unknown_client = token_call(graph, **credentials(app="00000000-0000-0000-0000-000000000000"))
    assert unknown_client.status == 401 and "AADSTS700016" in error_of(unknown_client)[1]
    resource = token_call(graph, **credentials(scope="https://nowhere.example/.default"))
    assert resource.status == 400
    assert error_of(resource)[0] == "invalid_resource" and "AADSTS500011" in error_of(resource)[1]


# ---- device code and refresh


def signed_in(
    graph: FakeGraph, *, scopes: tuple[str, ...] = ("Mail.Read", "User.Read")
) -> dict[str, Any]:
    """Run a whole device-code flow; return the token response."""

    graph.add_app(PUBLIC, public_client=True, scopes=scopes)
    graph.add_user("ada@contoso.test")
    start = graph.call(
        "POST",
        f"/{graph.tenant_id}/oauth2/v2.0/devicecode",
        form={"client_id": PUBLIC, "scope": "Mail.Read offline_access"},
    ).json()
    form = {"grant_type": DEVICE_GRANT, "client_id": PUBLIC, "device_code": start["device_code"]}
    reply = token_call(graph, **form)
    while reply.status == 400 and error_of(reply)[0] == "authorization_pending":
        reply = token_call(graph, **form)
    assert reply.status == 200, reply.text
    body: dict[str, Any] = reply.json()
    return body


@proves("entra.device_code_pending")
def test_device_code_is_pending_twice_then_signs_the_first_user_in(graph: FakeGraph) -> None:
    graph.add_app(PUBLIC, public_client=True, scopes=("Mail.Read", "User.Read"))
    ada = graph.add_user("ada@contoso.test")
    start = graph.call(
        "POST",
        f"/{graph.tenant_id}/oauth2/v2.0/devicecode",
        form={"client_id": PUBLIC, "scope": "Mail.Read offline_access"},
    )
    assert start.status == 200
    code = start.json()
    assert code["verification_uri"] == "https://microsoft.com/devicelogin"
    assert code["expires_in"] == 900 and code["interval"] == 5
    assert code["user_code"] in code["message"]
    form = {"grant_type": DEVICE_GRANT, "client_id": PUBLIC, "device_code": code["device_code"]}
    for _ in range(2):
        pending = token_call(graph, **form)
        assert pending.status == 400
        assert error_of(pending)[0] == "authorization_pending"
        assert "AADSTS70016" in error_of(pending)[1]
    done = token_call(graph, **form)
    assert done.status == 200
    body = done.json()
    claims = decode_jwt(body["access_token"])
    assert claims["upn"] == ada.upn and claims["scp"] == "Mail.Read"
    assert "roles" not in claims and "idtyp" not in claims
    assert body["refresh_token"]
    assert "Mail.Read" in body["scope"]
    again = token_call(graph, **form)
    assert error_of(again)[0] == "bad_verification_code"


@proves("entra.device_code_pending")
def test_an_expired_device_code_is_expired_token_aadsts70019(graph: FakeGraph) -> None:
    graph.add_app(PUBLIC, public_client=True, scopes=("Mail.Read",))
    graph.add_user("ada@contoso.test")
    code = graph.call(
        "POST",
        f"/{graph.tenant_id}/oauth2/v2.0/devicecode",
        form={"client_id": PUBLIC, "scope": "Mail.Read"},
    ).json()
    graph.clock.advance(901)
    reply = token_call(
        graph, grant_type=DEVICE_GRANT, client_id=PUBLIC, device_code=code["device_code"]
    )
    assert error_of(reply)[0] == "expired_token" and "AADSTS70019" in error_of(reply)[1]


@proves("entra.device_code_pending")
def test_polling_faster_than_the_interval_is_slow_down_when_enforced(graph: FakeGraph) -> None:
    graph.identity.enforce_device_interval = True
    graph.add_app(PUBLIC, public_client=True, scopes=("Mail.Read",))
    graph.add_user("ada@contoso.test")
    code = graph.call(
        "POST",
        f"/{graph.tenant_id}/oauth2/v2.0/devicecode",
        form={"client_id": PUBLIC, "scope": "Mail.Read"},
    ).json()
    form = {"grant_type": DEVICE_GRANT, "client_id": PUBLIC, "device_code": code["device_code"]}
    assert error_of(token_call(graph, **form))[0] == "authorization_pending"
    assert error_of(token_call(graph, **form))[0] == "slow_down"
    graph.clock.advance(5)
    assert error_of(token_call(graph, **form))[0] == "authorization_pending"


@proves("entra.device_code_needs_public_client")
def test_device_code_needs_allow_public_client_flows(graph: FakeGraph) -> None:
    graph.add_app(CLIENT, SECRET, scopes=("Mail.Read",))
    reply = graph.call(
        "POST",
        f"/{graph.tenant_id}/oauth2/v2.0/devicecode",
        form={"client_id": CLIENT, "scope": "Mail.Read"},
    )
    assert reply.status == 400
    assert error_of(reply)[0] == "invalid_client" and "AADSTS7000218" in error_of(reply)[1]


@proves("entra.device_code_needs_public_client")
def test_a_confidential_app_must_send_its_secret_to_refresh(graph: FakeGraph) -> None:
    graph.add_app(CLIENT, SECRET, scopes=("Mail.Read",))
    reply = token_call(graph, grant_type="refresh_token", client_id=CLIENT, refresh_token="x")
    assert reply.status == 400 and "AADSTS7000218" in error_of(reply)[1]


@proves("entra.device_code_blocked_by_conditional_access")
def test_a_conditional_access_block_fails_the_device_poll_with_aadsts53003(
    graph: FakeGraph,
) -> None:
    graph.add_app(PUBLIC, public_client=True, scopes=("Mail.Read",))
    graph.add_user("ada@contoso.test")
    graph.identity.block_device_code = True
    graph.identity.device_pending_polls = 0
    code = graph.call(
        "POST",
        f"/{graph.tenant_id}/oauth2/v2.0/devicecode",
        form={"client_id": PUBLIC, "scope": "Mail.Read"},
    ).json()
    reply = token_call(
        graph, grant_type=DEVICE_GRANT, client_id=PUBLIC, device_code=code["device_code"]
    )
    assert reply.status == 400 and "AADSTS53003" in error_of(reply)[1]


@proves("entra.delegated_scope_needs_consent")
def test_a_scope_the_app_was_not_consented_for_is_aadsts65001(graph: FakeGraph) -> None:
    graph.add_app(PUBLIC, public_client=True, scopes=("Mail.Read",))
    reply = graph.call(
        "POST",
        f"/{graph.tenant_id}/oauth2/v2.0/devicecode",
        form={"client_id": PUBLIC, "scope": "Mail.Read Files.ReadWrite.All"},
    )
    assert reply.status == 400 and "AADSTS65001" in error_of(reply)[1]
    assert "Files.ReadWrite.All" in error_of(reply)[1]


@proves("entra.refresh_token_rotates")
def test_every_refresh_returns_a_new_refresh_token_and_retires_the_old_one(
    graph: FakeGraph,
) -> None:
    first = signed_in(graph)
    second = token_call(
        graph, grant_type="refresh_token", client_id=PUBLIC, refresh_token=first["refresh_token"]
    )
    assert second.status == 200
    body = second.json()
    assert body["refresh_token"] != first["refresh_token"]
    assert decode_jwt(body["access_token"])["scp"] == "Mail.Read"
    stale = token_call(
        graph, grant_type="refresh_token", client_id=PUBLIC, refresh_token=first["refresh_token"]
    )
    assert stale.status == 400 and error_of(stale)[0] == "invalid_grant"
    assert "AADSTS70008" in error_of(stale)[1]
    third = token_call(
        graph, grant_type="refresh_token", client_id=PUBLIC, refresh_token=body["refresh_token"]
    )
    assert third.status == 200


@proves("entra.refresh_token_rotates")
def test_lenient_rotation_keeps_the_old_refresh_token_valid_like_entra_itself(
    graph: FakeGraph,
) -> None:
    graph.identity.strict_rotation = False
    first = signed_in(graph)
    form = {
        "grant_type": "refresh_token",
        "client_id": PUBLIC,
        "refresh_token": first["refresh_token"],
    }
    assert token_call(graph, **form).status == 200
    assert token_call(graph, **form).status == 200


@proves("entra.refresh_token_revoked")
def test_a_revoked_refresh_token_is_aadsts50173(graph: FakeGraph) -> None:
    first = signed_in(graph)
    graph.revoke_refresh_tokens()
    reply = token_call(
        graph, grant_type="refresh_token", client_id=PUBLIC, refresh_token=first["refresh_token"]
    )
    assert reply.status == 400 and "AADSTS50173" in error_of(reply)[1]


@proves("entra.refresh_token_idle_90_days")
def test_a_refresh_token_unused_for_90_days_is_aadsts70008(graph: FakeGraph) -> None:
    first = signed_in(graph)
    graph.clock.advance(91 * 86400)
    reply = token_call(
        graph, grant_type="refresh_token", client_id=PUBLIC, refresh_token=first["refresh_token"]
    )
    assert reply.status == 400 and "AADSTS70008" in error_of(reply)[1]


@proves("entra.delegated_scope_needs_consent")
def test_a_refresh_cannot_widen_the_scopes(graph: FakeGraph) -> None:
    first = signed_in(graph, scopes=("Mail.Read", "User.Read"))
    reply = token_call(
        graph,
        grant_type="refresh_token",
        client_id=PUBLIC,
        refresh_token=first["refresh_token"],
        scope="Mail.Read",
    )
    assert reply.status == 200
    widen = token_call(
        graph,
        grant_type="refresh_token",
        client_id=PUBLIC,
        refresh_token=reply.json()["refresh_token"],
        scope="Mail.Read User.Read",  # consented for the app, but not part of this grant
    )
    assert widen.status == 400 and "AADSTS65001" in error_of(widen)[1]


# --------------------------------------------------------------------------- bearer checks


def mail_app(graph: FakeGraph, *roles: str) -> tuple[Any, str]:
    app = graph.add_app(CLIENT, SECRET, roles=roles or ("Mail.Read",))
    graph.add_user("ada@contoso.test")
    return app, graph.app_token(app)


@proves("graph.bearer_token_required", "graph.error_body_shape")
def test_a_request_without_a_token_is_401_with_the_graph_error_body(graph: FakeGraph) -> None:
    mail_app(graph)
    reply = graph.call("GET", messages_url())
    assert reply.status == 401
    code, message = reply.error()
    assert code == "InvalidAuthenticationToken" and message == "Access token is empty."
    assert reply.headers["www-authenticate"].startswith('Bearer realm=""')
    inner = reply.json()["error"]["innerError"]
    assert set(inner) == {"date", "request-id", "client-request-id"}
    assert reply.headers["request-id"] == inner["request-id"]
    echoed = graph.call("GET", messages_url(), headers={"client-request-id": "abc-123"})
    assert echoed.headers["client-request-id"] == "abc-123"


@proves("graph.bearer_token_required")
def test_garbage_tampered_expired_revoked_and_foreign_audience_tokens_are_401(
    graph: FakeGraph,
) -> None:
    app, token = mail_app(graph)
    assert graph.call("GET", messages_url(), token=token).status == 200
    garbage = graph.call("GET", messages_url(), token="not.a.jwt")
    assert garbage.error()[1].startswith("CompactToken parsing failed")
    head, body, _ = token.split(".")
    forged = graph.call("GET", messages_url(), token=f"{head}.{body}.{'A' * 342}")
    assert forged.status == 401 and "Signature" in forged.error()[1]
    vault = graph.call(
        "POST",
        f"/{graph.tenant_id}/oauth2/v2.0/token",
        form=credentials(scope="https://vault.azure.net/.default"),
    ).json()["access_token"]
    wrong = graph.call("GET", messages_url(), token=vault)
    assert wrong.status == 401 and "Invalid audience" in wrong.error()[1]
    graph.revoke_access_token(token)
    revoked = graph.call("GET", messages_url(), token=token)
    assert revoked.status == 401 and "revoked" in revoked.error()[1]
    fresh = graph.app_token(app)
    graph.clock.advance(3600)
    expired = graph.call("GET", messages_url(), token=fresh)
    assert expired.status == 401 and "expired" in expired.error()[1]


@proves("graph.bearer_token_required")
def test_a_token_that_is_not_a_bearer_scheme_is_empty(graph: FakeGraph) -> None:
    mail_app(graph)
    reply = graph.call("GET", messages_url(), headers={"Authorization": "Basic Zm9vOmJhcg=="})
    assert reply.status == 401 and reply.error()[1] == "Access token is empty."


@proves("graph.permission_per_route")
def test_a_missing_permission_is_403_in_the_style_of_the_service(graph: FakeGraph) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Calendars.Read",))
    graph.add_user("ada@contoso.test")
    token = graph.app_token(app)
    mail = graph.call("GET", messages_url(), token=token)
    assert mail.status == 403
    assert mail.error() == (
        "ErrorAccessDenied",
        "Access is denied. Check credentials and try again.",
    )
    teams = graph.call("GET", f"{V1}/teams/x/channels/y/messages", token=token)
    code, message = teams.error()
    assert teams.status == 403 and code == "Forbidden"
    assert "Missing role permissions on the request" in message
    assert "ChannelMessage.Read.All" in message and "Calendars.Read" in message


@proves("graph.permission_per_route")
def test_a_user_token_is_judged_by_its_scopes_not_its_roles(graph: FakeGraph) -> None:
    app = graph.add_app(PUBLIC, public_client=True, scopes=("Mail.Read",))
    ada = graph.add_user("ada@contoso.test")
    token = graph.issue_token(app, kind="user", scopes=["User.Read"], user=ada)
    reply = graph.call("GET", f"{V1}/me/mailFolders/inbox/messages", token=token)
    assert reply.status == 403
    allowed = graph.issue_token(app, kind="user", scopes=["Mail.Read"], user=ada)
    assert graph.call("GET", f"{V1}/me/mailFolders/inbox/messages", token=allowed).status == 200


# --------------------------------------------------------------------------- fault injection


def one_mail(graph: FakeGraph) -> str:
    app, token = mail_app(graph)
    graph.store("mail").add_message(graph.find_user("ada@contoso.test"), subject="one")
    return token


@proves("graph.throttling_429_retry_after")
def test_throttle_answers_429_with_each_retry_after_variant(graph: FakeGraph) -> None:
    token = one_mail(graph)
    for value, expected in ((2, "2"), (120, "120"), (None, None), ("0", "0")):
        graph.clear_faults()
        graph.throttle("/users/*", retry_after=value)
        reply = graph.call("GET", messages_url(), token=token)
        assert reply.status == 429
        assert reply.headers.get("retry-after") == expected
        assert reply.error()[0] == "TooManyRequests"
        assert graph.call("GET", messages_url(), token=token).status == 200  # times=1


@proves("graph.throttling_429_retry_after")
def test_faults_can_skip_the_first_calls_fail_many_times_and_pick_the_method(
    graph: FakeGraph,
) -> None:
    token = one_mail(graph)
    fault = graph.fail("GET", "/users/*/mailFolders/*/messages", status=503, after=1, times=2)
    statuses = [graph.call("GET", messages_url(), token=token).status for _ in range(5)]
    assert statuses == [200, 503, 503, 200, 200]
    assert fault.fired == 2 and fault.seen == 5
    graph.clear_faults()
    graph.fail("POST", "*", status=503, times=None)
    assert graph.call("GET", messages_url(), token=token).status == 200


@proves("graph.throttling_429_retry_after")
def test_a_503_on_a_write_and_on_a_read_are_both_scriptable(graph: FakeGraph) -> None:
    app = graph.add_app(CLIENT, SECRET, roles=("Mail.Read", "Mail.Send"))
    graph.add_user("ada@contoso.test")
    token = graph.app_token(app)
    graph.fail("GET", "*", status=503)
    graph.fail("POST", "*", status=503)
    read = graph.call("GET", messages_url(), token=token)
    write = graph.call(
        "POST",
        f"{V1}/users/ada@contoso.test/sendMail",
        token=token,
        json_body={"message": {"toRecipients": [{"emailAddress": {"address": "b@x.test"}}]}},
    )
    assert (read.status, write.status) == (503, 503)
    assert read.error()[0] == "serviceNotAvailable"
    assert [r.status for r in graph.calls("POST", "*sendMail")] == [503]


@proves("graph.throttling_429_retry_after")
def test_faults_apply_after_authentication_so_auth_errors_are_not_masked(graph: FakeGraph) -> None:
    one_mail(graph)
    graph.fail("*", "*", status=503, times=None)
    assert graph.call("GET", messages_url()).status == 401


@proves("teams.channel_message_rate_limits")
def test_rate_limits_follow_the_fake_clock_and_key(graph: FakeGraph) -> None:
    token = one_mail(graph)
    limit = graph.rate_limit("GET", "/users/*/mailFolders/*/messages", 1, key="path")
    assert graph.call("GET", messages_url(), token=token).status == 200
    second = graph.call("GET", messages_url(), token=token)
    assert second.status == 429 and second.headers["retry-after"] == "1"
    other = graph.call("GET", messages_url("other@contoso.test"), token=token)
    assert other.status != 429  # another key (another path)
    graph.clock.advance(1)
    assert graph.call("GET", messages_url(), token=token).status == 200
    assert limit.rejected == 1


@proves("outlook.four_concurrent_requests_per_mailbox")
def test_more_than_four_concurrent_requests_per_mailbox_are_429(graph: FakeGraph) -> None:
    token = one_mail(graph)
    release = threading.Event()
    limit = graph.limit_concurrency("/users/*", limit=4, hold=release)
    replies: list[Any] = []
    lock = threading.Lock()

    def request() -> None:
        reply = graph.call("GET", messages_url(), token=token)
        with lock:
            replies.append(reply)

    threads = [threading.Thread(target=request) for _ in range(7)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 10
    while limit.rejected < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    release.set()
    for thread in threads:
        thread.join()
    rejected = [reply for reply in replies if reply.status == 429]
    assert sorted(reply.status for reply in replies) == [200] * 4 + [429] * 3
    for reply in rejected:
        assert reply.error() == (
            "ApplicationThrottled",
            "Application is over its MailboxConcurrency limit.",
        )
        assert reply.headers["retry-after"] == "1"
    assert limit.rejected == 3
    assert graph.call("GET", messages_url(), token=token).status == 200  # all released


def test_delay_holds_matching_requests(graph: FakeGraph) -> None:
    token = one_mail(graph)
    graph.delay("/users/*", 0.2)
    started = time.monotonic()
    assert graph.call("GET", messages_url(), token=token).status == 200
    assert time.monotonic() - started >= 0.2


# --------------------------------------------------------------------------- paging


def many_messages(graph: FakeGraph, count: int) -> str:
    app, token = mail_app(graph)
    ada = graph.find_user("ada@contoso.test")
    for number in range(count):
        graph.store("mail").add_message(ada, subject=f"m{number}")
        graph.clock.advance(1)
    return token


@proves("graph.next_link_is_absolute_and_opaque", "outlook.messages_paging")
def test_next_link_is_absolute_keeps_the_query_and_continues_after_the_page(
    graph: FakeGraph,
) -> None:
    token = many_messages(graph, 25)
    seen: list[str] = []
    url = messages_url() + "?$top=10&$select=subject,isRead"
    pages = 0
    while url:
        reply = graph.call("GET", url, token=token)
        assert reply.status == 200
        body = reply.json()
        pages += 1
        seen.extend(item["subject"] for item in body["value"])
        assert all(
            set(item) <= {"id", "@odata.etag", "subject", "isRead"} for item in body["value"]
        )
        url = body.get("@odata.nextLink", "")
        if url:
            assert url.startswith(graph.url + V1 + "/users/")
            assert "%24top=10" in url and "%24skiptoken=" in url and "%24select=subject" in url
    assert pages == 3
    assert seen == [f"m{n}" for n in reversed(range(25))]  # newest first, none twice


@proves("graph.next_link_is_absolute_and_opaque")
def test_a_next_link_without_the_original_query_behaves_like_graph_stateless(
    graph: FakeGraph,
) -> None:
    token = many_messages(graph, 25)
    first = graph.call("GET", messages_url() + "?$top=10&$select=subject", token=token).json()
    link = first["@odata.nextLink"]
    token_only = link.split("%24skiptoken=")[1]
    bare = graph.call("GET", messages_url() + "?$skiptoken=" + token_only, token=token).json()
    # the query is not remembered: default page size and no $select
    assert len(bare["value"]) == 10
    assert "bodyPreview" in bare["value"][0]
    assert bare["value"][0]["subject"] == "m14"


@proves("graph.next_link_is_absolute_and_opaque")
def test_a_skip_token_that_is_not_ours_is_400(graph: FakeGraph) -> None:
    token = many_messages(graph, 3)
    reply = graph.call("GET", messages_url() + "?$skiptoken=AAAA", token=token)
    assert reply.status == 400 and reply.error()[0] == "BadRequest"


@proves("outlook.messages_paging")
def test_top_is_capped_for_messages_and_an_unknown_select_property_is_400(graph: FakeGraph) -> None:
    token = many_messages(graph, 12)
    assert len(graph.call("GET", messages_url(), token=token).json()["value"]) == 10
    capped = graph.call("GET", messages_url() + "?$top=5000", token=token)
    assert capped.status == 200 and len(capped.json()["value"]) == 12
    bad_top = graph.call("GET", messages_url() + "?$top=zero", token=token)
    assert bad_top.status == 400
    unknown = graph.call("GET", messages_url() + "?$select=subject,colour", token=token)
    assert unknown.status == 400
    assert "colour" in unknown.error()[1] and "microsoft.graph.message" in unknown.error()[1]


# --------------------------------------------------------------------------- delta


def drive_with_files(graph: FakeGraph) -> tuple[str, Any, fake_graph_files.FilesStore]:
    app = graph.add_app(CLIENT, SECRET, roles=("Files.Read.All", "Sites.Read.All"))
    files: fake_graph_files.FilesStore = graph.store("files")
    site = files.add_site("contoso.sharepoint.com", "sites/awa-files")
    for name in ("a.txt", "b.txt", "c.txt"):
        files.put_file(site, f"inbox/{name}", name.encode())
    return graph.app_token(app), site, files


def follow(graph: FakeGraph, url: str, token: str) -> tuple[list[dict[str, Any]], str, int]:
    """Items of every page of a delta round, the final deltaLink and the number of pages."""

    items: list[dict[str, Any]] = []
    pages = 0
    while True:
        body = graph.call("GET", url, token=token).json()
        pages += 1
        items.extend(body["value"])
        if "@odata.nextLink" in body:
            url = body["@odata.nextLink"]
            continue
        return items, body["@odata.deltaLink"], pages


@proves("files.delta_rounds")
def test_delta_pages_through_everything_then_returns_only_changes(graph: FakeGraph) -> None:
    token, site, files = drive_with_files(graph)
    start = f"{V1}/sites/{site.id}/drive/root/delta?$top=2"
    first, link, pages = follow(graph, start, token)
    names = {item["name"] for item in first}
    assert {"a.txt", "b.txt", "c.txt", "inbox", "root"} <= names
    assert pages >= 3 and link.startswith(graph.url + V1) and "token=" in link
    assert follow(graph, link, token)[0] == []  # nothing changed
    files.put_file(site, "inbox/d.txt", b"new")
    changed = files.put_file(site, "inbox/a.txt", b"edited")
    inbox = files.folder_path(files.drives[site.drive_id], "inbox")
    victim = files.find_child(files.drives[site.drive_id], inbox["id"], "b.txt")
    files.delete(site, victim["id"])
    second, link2, _ = follow(graph, link, token)
    by_name = {i.get("name", i["id"]): i for i in second}
    assert {"d.txt", "a.txt"} <= set(by_name)
    assert by_name["a.txt"]["id"] == changed["id"]
    deleted = by_name[victim["id"]]
    assert deleted["deleted"] == {"state": "deleted"} and "name" not in deleted
    assert "b.txt" not in by_name and "c.txt" not in by_name
    assert follow(graph, link2, token)[0] == []


@proves("files.delta_rounds")
def test_delta_token_latest_returns_no_items_and_a_delta_link(graph: FakeGraph) -> None:
    token, site, files = drive_with_files(graph)
    items, link, _ = follow(graph, f"{V1}/sites/{site.id}/drive/root/delta?token=latest", token)
    assert items == []
    files.put_file(site, "inbox/new.txt", b"x")
    later, _, _ = follow(graph, link, token)
    assert [i["name"] for i in later if i["name"] == "new.txt"] == ["new.txt"]


@proves("files.delta_root_only")
def test_delta_on_a_folder_is_refused_on_sharepoint(graph: FakeGraph) -> None:
    token, site, files = drive_with_files(graph)
    inbox = files.folder_path(files.drives[site.drive_id], "inbox")
    reply = graph.call("GET", f"{V1}/sites/{site.id}/drive/items/{inbox['id']}/delta", token=token)
    assert reply.status == 400 and reply.error()[0] == "invalidRequest"


# --------------------------------------------------------------------------- the server itself


def test_unknown_routes_and_methods_are_clear_about_what_the_fake_lacks(graph: FakeGraph) -> None:
    token = one_mail(graph)
    missing = graph.call("GET", f"{V1}/places", token=token)
    assert missing.status == 404 and missing.error()[0] == "FakeGraphNoRoute"
    wrong = graph.call("DELETE", messages_url(), token=token)
    assert wrong.status == 405 and wrong.error()[0] == "MethodNotAllowed"


def test_request_log_records_method_path_query_headers_body_and_status(graph: FakeGraph) -> None:
    token = one_mail(graph)
    graph.clear_requests()
    graph.call("GET", messages_url() + "?$top=1", token=token, headers={"X-Probe": "yes"})
    graph.call("GET", messages_url())
    graph.call("POST", f"/{graph.tenant_id}/oauth2/v2.0/token", form=credentials())
    logged = graph.calls("GET", "/v1.0/users/*")
    assert [r.status for r in logged] == [200, 401]
    first = logged[0]
    assert first.query == {"$top": ["1"]} and first.header("x-probe") == "yes"
    assert first.header("authorization") == f"Bearer {token}"
    assert "Bearer" not in repr(first)
    form = graph.calls("POST", "/*/oauth2/v2.0/token")[0].form()
    assert form["grant_type"] == "client_credentials" and form["client_id"] == CLIENT
    assert [r.seq for r in graph.requests] == [1, 2, 3]
    graph.clear_requests()
    assert graph.requests == []


def test_an_exception_in_a_handler_is_a_500_and_is_recorded(graph: FakeGraph) -> None:
    broken = Plugin("broken")

    @broken.route("GET", "/broken")
    def handler(ctx: Ctx) -> Reply:
        raise RuntimeError("boom")

    graph.use(broken)
    token = one_mail(graph)
    reply = graph.call("GET", f"{V1}/broken", token=token)
    assert reply.status == 500 and reply.error()[0] == "FakeGraphInternalError"
    assert "boom" in graph.internal_errors[0]


def test_a_plugin_adds_routes_rules_and_state_without_touching_the_core(graph: FakeGraph) -> None:
    tasks = Plugin("tasks")
    rule = tasks.rule(
        "tasks.needs_a_header", "A header is required.", source="example", source_date="2026-10-05"
    )

    @tasks.setup
    def make_store(server: FakeGraph) -> list[str]:
        return []

    @tasks.route("POST", "/things/{thing-id}", perm=fake_graph_mail.READ)
    def create(ctx: Ctx) -> Reply:
        ctx.store.append(ctx.params["thing_id"])
        return Reply(201, {"id": ctx.params["thing_id"], "link": ctx.link("/things/x")})

    graph.use(tasks)
    token = one_mail(graph)
    reply = graph.call("POST", f"{V1}/things/42", token=token)
    assert reply.status == 201 and reply.json()["link"] == f"{graph.url}/v1.0/things/x"
    assert graph.store("tasks") == ["42"]
    assert rule in graph.rules


def test_env_and_connector_options_describe_the_local_server(graph: FakeGraph) -> None:
    app = graph.add_app(CLIENT, SECRET)
    env = graph.env(app, MS_MAILBOX="ada@contoso.test")
    assert env == {
        "AZURE_TENANT_ID": graph.tenant_id,
        "AZURE_CLIENT_ID": CLIENT,
        "AZURE_CLIENT_SECRET": SECRET,
        "AZURE_AUTHORITY_HOST": graph.url,
        "MS_GRAPH_URL": graph.url,
        "MS_MAILBOX": "ada@contoso.test",
    }
    assert graph.url.startswith("http://127.0.0.1:")
    assert graph.connector_options == {"allow_http": True, "allow_private_networks": True}
    assert graph.token_url().endswith(f"/{graph.tenant_id}/oauth2/v2.0/token")


def test_the_clock_is_controllable_and_drives_created_times(graph: FakeGraph) -> None:
    token = one_mail(graph)
    ada = graph.find_user("ada@contoso.test")
    graph.clock.advance(3600)
    later = graph.store("mail").add_message(ada, subject="later")
    assert later["receivedDateTime"] == "2026-10-05T10:00:00Z"
    assert graph.call("GET", messages_url(), token=token).status == 401  # the token expired


def test_stop_is_idempotent_and_a_stopped_server_has_no_address() -> None:
    server = FakeGraph().start()
    assert server.url.startswith("http://127.0.0.1:")
    server.stop()
    server.stop()
    with pytest.raises(RuntimeError):
        _ = server.url


def test_rules_carry_their_provenance() -> None:
    with FakeGraph() as graph:
        rules = graph.rules
    assert len(rules) >= 30
    ids = [rule.id for rule in rules]
    assert len(ids) == len(set(ids))
    for rule in rules:
        assert rule.text and rule.source and rule.source_date
        assert rule.source_date == "unverified" or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", rule.source_date
        )
        assert rule.verified_live is False  # until the live acceptance run confirms it


def test_every_rule_has_a_test() -> None:
    import test_connect_microsoft  # noqa: F401  (the certificate rule)
    import test_connectors_teams  # noqa: F401  (the sign-in and Teams rules)
    import test_fake_graph_mail  # noqa: F401  (registers its proves())
    import test_fake_graph_services  # noqa: F401  (registers its proves())
    import test_microsoft_auth  # noqa: F401  (the sign-in rules)

    with FakeGraph() as graph:
        missing = sorted({rule.id for rule in graph.rules} - COVERED_RULES)
    assert not missing, f"rules without a proving test: {missing}"


def test_token_lifetime_is_configurable_and_timedelta_free_tokens_expire_on_the_clock() -> None:
    with FakeGraph(token_lifetime=120) as graph:
        app = graph.add_app(CLIENT, SECRET, roles=("Mail.Read",))
        graph.add_user("ada@contoso.test")
        token = graph.app_token(app)
        assert decode_jwt(token)["exp"] - decode_jwt(token)["iat"] == 120
        graph.clock.advance(timedelta(minutes=3).total_seconds())
        assert graph.call("GET", messages_url(), token=token).status == 401


def test_fakegraph_listens_on_loopback_only() -> None:
    with FakeGraph() as graph:
        assert graph.url.startswith("http://127.0.0.1:")


APP = "11111111-2222-3333-4444-555555555555"
CHECK_SECRET = "s3cret-value-for-tests"
SEARCH = '/v1.0/users?$search="displayName:ad"&$count=true&$top=1'


@proves("sharepoint.sites_selected_cannot_read_the_root_site")
def test_sites_selected_cannot_read_the_root_site_but_sites_read_all_can() -> None:
    with FakeGraph() as graph:
        graph.store("files").add_site("contoso.sharepoint.com", "sites/sales")
        selected = graph.add_app(APP, CHECK_SECRET, roles=("Sites.Selected",))
        assert graph.call("GET", "/v1.0/sites/root", token=graph.app_token(selected)).status == 403
        wide = graph.add_app(
            "22222222-2222-3333-4444-555555555555", CHECK_SECRET, roles=("Sites.Read.All",)
        )
        reply = graph.call("GET", "/v1.0/sites/root", token=graph.app_token(wide))
        assert reply.status == 200 and reply.json()["webUrl"] == "https://contoso.sharepoint.com/"


@proves("people.search_needs_consistency_level", "people.list_users_permissions")
def test_people_search_needs_the_header_count_and_user_read_all() -> None:
    with FakeGraph() as graph:
        graph.use(directory_plugin)
        graph.add_user("ada@contoso.test", name="Ada Lovelace")
        graph.add_user("bob@contoso.test", name="Bob")
        reader = graph.app_token(graph.add_app(APP, CHECK_SECRET, roles=("User.Read.All",)))
        eventual = {"ConsistencyLevel": "eventual"}
        assert graph.call("GET", SEARCH, token=reader).status == 400
        found = graph.call("GET", SEARCH, token=reader, headers=eventual)
        assert found.status == 200
        assert [u["userPrincipalName"] for u in found.json()["value"]] == ["ada@contoso.test"]
        mail_only = graph.add_app(
            "33333333-2222-3333-4444-555555555555", CHECK_SECRET, roles=("Mail.Read",)
        )
        denied = graph.call("GET", SEARCH, token=graph.app_token(mail_only), headers=eventual)
        assert denied.status == 403
