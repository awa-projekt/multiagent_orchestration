"""The generic connector engine: auth, pagination, retries, errors, redaction."""

from __future__ import annotations

import base64
import dataclasses
import json
from typing import Any

import pytest

from awa._internal.upstream.tools import ToolException
from awa.connectors import (
    ApiKeyAuth,
    BasicAuth,
    BearerAuth,
    ConnectorSpec,
    EnvVar,
    HttpRequest,
    MockTransport,
    OAuth2ClientCredentials,
    OAuth2RefreshToken,
    Operation,
    Pagination,
    Param,
    _engine,
    connector_tools,
    mock_response,
)
from awa.connectors._catalog import connector_context
from awa.connectors._engine import xml_to_data
from awa.errors import ConfigurationError
from awa.tools.approval import APPROVAL_METADATA_KEY

SECRET = "s3cr3t-token-value-XYZ"
BASE = "https://api.example.test"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(_engine, "_sleep", slept.append)
    return slept


def spec(*operations: Operation, **options: Any) -> ConnectorSpec:
    values: dict[str, Any] = {
        "name": "demo",
        "title": "Demo",
        "category": "data",
        "summary": "A demo system.",
        "base_url": "{DEMO_URL}",
        "env": (EnvVar("DEMO_URL", "Address.", secret=False),),
        "auth": (BearerAuth(EnvVar("DEMO_TOKEN", "Token.")),),
        "operations": operations,
    }
    values.update(options)
    return ConnectorSpec(**values)


ENV = {"DEMO_URL": BASE, "DEMO_TOKEN": SECRET}
LIST = Operation(
    "list_items",
    "List items.",
    path="/items",
    items="data",
    pages=Pagination("cursor", next="next", param="cursor", size_param="size", size_max=2),
)
GET = Operation(
    "get_item",
    "Get one item.",
    path="/items/{item_id}",
    params=(Param("item_id", "Id.", location="path", required=True),),
)
CREATE = Operation(
    "create_item",
    "Create an item.",
    method="POST",
    path="/items",
    params=(Param("title", "Title.", location="body", key="fields.title", required=True),),
    write=True,
)


def content(result: str) -> Any:
    """The content of a framed tool result, parsed as JSON when it is JSON."""

    assert result.startswith("<tool_output"), result
    text = json.loads(result.split(">", 1)[1].rsplit("</tool_output>", 1)[0])["content"]
    try:
        return json.loads(text)
    except ValueError:
        return text


def tools_of(connector: ConnectorSpec, transport: MockTransport, **config: Any) -> dict[str, Any]:
    tools = connector_tools(connector, env=ENV, config=config, transport=transport)
    return {tool.name: tool for tool in tools}


def test_request_url_method_auth_and_json_result() -> None:
    transport = MockTransport({"GET /items/a%2Fb": mock_response(json={"id": "a/b"})})
    tools = tools_of(spec(GET), transport)
    result = tools["demo_get_item"].invoke({"item_id": "a/b"})
    assert content(result) == {"id": "a/b"}
    [request] = transport.requests
    assert request.method == "GET"
    assert request.url == f"{BASE}/items/a%2Fb"
    assert request.header("authorization") == f"Bearer {SECRET}"
    assert SECRET not in repr(request)


def test_cursor_pagination_collects_up_to_the_limit() -> None:
    pages = [
        mock_response(json={"data": [1, 2], "next": "c2"}),
        mock_response(json={"data": [3, 4], "next": "c3"}),
    ]
    transport = MockTransport({"GET /items": pages})
    tools = tools_of(spec(LIST), transport)
    data = content(tools["demo_list_items"].invoke({"limit": 3}))
    token = data.pop("next_page")
    assert data == {"items": [1, 2, 3], "count": 3, "has_more": True}
    assert [r.url for r in transport.requests] == [
        f"{BASE}/items?size=2",
        f"{BASE}/items?size=2&cursor=c2",
    ]
    transport.add("GET /items", mock_response(json={"data": [3, 4]}))
    rest = content(tools["demo_list_items"].invoke({"limit": 3, "page_token": token}))
    assert rest == {"items": [4], "count": 1, "has_more": False}  # 3 was already returned
    assert transport.requests[-1].url == f"{BASE}/items?size=2&cursor=c2"
    assert "not a next_page" in tools["demo_list_items"].invoke({"page_token": "garbage"})


