"""Every catalog connector against a local mock transport, `discover`, and the `awa connectors` commands."""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from awa.cli import _doctor as doctor
from awa.cli import build_parser
from awa.connectors import (
    ConnectorStatus,
    EnvironmentReport,
    HttpRequest,
    MockTransport,
    Operation,
    _catalog,
    _engine,
    check_connector,
    connector_tools,
    discover,
    get_connector,
    list_connectors,
    mock_response,
)
from awa.connectors._saas import JIRA, SLACK
from awa.connectors._saas_microsoft import MICROSOFT_GRAPH
from awa.connectors._saas_microsoft_outlook import OUTLOOK
from awa.connectors._saas_microsoft_sharepoint import SHAREPOINT
from awa.connectors._spec import ConnectorSpec, auth_identity, fill_template
from awa.errors import ConfigurationError
from awa.tools.approval import APPROVAL_METADATA_KEY


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_engine, "_sleep", lambda seconds: None)


SAMPLES = {
    "issue_key": "OPS-42",
    "channel": "C0123456",
    "file_id": "file_1",
    "to": ["ada@example.test"],
    "cc": ["bob@example.test"],
}


DEFAULT_HOSTS = frozenset({"AZURE_AUTHORITY_HOST", "MS_GRAPH_URL"})
# A certificate wins over a client secret; these tests document the requests made with the
# secret, and the certificate sign-in has its own tests (test_connect_microsoft.py).
CERTIFICATE = frozenset({"AZURE_CLIENT_CERTIFICATE_PATH", "AZURE_CLIENT_CERTIFICATE_PASSWORD"})


def env_for(spec: ConnectorSpec) -> dict[str, str]:
    """Sample values: addresses for URL variables, a unique secret for every credential."""

    values: dict[str, str] = {}
    for variable in spec.variables():
        name = variable.name
        if name in DEFAULT_HOSTS or name in CERTIFICATE:
            continue  # the public-cloud default and the secret sign-in are what these tests assert
        if name.endswith("_URL") and not variable.secret:
            values[name] = f"https://{name.lower().replace('_', '-')}.example.test"
        elif variable.secret:
            values[name] = f"secret-{name.lower()}-value-0123"
        else:
            values[name] = name.lower().replace("_", "-")
    return values


def sample(param: Any, spec: ConnectorSpec) -> Any:
    if param.enum:
        return param.enum[0]
    if param.name in SAMPLES:
        return SAMPLES[param.name]
    return {
        "integer": 7,
        "number": 1.5,
        "boolean": True,
        "object": {"field": "value"},
        "array": [{"field": "value"}] if param.items == "object" else ["one", "two"],
    }.get(param.type, "sample-text")


def arguments(operation: Operation, spec: ConnectorSpec) -> dict[str, Any]:
    values = {
        param.name: sample(param, spec) if param.default is None else param.default
        for param in operation.params
        if param.required or param.default is None or param.location == "path"
    }
    if operation.name == "query" and spec.name == "graphql":
        values["query"] = "query { viewer { id } }"
    return values


def listing(operation: Operation) -> Any:
    """A response holding one item at the operation's items path, and no next page."""

    path = (operation.items or "").split("|")[0]
    data: Any = [{"id": "item-1"}]
    for part in reversed([p for p in path.split(".") if p]):
        data = {part: data}
    if isinstance(data, dict):
        data["ok"] = True
    return mock_response(json=data)


def answer(spec: ConnectorSpec) -> Any:
    operations = {op.name: op for op in spec.operations}

    def respond(request: HttpRequest) -> Any:
        if "token" in urllib.parse.urlsplit(request.url).path.rsplit("/", 1)[-1]:
            return mock_response(
                json={"access_token": "access-token-value-999", "expires_in": 3600}
            )
        current = operations.get(request.header("X-Test-Operation") or "")
        if current is not None and current.items is not None:
            return listing(current)
        if current is not None and current.response == "text":
            return mock_response(text="file text")
        return mock_response(json={"ok": True, "id": "item-1"})

    return respond


