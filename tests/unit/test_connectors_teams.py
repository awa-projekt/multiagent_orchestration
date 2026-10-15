"""The `teams` connector against FakeGraph: Workflows webhook cards and messages as a signed-in user.

The real connector runs over HTTP; only the clocks and sleeps are tied to the fake server's clock.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from _helpers.fake_graph_proof import proves
from _helpers.fakegraph import teams as fake_graph_teams
from _helpers.fakegraph.core import decode_jwt
from _helpers.fakegraph.server import FakeGraph
from _helpers.fakegraph.teams import add_system_message
from awa._internal.upstream.tools import ToolException
from awa.connectors import (
    EnvVar,
    _engine,
    check_connector,
    connector_tools,
    get_connector,
    list_connectors,
)
from awa.connectors import _microsoft_auth as auth
from awa.connectors import _saas_microsoft_teams as teams_module
from awa.connectors._catalog import connector_context
from awa.connectors._saas_microsoft_teams import TEAMS
from awa.connectors._spec import BearerAuth
from awa.tools.approval import APPROVAL_METADATA_KEY

PUBLIC = "99999999-8888-7777-6666-555555555555"
SCOPES = ("User.Read", "ChannelMessage.Send", "ChannelMessage.Read.All")
WORKFLOW = "wf-1"
SIGNATURE = "SIGNATURE-VALUE-1234"
ELEMENTS_1_2 = {"TextBlock", "FactSet", "ActionSet", "Container", "ColumnSet", "Column", "Image"}
ACTIONS_1_2 = {"Action.OpenUrl"}  # buttons that need no bot (Action.Submit/Execute do)


@pytest.fixture
def graph() -> Iterator[FakeGraph]:
    with FakeGraph() as server:
        server.add_app(PUBLIC, public_client=True, scopes=SCOPES)
        server.add_user("ada@contoso.test", name="Ada")
        server.identity.device_pending_polls = 0
        yield server


@pytest.fixture(autouse=True)
def seams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, graph: FakeGraph) -> list[float]:
    """Spacing and sign-in run on the fake server's clock; every wait advances it."""

    monkeypatch.chdir(tmp_path)
    slept: list[float] = []

    def wait(seconds: float) -> None:
        slept.append(seconds)
        graph.clock.advance(seconds)

    monkeypatch.setattr(teams_module, "_clock", graph.clock.time)
    monkeypatch.setattr(teams_module, "_sleep", wait)
    monkeypatch.setattr(auth, "_now", graph.clock.time)
    monkeypatch.setattr(auth, "_sleep", lambda seconds: graph.clock.advance(seconds))
    monkeypatch.setattr(_engine, "_sleep", lambda seconds: None)
    monkeypatch.setattr(teams_module._SPACE, "_next", {})
    return slept


def webhook(graph: FakeGraph, name: str = WORKFLOW) -> str:
    return f"{graph.url}/workflows/{name}?sig={SIGNATURE}"


def env(graph: FakeGraph, **extra: str) -> dict[str, str]:
    return {
        "TEAMS_WEBHOOK_URL": webhook(graph),
        "AZURE_TENANT_ID": graph.tenant_id,
        "AZURE_CLIENT_ID": PUBLIC,
        "AZURE_AUTHORITY_HOST": graph.authority,
        "MS_GRAPH_URL": graph.graph_url,
        **extra,
    }


def tools(graph: FakeGraph, spec: Any = "teams", **config: Any) -> Pinned:
    return Pinned(graph, spec, {**graph.connector_options, "requires_approval": False, **config})


class Pinned:
    """The tools of one connector instance per channel. The channel is pinned in .env
    (`TEAMS_TEAM_ID`, `TEAMS_CHANNEL_ID`), so an invocation that names `team_id` and `channel_id`
    runs on the instance pinned to them (built once per channel, as a deployment would be)."""

    def __init__(self, graph: FakeGraph, spec: Any, config: dict[str, Any]) -> None:
        self.graph, self.spec, self.config = graph, spec, config
        self._built: dict[tuple[str | None, str | None], dict[str, Any]] = {}

    def build(self, team: str | None, channel: str | None) -> dict[str, Any]:
        if (team, channel) not in self._built:
            extra = {"TEAMS_TEAM_ID": team, "TEAMS_CHANNEL_ID": channel} if team and channel else {}
            built = connector_tools(self.spec, env=env(self.graph, **extra), config=self.config)
            self._built[team, channel] = {t.name.removeprefix("teams_"): t for t in built}
        return self._built[team, channel]

    def __getitem__(self, name: str) -> PinnedTool:
        return PinnedTool(self, name)

    def __iter__(self) -> Iterator[str]:
        return iter(self.build(None, None))

    def __contains__(self, name: object) -> bool:
        return name in self.build(None, None)

    def values(self) -> list[Any]:
        return list(self.build(None, None).values())


