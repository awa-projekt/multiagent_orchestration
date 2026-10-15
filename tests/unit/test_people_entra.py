"""`EntraPeople` against FakeGraph and `role:` recipients in Outlook and Teams."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from _helpers.fakegraph import people as fake_graph_people
from _helpers.fakegraph.server import V1, FakeGraph
from awa.connectors import _engine, connector_tools
from awa.connectors import _microsoft_auth as auth
from awa.connectors import _saas_microsoft_teams as teams_module
from awa.connectors._catalog import config_problems
from awa.people import EntraPeople, PeopleError, PeopleFile, PersonRef
from awa.people._entra import graph_term, redacted_query
from awa.testing.doubles import FakeClock

SECRET = "people-app-secret"
PUBLIC = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def graph() -> Iterator[FakeGraph]:
    with FakeGraph() as server:
        server.use(fake_graph_people)
        store = server.store("people")
        store.add("Carla Meier", "carla.meier@contoso.example")
        store.add("Carl Meyer", "carl.meyer@contoso.example")
        store.add("Hiking&Recreation Team", "hiking@contoso.example")
        store.add("Gail Gast", "gail.gast@fabrikam.example", guest=True)
        yield server


@pytest.fixture(autouse=True)
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(_engine, "_sleep", waits.append)
    return waits


def people(graph: FakeGraph, *roles: str, clock: FakeClock | None = None, **kw: Any) -> EntraPeople:
    app = graph.add_app("people-app", SECRET, roles=roles or ("User.Read.All",))
    return EntraPeople(
        env=graph.env(app), dotenv=None, options=graph.connector_options, clock=clock, **kw
    )


def searches(graph: FakeGraph) -> list[Any]:
    return graph.calls("GET", f"{V1}/users")


def person(graph: FakeGraph, name: str) -> dict[str, Any]:
    return next(p for p in graph.store("people").people.values() if p["displayName"] == name)


# --------------------------------------------------------------------------- the request


def test_search_sends_the_documented_advanced_query(graph: FakeGraph) -> None:
    found = people(graph).search("carla mei")
    assert [p.name for p in found] == ["Carla Meier"]
    assert found[0].state == "unknown"  # a search does not read whether the account is enabled
    (sent,) = searches(graph)
    assert sent.header("ConsistencyLevel") == "eventual"
    assert sent.query["$search"] == ['"displayName:carla mei" OR "mail:carla mei"']
    assert sent.query["$count"] == ["true"]
    assert sent.query["$top"] == ["10"]
    assert sent.query["$select"] == ["id,displayName,mail,userPrincipalName"]
    assert sent.header("User-Agent").startswith("NONISV|awa|")


def test_the_identity_is_tenant_and_object_id(graph: FakeGraph) -> None:
    (carla,) = people(graph).search("carla")
    assert (
        carla.tenant_id == graph.tenant_id and carla.object_id == person(graph, "Carla Meier")["id"]
    )
    assert carla.identity == ("entra", graph.tenant_id, carla.object_id)
    assert carla.key == "carla_meier"


@pytest.mark.parametrize(
    ("typed", "clause"),
    [
        ('carla"x', 'carla\\"x'),
        ("back\\slash", "back\\\\slash"),
        ("Hiking&Recreation", "Hiking Recreation"),
        ('a" OR "mail:', 'a\\" OR \\"mail:'),
    ],
)
def test_quotes_and_backslashes_are_escaped_and_ampersands_dropped(
    graph: FakeGraph, typed: str, clause: str
) -> None:
    assert graph_term(typed) == clause
    people(graph).search(typed)
    (sent,) = searches(graph)
    assert sent.query["$search"] == [f'"displayName:{clause}" OR "mail:{clause}"']
    assert sent.status == 200


def test_an_ampersand_name_is_still_found(graph: FakeGraph) -> None:
    assert [p.name for p in people(graph).search("Hiking&Recreation")] == ["Hiking&Recreation Team"]


@pytest.mark.parametrize("text", ["c", "x" * 65, "carla\x00", "&&", "\\"])
def test_bad_text_is_refused_before_any_request(graph: FakeGraph, text: str) -> None:
    with pytest.raises(PeopleError):
        people(graph).search(text)
    assert searches(graph) == []


def test_a_tenant_without_search_falls_back_to_startswith(graph: FakeGraph) -> None:
    graph.fail("GET", "/users", status=400, code="Request_UnsupportedQuery", times=1)
    found = people(graph).search("carl")
    assert [p.name for p in found] == ["Carl Meyer", "Carla Meier"]
    first, second = searches(graph)
    assert "$search" in first.query and "$filter" in second.query
    assert second.query["$filter"] == ["startswith(displayName,'carl')"]
    assert second.header("ConsistencyLevel") is None


def test_guests_are_hidden_by_default(graph: FakeGraph) -> None:
    directory = people(graph)
    assert directory.search("gail") == []
    (gail,) = directory.search("gail", guests=True)
    assert gail.kind == "guest"


# --------------------------------------------------------------------------- throttling


def test_a_429_is_waited_for_as_retry_after_says(graph: FakeGraph, slept: list[float]) -> None:
    graph.throttle("/users", retry_after=7)
    assert [p.name for p in people(graph).search("carla")] == ["Carla Meier"]
    assert slept == [7.0]
    assert [r.status for r in searches(graph)] == [429, 200]


def test_a_long_retry_after_is_reported_not_hammered(graph: FakeGraph, slept: list[float]) -> None:
    graph.throttle("/users", retry_after=120, times=None)
    with pytest.raises(PeopleError, match="120 seconds") as raised:
        people(graph).search("carla")
    assert raised.value.details["retry_after"] == 120.0
    assert len(searches(graph)) == 1 and slept == []


def test_a_missing_permission_names_the_one_to_grant(graph: FakeGraph) -> None:
    with pytest.raises(PeopleError, match="User.Read.All"):
        people(graph, "Mail.Read").search("carla")


# --------------------------------------------------------------------------- get and check


def test_a_deleted_person_is_deleted_and_a_disabled_one_disabled(graph: FakeGraph) -> None:
    clock = FakeClock()
    directory = people(graph, clock=clock)
    carla, carl = directory.search("carla")[0], directory.search("carl meyer")[0]
    graph.store("people").delete(str(carla.object_id))
    graph.store("people").update(str(carl.object_id), accountEnabled=False)
    gone, off = directory.check([carla, carl])
    assert gone.state == "deleted" and off.state == "disabled"
    assert not gone.usable and not off.usable


def test_a_read_is_cached_for_15_minutes(graph: FakeGraph) -> None:
    clock = FakeClock()
    directory = people(graph, clock=clock)
    (carla,) = directory.search("carla")
    directory.get(carla)
    graph.store("people").update(str(carla.object_id), mail="carla@new.contoso.example")
    assert directory.get(carla).mail == "carla.meier@contoso.example"  # still cached
    clock.advance(seconds=901)
    assert directory.get(carla).mail == "carla@new.contoso.example"
    assert len(graph.calls("GET", f"{V1}/users/*")) == 2


def test_a_person_of_another_tenant_is_not_checked_here(graph: FakeGraph) -> None:
    (carla,) = people(graph).search("carla")
    other = carla.model_copy(update={"tenant_id": "00000000-0000-0000-0000-000000000001"})
    with pytest.raises(PeopleError, match="another tenant"):
        people(graph).get(other)


def test_lookup_by_user_principal_name_resolves_the_object_id(graph: FakeGraph) -> None:
    upn = person(graph, "Carla Meier")["userPrincipalName"]
    found = people(graph).lookup(upn)
    assert found.object_id == person(graph, "Carla Meier")["id"] and found.state == "active"
    with pytest.raises(PeopleError, match="no person"):
        people(graph).lookup("nobody@contoso.onmicrosoft.com")
    with pytest.raises(PeopleError, match="object id"):
        people(graph).lookup("carla/../../me")


def test_readbasic_cannot_see_disabled_accounts_so_their_state_is_unknown(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delegated User.ReadBasic.All (no admin consent) works; accountEnabled is not visible."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "_sleep", lambda seconds: None)
    graph.add_app(PUBLIC, public_client=True, scopes=("User.Read", "User.ReadBasic.All"))
    graph.add_user("ada@contoso.test", name="Ada")
    graph.identity.device_pending_polls = 0
    auth.sign_in(
        tenant=graph.tenant_id,
        client_id=PUBLIC,
        show=lambda prompt: None,
        scopes=("User.Read", "User.ReadBasic.All"),
        authority=graph.authority,
        options=graph.connector_options,
    )
    env = {
        "AZURE_TENANT_ID": graph.tenant_id,
        "AZURE_CLIENT_ID": PUBLIC,
        "AZURE_AUTHORITY_HOST": graph.authority,
        "MS_GRAPH_URL": graph.graph_url,
    }
    directory = EntraPeople(env=env, dotenv=None, options=graph.connector_options)
    (carla,) = directory.search("carla")
    graph.store("people").update(str(carla.object_id), accountEnabled=False)
    assert directory.get(carla).state == "unknown"


def test_without_any_sign_in_the_error_says_how_to_connect(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    env = {"MS_GRAPH_URL": graph.graph_url, "AZURE_AUTHORITY_HOST": graph.authority}
    with pytest.raises(PeopleError, match="AZURE_CLIENT_SECRET"):
        EntraPeople(env=env, dotenv=None, options=graph.connector_options).search("carla")


# --------------------------------------------------------------------------- privacy


def test_no_search_text_or_mail_reaches_spans_or_logs(
    graph: FakeGraph, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("awa.observability.spans._tracer", lambda: provider.get_tracer("t"))
    directory = people(graph)
    graph.fail("GET", "/users", status=400, code="Request_UnsupportedQuery", times=1)
    with caplog.at_level(logging.DEBUG, logger="awa"):
        (carla,) = directory.search("Carla Mei")
        directory.get(carla)
    provider.shutdown()
    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} == {"awa.people.search", "awa.people.get"}
    values = [str(v) for s in spans for v in (s.attributes or {}).values()]
    text = " ".join(values + [r.getMessage() for r in caplog.records]).lower()
    assert "carla" not in text and "mei" not in text and "@" not in text
    assert str(carla.object_id) not in text  # the path is a template, not the id
    search = next(s for s in spans if s.name == "awa.people.search")
    assert search.attributes["awa.people.results"] == 1
    assert search.attributes["awa.people.fallback"] is True
    assert "$search=[redacted]" in str(
        search.attributes["url.query"]
    ) or "$filter=[redacted]" in str(search.attributes["url.query"])
    assert any("people search: 1 results" in r.getMessage() for r in caplog.records)


def test_every_graph_request_records_a_timed_span_and_a_failure_its_error_type(
    graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("awa.observability.spans._tracer", lambda: provider.get_tracer("t"))
    people(graph).search("carla")
    with pytest.raises(PeopleError):
        people(graph, "Mail.Read").search("carla")
    provider.shutdown()

    ok, refused = exporter.get_finished_spans()
    assert ok.end_time is not None and ok.start_time is not None
    assert ok.end_time > ok.start_time
    assert "error.type" not in (ok.attributes or {})
    assert refused.attributes is not None
    assert refused.attributes["http.response.status_code"] == 403
    assert refused.attributes["error.type"] == "PeopleError"


def test_the_redacted_query_keeps_only_harmless_values() -> None:
    query = {"$search": '"displayName:x"', "$filter": "startswith(displayName,'x')", "$top": "10"}
    assert redacted_query(query) == "$search=[redacted]&$filter=[redacted]&$top=10"


def test_a_person_ref_is_frozen() -> None:
    ref = PersonRef(key="a_b", source="local", name="A B")
    with pytest.raises(ValidationError):
        ref.name = "C"  # type: ignore[misc]


MAILBOX = "desk@contoso.test"
SECRET_RECIPIENTS = "outlook-and-people-secret"
LONG_AGO = FakeClock(datetime(2026, 3, 2, 9, 0, tzinfo=UTC))  # pins older than 15 minutes


@pytest.fixture
def graph_recipients(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FakeGraph]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_engine, "_sleep", lambda seconds: None)
    with FakeGraph() as server:
        server.use(fake_graph_people)
        server.add_user(MAILBOX, name="Desk")
        store = server.store("people")
        store.add("Carla Meier", "carla.meier@contoso.example")
        store.add("Dieter Vogt", "dieter.vogt@contoso.example")
        store.add("Erik Engel", "erik.engel@contoso.example")
        yield server


class Project:
    """A project folder with pinned people, and the Outlook connector of its mailbox."""

    def __init__(self, graph_recipients: FakeGraph, root: Path) -> None:
        self.graph, self.root = graph_recipients, root
        self.app = graph_recipients.add_app(
            "desk-app", SECRET_RECIPIENTS, roles=("Mail.ReadWrite", "Mail.Send", "User.Read.All")
        )
        self.file = PeopleFile.of_project(root)

    def entra(self, clock: FakeClock | None = LONG_AGO) -> EntraPeople:
        return EntraPeople(
            env=self.graph.env(self.app),
            dotenv=None,
            options=self.graph.connector_options,
            clock=clock,
        )

    def pin(self, text: str, *roles: str, clock: FakeClock | None = LONG_AGO) -> PersonRef:
        (person,) = self.entra(clock).search(text)
        book = self.file.load()
        key = book.pin(person, roles)
        self.file.save(book)
        return book.people[key]

    def outlook(self, **options: Any) -> dict[str, Any]:
        config = {
            **self.graph.connector_options,
            "requires_approval": False,
            "people_file": str(self.file.path),
            **options,
        }
        built = connector_tools(
            "outlook", env=self.graph.env(self.app, MS_MAILBOX=MAILBOX), config=config
        )
        return {t.name.removeprefix("outlook_"): t for t in built}

    def send(self, to: list[str], cc: list[str] | None = None, **options: Any) -> Any:
        args: dict[str, Any] = {"to": to, "subject": "Payment run", "body": "Please check."}
        if cc:
            args["cc"] = cc
        return output(self.outlook(**options)["send_mail"].invoke(args))

    def drafts(self) -> list[dict[str, Any]]:
        return [r.json() for r in self.graph.calls("POST", f"{V1}/users/{MAILBOX}/messages")]


def output(result: str) -> Any:
    if not result.startswith("<tool_output"):
        return result
    return json.loads(json.loads(result.split(">", 1)[1].rsplit("</tool_output>", 1)[0])["content"])


def addresses(draft: dict[str, Any], field: str = "toRecipients") -> list[str]:
    return [r["emailAddress"]["address"] for r in draft.get(field) or []]


@pytest.fixture
def project(graph_recipients: FakeGraph, tmp_path: Path) -> Project:
    return Project(graph_recipients, tmp_path)


# --------------------------------------------------------------------------- Outlook


def test_a_role_is_resolved_to_its_people_when_the_mail_is_sent(project: Project) -> None:
    project.pin("carla", "finance")
    project.pin("dieter", "finance")
    result = project.send(["role:finance"])
    assert result["sent"] is True
    assert addresses(project.drafts()[0]) == [
        "carla.meier@contoso.example",
        "dieter.vogt@contoso.example",
    ]


def test_a_changed_mail_address_is_used_and_saved(
    project: Project, graph_recipients: FakeGraph
) -> None:
    carla = project.pin("carla", "finance")
    graph_recipients.store("people").update(
        str(carla.object_id), mail="carla@finance.contoso.example"
    )
    assert project.send(["role:finance"])["sent"] is True
    assert addresses(project.drafts()[0]) == ["carla@finance.contoso.example"]
    assert project.file.load().people[carla.key].mail == "carla@finance.contoso.example"


def test_a_recently_checked_person_is_not_asked_for_again(
    project: Project, graph_recipients: FakeGraph
) -> None:
    project.pin("carla", "finance", clock=None)  # checked just now
    graph_recipients.clear_requests()
    assert project.send(["role:finance"])["sent"] is True
    assert graph_recipients.calls("GET", f"{V1}/users/*") == []


def test_a_recipient_outside_the_roles_is_refused(project: Project) -> None:
    project.pin("carla", "finance")
    project.pin("erik", "sales")
    options = {"recipients": "role:finance"}
    refused = project.send(["erik.engel@contoso.example"], **options)
    assert isinstance(refused, str) and "not people of role:finance" in refused
    refused_role = project.send(["role:sales"], **options)
    assert isinstance(refused_role, str) and "not among the allowed recipients" in refused_role
    refused_cc = project.send(["role:finance"], cc=["outsider@fabrikam.example"], **options)
    assert isinstance(refused_cc, str) and refused_cc.startswith("Error")
    assert project.drafts() == []
    allowed = project.send(["carla.meier@contoso.example"], **options)
    assert allowed["sent"] is True


def test_an_address_of_a_deleted_person_is_no_longer_allowed(
    project: Project, graph_recipients: FakeGraph
) -> None:
    carla = project.pin("carla", "finance")
    project.pin("dieter", "finance")
    graph_recipients.store("people").delete(str(carla.object_id))
    refused = project.send(["carla.meier@contoso.example"], recipients=["role:finance"])
    assert isinstance(refused, str) and "not people of role:finance" in refused
    assert project.send(["role:finance"])["sent"] is True
    assert addresses(project.drafts()[0]) == ["dieter.vogt@contoso.example"]
    assert project.file.load().people[carla.key].state == "deleted"


def test_when_everyone_of_a_role_is_gone_nothing_is_sent(
    project: Project, graph_recipients: FakeGraph
) -> None:
    carla = project.pin("carla", "finance")
    graph_recipients.store("people").update(str(carla.object_id), accountEnabled=False)
    refused = project.send(["role:finance"])
    assert isinstance(refused, str) and "Nobody of the role 'finance'" in refused
    assert "Carla Meier (disabled)" in refused
    assert project.drafts() == []


def test_an_unknown_role_or_a_malformed_one_is_refused(project: Project) -> None:
    project.pin("carla", "finance")
    assert "No one holds the role 'board'" in project.send(["role:board"])
    assert project.send(["role:Board Members"]).startswith("Error")
    assert project.drafts() == []


def test_when_entra_cannot_be_asked_nothing_is_sent(
    project: Project, graph_recipients: FakeGraph
) -> None:
    project.pin("carla", "finance")
    graph_recipients.fail("GET", "/users/*", status=403, times=None)
    refused = project.send(["role:finance"])
    assert isinstance(refused, str) and "nothing was sent" in refused
    assert project.drafts() == []


def test_a_reply_goes_only_to_people_of_the_roles(
    project: Project, graph_recipients: FakeGraph
) -> None:
    project.pin("carla", "finance")
    desk = graph_recipients.find_user(MAILBOX)
    mail = graph_recipients.store("mail")
    stranger = mail.add_message(desk, subject="Hello", sender="stranger@fabrikam.example")
    tools = project.outlook(recipients="role:finance")
    refused = output(
        tools["reply_to_message"].invoke({"message_id": stranger["id"], "comment": "Hi"})
    )
    assert isinstance(refused, str) and "not people of role:finance" in refused
    known = mail.add_message(desk, subject="Invoice", sender="carla.meier@contoso.example")
    sent = output(tools["reply_to_message"].invoke({"message_id": known["id"], "comment": "Done"}))
    assert sent["sent"] is True and sent["to"] == ["carla.meier@contoso.example"]


def test_the_recipients_option_is_checked_when_the_connector_is_built() -> None:
    assert config_problems("outlook", {"recipients": "role:finance"}) == []
    assert config_problems("outlook", {"recipients": ["role:a", "role:b"]}) == []
    assert "roles such as role:" in config_problems("outlook", {"recipients": "ada@x.test"})[0]
    assert config_problems("outlook", {"recipients": 5})
    assert config_problems("teams", {"recipients": ["finance"]})
    assert config_problems("outlook", {"people_file": ""})


def test_without_roles_and_without_the_option_nothing_changes(project: Project) -> None:
    assert project.send(["someone@fabrikam.example"])["sent"] is True
    assert not project.file.path.exists()


# --------------------------------------------------------------------------- Teams


@pytest.fixture
def teams(graph_recipients: FakeGraph, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(teams_module, "_clock", graph_recipients.clock.time)
    monkeypatch.setattr(
        teams_module, "_sleep", lambda seconds: graph_recipients.clock.advance(seconds)
    )
    monkeypatch.setattr(teams_module._SPACE, "_next", {})
    monkeypatch.setattr(auth, "_now", graph_recipients.clock.time)
    monkeypatch.setattr(auth, "_sleep", lambda seconds: graph_recipients.clock.advance(seconds))
    scopes = ("User.Read", "ChannelMessage.Send", "User.ReadBasic.All")
    graph_recipients.add_app(PUBLIC, public_client=True, scopes=scopes)
    graph_recipients.identity.device_pending_polls = 0
    graph_recipients.identity.device_user = MAILBOX
    auth.sign_in(
        tenant=graph_recipients.tenant_id,
        client_id=PUBLIC,
        show=lambda prompt: None,
        scopes=scopes,
        authority=graph_recipients.authority,
        options=graph_recipients.connector_options,
    )
    desk = graph_recipients.find_user(MAILBOX)
    store = graph_recipients.store("teams")
    team = store.add_team("Finance", desk)
    channel = store.add_channel(team, "Payments")
    return {"team": team, "channel": channel}


def teams_tools(
    graph_recipients: FakeGraph, project: Project, teams: dict[str, Any], **options: Any
) -> dict[str, Any]:
    env = {
        "TEAMS_TEAM_ID": teams["team"].id,  # the one pinned channel of the connection
        "TEAMS_CHANNEL_ID": teams["channel"].id,
        "AZURE_TENANT_ID": graph_recipients.tenant_id,
        "AZURE_CLIENT_ID": PUBLIC,
        "AZURE_AUTHORITY_HOST": graph_recipients.authority,
        "MS_GRAPH_URL": graph_recipients.graph_url,
    }
    config = {
        **graph_recipients.connector_options,
        "requires_approval": False,
        "people_file": str(project.file.path),
        **options,
    }
    return {
        t.name.removeprefix("teams_"): t for t in connector_tools("teams", env=env, config=config)
    }


def test_teams_mentions_the_people_of_a_role_by_their_entra_id(
    graph_recipients: FakeGraph, project: Project, teams: dict[str, Any]
) -> None:
    carla = project.pin("carla", "approvers")
    tools = teams_tools(graph_recipients, project, teams, recipients="role:approvers")
    result = tools["post_message"].invoke({"text": "Run 7 <waits>", "mention": ["role:approvers"]})
    assert result.startswith("<tool_output"), result
    (sent,) = graph_recipients.calls("POST", f"{V1}/teams/*/messages")
    body = sent.json()
    assert body["body"]["contentType"] == "html"
    assert body["body"]["content"] == 'Run 7 &lt;waits&gt;<p><at id="0">Carla Meier</at></p>'
    assert body["mentions"][0]["mentioned"]["user"]["id"] == carla.object_id
    refused = tools["post_message"].invoke({"text": "x", "mention": ["role:sales"]})
    assert "not among the allowed recipients" in refused
    assert len(graph_recipients.calls("POST", f"{V1}/teams/*/messages")) == 1


def test_teams_does_not_mention_a_deleted_person(
    graph_recipients: FakeGraph, project: Project, teams: dict[str, Any]
) -> None:
    carla = project.pin("carla", "approvers")
    graph_recipients.store("people").delete(str(carla.object_id))
    refused = teams_tools(graph_recipients, project, teams)["post_message"].invoke(
        {"text": "x", "mention": ["role:approvers"]}
    )
    assert "Nobody of the role 'approvers'" in refused
    assert graph_recipients.calls("POST", f"{V1}/teams/*/messages") == []


def test_the_result_names_the_role_not_the_addresses(project: Project) -> None:
    project.pin("carla", "finance")
    assert project.send(["role:finance"])["to"] == ["role:finance"]


def test_a_look_alike_address_is_refused(project: Project) -> None:
    project.pin("erik", "sales")
    look_alike = "eriK.engel@contoso.example"  # KELVIN SIGN lowercases to k
    assert look_alike.lower() == "erik.engel@contoso.example"
    refused = project.send([look_alike], recipients="role:sales")
    assert isinstance(refused, str) and refused.startswith("Error")
    assert project.drafts() == []


def test_one_role_without_people_does_not_block_another(
    project: Project, graph_recipients: FakeGraph
) -> None:
    carla = project.pin("carla", "finance")
    project.pin("erik", "sales")
    graph_recipients.store("people").delete(str(carla.object_id))
    result = project.send(["erik.engel@contoso.example"], recipients=["role:finance", "role:sales"])
    assert result["sent"] is True


def test_a_check_dated_in_the_future_is_not_trusted(
    project: Project, graph_recipients: FakeGraph
) -> None:
    future = FakeClock(datetime(2027, 1, 1, tzinfo=UTC))
    carla = project.pin("carla", "finance", clock=future)
    graph_recipients.store("people").delete(str(carla.object_id))
    refused = project.send(["role:finance"])
    assert isinstance(refused, str) and "Nobody of the role 'finance'" in refused


def test_from_context_reads_no_environment_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    from awa.connectors import _catalog
    from awa.people import EntraPeople

    ctx = _catalog.connector_context("outlook", env={"AZURE_TENANT_ID": "t"})

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a second context was built")

    monkeypatch.setattr(_catalog, "connector_context", refuse)

    people = EntraPeople.from_context(ctx)

    assert people._ctx.env.get("AZURE_TENANT_ID") == "t"