class Recorder(MockTransport):
    """Routes everything to one answer; tags requests with the operation under test."""

    def __init__(self, spec: ConnectorSpec) -> None:
        super().__init__()
        self.current = ""
        self.respond = answer(spec)

    def __call__(self, request: HttpRequest) -> Any:
        tagged = HttpRequest(
            request.method,
            request.url,
            {**request.headers, "X-Test-Operation": self.current},
            request.body,
        )
        self.requests.append(request)
        response = self.respond(tagged)
        return mock_response(
            response.status,
            text=response.body.decode(),
            content_type=response.headers.get("Content-Type"),
            url=request.url,
        )


def secrets_of(spec: ConnectorSpec, env: dict[str, str]) -> list[str]:
    return [env[v.name] for v in spec.variables() if v.secret and v.name in env]


SAAS = (MICROSOFT_GRAPH, OUTLOOK, SHAREPOINT, SLACK, JIRA)
CASES = [
    (spec, operation) for spec in SAAS if spec.factory is None for operation in spec.operations
]


@pytest.mark.parametrize(("spec", "operation"), CASES, ids=[f"{s.name}.{o.name}" for s, o in CASES])
def test_every_operation_sends_the_documented_request(
    spec: ConnectorSpec, operation: Operation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if operation.response == "file" or operation.body_format == "file":
        monkeypatch.chdir(tmp_path)  # file operations use the workspace folder `data`
        (tmp_path / "data").mkdir()
    env = env_for(spec)
    if operation.needs != "any":  # sign in the way the operation needs: drop the other credentials
        for other in spec.auth:
            if auth_identity(other) not in (None, operation.needs):
                for variable in other.variables():
                    env.pop(variable.name, None)
    transport = Recorder(spec)
    transport.current = operation.name
    [tool] = connector_tools(
        spec.name,
        env=env,
        config={"include": [operation.name], "requires_approval": False},
        transport=transport,
    )
    args = arguments(operation, spec)
    result = tool.invoke(args)
    assert not result.startswith("Error"), result
    calls = [r for r in transport.requests if not r.url.split("?")[0].endswith("token")]
    assert calls, "no request was sent"
    request = calls[0]
    defaults = {v.name: v.default or "" for v in spec.variables()}
    base = fill_template(spec.base_url or "", lambda name: env.get(name) or defaults.get(name, ""))
    expected_path = re.sub(
        r"\{(\w+)\}",
        lambda m: urllib.parse.quote(
            str(args[m.group(1)]),
            safe=next(p.safe for p in operation.params if p.name == m.group(1)),
        ),
        operation.path,
    )
    target = expected_path if expected_path.startswith("https://") else base + expected_path
    assert request.url.split("?")[0] == target.split("?")[0]
    assert request.method == operation.method
    auth = request.header("Authorization") or request.header("PRIVATE-TOKEN")
    assert auth, "no credentials were sent"
    for secret in secrets_of(spec, env):
        assert secret not in repr(request) and secret not in result
    if operation.write and operation.method != "GET" and operation.body_format != "raw":
        assert request.body, "a write sent no body"


@pytest.mark.parametrize("spec", SAAS, ids=[s.name for s in SAAS])
def test_writes_wait_for_approval_and_reads_do_not(spec: ConnectorSpec) -> None:
    tools = connector_tools(spec.name, env=env_for(spec), transport=MockTransport())
    by_name = {tool.metadata["awa_connector"].split(".", 1)[1]: tool for tool in tools}
    for operation in spec.operations:
        tool = by_name[operation.name]
        assert bool(tool.metadata.get(APPROVAL_METADATA_KEY)) is operation.write
        assert tool.metadata["awa_risk"]["external_action"] is operation.write


@pytest.mark.parametrize("spec", SAAS, ids=[s.name for s in SAAS])
def test_health_check_and_redacted_failure(spec: ConnectorSpec) -> None:
    env = env_for(spec)
    transport = Recorder(spec)
    health = spec.health if isinstance(spec.health, Operation) else spec.operation(str(spec.health))
    transport.current = health.name
    result = check_connector(spec.name, env=env, transport=transport)
    assert result.ok, result.message
    secret = secrets_of(spec, env)[-1]

    def failing(request: HttpRequest) -> Any:
        if request.url.split("?")[0].endswith("token"):
            return mock_response(json={"access_token": "access-token-value-999"})
        return mock_response(401, json={"message": f"invalid {secret}", "error": secret})

    failed = check_connector(spec.name, env=env, transport=failing)
    assert not failed.ok and "401" in failed.message and secret not in failed.message
    missing = check_connector(spec.name, env={}, transport=transport)
    assert not missing.ok and missing.missing


def test_catalog_has_the_promised_systems() -> None:
    names = {spec.name for spec in list_connectors()}
    assert {
        "openapi",
        "rest",
        "odata",
        "graphql",
        "workspace",
        "sql",
        "web",
        "smtp",
        "imap",
        "webhook",
        "microsoft_graph",
        "slack",
        "jira",
    } <= names
    for spec in list_connectors():
        assert spec.summary and spec.setup, spec.name
        assert all(op.description for op in spec.operations), spec.name


# ---------------------------------------------------------------------------- specific systems


def content(result: str) -> Any:
    text = json.loads(result.split(">", 1)[1].rsplit("</tool_output>", 1)[0])["content"]
    return json.loads(text)


def tools_for(name: str, transport: Any, **config: Any) -> dict[str, Any]:
    env = env_for(get_connector(name))
    config.setdefault("requires_approval", False)
    tools = connector_tools(name, env=env, config=config, transport=transport)
    return {tool.name: tool for tool in tools}


def test_jira_search_follows_next_page_tokens_and_uses_basic_auth() -> None:
    transport = MockTransport(
        {
            "GET /rest/api/3/search/jql": [
                mock_response(json={"issues": [{"key": "A-1"}], "nextPageToken": "t2"}),
                mock_response(json={"issues": [{"key": "A-2"}], "isLast": True}),
            ]
        }
    )
    tools = tools_for("jira", transport)
    tools["jira_search_issues"].invoke({"jql": "project = A", "limit": 10})
    first, second = transport.requests
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(second.url).query)
    assert query["nextPageToken"] == ["t2"] and query["jql"] == ["project = A"]
    user = base64.b64decode(first.header("Authorization").split()[1]).decode()  # type: ignore[union-attr]
    assert user == "jira-email:secret-jira_api_token-value-0123"