class PinnedTool:
    """One tool of `Pinned`: `team_id` and `channel_id` choose the instance, not the call."""

    def __init__(self, owner: Pinned, name: str) -> None:
        self.owner, self.name = owner, name

    def invoke(self, arguments: dict[str, Any]) -> Any:
        values = dict(arguments)
        team, channel = values.pop("team_id", None), values.pop("channel_id", None)
        return self.owner.build(team, channel)[self.name].invoke(values)

    def __getattr__(self, attribute: str) -> Any:
        return getattr(self.owner.build(None, None)[self.name], attribute)


def sign_in(graph: FakeGraph, scopes: tuple[str, ...] = SCOPES, user: str | None = None) -> None:
    if user:
        graph.identity.device_user = user
    auth.sign_in(
        tenant=graph.tenant_id,
        client_id=PUBLIC,
        show=lambda prompt: None,
        scopes=scopes,
        authority=graph.authority,
        options=graph.connector_options,
    )


def world(graph: FakeGraph, member: bool = True) -> tuple[Any, Any, Any]:
    ada = graph.find_user("ada@contoso.test")
    teams = graph.store("teams")
    team = teams.add_team("Ops", *([ada] if member else []))
    return ada, team, teams.add_channel(team, "General")


def content(result: str) -> Any:
    assert result.startswith("<tool_output"), result
    text = json.loads(result.split(">", 1)[1].rsplit("</tool_output>", 1)[0])["content"]
    try:
        return json.loads(text)
    except ValueError:
        return text


def posted(graph: FakeGraph, name: str = WORKFLOW) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = graph.store("teams").workflow_posts.get(name, [])
    return result


def walk(node: Any) -> Iterator[dict[str, Any]]:
    """Every element and action of a card, whatever its depth."""

    if isinstance(node, dict):
        if "type" in node:
            yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


# --------------------------------------------------------------------------- the connector


def test_the_connector_has_the_listed_operations_and_identities() -> None:
    spec = get_connector("teams")
    assert spec is TEAMS and spec in list_connectors()
    by_name = {op.name: op for op in spec.operations}
    assert list(by_name) == [
        "post_card",
        "notify_webhook",
        "list_channel_messages",
        "post_message",
        "reply_message",
    ]
    assert {n for n, op in by_name.items() if op.needs == "user"} == {
        "list_channel_messages",
        "post_message",
        "reply_message",
    }
    assert {n for n, op in by_name.items() if op.write} == {
        "post_card",
        "notify_webhook",
        "post_message",
        "reply_message",
    }
    assert spec.to_dict()["operations"][2]["needs"] == "user"
    assert spec.setup_steps["user"].startswith("sign in the Teams user")


def test_nothing_is_configured_until_the_webhook_address_is_set() -> None:
    assert connector_context("teams", env={}).missing() == ("TEAMS_WEBHOOK_URL",)
    ok = connector_context("teams", env={"TEAMS_WEBHOOK_URL": "https://x.test/a?sig=b"})
    assert ok.missing() == ()
    result = check_connector("teams", env={})
    assert not result.ok and result.missing == ("TEAMS_WEBHOOK_URL",)


def test_writes_wait_for_approval_and_reading_does_not(graph: FakeGraph) -> None:
    built = {t.name.removeprefix("teams_"): t for t in connector_tools("teams", env=env(graph))}
    gated = {name for name, tool in built.items() if tool.metadata.get(APPROVAL_METADATA_KEY)}
    assert gated == {"post_card", "notify_webhook", "post_message", "reply_message"}
    only = connector_tools("teams", env=env(graph), config={"read_only": True})
    assert [t.name for t in only] == ["teams_list_channel_messages"]


def test_descriptions_remind_the_model_not_to_use_teams_as_a_log() -> None:
    texts = {op.name: op.description for op in TEAMS.operations}
    for name in ("post_card", "post_message"):
        assert "do not use Teams as a log" in texts[name]


def test_the_token_cache_and_account_are_options_other_names_are_not() -> None:
    connector_context("teams", env={}, config={"token_cache": "x.json", "account": "a@b.test"})
    with pytest.raises(Exception, match="unknown option"):
        connector_context("teams", env={}, config={"tokens": "x.json"})


# --------------------------------------------------------------------------- the webhook