@pytest.mark.parametrize(
    ("pages", "responses", "expected"),
    [
        (
            Pagination("next_url", next="links.next", relative_to="base"),
            [
                mock_response(json={"data": [1], "links": {"next": "/items?page=2"}}),
                mock_response(json={"data": [2]}),
            ],
            [f"{BASE}/items", f"{BASE}/items?page=2"],
        ),
        (
            Pagination("link_header", size_param="per_page"),
            [
                mock_response(
                    json={"data": [1]},
                    headers={"Link": f'<{BASE}/items?page=2>; rel="next", <x>; rel="last"'},
                ),
                mock_response(json={"data": [2]}),
            ],
            [f"{BASE}/items?per_page=50", f"{BASE}/items?page=2"],
        ),
        (
            Pagination("offset", param="offset", size_param="limit", size_max=1),
            [
                mock_response(json={"data": [1]}),
                mock_response(json={"data": [2]}),
                mock_response(json={"data": []}),
            ],
            [
                f"{BASE}/items?limit=1",
                f"{BASE}/items?limit=1&offset=1",
                f"{BASE}/items?limit=1&offset=2",
            ],
        ),
        (
            Pagination("cursor", next="data.-1.id", param="after", more="has_more"),
            [
                mock_response(json={"data": [{"id": "x1"}], "has_more": True}),
                mock_response(json={"data": [{"id": "x2"}], "has_more": False}),
            ],
            [f"{BASE}/items", f"{BASE}/items?after=x1"],
        ),
    ],
)
def test_pagination_kinds(pages: Pagination, responses: list[Any], expected: list[str]) -> None:
    operation = Operation("list_items", "List.", path="/items", items="data", pages=pages)
    transport = MockTransport({"GET /items": responses})
    tools_of(spec(operation), transport)["demo_list_items"].invoke({"limit": 50})
    assert [r.url for r in transport.requests] == expected


def test_body_cursor_pagination() -> None:
    operation = Operation(
        "search",
        "Search.",
        method="POST",
        path="/search",
        params=(Param("query", "Words.", location="body"),),
        items="results",
        pages=Pagination(
            "cursor",
            next="next_cursor",
            param="start",
            location="body",
            size_param="page_size",
            more="has_more",
        ),
    )
    transport = MockTransport(
        {
            "POST /search": [
                mock_response(json={"results": [1], "next_cursor": "n2", "has_more": True}),
                mock_response(json={"results": [2], "has_more": False}),
            ]
        }
    )
    tools_of(spec(operation), transport)["demo_search"].invoke({"query": "q", "limit": 5})
    assert [r.json() for r in transport.requests] == [
        {"query": "q", "page_size": 5},
        {"query": "q", "page_size": 5, "start": "n2"},
    ]


@pytest.mark.parametrize(
    ("pages", "first", "second", "second_url"),
    [
        (
            Pagination("next_url", next="next"),
            {"data": [], "next": f"{BASE}/items?page=2"},
            {"data": [7]},
            f"{BASE}/items?page=2",
        ),
        (
            Pagination("cursor", next="next", param="cursor"),
            {"data": [], "next": "c2"},
            {"data": [7]},
            f"{BASE}/items?cursor=c2",
        ),
    ],
)
def test_an_empty_page_with_a_continuation_does_not_end_the_listing(
    pages: Pagination, first: Any, second: Any, second_url: str
) -> None:
    # Some services (Microsoft Graph for filtered or searched lists) send an empty page that
    # still has a next link; stopping there would lose every item behind it.
    operation = Operation("list_items", "List.", path="/items", items="data", pages=pages)
    transport = MockTransport(
        {"GET /items": [mock_response(json=first), mock_response(json=second)]}
    )
    result = content(tools_of(spec(operation), transport)["demo_list_items"].invoke({"limit": 5}))
    assert result == {"items": [7], "count": 1, "has_more": False}
    assert transport.requests[-1].url == second_url


def test_a_listing_answered_with_text_is_an_error_not_an_empty_list() -> None:
    # A captive portal or proxy answers 200 with a page: "no items" would be a false statement.
    operation = Operation("list_items", "List.", path="/items", items="data")
    page = mock_response(text="<html>Sign in to continue</html>", content_type="text/html")
    transport = MockTransport({"GET /items": page})
    result = tools_of(spec(operation), transport)["demo_list_items"].invoke({})
    assert result.startswith("Error:") and "not a list" in result
    assert "Sign in" not in result  # the page itself is not echoed


