"""Shared helpers of the Microsoft process tests: a fake tenant filled with the synthetic data of
`examples/microsoft/testdata`, a project copy of an example, and the scripted models.

Nothing is patched. The connectors reach the local Microsoft Graph test server (`FakeGraph`)
through `MS_GRAPH_URL`, `AZURE_AUTHORITY_HOST` and the explicit options of
`graph.connector_options`; the example's blueprint file is the shipped one.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from _helpers.fakegraph.files import FilesStore
from _helpers.fakegraph.mail import MailStore
from _helpers.fakegraph.server import FakeGraph
from _helpers.framing import tool_content
from awa.blueprints import Blueprint
from awa.models import ModelRegistry
from awa.testing import FunctionChatModel, ModelCall, structured_output, tool_call

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "microsoft"
CLIENT = "00000000-1111-2222-3333-444444444444"
SECRET = "client-secret-of-the-process-tests"
SITE_HOST, SITE_PATH = "contoso.sharepoint.com", "sites/company"
SITE_URL = f"https://{SITE_HOST}/{SITE_PATH}"
MAILBOXES = {
    "sales": "sales@company.example",
    "administration": "administration@company.example",
    "process": "process@company.example",
}
PROCESSES = (
    "sharepoint-briefing",
    "outlook-briefing",
    "approved-digest",
    "sales-inbox",
    "sales-offer-preparation",
    "administration-inbox",
)
FOLDERS = {
    "sharepoint-briefing": "briefings",
    "outlook-briefing": "briefings",
    "approved-digest": "digest",
    "sales-inbox": "sales",
    "sales-offer-preparation": "sales",
    "administration-inbox": "administration",
}


def example_folder(name: str) -> Path:
    """The folder of the example `name` (`examples/microsoft/<area>/<name>`)."""

    return EXAMPLES / FOLDERS[name] / name


GRAPH_CONNECTORS = ("outlook", "sharepoint", "teams")


def make_testdata() -> ModuleType:
    """The generator of the synthetic workbooks (`examples/microsoft/make_testdata.py`)."""

    spec = importlib.util.spec_from_file_location("ms_make_testdata", EXAMPLES / "make_testdata.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mail_data() -> dict[str, dict[str, Any]]:
    """The synthetic mails by label."""

    found = yaml.safe_load((EXAMPLES / "testdata" / "mails" / "mails.yaml").read_text("utf-8"))
    return {mail["label"]: mail for mail in found["mails"]}


class Tenant:
    """A fake Microsoft 365 tenant: the three mailboxes, one SharePoint site, one app.

    The app holds `Mail.ReadWrite` (drafts need it) and `Sites.Selected` with a `read` grant on the
    one site; `send=True` adds `Mail.Send`, `grant="write"` a write role. It has no other right.
    """

    def __init__(self, graph: FakeGraph, *, send: bool = False, grant: str = "read") -> None:
        self.graph = graph
        self.mail: MailStore = graph.store("mail")
        self.files: FilesStore = graph.store("files")
        roles = ("Mail.ReadWrite", "Sites.Selected", *(("Mail.Send",) if send else ()))
        self.app = graph.add_app(CLIENT, SECRET, roles=roles)
        self.users = {key: graph.add_user(upn) for key, upn in MAILBOXES.items()}
        self.site = self.files.add_site(SITE_HOST, SITE_PATH)
        self.files.grant(self.site, CLIENT, grant)

    def seed_mails(self, *labels: str) -> None:
        """Put the mails with these `labels` (in the order given) into their mailboxes."""

        data = mail_data()
        for label in labels:
            mail = data[label]
            self.mail.add_message(
                self.users[mail["mailbox"]],
                subject=mail["subject"],
                body=mail["body"],
                sender=mail["from"],
                received=mail["received"],
                headers=mail.get("headers"),
            )

    def seed_documents(self, *names: str) -> None:
        """Put the sample documents (all of them without `names`) into the folder `Approved`."""

        found = make_testdata().documents()
        for name in names or tuple(found):
            self.files.put_file(self.site, f"Approved/{name}", found[name])

    def env(self, mailbox: str) -> dict[str, str]:
        return {
            **self.graph.env(self.app),
            "MS_MAILBOX": MAILBOXES[mailbox],
            "MS_SITE_URL": SITE_URL,
        }

    def drafts(self, mailbox: str) -> list[dict[str, Any]]:
        """The messages in the Drafts folder of `mailbox`, oldest first."""

        return list(reversed(self.mail.box(self.users[mailbox].id)["drafts"]))

    def sent(self, mailbox: str) -> list[dict[str, Any]]:
        return list(reversed(self.mail.box(self.users[mailbox].id)["sentitems"]))

    def requests(self, method: str, path: str = "*") -> list[Any]:
        return self.graph.calls(method, f"/v1.0/{path}")


class Project:
    """A copy of one example in a temporary folder, run against a `Tenant`."""

    def __init__(self, name: str, folder: Path, tenant: Tenant, mailbox: str) -> None:
        self.name, self.folder, self.tenant, self.mailbox = name, folder, tenant, mailbox

    @property
    def blueprint_file(self) -> Path:
        return self.folder / "blueprint.yaml"

    def blueprint(self) -> Blueprint:
        """The shipped blueprint; the Microsoft connectors get the options for the local server."""

        blueprint = Blueprint.from_yaml_file(self.blueprint_file)
        for options in blueprint.connectors.values():
            if options.get("use") in GRAPH_CONNECTORS:
                options.update(self.tenant.graph.connector_options)
        return blueprint

    def build(self, model: Callable[[ModelCall], Any]) -> Any:
        models = ModelRegistry()
        models.register("default", FunctionChatModel(model))
        return self.blueprint().build(model_registry=models, checkpointer=InMemorySaver())


def project_for(
    name: str, mailbox: str, tenant: Tenant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Project:
    """Copy the example `name` to `tmp_path`, make it the working directory and set its `.env`."""

    folder = tmp_path / name
    shutil.copytree(example_folder(name), folder, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.chdir(folder)
    for variable, value in tenant.env(mailbox).items():
        monkeypatch.setenv(variable, value)
    return Project(name, folder, tenant, mailbox)


# ------------------------------------------------------------------------ reading a conversation


def request_text(call: ModelCall) -> str:
    return next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))


class _Next(Exception):
    """Raised by `Replay` when the tool call it was asked for has not been made yet."""

    def __init__(self, response: Any) -> None:
        super().__init__("next call")
        self.response = response


class Replay:
    """The conversation so far as a table of finished tool calls.

    A scripted model is plain Python that follows the procedure of the example: each time it needs
    a tool result it asks `replay(tool, **arguments)`. A call made before returns its parsed result;
    a new one ends this model turn with that tool call. So the model decides the next call from what
    the real tools answered, like a real model would, and the whole procedure is replayed each turn.
    """

    def __init__(self, call: ModelCall) -> None:
        self.call = call
        self.done: dict[str, str] = {}
        pending: dict[str, str] = {}
        for message in call.messages:
            if isinstance(message, AIMessage):
                for item in message.tool_calls:
                    pending[item["id"]] = self.key(item["name"], item["args"])
            elif isinstance(message, ToolMessage) and message.tool_call_id in pending:
                self.done[pending[message.tool_call_id]] = str(message.content)

    @staticmethod
    def key(name: str, arguments: dict[str, Any]) -> str:
        return name + json.dumps(arguments, sort_keys=True, default=str)

    def raw(self, name: str, **arguments: Any) -> str:
        found = self.done.get(self.key(name, arguments))
        if found is None:
            raise _Next(tool_call(name, arguments))
        return found

    def __call__(self, name: str, **arguments: Any) -> Any:
        text = tool_content(self.raw(name, **arguments))
        try:
            return json.loads(text)
        except ValueError:
            return text

    def calls(self, name: str) -> list[dict[str, Any]]:
        return [
            dict(item["args"])
            for m in self.call.messages
            if isinstance(m, AIMessage)
            for item in m.tool_calls
            if item["name"] == name
        ]


def scripted(procedure: Callable[[Replay, str], Any], schema_name: str | None = None) -> Any:
    """A model function from `procedure(replay, request) -> final answer`.

    The final answer is text, or a dict that is returned as the structured output named
    `schema_name`. Tool calls the procedure still needs are made one per turn.
    """

    def model(call: ModelCall) -> Any:
        replay = Replay(call)
        try:
            answer = procedure(replay, request_text(call))
        except _Next as step:
            return step.response
        if isinstance(answer, dict):
            assert schema_name is not None
            return structured_output(schema_name, answer)
        return answer

    return model


def output_name(project: Project, agent: str) -> str:
    """The name of the structured-output tool of `agent` (what a model calls to finish)."""

    from awa.blueprints.json_schema import model_from_json_schema

    schema = project.blueprint().agent(agent).output_schema
    assert schema is not None
    return model_from_json_schema(schema, name=f"{agent}_output").__name__


__all__ = [
    "CLIENT",
    "EXAMPLES",
    "FOLDERS",
    "example_folder",
    "MAILBOXES",
    "PROCESSES",
    "SECRET",
    "SITE_URL",
    "Project",
    "Replay",
    "Tenant",
    "mail_data",
    "make_testdata",
    "output_name",
    "project_for",
    "request_text",
    "scripted",
]