@proves("teams.workflows_envelope")
def test_post_card_sends_one_adaptive_card_in_the_workflows_envelope(graph: FakeGraph) -> None:
    result = tools(graph)["post_card"].invoke(
        {
            "title": "Order A-17",
            "text": "Waiting for approval.",
            "facts": {"Customer": "ACME", "Total": "1,200 EUR"},
            "link_title": "Open",
            "link_url": "https://example.com/orders/A-17",
            "style": "warning",
        }
    )
    assert content(result)["accepted"] is True
    [sent] = posted(graph)
    assert sent["type"] == "message" and len(sent["attachments"]) == 1  # one card per call
    card = sent["attachments"][0]["content"]
    assert sent["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["version"] == "1.2" and card["type"] == "AdaptiveCard"
    assert card["body"][0]["text"] == "Order A-17" and card["body"][0]["color"] == "warning"
    assert card["body"][2]["facts"] == [
        {"title": "Customer", "value": "ACME"},
        {"title": "Total", "value": "1,200 EUR"},
    ]
    assert card["actions"] == [
        {"type": "Action.OpenUrl", "title": "Open", "url": "https://example.com/orders/A-17"}
    ]


def test_nothing_but_the_envelope_is_in_the_request_body(graph: FakeGraph) -> None:
    tools(graph)["post_card"].invoke({"title": "T", "text": "x"})
    tools(graph)["notify_webhook"].invoke({"text": "x"})
    for request in graph.calls("POST", "/workflows/*"):
        assert set(request.json()) == {"type", "attachments"}  # the arguments are not sent
        assert request.header("content-type") == "application/json"


def test_a_card_uses_only_elements_and_actions_of_adaptive_cards_1_2(graph: FakeGraph) -> None:
    tools(graph)["post_card"].invoke(
        {
            "title": "T",
            "text": "x",
            "facts": {"a": "b"},
            "link_title": "Open",
            "link_url": "https://example.com",
        }
    )
    tools(graph)["notify_webhook"].invoke({"text": "short"})
    for sent in posted(graph):
        card = sent["attachments"][0]["content"]
        kinds = {node["type"] for node in walk(card["body"])}
        assert kinds <= ELEMENTS_1_2, kinds
        assert {node["type"] for node in walk(card.get("actions", []))} <= ACTIONS_1_2
        # nothing newer than 1.2, no button that needs a bot
        assert not {"msteams", "Table", "Action.Submit", "Action.Execute"} & set(json.dumps(card))


def test_notify_webhook_is_a_one_line_card(graph: FakeGraph) -> None:
    tools(graph)["notify_webhook"].invoke({"text": "Backup finished"})
    [sent] = posted(graph)
    assert sent["attachments"][0]["content"]["body"] == [
        {"type": "TextBlock", "text": "Backup finished", "wrap": True}
    ]


def test_the_webhook_receives_no_credentials_at_all(graph: FakeGraph) -> None:
    sign_in(graph)  # both ways in are set up: the user's token exists
    ada, team, channel = world(graph)
    ops = tools(graph)
    ops["post_message"].invoke({"team_id": team.id, "channel_id": channel.id, "text": "hi"})
    ops["post_card"].invoke({"title": "T", "text": "x"})
    ops["notify_webhook"].invoke({"text": "x"})
    hooks = graph.calls("POST", "/workflows/*")
    assert len(hooks) == 2
    for request in hooks:
        assert request.header("authorization") is None and request.header("cookie") is None
    assert graph.calls("POST", "/v1.0/teams/*")[0].header("authorization").startswith("Bearer ")  # type: ignore[union-attr]


def test_a_base_url_option_does_not_redirect_the_webhook(graph: FakeGraph) -> None:
    ops = tools(graph, base_url="http://127.0.0.1:1/v1.0")  # a Graph address that is not there
    assert content(ops["notify_webhook"].invoke({"text": "x"}))["accepted"] is True
    assert len(posted(graph)) == 1


@pytest.mark.parametrize(
    "address",
    [
        "https://contoso.webhook.office.com/webhookb2/aaa@bbb/IncomingWebhook/ccc/ddd",
        "https://outlook.office.com/webhook/aaa@bbb/IncomingWebhook/ccc/ddd",
        "https://outlook.office365.com/webhook/aaa@bbb/IncomingWebhook/ccc/ddd",
    ],
)
def test_an_old_office_365_connector_address_is_refused_with_the_way_forward(
    graph: FakeGraph, address: str
) -> None:
    built = connector_tools(
        "teams",
        env=env(graph, TEAMS_WEBHOOK_URL=address),
        config={**graph.connector_options, "requires_approval": False},
    )
    ops = {t.name.removeprefix("teams_"): t for t in built}
    result = ops["post_card"].invoke({"title": "T", "text": "x"})
    assert "switched off in May 2026" in result and "Workflows" in result
    assert "template 'Send webhook alerts to a channel'" in result
    assert graph.requests == []
    assert not check_connector(
        "teams", env=env(graph, TEAMS_WEBHOOK_URL=address), config=graph.connector_options
    ).ok


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"title": " ", "text": "x"}, "title must not be empty"),
        ({"title": "T", "text": ""}, "text must not be empty"),
        ({"title": "x" * 201, "text": "x"}, "title is longer than 200"),
        ({"title": "T", "text": "x" * 8001}, "text is longer than 8000"),
        ({"title": "T", "text": "x", "facts": {str(n): "v" for n in range(21)}}, "more than 20"),
        ({"title": "T", "text": "x", "link_url": "https://x.test"}, "go together"),
        ({"title": "T", "text": "x", "link_title": "Open"}, "go together"),
        (
            {"title": "T", "text": "x", "link_title": "Open", "link_url": "http://x.test"},
            "https://",
        ),
        (
            {"title": "T", "text": "x", "link_title": "Open", "link_url": "javascript:alert(1)"},
            "https://",
        ),
    ],
)
def test_a_bad_card_is_refused_before_anything_is_sent(
    graph: FakeGraph, arguments: dict[str, Any], message: str
) -> None:
    result = tools(graph)["post_card"].invoke(arguments)
    assert result.startswith("Error:") and message in result
    assert graph.requests == [] and posted(graph) == []