@pytest.mark.parametrize("response", [None, "json"])
def test_an_answer_cut_at_the_size_limit_is_an_error(response: str | None) -> None:
    cut = dataclasses.replace(mock_response(json={"id": 1, "name": "x" * 50}), truncated=True)
    listing = Operation("list_items", "List.", path="/items", items="data", response=response)
    single = Operation("get_item", "Get.", path="/items/1", response=response)
    transport = MockTransport({"GET /items": cut, "GET /items/1": cut})
    tools = tools_of(spec(listing, single), transport)
    for name in ("demo_list_items", "demo_get_item"):
        result = tools[name].invoke({})
        assert result.startswith("Error:") and "max_response_bytes" in result, name


def test_text_answers_cut_at_the_size_limit_stay_usable() -> None:
    # A long text page is read up to the limit on purpose; only structured data must be whole.
    cut = dataclasses.replace(mock_response(text="first part"), truncated=True)
    operation = Operation("read_page", "Read.", path="/page", response="text")
    tools = tools_of(spec(operation), MockTransport({"GET /page": cut}))
    assert content(tools["demo_read_page"].invoke({})) == "first part"


def test_a_next_link_to_another_host_is_refused() -> None:
    operation = Operation(
        "list_items",
        "List.",
        path="/items",
        items="data",
        pages=Pagination("next_url", next="next"),
    )
    transport = MockTransport(
        {"GET /items": mock_response(json={"data": [1], "next": "https://evil.test/steal"})}
    )
    result = tools_of(spec(operation), transport)["demo_list_items"].invoke({"limit": 5})
    assert result.startswith("Error:") and "not in the allowed hosts" in result
    assert len(transport.requests) == 1


def test_writes_wait_for_approval_unless_opted_out() -> None:
    transport = MockTransport({"POST /items": mock_response(201, json={"id": 7})})
    gated = tools_of(spec(CREATE), transport)["demo_create_item"]
    assert gated.metadata[APPROVAL_METADATA_KEY] is True
    assert gated.metadata["awa_risk"]["external_action"] is True
    assert "Needs human approval." in gated.description
    direct = tools_of(spec(CREATE), transport, requires_approval=False)["demo_create_item"]
    assert APPROVAL_METADATA_KEY not in direct.metadata
    assert content(direct.invoke({"title": "New"})) == {"id": 7}
    assert transport.requests[-1].json() == {"fields": {"title": "New"}}
    assert transport.requests[-1].header("content-type") == "application/json"


def test_include_and_read_only_select_operations() -> None:
    transport = MockTransport()
    assert set(tools_of(spec(LIST, GET, CREATE), transport, read_only=True)) == {
        "demo_list_items",
        "demo_get_item",
    }
    assert set(tools_of(spec(LIST, GET, CREATE), transport, include=["get_item"])) == {
        "demo_get_item"
    }
    with pytest.raises(ConfigurationError, match="no operation 'nope'"):
        tools_of(spec(GET), transport, include=["nope"])
    with pytest.raises(ConfigurationError, match="unknown option 'colour'"):
        tools_of(spec(GET), transport, colour="red")


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (401, "rejected the credentials (HTTP 401): bad token; check DEMO_TOKEN"),
        (403, "denied access (HTTP 403)"),
        (404, "found nothing there (HTTP 404)"),
        (409, "rejected the request (HTTP 409): bad token"),
        (500, "had a server error (HTTP 500)"),
    ],
)
def test_error_statuses_are_mapped_and_redacted(status: int, text: str) -> None:
    body = {"message": f"bad token {SECRET}"}
    transport = MockTransport({"GET /items/1": mock_response(status, json=body)})
    result = tools_of(spec(GET), transport)["demo_get_item"].invoke({"item_id": "1"})
    assert result.startswith("Error: Demo ") and text.split(": bad")[0] in result
    assert SECRET not in result and "***" in result


def test_ok_field_errors_on_http_200() -> None:
    transport = MockTransport({"GET /items/1": mock_response(json={"ok": False, "error": "nope"})})
    connector = spec(GET, ok="ok", errors=("error",))
    result = tools_of(connector, transport)["demo_get_item"].invoke({"item_id": "1"})
    assert result == "Error: Demo reported an error: nope"


