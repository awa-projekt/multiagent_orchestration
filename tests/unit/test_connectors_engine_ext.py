"""Engine extensions for company-system connectors: identity, bodies, tokens, waits, writes.

Offline over `MockTransport`; `test_connectors_fake_graph.py` repeats the important ones over
real HTTP against FakeGraph.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from awa._internal.upstream.tools import ToolException
from awa.connectors import (
    BearerAuth,
    ConnectorSpec,
    EnvVar,
    HttpRequest,
    MockTransport,
    OAuth2ClientCredentials,
    Operation,
    Param,
    _engine,
    connector_tools,
    mock_response,
)
from awa.connectors._catalog import connector_context
from awa.connectors._engine import RetryAfter, UncertainOutcome, call_operation
from awa.connectors._spec import ConnectorSpec as Spec
from awa.connectors._spec import TokenAuth, UploadSession, auth_identity
from awa.errors import ConfigurationError

BASE = "https://api.example.test"
SECRET = "client-secret-value-123"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(_engine, "_sleep", slept.append)
    return slept


def spec(*operations: Operation, **fields: Any) -> ConnectorSpec:
    values: dict[str, Any] = {
        "name": "demo",
        "title": "Demo",
        "category": "data",
        "summary": "A demo system.",
        "base_url": BASE,
        "auth": (BearerAuth(EnvVar("DEMO_TOKEN", "Token.")),),
        "operations": operations,
    }
    values.update(fields)
    return ConnectorSpec(**values)


ENV = {"DEMO_TOKEN": "tok-demo-123456"}


def tool(connector: ConnectorSpec, transport: MockTransport, name: str, **config: Any) -> Any:
    tools = connector_tools(connector, env=ENV, config=config, transport=transport)
    return next(t for t in tools if t.name == f"demo_{name}")


# --------------------------------------------------------------------------- needs


def two_identities() -> ConnectorSpec:
    app = OAuth2ClientCredentials(
        f"{BASE}/token",
        EnvVar("APP_ID", "Id.", secret=False),
        EnvVar("APP_SECRET", "Secret."),
        scope="x/.default",
    )
    user = BearerAuth(EnvVar("USER_TOKEN", "A signed-in user's token."), identity="user")
    return spec(
        Operation("read", "Read.", path="/read"),
        Operation(
            "post", "Post as a person.", method="POST", path="/post", write=True, needs="user"
        ),
        Operation("sweep", "Needs the app.", path="/sweep", needs="app"),
        auth=(app, user),
        setup_steps={"user": "run `awa connect demo --user`", "app": "set APP_ID and APP_SECRET"},
    )


APP_ENV = {"APP_ID": "app-1", "APP_SECRET": SECRET}
USER_ENV = {"USER_TOKEN": "user-token-abcdef"}


def run(connector: ConnectorSpec, env: Mapping[str, str], op: str, transport: MockTransport) -> str:
    tools = connector_tools(
        connector, env=env, config={"requires_approval": False}, transport=transport
    )
    return str(next(t for t in tools if t.name == f"demo_{op}").invoke({}))


def test_an_operation_that_needs_a_person_refuses_an_app_identity_before_sending_anything() -> None:
    transport = MockTransport()
    result = run(two_identities(), APP_ENV, "post", transport)
    assert result.startswith("Error: Demo: post acts as a signed-in person")
    assert "set up with the app's own identity" in result
    assert "Setup step: run `awa connect demo --user`" in result
    assert transport.requests == []  # not even a token request


def test_an_operation_that_needs_the_app_refuses_a_signed_in_person() -> None:
    transport = MockTransport()
    result = run(two_identities(), USER_ENV, "sweep", transport)
    assert "acts as the app's own identity" in result and "set up with a signed-in person" in result
    assert "Setup step: set APP_ID and APP_SECRET" in result
    assert transport.requests == []


def test_the_matching_identity_and_any_operations_go_through() -> None:
    transport = MockTransport(
        {
            "POST /token": mock_response(
                json={"access_token": "app-token-xyz", "expires_in": 3600}
            ),
            "GET /sweep": mock_response(json={"ok": 1}),
            "GET /read": mock_response(json={"ok": 2}),
            "POST /post": mock_response(json={"ok": 3}),
        }
    )
    assert not run(two_identities(), APP_ENV, "sweep", transport).startswith("Error")
    assert not run(two_identities(), APP_ENV, "read", transport).startswith("Error")
    assert not run(two_identities(), USER_ENV, "post", transport).startswith("Error")
    assert not run(two_identities(), USER_ENV, "read", transport).startswith("Error")


def test_the_identity_of_each_authentication() -> None:
    connector = two_identities()
    assert [auth_identity(a) for a in connector.auth] == ["app", "user"]
    assert auth_identity(BearerAuth(EnvVar("T", "t"))) is None
    assert auth_identity(None) is None
    ctx = connector_context(connector, env=USER_ENV)
    assert ctx.identity() == "user"


def test_needs_without_a_setup_step_points_to_connectors_show() -> None:
    connector = spec(
        Operation("post", "Post.", method="POST", path="/p", needs="user"),
        auth=(
            OAuth2ClientCredentials(f"{BASE}/t", EnvVar("I", "i", secret=False), EnvVar("S", "s")),
        ),
    )
    result = run(connector, {"I": "x", "S": SECRET}, "post", MockTransport())
    assert "see `awa connectors show demo` for the setup" in result


def test_an_operation_with_needs_any_does_not_care_and_other_auths_have_no_identity() -> None:
    connector = spec(Operation("post", "Post.", method="POST", path="/p", needs="user"))
    # a plain API key has no identity: needs cannot be judged, so the call is not blocked
    transport = MockTransport({"POST /p": mock_response(json={"ok": True})})
    assert not run(connector, ENV, "post", transport).startswith("Error")


def test_needs_shows_in_the_connector_description() -> None:
    described = two_identities().to_dict()
    needs = {op["name"]: op.get("needs") for op in described["operations"]}
    assert needs == {"read": None, "post": "user", "sweep": "app"}


# --------------------------------------------------------------------------- body builder


def test_a_body_builder_shapes_the_body_from_the_arguments_and_merges_over_fixed_fields() -> None:
    def build(arguments: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "message": {
                "toRecipients": [{"emailAddress": {"address": a}} for a in arguments["to"]]
            },
            "attachments": [
                {"contentType": "card", "content": json.dumps({"text": arguments["text"]})}
            ],
        }

    operation = Operation(
        "send",
        "Send.",
        method="POST",
        path="/send",
        params=(
            Param(
                "to", "Recipients.", type="array", items="string", location="body", required=True
            ),
            Param("subject", "Subject.", location="body", key="message.subject", required=True),
            Param("text", "Text.", location="body", key="text"),
        ),
        body={"saveToSentItems": True, "message.body.contentType": "Text"},
        body_builder=build,
        write=True,
    )
    transport = MockTransport({"POST /send": mock_response(status=202, text="")})
    tool(spec(operation), transport, "send", requires_approval=False).invoke(
        {"to": ["a@x.test", "b@x.test"], "subject": "Hi", "text": "hello"}
    )
    body = transport.requests[0].json()
    assert body["saveToSentItems"] is True
    assert body["message"]["subject"] == "Hi"  # from a body argument
    assert body["message"]["body"] == {"contentType": "Text"}  # fixed field kept
    # nested mappings merged, not replaced
    assert body["message"]["toRecipients"] == [
        {"emailAddress": {"address": "a@x.test"}},
        {"emailAddress": {"address": "b@x.test"}},
    ]
    assert json.loads(body["attachments"][0]["content"]) == {"text": "hello"}
    assert body["to"] == ["a@x.test", "b@x.test"]


def test_a_body_builder_error_reaches_the_model_and_nothing_is_sent() -> None:
    def build(arguments: Mapping[str, Any]) -> dict[str, Any]:
        raise ToolException("a card needs at least one fact")

    operation = Operation(
        "card",
        "Post a card.",
        method="POST",
        path="/card",
        params=(Param("title", "Title.", location="body"),),
        body_builder=build,
        write=True,
    )
    transport = MockTransport()
    result = tool(spec(operation), transport, "card", requires_approval=False).invoke(
        {"title": "x"}
    )
    assert result == "Error: a card needs at least one fact"
    assert transport.requests == []


def test_a_body_builder_works_for_form_bodies_and_is_ignored_for_get() -> None:
    operation = Operation(
        "form",
        "Form.",
        method="POST",
        path="/form",
        body_format="form",
        body_builder=lambda args: {"a": {"b": "c"}},
        write=True,
    )
    transport = MockTransport({"POST /form": mock_response(json={})})
    tool(spec(operation), transport, "form", requires_approval=False).invoke({})
    assert transport.requests[0].body == b"a%5Bb%5D=c"
    reading = Operation("get", "Get.", path="/g", body_builder=lambda args: {"x": 1})
    transport = MockTransport({"GET /g": mock_response(json={})})
    tool(spec(reading), transport, "get").invoke({})
    assert transport.requests[0].body is None


# --------------------------------------------------------------------------- definitions


def test_file_operations_and_sessions_are_validated_when_declared() -> None:
    local = Param("path", "Workspace path.", location="local", required=True)
    Operation("download", "d", path="/d", params=(local,), response="file", file_param="path")
    with pytest.raises(ConfigurationError, match="file_param must name a parameter"):
        Operation("download", "d", path="/d", response="file")
    with pytest.raises(ConfigurationError, match="file_param must name"):
        Operation(
            "download",
            "d",
            path="/d",
            params=(Param("path", "p"),),
            response="file",
            file_param="path",
        )
    with pytest.raises(ConfigurationError, match="not both"):
        Operation(
            "x",
            "x",
            path="/x",
            params=(local,),
            response="file",
            body_format="file",
            file_param="path",
        )
    with pytest.raises(ConfigurationError, match="no pages"):
        Operation(
            "x", "x", path="/x", params=(local,), response="file", file_param="path", items="v"
        )
    with pytest.raises(ConfigurationError, match="session needs"):
        Operation("x", "x", path="/x", session=UploadSession("/s"))
    with pytest.raises(ConfigurationError, match="body_builder needs"):
        Operation("x", "x", path="/x", body_format="raw", body_builder=lambda a: {})
    with pytest.raises(ConfigurationError, match="multiple of 327680"):
        UploadSession("/s", fragment=1000)
    assert UploadSession("/s").fragment % 327_680 == 0
    assert UploadSession("/s", fragment=10, align=1).fragment == 10


def test_a_local_parameter_is_never_sent() -> None:
    local = Param("save_to", "Workspace path.", location="local", required=True)
    operation = Operation("get", "Get.", path="/g", params=(local,))
    transport = MockTransport({"GET /g": mock_response(json={})})
    tool(spec(operation), transport, "get").invoke({"save_to": "out.bin"})
    assert "out.bin" not in transport.requests[0].url and transport.requests[0].body is None


# --------------------------------------------------------------------------- token provider


class CountingProvider:
    kind = "user"

    def __init__(self) -> None:
        self.issued = 0
        self.invalidated = 0

    def token(self, ctx: Any) -> str:
        self.issued += 1
        return f"provider-token-{self.issued:04d}"

    def invalidate(self, ctx: Any) -> None:
        self.invalidated += 1


def provider_spec(provider: CountingProvider, *operations: Operation) -> ConnectorSpec:
    return spec(
        *operations,
        auth=(
            TokenAuth(
                lambda ctx: provider,  # type: ignore[arg-type,return-value]
                kind="user",
                env=(EnvVar("LOGIN_HINT", "Who signed in.", secret=False),),
            ),
        ),
    )


def test_the_engine_asks_the_provider_for_a_bearer_and_redacts_it() -> None:
    provider = CountingProvider()
    operation = Operation("get", "Get.", path="/g")
    transport = MockTransport({"GET /g": mock_response(json={"echo": "provider-token-0001"})})
    connector = provider_spec(provider, operation)
    tools = connector_tools(connector, env={"LOGIN_HINT": "ada"}, transport=transport)
    result = tools[0].invoke({})
    assert transport.requests[0].header("authorization") == "Bearer provider-token-0001"
    assert "provider-token-0001" not in result and "***" in result  # redacted like any token
    tools[0].invoke({})
    assert provider.issued == 2  # the provider owns caching: the engine asks every time
    assert provider.invalidated == 0


def test_a_401_invalidates_the_provider_token_and_retries_once() -> None:
    provider = CountingProvider()
    operation = Operation("get", "Get.", path="/g")
    transport = MockTransport(
        {"GET /g": [mock_response(401, json={"error": "expired"}), mock_response(json={"ok": 1})]}
    )
    tools = connector_tools(
        provider_spec(provider, operation), env={"LOGIN_HINT": "ada"}, transport=transport
    )
    assert "ok" in tools[0].invoke({})
    assert provider.invalidated == 1 and provider.issued == 2
    assert [r.header("authorization") for r in transport.requests] == [
        "Bearer provider-token-0001",
        "Bearer provider-token-0002",
    ]
    always = MockTransport({"GET /g": mock_response(401, json={"error": "no"})})
    provider2 = CountingProvider()
    tools = connector_tools(
        provider_spec(provider2, operation), env={"LOGIN_HINT": "ada"}, transport=always
    )
    assert "rejected the credentials" in tools[0].invoke({})
    assert len(always.requests) == 2 and provider2.invalidated == 1  # once, not a loop


def test_a_provider_that_cannot_sign_in_explains_the_setup_step() -> None:
    class SignedOut(CountingProvider):
        def token(self, ctx: Any) -> str:
            raise ToolException("Demo: nobody is signed in; run `awa connect demo --user`")

    transport = MockTransport()
    tools = connector_tools(
        provider_spec(SignedOut(), Operation("get", "Get.", path="/g")),
        env={"LOGIN_HINT": "ada"},
        transport=transport,
    )
    assert tools[0].invoke({}) == "Error: Demo: nobody is signed in; run `awa connect demo --user`"
    assert transport.requests == []


def test_a_token_auth_is_used_only_when_its_variables_are_set_and_has_a_kind() -> None:
    provider = CountingProvider()
    connector = provider_spec(provider, Operation("get", "Get.", path="/g"))
    assert connector_context(connector, env={}).active_auth() is None
    assert connector_context(connector, env={"LOGIN_HINT": "ada"}).identity() == "user"


def test_the_hosts_a_provider_calls_are_allowed_through_its_token_urls() -> None:
    provider = CountingProvider()
    auth = TokenAuth(
        lambda ctx: provider, kind="app", token_urls=("https://login.example.test/{TENANT}/t",)
    )  # type: ignore[arg-type,return-value]
    connector = spec(
        Operation("get", "Get.", path="/g"),
        auth=(auth,),
        env=(EnvVar("TENANT", "Tenant.", secret=False),),
    )
    ctx = connector_context(connector, env={"TENANT": "t1"})
    assert ctx.policy().allowed_hosts is not None
    assert "login.example.test" in ctx.policy().allowed_hosts  # type: ignore[operator]


# --------------------------------------------------------------------------- waits and writes


def test_retry_after_zero_still_waits_a_second(no_sleep: list[float]) -> None:
    transport = MockTransport(
        {
            "GET /g": [
                mock_response(429, headers={"Retry-After": "0"}),
                mock_response(json={"ok": 1}),
            ]
        }
    )
    assert "ok" in tool(spec(Operation("get", "Get.", path="/g")), transport, "get").invoke({})
    assert no_sleep == [1.0]


def test_retry_after_accepts_an_http_date(no_sleep: list[float]) -> None:
    transport = MockTransport(
        {
            "GET /g": [
                mock_response(503, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
                mock_response(json={"ok": 1}),
            ]
        }
    )
    assert "ok" in tool(spec(Operation("get", "Get.", path="/g")), transport, "get").invoke({})
    assert no_sleep == [1.0]  # a date in the past is "now": back off one second


def test_a_wait_above_the_cap_is_a_typed_error_with_the_seconds() -> None:
    transport = MockTransport({"GET /g": mock_response(429, headers={"Retry-After": "120"})})
    connector = spec(Operation("get", "Get.", path="/g"))
    ctx = connector_context(connector, env=ENV, transport=transport)
    with pytest.raises(RetryAfter) as caught:
        call_operation(ctx, connector.operation("get"), {})
    assert caught.value.seconds == 120.0
    assert "retry after 120 seconds" in str(caught.value) and len(transport.requests) == 1
    assert isinstance(caught.value, ToolException)  # tools still report it as a plain error


def test_an_exhausted_429_without_a_header_is_still_a_retry_after_error() -> None:
    transport = MockTransport({"GET /g": mock_response(429)})
    connector = spec(Operation("get", "Get.", path="/g"))
    ctx = connector_context(connector, env=ENV, transport=transport, config={"retries": 1})
    with pytest.raises(RetryAfter) as caught:
        call_operation(ctx, connector.operation("get"), {})
    assert caught.value.seconds is None and len(transport.requests) == 2


def test_a_long_retry_after_on_a_503_read_is_also_surfaced(no_sleep: list[float]) -> None:
    transport = MockTransport({"GET /g": mock_response(503, headers={"Retry-After": "300"})})
    connector = spec(Operation("get", "Get.", path="/g"))
    ctx = connector_context(connector, env=ENV, transport=transport)
    with pytest.raises(RetryAfter) as caught:
        call_operation(ctx, connector.operation("get"), {})
    assert caught.value.seconds == 300.0 and no_sleep == []


def test_a_server_error_on_a_write_is_an_uncertain_outcome_and_never_repeated(
    no_sleep: list[float],
) -> None:
    transport = MockTransport({"POST /p": [mock_response(500), mock_response(201, json={})]})
    connector = spec(Operation("post", "Post.", method="POST", path="/p", write=True))
    ctx = connector_context(
        connector, env=ENV, transport=transport, config={"requires_approval": False}
    )
    with pytest.raises(UncertainOutcome, match="may or may not have been made"):
        call_operation(ctx, connector.operation("post"), {})
    assert len(transport.requests) == 1 and no_sleep == []


def test_a_lost_answer_on_a_write_is_uncertain_but_a_read_is_just_retried(
    no_sleep: list[float],
) -> None:
    token = ENV["DEMO_TOKEN"]

    def broken(request: HttpRequest) -> Any:
        raise ToolException(f"the request failed: connection reset {token}")

    reads: list[int] = []

    def flaky(request: HttpRequest) -> Any:
        reads.append(1)
        return broken(request) if len(reads) == 1 else mock_response(json={"ok": 1})

    transport = MockTransport({"POST /p": broken, "GET /g": flaky})
    connector = spec(
        Operation("post", "Post.", method="POST", path="/p", write=True),
        Operation("get", "Get.", path="/g"),
    )
    ctx = connector_context(connector, env=ENV, transport=transport)
    with pytest.raises(UncertainOutcome, match="may or may not have been processed") as caught:
        call_operation(ctx, connector.operation("post"), {})
    assert token not in str(caught.value) and len(transport.requests) == 1
    assert call_operation(ctx, connector.operation("get"), {}) == {"ok": 1}
    assert len(reads) == 2 and no_sleep == [0.5]


def test_a_429_on_a_write_is_safe_to_retry_because_nothing_was_processed(
    no_sleep: list[float],
) -> None:
    transport = MockTransport(
        {
            "POST /p": [
                mock_response(429, headers={"Retry-After": "3"}),
                mock_response(201, json={"id": 1}),
            ]
        }
    )
    connector = spec(Operation("post", "Post.", method="POST", path="/p", write=True))
    ctx = connector_context(connector, env=ENV, transport=transport)
    assert call_operation(ctx, connector.operation("post"), {}) == {"id": 1}
    assert no_sleep == [3.0] and len(transport.requests) == 2


def test_a_refused_request_is_neither_retried_nor_uncertain(no_sleep: list[float]) -> None:
    # The host resolves to the loopback address: refused before any connection is made.
    connector = Spec(
        name="demo",
        title="Demo",
        category="data",
        summary="s",
        base_url="https://localhost",
        auth=(BearerAuth(EnvVar("DEMO_TOKEN", "t")),),
        operations=(
            Operation("post", "Post.", method="POST", path="/p", write=True),
            Operation("get", "Get.", path="/g"),
        ),
    )
    ctx = connector_context(connector, env=ENV)
    for name in ("post", "get"):
        with pytest.raises(ToolException) as caught:
            call_operation(ctx, connector.operation(name), {})
        assert not isinstance(caught.value, UncertainOutcome)
    assert no_sleep == []