def test_slack_errors_on_http_200_are_reported() -> None:
    transport = MockTransport(
        {
            "POST /api/chat.postMessage": mock_response(
                json={"ok": False, "error": "channel_not_found"}
            )
        }
    )
    result = tools_for("slack", transport)["slack_post_message"].invoke(
        {"channel": "C1", "text": "hi"}
    )
    assert result == "Error: Slack reported an error: channel_not_found"
    assert transport.requests[0].json() == {"channel": "C1", "text": "hi"}


def test_microsoft_graph_gets_a_tenant_token_and_follows_next_links() -> None:
    transport = MockTransport(
        {
            "POST /azure-tenant-id/oauth2/v2.0/token": mock_response(
                json={"access_token": "graph-token-555", "expires_in": 3599}
            ),
            "GET /v1.0/users/me%40x.test/mailFolders/inbox/messages": mock_response(
                json={
                    "value": [{"id": "m1"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/next?skip=1",
                }
            ),
            "GET /v1.0/next": mock_response(json={"value": [{"id": "m2"}]}),
        }
    )
    tools = tools_for("microsoft_graph", transport)
    tools["microsoft_graph_list_messages"].invoke({"user": "me@x.test", "limit": 5})
    token = transport.requests[0]
    assert token.url == "https://login.microsoftonline.com/azure-tenant-id/oauth2/v2.0/token"
    assert b"scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in (token.body or b"")
    assert transport.requests[-1].url == "https://graph.microsoft.com/v1.0/next?skip=1"
    tools["microsoft_graph_send_mail"].invoke(
        {"user": "me@x.test", "to": ["a@x.test"], "subject": "Hi", "body": "Text"}
    )
    assert transport.requests[-1].json() == {
        "message": {
            "body": {"contentType": "Text", "content": "Text"},
            "toRecipients": [{"emailAddress": {"address": "a@x.test"}}],
            "subject": "Hi",
        },
        "saveToSentItems": True,
    }


VALUES = {
    "AZURE_OPENAI_API_KEY": "azure-key-value-0001",
    "AZURE_OPENAI_ENDPOINT": "https://azure-endpoint-value-0002.example.test",
    "AZURE_OPENAI_DEPLOYMENT_NAME": "deployment-value-0003",
    "OPENAI_API_VERSION": "2024-10-21-value-0004",
    "JIRA_URL": "https://jira-value-0005.example.test",
    "JIRA_EMAIL": "jira-email-value-0006@example.test",
    "JIRA_API_TOKEN": "jira-token-value-0007",
    "SLACK_BOT_TOKEN": "slack-token-value-0008",
    "TEAMS_WEBHOOK_URL": "",
    "SMTP_HOST": "smtp-value-0009.example.test",
}


def assert_no_values(text: str) -> None:
    for value in VALUES.values():
        if value:
            assert value not in text


def test_discover_reports_names_and_never_values() -> None:
    report = discover(VALUES, dotenv=None)
    providers = {p.name: p for p in report.providers}
    assert providers["azure_openai"].configured and not providers["anthropic"].configured
    assert providers["anthropic"].missing == ("ANTHROPIC_API_KEY",)
    connectors = {c.name: c for c in report.connectors}
    assert connectors["jira"].configured and connectors["slack"].configured
    assert not connectors["teams"].configured and connectors["teams"].missing == (
        "TEAMS_WEBHOOK_URL",
    )
    assert connectors["smtp"].partial
    assert connectors["smtp"].missing == ("SMTP_SENDER",)
    assert "jira.create_issue" in connectors["jira"].approval
    assert connectors["jira"].env == ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_TOKEN")
    for text in (repr(report), str(report), json.dumps(report.to_dict()), report.brief()):
        assert_no_values(text)
    brief = report.brief()
    assert "Model providers: azure_openai" in brief
    assert (
        "- jira (Jira): jira.search_issues, jira.get_issue, jira.create_issue [approval]" in brief
    )
    assert report.available() == tuple(c for c in report.connectors if c.configured)


def test_azure_without_a_dated_api_version_counts_as_configured() -> None:
    env = {k: v for k, v in VALUES.items() if k != "OPENAI_API_VERSION"}

    azure = {p.name: p for p in discover(env, dotenv=None).providers}["azure_openai"]

    assert azure.configured and azure.missing == ()


def test_azure_with_entra_needs_the_service_principal_instead_of_the_key() -> None:
    env = {
        "AZURE_OPENAI_AUTH": "entra",
        "AZURE_OPENAI_ENDPOINT": VALUES["AZURE_OPENAI_ENDPOINT"],
        "AZURE_OPENAI_DEPLOYMENT_NAME": VALUES["AZURE_OPENAI_DEPLOYMENT_NAME"],
        "AZURE_TENANT_ID": "tenant-value-0010",
    }

    azure = {p.name: p for p in discover(env, dotenv=None).providers}["azure_openai"]

    assert not azure.configured
    assert azure.missing == ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
    assert "tenant-value-0010" not in repr(azure)


def test_discover_reads_dotenv_without_exporting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('SLACK_BOT_TOKEN="slack-token-value-0010"\n', encoding="utf-8")
    report = discover({}, dotenv=env_file)
    assert {c.name: c for c in report.connectors}["slack"].configured
    assert "SLACK_BOT_TOKEN" not in os.environ
    assert "slack-token-value-0010" not in json.dumps(report.to_dict())


def test_brief_marks_systems_that_list_their_own_tools() -> None:
    report = EnvironmentReport(connectors=(ConnectorStatus("crm", "CRM", "generic", True),))
    assert "crm (CRM): tools listed by the system (connector:crm)" in report.brief()


def test_doctor_lists_your_systems(tmp_path: Path) -> None:
    checks = doctor._check_systems(discover(VALUES, dotenv=None), tmp_path / ".env")
    by_name = {check.name: check for check in checks}
    assert by_name["systems"].summary.startswith(
        "external systems: 2 connected (slack, jira), 1 incomplete"
    )
    assert by_name["system:jira"].status == "ok"
    assert by_name["system:smtp"].status == "warn"
    assert "SMTP_SENDER" in by_name["system:smtp"].summary
    assert "system:workspace" not in by_name
    assert_no_values(json.dumps([c.data for c in checks]) + " ".join(c.summary for c in checks))
    report = doctor.run_doctor(cwd=tmp_path, environment=discover(VALUES, dotenv=None))
    assert report.check("system:jira") is not None


def run(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith(("JIRA", "SLACK", "SMTP", "TEAMS")):
            monkeypatch.delenv(name)
    for name, value in VALUES.items():
        if value:
            monkeypatch.setenv(name, value)


def test_cli_list_and_show(environment: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["connectors", "list", "--configured", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {"jira", "slack"} <= {row["name"] for row in rows}
    assert run(["connectors", "show", "jira"]) == 0
    shown = capsys.readouterr().out
    assert "JIRA_API_TOKEN" in shown and "SET" in shown and "[approval]" in shown
    assert_no_values(shown)
    from awa.cli import main

    assert main(["connectors", "show", "nope"]) == 1
    assert "error: unknown connector 'nope'" in capsys.readouterr().err
    assert main(["connectors", "test"]) == 1
    assert "name one or more connectors" in capsys.readouterr().err


def test_cli_test_runs_health_checks(
    environment: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = MockTransport({"GET /rest/api/2/myself": mock_response(json={"name": "me"})})
    real = _catalog.check_connector

    def with_transport(name: str, **options: Any) -> Any:
        return real(name, transport=transport, **options)

    monkeypatch.setattr("awa.connectors._catalog.check_connector", with_transport)
    assert run(["connectors", "test", "jira"]) == 0
    assert "OK   jira: Jira answered (health)" in capsys.readouterr().out
    assert run(["connectors", "test", "teams", "--json"]) == 1
    [result] = json.loads(capsys.readouterr().out)
    assert result["missing"] == ["TEAMS_WEBHOOK_URL"]


def test_cli_test_uses_a_blueprints_instances(
    environment: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "notes.txt").write_text("hello", encoding="utf-8")
    blueprint = tmp_path / "system.yaml"
    blueprint.write_text(
        "name: demo\nconnectors:\n  files: {use: workspace, root: data}\n"
        "agents:\n  - name: clerk\n    instructions: Read.\n    tools: [connector:files.read_file]\n",
        encoding="utf-8",
    )
    assert run(["connectors", "test", "--all", "--blueprint", str(blueprint)]) == 0
    assert "OK   files: folder" in capsys.readouterr().out


def test_discover_lists_every_provider_and_accepts_the_gemini_key() -> None:
    providers = {p.name: p for p in discover({"GEMINI_API_KEY": "g-value"}, dotenv=None).providers}

    assert {"mistralai", "groq", "ollama"} <= providers.keys()
    assert providers["google_genai"].configured and providers["google_genai"].missing == ()
    assert providers["mistralai"].missing == ("MISTRAL_API_KEY",)
    assert providers["groq"].missing == ("GROQ_API_KEY",)


def test_a_secret_variable_never_has_a_default() -> None:
    from awa.connectors._spec import EnvVar

    with pytest.raises(ConfigurationError, match="a secret has no default"):
        EnvVar("API_TOKEN", "The token.", default="changeme")
    assert EnvVar("API_URL", "Base URL.", secret=False, default="https://x.test").default


def test_a_python_registration_under_a_builtin_name_counts_as_a_replacement() -> None:
    from dataclasses import replace

    from awa.connectors import register_connector
    from awa.plugins import REGISTRY
    from awa.plugins.switches import choice, set_python

    mine = replace(JIRA, summary="mine")
    with REGISTRY.preserved():
        try:
            register_connector(mine, replace=True)
            picked = choice("connector.jira")

            assert picked.state == "replaced"
            assert picked.entry is not None and picked.entry.use == "python:register_connector"
            assert _catalog.builtin_connector("jira") is JIRA
            assert get_connector("jira") is mine
        finally:
            set_python("connector.jira", None)
    assert get_connector("jira") is JIRA


def test_the_catalog_lists_the_built_ins_in_provider_order() -> None:
    names = [spec.name for spec in list_connectors() if spec.category == "communication"]

    assert names == ["smtp", "imap", "webhook", "slack", "outlook", "teams"]