def test_rate_limits_are_retried_after_the_announced_delay(no_sleep: list[float]) -> None:
    transport = MockTransport(
        {
            "GET /items/1": [
                mock_response(429, headers={"Retry-After": "2"}),
                mock_response(503),
                mock_response(json={"id": 1}),
            ]
        }
    )
    tool = tools_of(spec(GET), transport)["demo_get_item"]
    assert content(tool.invoke({"item_id": "1"})) == {"id": 1}
    assert no_sleep == [2.0, 1.0]


def test_writes_are_not_retried_after_server_errors(no_sleep: list[float]) -> None:
    transport = MockTransport({"POST /items": [mock_response(503), mock_response(201)]})
    tool = tools_of(spec(CREATE), transport, requires_approval=False)["demo_create_item"]
    assert "HTTP 503" in tool.invoke({"title": "x"})
    assert len(transport.requests) == 1 and no_sleep == []


def test_a_long_retry_after_is_reported_not_waited_for(no_sleep: list[float]) -> None:
    transport = MockTransport({"GET /items/1": mock_response(429, headers={"Retry-After": "600"})})
    result = tools_of(spec(GET), transport)["demo_get_item"].invoke({"item_id": "1"})
    assert "rate limiting" in result and "retry after 600 seconds" in result
    assert no_sleep == []


def test_connection_failures_of_reads_are_retried(no_sleep: list[float]) -> None:
    calls: list[int] = []

    def flaky(request: HttpRequest) -> Any:
        calls.append(1)
        if len(calls) < 3:
            raise ToolException(f"the request failed: reset while sending {SECRET}")
        return mock_response(json={"ok": 1})

    transport = MockTransport({"GET /items/1": flaky})
    tool = tools_of(spec(GET), transport)["demo_get_item"]
    assert content(tool.invoke({"item_id": "1"})) == {"ok": 1}
    transport = MockTransport({"GET /items/1": flaky})
    calls.clear()
    result = tools_of(spec(GET), transport, retries=0)["demo_get_item"].invoke({"item_id": "1"})
    assert result.startswith("Error: the request failed") and SECRET not in result


def test_unconfigured_connector_names_the_missing_variables() -> None:
    tools = connector_tools(spec(GET), env={}, transport=MockTransport())
    result = tools[0].invoke({"item_id": "1"})
    assert result == "Error: Demo is not configured: set the environment variable DEMO_URL"


def test_variables_can_be_renamed_and_the_base_url_overridden() -> None:
    transport = MockTransport({"GET /v2/items/1": mock_response(json={})})
    tools = connector_tools(
        spec(GET),
        env={"OTHER_TOKEN": "tok-other-123", "CUSTOM_URL": "https://custom.example.test/v2"},
        config={"env": {"DEMO_TOKEN": "OTHER_TOKEN"}, "base_url_env": "CUSTOM_URL"},
        transport=transport,
    )
    tools[0].invoke({"item_id": "1"})
    assert transport.requests[0].url == "https://custom.example.test/v2/items/1"
    assert transport.requests[0].header("Authorization") == "Bearer tok-other-123"


def test_basic_and_api_key_auth() -> None:
    basic = spec(
        GET,
        auth=(
            BasicAuth(
                EnvVar("U", "user", secret=False), EnvVar("P", "pw"), username_format="{}/token"
            ),
        ),
    )
    transport = MockTransport({"GET /items/1": mock_response(json={})})
    connector_tools(
        basic, env={"DEMO_URL": BASE, "U": "me@x.test", "P": SECRET}, transport=transport
    )[0].invoke({"item_id": "1"})
    expected = base64.b64encode(f"me@x.test/token:{SECRET}".encode()).decode()
    assert transport.requests[0].header("Authorization") == f"Basic {expected}"
    keyed = spec(GET, auth=(ApiKeyAuth(EnvVar("K", "key"), header="X-Key", prefix="Token "),))
    transport = MockTransport({"GET /items/1": mock_response(json={})})
    connector_tools(keyed, env={"DEMO_URL": BASE, "K": SECRET}, transport=transport)[0].invoke(
        {"item_id": "1"}
    )
    assert transport.requests[0].header("X-Key") == f"Token {SECRET}"