def test_a_card_over_28_kb_is_refused_even_when_the_limits_per_field_are_kept(
    graph: FakeGraph,
) -> None:
    facts = {f"{n:02d}" + "名" * 78: "値" * 500 for n in range(20)}
    result = tools(graph)["post_card"].invoke(
        {"title": "名" * 200, "text": "文" * 8000, "facts": facts}
    )
    assert "larger than Teams allows (28 KB)" in result
    assert graph.requests == []


# --------------------------------------------------------------------------- one message per second


@proves("teams.workflows_four_requests_per_second")
def test_cards_to_one_webhook_are_at_least_1_1_seconds_apart(
    graph: FakeGraph, seams: list[float]
) -> None:
    fake_graph_teams.apply_workflow_limit(graph, per_second=1)  # stricter than Teams' four
    ops = tools(graph)
    for number in range(3):
        assert "accepted" in ops["notify_webhook"].invoke({"text": f"n{number}"})
    assert seams == [pytest.approx(1.1), pytest.approx(1.1)]  # the first goes at once
    assert [r.status for r in graph.calls("POST", "/workflows/*")] == [202, 202, 202]


def test_without_the_queue_the_third_card_would_be_throttled(
    graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_graph_teams.apply_workflow_limit(graph, per_second=1)
    monkeypatch.setattr(teams_module._SPACE, "gap", 0.0)  # no spacing: the control
    ops = tools(graph)
    ops["notify_webhook"].invoke({"text": "a"})
    result = ops["notify_webhook"].invoke({"text": "b"})
    assert "rate limiting the requests (HTTP 429)" in result
    assert 429 in [r.status for r in graph.calls("POST", "/workflows/*")]


def test_two_webhooks_do_not_wait_for_each_other(graph: FakeGraph, seams: list[float]) -> None:
    first = tools(graph)
    second = connector_tools(
        "teams",
        env=env(graph, TEAMS_WEBHOOK_URL=webhook(graph, "wf-2")),
        config={**graph.connector_options, "requires_approval": False},
    )
    other = next(t for t in second if t.name == "teams_notify_webhook")
    first["notify_webhook"].invoke({"text": "a"})
    other.invoke({"text": "b"})
    assert seams == []
    first["notify_webhook"].invoke({"text": "c"})
    assert seams == [pytest.approx(1.1)]
    assert len(posted(graph, "wf-1")) == 2 and len(posted(graph, "wf-2")) == 1


def test_waiting_senders_queue_in_order_and_the_queue_has_a_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(teams_module, "_sleep", waits.append)  # the clock stands still
    spacing = teams_module._Spacing(1.1)
    threads = [threading.Thread(target=spacing.wait, args=("k",)) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(waits) == [pytest.approx(x) for x in (1.1, 2.2, 3.3, 4.4)]
    with pytest.raises(ToolException, match="Too many Teams messages"):
        for _ in range(40):  # a queue longer than half a minute is refused
            spacing.wait("k")
    assert max(waits) <= 30.0
    spacing.wait("other")  # another destination is not affected


def test_delegated_posts_of_one_user_are_spaced_across_channels_and_reads_per_channel(
    graph: FakeGraph, seams: list[float]
) -> None:
    sign_in(graph)
    fake_graph_teams.apply_limits(graph)  # Teams' own 1 per second per channel and per user
    _, team, first = world(graph)
    second = graph.store("teams").add_channel(team, "Random")
    ops = tools(graph)
    ops["post_message"].invoke({"team_id": team.id, "channel_id": first.id, "text": "a"})
    ops["post_message"].invoke({"team_id": team.id, "channel_id": second.id, "text": "b"})
    assert seams == [pytest.approx(1.1)]  # the user's limit counts over channels
    ops["list_channel_messages"].invoke({"team_id": team.id, "channel_id": first.id})
    ops["list_channel_messages"].invoke({"team_id": team.id, "channel_id": second.id})
    assert seams == [pytest.approx(1.1)]  # reads are counted per channel
    ops["list_channel_messages"].invoke({"team_id": team.id, "channel_id": first.id})
    assert seams == [pytest.approx(1.1)] * 2
    assert 429 not in [r.status for r in graph.calls(path="/v1.0/teams/*")]


@proves("teams.channel_message_rate_limits")
def test_without_spacing_the_second_post_of_a_user_is_throttled_by_microsoft(
    graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(graph)
    fake_graph_teams.apply_limits(graph)
    monkeypatch.setattr(teams_module._SPACE, "gap", 0.0)
    _, team, channel = world(graph)
    ops = tools(graph)
    ops["post_message"].invoke({"team_id": team.id, "channel_id": channel.id, "text": "a"})
    result = ops["post_message"].invoke({"team_id": team.id, "channel_id": channel.id, "text": "b"})
    assert "limiting" in result  # what the queue exists to prevent


# --------------------------------------------------------------------------- webhook errors


def test_a_wrong_signature_is_explained_and_the_address_is_not_repeated(graph: FakeGraph) -> None:
    graph.store("teams").workflow_sig = "another-signature"
    result = tools(graph)["post_card"].invoke({"title": "T", "text": "x"})
    assert "refused the request" in result and "Copy the webhook URL again" in result
    assert SIGNATURE not in result and webhook(graph) not in result


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (400, "did not accept the card"),
        (404, "does not exist any more"),
        (413, "larger than Teams allows"),
    ],
)
def test_other_webhook_failures_are_explained(graph: FakeGraph, status: int, text: str) -> None:
    graph.fail("POST", "/workflows/*", status=status, times=1)
    assert text in tools(graph)["notify_webhook"].invoke({"text": "x"})


def test_a_long_wait_asked_by_the_service_is_reported_as_it_is(graph: FakeGraph) -> None:
    graph.throttle("/workflows/*", retry_after=120)
    result = tools(graph)["notify_webhook"].invoke({"text": "x"})
    assert "rate limiting" in result and "120 seconds" in result


def test_a_server_error_is_never_repeated_because_the_card_may_have_been_posted(
    graph: FakeGraph,
) -> None:
    graph.fail("POST", "/workflows/*", status=503, times=1)
    result = tools(graph)["notify_webhook"].invoke({"text": "x"})
    assert "may or may not" in result
    assert len(graph.calls("POST", "/workflows/*")) == 1


GUID = "3f2c1a9e-7b44-4d0a-9c11-5e6d2b8a1f00"


def test_the_address_never_shows_up_whatever_the_failure(graph: FakeGraph) -> None:
    url = f"{graph.url}/workflows/{GUID}?sig={SIGNATURE}&sv=1.0"
    graph.fail(
        "POST",
        "/workflows/*",
        status=500,
        message=f"failed for {url} {SIGNATURE} " + url.replace(":", "%3A"),
    )
    options = {**graph.connector_options, "requires_approval": False}
    built = connector_tools("teams", env=env(graph, TEAMS_WEBHOOK_URL=url), config=options)
    ops = {t.name.removeprefix("teams_"): t for t in built}
    outputs = [ops["notify_webhook"].invoke({"text": "x"})]
    gone = f"http://127.0.0.1:9/workflows/{GUID}?sig={SIGNATURE}"  # nothing listens there
    closed = connector_tools("teams", env=env(graph, TEAMS_WEBHOOK_URL=gone), config=options)
    outputs.append(
        next(t for t in closed if t.name == "teams_post_card").invoke({"title": "T", "text": "x"})
    )
    outputs.append(
        check_connector(
            "teams", env=env(graph, TEAMS_WEBHOOK_URL=gone), config=graph.connector_options
        ).message
    )
    ctx = connector_context("teams", env=env(graph, TEAMS_WEBHOOK_URL=url), config=options)
    outputs += [repr(ctx), repr(TEAMS), repr(ops["post_card"]), str(TEAMS.to_dict())]
    for text in outputs:
        assert SIGNATURE not in text and GUID not in text, text
    assert "request failed" in outputs[1] and "may or may not" in outputs[1]


# --------------------------------------------------------------------------- as a user


def test_a_user_posts_replies_and_reads_as_themselves(graph: FakeGraph) -> None:
    sign_in(graph)
    ada, team, channel = world(graph)
    ops = tools(graph)
    where = {"team_id": team.id, "channel_id": channel.id}
    posted_message = content(ops["post_message"].invoke({**where, "text": "Hello team"}))
    assert posted_message["from"]["user"]["displayName"] == "Ada"  # a person, not the app
    reply = content(
        ops["reply_message"].invoke(
            {**where, "message_id": posted_message["id"], "text": "<b>Done</b>", "format": "html"}
        )
    )
    assert reply["replyToId"] == posted_message["id"] and reply["body"]["contentType"] == "html"
    listing = content(ops["list_channel_messages"].invoke({**where, "include_replies": True}))
    [thread] = listing["items"]
    assert thread["text"] == "Hello team" and thread["from"] == {"name": "Ada", "kind": "user"}
    assert [r["text"] for r in thread["replies"]] == ["<b>Done</b>"]
    request = graph.calls("GET", "/v1.0/teams/*/messages")[-1]
    assert request.query["$expand"] == ["replies"] and request.query["$top"] == ["20"]
    sent = graph.calls("POST", "/v1.0/teams/*")[0]
    claims = decode_jwt(sent.header("authorization").split()[1])  # type: ignore[union-attr]
    assert claims["upn"] == "ada@contoso.test" and "roles" not in claims
    assert sent.json() == {"body": {"content": "Hello team", "contentType": "text"}}


@proves("teams.channel_messages_top_max_50")
def test_reading_pages_by_50_and_never_asks_for_more(graph: FakeGraph) -> None:
    sign_in(graph)
    _, team, channel = world(graph)
    store = graph.store("teams")
    for number in range(60):
        store.add_message(channel, f"m{number}", sender=graph.find_user("ada@contoso.test"))
        graph.clock.advance(1)
    ops = tools(graph)
    where = {"team_id": team.id, "channel_id": channel.id}
    first = content(ops["list_channel_messages"].invoke({**where, "limit": 50}))
    assert first["count"] == 50 and first["has_more"] is True
    second = content(
        ops["list_channel_messages"].invoke(
            {**where, "limit": 50, "page_token": first["next_page"]}
        )
    )
    assert second["count"] == 10 and second["has_more"] is False
    assert {m["id"] for m in first["items"]}.isdisjoint(m["id"] for m in second["items"])
    assert [r.query["$top"] for r in graph.calls("GET", "/v1.0/teams/*/messages")][:1] == [["50"]]
    too_many = ops["list_channel_messages"].invoke({**where, "limit": 51})
    assert "less than or equal to 50" in too_many  # Graph's cap is the tool's cap


@proves("teams.channel_lists_include_system_messages")
def test_system_messages_and_bot_messages_are_shaped_without_a_sender(graph: FakeGraph) -> None:
    sign_in(graph)
    _, team, channel = world(graph)
    store = graph.store("teams")
    add_system_message(store, channel)
    graph.clock.advance(1)
    store.add_message(channel, "from a bot", application="Build bot")
    listing = content(
        tools(graph)["list_channel_messages"].invoke({"team_id": team.id, "channel_id": channel.id})
    )
    by_text = {m["text"]: m for m in listing["items"]}
    assert by_text["<systemEventMessage/>"]["from"] == {"name": None, "kind": None}
    assert by_text["<systemEventMessage/>"]["type"] == "systemEventMessage"
    assert by_text["from a bot"]["from"] == {"name": "Build bot", "kind": "application"}


def test_without_a_sign_in_a_user_operation_names_the_next_step_and_sends_nothing(
    graph: FakeGraph,
) -> None:
    _, team, channel = world(graph)
    result = tools(graph)["post_message"].invoke(
        {"team_id": team.id, "channel_id": channel.id, "text": "hi"}
    )
    assert "Not signed in to Microsoft 365" in result and "awa connect microsoft --teams" in result
    assert graph.requests == []


def test_a_user_who_may_not_post_gets_the_advice_for_it(graph: FakeGraph) -> None:
    sign_in(graph, scopes=("User.Read",))  # consented, but without the posting permission
    _, team, channel = world(graph)
    where = {"team_id": team.id, "channel_id": channel.id, "text": "hi"}
    result = tools(graph)["post_message"].invoke(where)
    assert "lacks the permission ChannelMessage.Send" in result and "Sign in again" in result


def test_a_user_who_is_not_a_member_is_told_to_add_them(graph: FakeGraph) -> None:
    sign_in(graph)
    _, team, channel = world(graph, member=False)
    result = tools(graph)["post_message"].invoke(
        {"team_id": team.id, "channel_id": channel.id, "text": "hi"}
    )
    assert "not a member" in result and "Add that user" in result


def test_other_failures_of_a_user_operation_are_explained_or_left_as_microsoft_said(
    graph: FakeGraph,
) -> None:
    sign_in(graph)
    _, team, channel = world(graph)
    ops = tools(graph)
    where = {"team_id": team.id, "channel_id": channel.id}
    gone = ops["list_channel_messages"].invoke(
        {"team_id": team.id, "channel_id": "19:nope@thread.tacv2"}
    )
    assert "does not exist, or the signed-in user cannot see it" in gone
    graph.fail("POST", "/teams/*", status=403, message="Nope.", times=1)
    denied = ops["post_message"].invoke({**where, "text": "hi"})
    assert "may not do this" in denied and "member of the team and channel" in denied
    unusual = ops["post_message"].invoke(
        {**where, "text": 'hi <at id="0">x</at>', "format": "html"}
    )
    assert unusual.startswith("Error:") and "matching mention" in unusual  # Microsoft's own words


def test_long_or_empty_messages_are_refused_before_sending(graph: FakeGraph) -> None:
    sign_in(graph)
    _, team, channel = world(graph)
    where = {"team_id": team.id, "channel_id": channel.id}
    assert "text must not be empty" in tools(graph)["post_message"].invoke({**where, "text": " "})
    assert "longer than 28000" in tools(graph)["post_message"].invoke(
        {**where, "text": "x" * 28_001}
    )
    wide = "\u00e4" * 15_000  # 15 000 characters, 30 000 bytes
    assert "larger than Teams' limit of 28000 bytes" in tools(graph)["post_message"].invoke(
        {**where, "text": wide}
    )
    assert graph.calls("POST", "/v1.0/*") == []


def app_only_spec() -> Any:
    """The same connector, set up (wrongly) with an application token: no sign-in to refuse it."""

    return replace(TEAMS, auth=(BearerAuth(EnvVar("MS_GRAPH_TOKEN", "A token."), identity=None),))


@proves("teams.app_only_post_forbidden")
def test_an_app_that_tries_to_post_gets_microsofts_403_in_plain_words(graph: FakeGraph) -> None:
    app = graph.add_app("app-1234-5678", "secret", roles=("Teamwork.Migrate.All",))
    _, team, channel = world(graph)
    built = connector_tools(
        app_only_spec(),
        env={
            **env(graph),
            "TEAMS_TEAM_ID": team.id,
            "TEAMS_CHANNEL_ID": channel.id,
            "MS_GRAPH_TOKEN": graph.issue_token(app),
        },
        config={**graph.connector_options, "requires_approval": False},
    )
    ops = {t.name.removeprefix("teams_"): t for t in built}
    result = ops["post_message"].invoke({"text": "x"})
    assert graph.calls("POST", "/v1.0/teams/*")[0].status == 403  # Microsoft's own answer
    assert "does not let an app post Teams messages as itself" in result
    assert "Workflows webhook (post_card)" in result and "awa connect microsoft --teams" in result


@proves("teams.app_only_read_is_protected_api")
def test_an_app_that_tries_to_read_is_told_about_the_approval_it_may_need(graph: FakeGraph) -> None:
    app = graph.add_app("app-1234-5678", "secret", roles=("ChannelMessage.Read.All",))
    _, team, channel = world(graph)
    built = connector_tools(
        app_only_spec(),
        env={
            **env(graph),
            "TEAMS_TEAM_ID": team.id,
            "TEAMS_CHANNEL_ID": channel.id,
            "MS_GRAPH_TOKEN": graph.issue_token(app),
        },
        config={**graph.connector_options, "requires_approval": False},
    )
    ops = {t.name.removeprefix("teams_"): t for t in built}
    result = ops["list_channel_messages"].invoke({})
    assert "may need Microsoft's approval" in result and "signed-in user" in result
    graph.store("teams").approve_protected_api()
    assert ops["list_channel_messages"].invoke({}).startswith("<tool_output")


@proves("graph.me_profile_needs_user_read")
def test_the_profile_of_the_signed_in_user_needs_user_read_and_a_user(graph: FakeGraph) -> None:
    app = graph.apps[PUBLIC]
    ada = graph.find_user("ada@contoso.test")
    user = graph.issue_token(app, kind="user", scopes=["User.Read"], user=ada)
    answer = graph.call("GET", "/v1.0/me?$select=userPrincipalName", token=user).json()
    assert answer == {"id": ada.id, "userPrincipalName": "ada@contoso.test"}  # type: ignore[union-attr]
    limited = graph.issue_token(app, kind="user", scopes=["Chat.Read"], user=ada)
    assert graph.call("GET", "/v1.0/me", token=limited).status == 403
    application = graph.add_app("app-1234-5678", "s", roles=("User.Read.All",))
    assert graph.call("GET", "/v1.0/me", token=graph.app_token(application)).status == 400


# --------------------------------------------------------------------------- health


def test_health_of_the_webhook_checks_the_address_and_sends_no_card(graph: FakeGraph) -> None:
    result = check_connector(
        "teams", env={"TEAMS_WEBHOOK_URL": webhook(graph)}, config=graph.connector_options
    )
    assert (
        result.ok
        and "address accepted" in result.message
        and "no test card is sent" in result.message
    )
    assert graph.requests == []
    odd = check_connector(
        "teams",
        env={"TEAMS_WEBHOOK_URL": f"{graph.url}/workflows/x"},
        config=graph.connector_options,
    )
    assert odd.ok and "no sig= part" in odd.message
    assert SIGNATURE not in result.message


def test_health_of_the_user_sign_in_says_who_is_signed_in_and_with_what(graph: FakeGraph) -> None:
    sign_in(graph)
    result = check_connector("teams", env=env(graph), config=graph.connector_options)
    assert result.ok, result.message
    assert "signed in as ada@contoso.test" in result.message
    assert "ChannelMessage.Send" in result.message and "address accepted" in result.message
    assert [r.path for r in graph.calls("GET", "/v1.0/*")] == ["/v1.0/me"]
    assert graph.calls("POST", "/workflows/*") == []


def test_health_fails_with_the_next_step_when_the_sign_in_is_gone(graph: FakeGraph) -> None:
    sign_in(graph)
    graph.revoke_refresh_tokens()
    graph.clock.advance(3500)
    result = check_connector("teams", env=env(graph), config=graph.connector_options)
    assert not result.ok and "revoked" in result.message
    assert "awa connect microsoft --teams" in result.message


def test_health_of_a_webhook_stays_green_when_the_app_variables_are_set_for_other_connectors(
    graph: FakeGraph,
) -> None:
    # `outlook` and `sharepoint` use the same AZURE_* variables: setting them for those must not
    # turn a working webhook red because nobody ever signed in as a Teams user.
    result = check_connector("teams", env=env(graph), config=graph.connector_options)
    assert result.ok, result.message
    assert "address accepted" in result.message and "no user is signed in" in result.message
    assert graph.requests == [], "no request: there is no sign-in to try"
    sign_in(graph)
    graph.revoke_refresh_tokens()
    graph.clock.advance(3500)
    broken = check_connector("teams", env=env(graph), config=graph.connector_options)
    assert not broken.ok and "revoked" in broken.message, "a sign-in that existed must work"


# --------------------------------------------------------------------------- the pinned channel


def test_the_model_never_chooses_the_team_or_the_channel(graph: FakeGraph) -> None:
    built = connector_tools("teams", env=env(graph), config=graph.connector_options)
    for tool in built:
        fields = set(tool.args_schema.model_json_schema().get("properties", {}))
        assert not fields & {"team_id", "channel_id"}, tool.name


def test_an_unset_or_malformed_channel_is_refused_before_anything_is_sent(graph: FakeGraph) -> None:
    sign_in(graph)
    graph.clear_requests()
    options = {**graph.connector_options, "requires_approval": False}
    unset = {t.name: t for t in connector_tools("teams", env=env(graph), config=options)}
    assert "TEAMS_TEAM_ID and TEAMS_CHANNEL_ID are not set" in unset["teams_post_message"].invoke(
        {"text": "hi"}
    )
    wrong = {
        t.name: t
        for t in connector_tools(
            "teams",
            env=env(graph, TEAMS_TEAM_ID="not-a-guid", TEAMS_CHANNEL_ID="19:a@thread.tacv2"),
            config=options,
        )
    }
    assert "must be the team's id" in wrong["teams_list_channel_messages"].invoke({})
    assert graph.calls(path="/v1.0/*") == []


def test_the_operations_post_to_the_pinned_channel_only(graph: FakeGraph) -> None:
    sign_in(graph)
    _, team, channel = world(graph)
    other = graph.store("teams").add_channel(team, "Other")
    pinned = {"TEAMS_TEAM_ID": team.id, "TEAMS_CHANNEL_ID": channel.id}
    options = {**graph.connector_options, "requires_approval": False}
    built = {t.name: t for t in connector_tools("teams", env=env(graph, **pinned), config=options)}
    built["teams_post_message"].invoke({"text": "hello"})
    [posted_to] = graph.calls("POST", "/v1.0/teams/*")
    assert channel.id in posted_to.path and other.id not in posted_to.path