def test_auth_alternatives_use_the_first_complete_one() -> None:
    connector = spec(
        GET,
        auth=(
            BasicAuth(EnvVar("U", "user", secret=False), EnvVar("P", "pw")),
            BearerAuth(EnvVar("T", "token")),
        ),
    )
    ctx = connector_context(connector, env={"DEMO_URL": BASE, "U": "me"})
    assert ctx.missing() == ("P",)
    transport = MockTransport({"GET /items/1": mock_response(json={})})
    env = {"DEMO_URL": BASE, "U": "me", "T": SECRET}
    [read] = connector_tools(connector, env=env, transport=transport)
    read.invoke({"item_id": "1"})
    assert transport.requests[0].header("Authorization") == f"Bearer {SECRET}"


def test_oauth_client_credentials_token_is_cached_and_renewed_after_401() -> None:
    connector = spec(
        GET,
        auth=(
            OAuth2ClientCredentials(
                "https://login.example.test/{TENANT}/token",
                EnvVar("CID", "id", secret=False),
                EnvVar("CSECRET", "secret"),
                scope="api/.default",
                extra=(EnvVar("TENANT", "tenant", secret=False),),
            ),
        ),
    )
    tokens = iter(["token-one-111", "token-two-222"])
    transport = MockTransport(
        {
            "POST https://login.example.test/t1/token": lambda request: mock_response(
                json={"access_token": next(tokens), "expires_in": 3600}
            ),
            "GET /items/1": [
                mock_response(json={"n": 1}),
                mock_response(401),
                mock_response(json={"n": 2}),
            ],
        }
    )
    env = {"DEMO_URL": BASE, "CID": "client", "CSECRET": SECRET, "TENANT": "t1"}
    [tool] = connector_tools(connector, env=env, transport=transport)
    tool.invoke({"item_id": "1"})
    tool.invoke({"item_id": "1"})
    token_requests = [r for r in transport.requests if r.url.endswith("/token")]
    assert len(token_requests) == 2
    assert token_requests[0].body is not None and b"grant_type=client_credentials" in (
        token_requests[0].body
    )
    assert b"scope=api%2F.default" in token_requests[0].body
    auths = [r.header("Authorization") for r in transport.requests if "/items/" in r.url]
    assert auths == ["Bearer token-one-111", "Bearer token-one-111", "Bearer token-two-222"]


def test_oauth_token_failure_is_explained_without_the_secret() -> None:
    connector = spec(
        GET,
        auth=(
            OAuth2RefreshToken(
                "https://oauth.example.test/token",
                EnvVar("CID", "id", secret=False),
                EnvVar("CSECRET", "secret"),
                EnvVar("REFRESH", "refresh"),
            ),
        ),
    )
    transport = MockTransport(
        {
            "POST /token": mock_response(
                400, json={"error": "invalid_grant", "error_description": f"bad {SECRET}"}
            )
        }
    )
    env = {"DEMO_URL": BASE, "CID": "c", "CSECRET": "cs-secret-1", "REFRESH": SECRET}
    result = connector_tools(connector, env=env, transport=transport)[0].invoke({"item_id": "1"})
    assert "no access token from oauth.example.test (HTTP 400)" in result
    assert "check CID, CSECRET, REFRESH" in result and SECRET not in result
    assert b"grant_type=refresh_token" in (transport.requests[0].body or b"")


def test_path_values_cannot_climb_out_of_the_base_path() -> None:
    operation = Operation(
        "read",
        "Read.",
        path="/files/{path}",
        params=(Param("path", "Path.", location="path", required=True, safe="/"),),
        response="text",
    )
    transport = MockTransport({"GET /files/a/b.txt": mock_response(text="content")})
    tools = tools_of(spec(operation), transport)
    assert content(tools["demo_read"].invoke({"path": "a/b.txt"})) == "content"
    assert "must not contain" in tools["demo_read"].invoke({"path": "a/../../etc"})
    # Without `/` kept, a bare `..` would still name the parent folder.
    get = tools_of(spec(GET), MockTransport())["demo_get_item"]
    assert "must not contain" in get.invoke({"item_id": ".."})


def test_header_arguments_cannot_replace_credentials() -> None:
    operation = Operation(
        "get",
        "Get.",
        path="/x",
        params=(Param("auth", "Header.", location="header", key="Authorization"),),
    )
    result = tools_of(spec(operation), MockTransport())["demo_get"].invoke({"auth": "Bearer x"})
    assert "cannot be set" in result


def test_form_raw_bodies_and_idempotency_keys() -> None:
    form = Operation(
        "refund",
        "Refund.",
        method="POST",
        path="/refunds",
        body_format="form",
        params=(
            Param("amount", "Amount.", type="integer", location="body"),
            Param("meta", "Metadata.", type="object", location="body"),
        ),
        idempotency_header="Idempotency-Key",
        write=True,
    )
    raw = Operation(
        "put",
        "Put.",
        method="PUT",
        path="/blob",
        body_format="raw",
        write=True,
        params=(Param("content", "Content.", location="body", required=True),),
    )
    transport = MockTransport()
    tools = tools_of(spec(form, raw), transport, requires_approval=False)
    tools["demo_refund"].invoke({"amount": 5, "meta": {"order": "7"}})
    tools["demo_put"].invoke({"content": "hello"})
    first, second = transport.requests
    assert first.body == b"amount=5&meta%5Border%5D=7"
    assert first.header("Content-Type") == "application/x-www-form-urlencoded"
    assert len(first.header("Idempotency-Key") or "") == 36
    assert second.body == b"hello" and second.method == "PUT"


def test_empty_write_answers_are_confirmed() -> None:
    transport = MockTransport({"POST /items": mock_response(204, text="")})
    tool = tools_of(spec(CREATE), transport, requires_approval=False)["demo_create_item"]
    assert content(tool.invoke({"title": "x"})) == {"ok": True, "status": 204}


def test_csrf_token_is_fetched_before_writes() -> None:
    transport = MockTransport(
        {
            "GET /": mock_response(
                headers={"X-CSRF-Token": "csrf-abc-123", "Set-Cookie": ["s=1; Path=/", "t=2"]}
            ),
            "POST /items": mock_response(201, json={}),
        }
    )
    connector = spec(CREATE, csrf_header="X-CSRF-Token", options=("csrf",))
    tools_of(connector, transport, requires_approval=False, csrf=True)["demo_create_item"].invoke(
        {"title": "x"}
    )
    fetch_token, write = transport.requests
    assert fetch_token.header("X-CSRF-Token") == "Fetch"
    assert write.header("X-CSRF-Token") == "csrf-abc-123"
    assert write.header("Cookie") == "s=1; t=2"


def test_xml_answers_are_parsed_and_document_types_refused() -> None:
    data = xml_to_data(
        '<?xml version="1.0"?><R xmlns="urn:x"><Item><K>a</K></Item><Item><K>b</K></Item>'
        "<Next>n</Next></R>"
    )
    assert data == {"R": {"Item": [{"K": "a"}, {"K": "b"}], "Next": "n"}}
    with pytest.raises(ToolException, match="document type"):
        xml_to_data('<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')


def test_an_xml_listing_follows_its_continuation_token() -> None:
    listing = Operation(
        "list_objects",
        "List objects.",
        path="/",
        response="xml",
        items="Result.Contents",
        pages=Pagination("cursor", next="Result.Next", param="token", size_param="max"),
    )
    page_one = "<Result><Contents><Key>a</Key></Contents><Next>tok==</Next></Result>"
    page_two = "<Result><Contents><Key>b</Key></Contents></Result>"
    transport = MockTransport(
        {
            "GET /": [
                mock_response(text=page_one, content_type="application/xml"),
                mock_response(text=page_two, content_type="application/xml"),
            ]
        }
    )
    result = tools_of(spec(listing), transport)["demo_list_objects"].invoke({"limit": 10})
    assert content(result)["items"] == [{"Key": "a"}, {"Key": "b"}]
    assert "token=tok%3D%3D" in transport.requests[1].url


def test_context_and_environment_reprs_hold_no_values() -> None:
    ctx = connector_context(spec(GET), env=ENV)
    assert SECRET not in repr(ctx) and SECRET not in repr(ctx.env)


def test_long_tool_names_are_shortened() -> None:
    long = Operation("x" * 70, "Long.", path="/x")
    [tool] = connector_tools(spec(long), env=ENV, transport=MockTransport())
    assert len(tool.name) == 64


def test_a_path_parameter_is_always_required() -> None:
    assert Param("sku", "Item number.", location="path").required is True


def test_an_operation_needs_a_path_parameter_for_every_placeholder() -> None:
    with pytest.raises(ConfigurationError, match="sku"):
        Operation("get_stock", "Stock.", path="/stock/{sku}")
    Operation(
        "get_stock", "Stock.", path="/stock/{sku}", params=(Param("sku", "Item.", location="path"),)
    )


def test_a_path_parameter_with_a_default_stays_optional() -> None:
    assert Param("folder", "Folder.", location="path", default="inbox").required is False
