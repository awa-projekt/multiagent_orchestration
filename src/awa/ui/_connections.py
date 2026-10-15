"""Settings forms generated from catalog metadata: model providers and the systems a project uses."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from awa._internal.http import HttpPolicy
from awa._internal.upstream.tools import ToolException
from awa.connectors import get_connector, list_connectors
from awa.connectors._catalog import connector_context
from awa.core.dotenv import Environment, read_env_file
from awa.core.providers import PROVIDERS
from awa.core.providers import provider as provider_spec
from awa.errors import AwaError, ConfigurationError
from awa.kernel.redaction import redact_values
from awa.ui._plain import humanize

if TYPE_CHECKING:
    from awa.connectors._spec import ConnectorSpec
    from awa.ui._project import Project

Kind = str  # "secret" (hidden input), "link" (a validated address) or "text"

_ACRONYM_LIST = (
    "api url uri id dsn sql smtp imap http https tls ssl aws db sap sso ldap sftp ftp ip tcp "
    "crm erp hr bi kpi pdf csv xml json"
)
_ACRONYMS = frozenset(_ACRONYM_LIST.split())
_BRANDS = {
    "openai": "OpenAI",
    "github": "GitHub",
    "gitlab": "GitLab",
    "sharepoint": "SharePoint",
    "hubspot": "HubSpot",
    "servicenow": "ServiceNow",
    "netsuite": "NetSuite",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "oauth": "OAuth",
}
_SECRET_WORDS = frozenset({"key", "token", "secret", "password", "dsn"})
_LINK_WORDS = frozenset({"url", "uri", "endpoint", "spec"})
_NOT_LINKS = frozenset(
    {"database", "db", "dsn", "connection", "broker"}
)  # addresses with a password
_PROVIDER_HINTS = {
    "AZURE_OPENAI_API_KEY": "The key of your Azure OpenAI resource.",
    "AZURE_OPENAI_ENDPOINT": "The address of your Azure OpenAI resource, copied from the resource, for example https://my-resource.openai.azure.com",
    "AZURE_OPENAI_DEPLOYMENT_NAME": "The name you gave the model deployment in Azure (not the model's own name).",
    "OPENAI_API_VERSION": "The API version your Azure resource supports; ask the person who manages it if unsure.",
    "OLLAMA_HOST": "Where Ollama listens, on this computer or in your network, for example http://localhost:11434",
}
_PROBLEMS = (
    (("401", "403", "unauthorized", "forbidden", "authentication", "invalid api key", "denied"),
     "The system refused the key or the login. Check the key (or user name and password) and that it is allowed to read this data."),
    (("does not exist", "no such file"),
     "The file or folder was not found. Check the path and that it is spelled exactly."),
    (("404", "not found"),
     "The address was not found. Check the link for typos and that it is the address of the API, not of its web page."),
    (("timed out", "timeout"),
     "The system did not answer in time. Check the link and your network (a system in the company network may need the VPN)."),
    (("ssl", "certificate"),
     "The secure connection failed. Check that the link starts with https:// and that the certificate is valid."),
    (("500", "502", "503", "504", "server error", "unavailable"),
     "The system reported a problem on its side. Try again later."),
    (("private", "not in the allowed hosts"),
     "This address points to a private network, which awa blocks by default. Ask your administrator."),
    (("429", "rate limit", "too many"),
     "The system is limiting requests. Wait a minute and try again."),
    (("name or service", "getaddrinfo", "cannot resolve", "resolve host", "connection refused", "unreachable", "connect"),
     "awa could not reach this address. Check the link, your internet connection and, for a system in the company network, the VPN."),
    (("password authentication", "access denied", "login failed"),
     "The user name or password was refused. Check the connection address."),
)  # fmt: skip
_MAX_VALUE = 2048
CATEGORY_TITLES = {
    "crm": "Customers (CRM)",
    "code": "Code",
    "data": "Data and databases",
    "files": "Files",
    "web": "Web",
    "generic": "Any other system",
    "payments": "Payments",
    "ticketing": "Tickets and issues",
    "communication": "Messages and email",
    "collaboration": "Documents and collaboration",
}
"""How each connector category is named on a page."""


def label_of(name: str) -> str:
    """`JIRA_API_TOKEN` as `Jira API token`."""

    words = [word.lower() for word in name.split("_") if word]
    shown = [_BRANDS.get(w) or (w.upper() if w in _ACRONYMS else w) for w in words]
    if shown and shown[0] == words[0]:
        shown[0] = shown[0].capitalize()
    return " ".join(shown)


_INSTANCE_LABELS = {
    "URL": "API link",
    "BASE_URL": "API link",
    "TOKEN": "API key",
    "KEY": "API key",
    "API_KEY": "API key",
    "USERNAME": "User name",
    "PASSWORD": "Password",
    "SPEC": "API description (link or file)",
    "DATABASE_URL": "Database connection address",
}


def system_title(key: str) -> str:
    """The name of a system the project calls `key`: `crm` as `CRM`, `order_desk` as `Order desk`."""

    return label_of(key)


def instance_label(name: str, key: str) -> str:
    """The label of a setting of the system `key`, without repeating the system: `CRM_URL` of the
    system `crm` is the `API link`, `CRM_TOKEN` the `API key`."""

    prefix = key.upper() + "_"
    if not name.startswith(prefix):
        return label_of(name)
    rest = name[len(prefix) :]
    return _INSTANCE_LABELS.get(rest) or label_of(rest)


def kind_of(name: str, *, secret: bool | None = None, example: str | None = None) -> Kind:
    """How a setting is entered: `secret`, `link` or `text`; without `secret` the name decides."""

    words = set(name.lower().split("_"))
    if secret or (secret is None and words & _SECRET_WORDS):
        return "secret"
    if (words & _LINK_WORDS and not words & _NOT_LINKS) or (example or "").startswith(
        ("http://", "https://")
    ):
        return "link"
    return "text"


def check_link(
    name: str,
    value: str,
    *,
    allow_http: bool = False,
    allow_path: bool = False,
    label: str | None = None,
) -> str:
    """The address `value` for the setting `name`, or an error that says what to change.

    `https://` without a user name; `http://` only locally or with `allow_http`; with
    `allow_path` a file path too. The error never contains the value.
    """

    text = value.strip()
    label = label or label_of(name)
    if not text or len(text) > _MAX_VALUE:
        raise ValueError(f"{label}: enter an address that starts with https://")
    if allow_path and "://" not in text:
        return text
    policy = HttpPolicy(allowed_hosts=None, allow_private_networks=True, allow_http=allow_http)
    try:
        policy.check_url(text)
    except ToolException as exc:
        reason = str(exc)
        if "http://" in reason and "only" in reason:
            reason = "plain http:// is not secure; use an address that starts with https://"
        raise ValueError(f"{label}: {reason}") from None
    return text


def advice(message: str, ok: bool) -> str:
    """What to do about the outcome of a connection test, in plain words."""

    if ok:
        return "The connection works."
    lowered = message.lower()
    if lowered.startswith("not configured") or "is not configured" in lowered:
        return "Enter the missing settings first, then test again."
    for words, text in _PROBLEMS:
        if any(word in lowered for word in words):
            return text
    return (
        "Check the settings above, or ask the person who manages this system for the right values."
    )


@dataclass(frozen=True)
class _Entry:
    """One system of the project, ready for a form."""

    key: str
    system: str
    spec: ConnectorSpec
    config: Mapping[str, Any]
    step: Mapping[str, Any] | None
    fields: list[dict[str, Any]] = field(default_factory=list)
    optional: list[dict[str, Any]] = field(default_factory=list)

    @property
    def names(self) -> set[str]:
        return {item["name"] for item in (*self.fields, *self.optional)}

    def lookup(self, name: str) -> dict[str, Any]:
        return next(item for item in (*self.fields, *self.optional) if item["name"] == name)


def _field(
    name: str,
    description: str,
    kind: Kind,
    *,
    required: bool,
    is_set: bool,
    hint: str | None,
    allow_http: bool = False,
    label: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label or label_of(name),
        "description": description,
        "kind": kind,
        "required": required,
        "set": is_set,
        "placeholder": hint or "",
        "allow_http": allow_http,
        "allow_path": "file path" in description.lower(),
    }


def _spec_fields(
    spec: ConnectorSpec,
    renamed: Mapping[str, str],
    active: Any,
    is_set: Any,
    *,
    allow_http: bool = False,
    system: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The required and optional fields of a connector; labels omit the instance name `system`."""

    needed = {v.name for v in spec.env if v.required}
    if spec.auth and not spec.auth_optional:
        needed |= {v.name for v in active.variables() if v.required}
    required: list[dict[str, Any]] = []
    optional: list[dict[str, Any]] = []
    for variable in spec.variables():
        name = renamed.get(variable.name, variable.name)
        item = _field(
            name,
            variable.description,
            kind_of(name, secret=variable.secret, example=variable.example or variable.default),
            required=variable.name in needed,
            is_set=is_set(name),
            hint=variable.default or variable.example,
            allow_http=allow_http,
            label=instance_label(name, system) if system else None,
        )
        (required if item["required"] else optional).append(item)
    return required, optional


def provider_variables() -> dict[str, tuple[str, ...]]:
    """The model providers and the settings each takes."""

    return {spec.name: spec.fields() for spec in PROVIDERS}


def provider_forms(report: Any, is_set: Any) -> list[dict[str, Any]]:
    """One form per model provider: its title, whether it is configured and its fields.

    The providers of the discovery report and the provider table; one unknown to the table
    still gets a field per missing setting.
    """

    known = provider_variables()
    entries = {p.name: (p.configured, tuple(p.missing)) for p in report.providers}
    for name, names in known.items():
        missing = tuple(n for n in names if not is_set(n))
        entries.setdefault(name, (not missing, missing))
    forms = []
    for name, (configured, missing) in entries.items():
        spec = provider_spec(name)
        local = spec is not None and spec.local
        fields = [
            _field(
                variable,
                _PROVIDER_HINTS.get(variable, ""),
                kind_of(variable, example="http://" if local else None),
                required=True,
                is_set=is_set(variable),
                hint=None,
                allow_http=local,
            )
            for variable in known.get(name) or missing
        ]
        forms.append(
            {
                "name": name,
                "title": spec.title if spec is not None else humanize(name),
                "configured": configured,
                "missing": list(missing),
                "fields": fields,
            }
        )
    return forms


def connector_forms(report: Any, is_set: Any) -> list[dict[str, Any]]:
    """What the Set up page lists per catalog connector (title, category, status, fields)."""

    specs = {spec.name: spec for spec in list_connectors()}
    forms = []
    for status in report.connectors:
        spec = specs.get(status.name)
        fields: list[dict[str, Any]] = []
        optional: list[dict[str, Any]] = []
        if spec is not None:
            first = spec.auth[0] if spec.auth else None
            fields, optional = _spec_fields(spec, {}, first, is_set)
        forms.append(
            {
                "name": status.name,
                "title": status.title,
                "category": status.category,
                "configured": status.configured,
                "missing": list(status.missing),
                "tools": list(status.tools),
                "fields": fields,
                "optional": optional,
                "setup": spec.setup if spec else "",
                "docs": spec.docs if spec else "",
            }
        )
    return forms


def setting_kinds(project: Project) -> dict[str, dict[str, Any]]:
    """How every savable setting is entered and checked (the project's systems win)."""

    kinds: dict[str, dict[str, Any]] = {}
    for provider in PROVIDERS:
        for name in provider.fields():
            kinds[name] = {
                "kind": kind_of(name, example="http://" if provider.local else None),
                "allow_http": provider.local,
                "allow_path": False,
            }
    for spec in list_connectors():
        for variable in spec.variables():
            kinds.setdefault(
                variable.name,
                {
                    "kind": kind_of(
                        variable.name,
                        secret=variable.secret,
                        example=variable.example or variable.default,
                    ),
                    "allow_http": False,
                    "allow_path": "file path" in variable.description.lower(),
                },
            )
    if project.blueprint_file.is_file():
        for entry in Connections(project)._entries():
            for item in (*entry.fields, *entry.optional):
                kinds[item["name"]] = {
                    key: item[key] for key in ("kind", "allow_http", "allow_path")
                }
    return kinds


def check_values(values: Mapping[str, str], kinds: Mapping[str, Mapping[str, Any]]) -> None:
    """Refuse an invalid address among `values`; the message never contains a value."""

    for name, value in values.items():
        meta = kinds.get(name)
        if meta and meta["kind"] == "link" and value.strip():
            check_link(
                name,
                value,
                allow_http=bool(meta.get("allow_http")),
                allow_path=bool(meta.get("allow_path")),
                label=meta.get("label"),
            )


class Connections:
    """The Connect step of one project: its systems, their forms, tests and connecting.

    A saved value goes to the project's `.env` and is never sent back; links must be `https://`
    (`http://` only locally or with `allow_http`); errors never contain a value.
    """

    def __init__(self, project: Project) -> None:
        self._project = project

    def _environment(self, entered: Mapping[str, str] | None = None) -> dict[str, str]:
        """What the connectors would read: the process environment, the saved `.env` for names
        it does not set (as `awa ui` exports it), then the values typed on the page."""

        merged = {k: v for k, v in self._project.environ.items() if v}
        for name, value in read_env_file(self._project.env_file).items():
            if value and not merged.get(name):
                merged[name] = value
        merged.update({k: v for k, v in (entered or {}).items() if v.strip()})
        return merged

    def _entries(self) -> list[_Entry]:
        from awa.blueprints._project_files import parse_steps

        project = self._project
        blueprint = project.blueprint()
        steps = {str(step.get("key")): step for step in parse_steps(project.guide())}
        wanted: list[tuple[str, str, Mapping[str, Any], Mapping[str, Any] | None]] = [
            (key, str(step["system"]), dict(step.get("config") or {}), step)
            for key, step in steps.items()
        ]
        for key, options in blueprint.connectors.items():
            if key not in steps and options.get("use", key) != "workspace":
                title = get_connector(str(options.get("use", key))).title
                own = system_title(key) if options.get("use", key) != key else title
                wanted.append((key, own, dict(options), None))
        used = {
            tool.removeprefix("connector:").split(".", 1)[0]
            for agent in blueprint.agents
            for tool in agent.tools
            if tool.startswith("connector:")
        }
        for name in sorted(used - set(blueprint.connectors) - set(steps)):
            try:
                wanted.append((name, get_connector(name).title, {"use": name}, None))
            except ConfigurationError:
                continue
        entries = []
        for key, system, config, step in wanted:
            try:
                entries.append(self._entry(key, system, config, step))
            except AwaError:
                continue  # a system whose connector is unknown here has no form
        return entries

    def _entry(
        self, key: str, system: str, config: Mapping[str, Any], step: Mapping[str, Any] | None
    ) -> _Entry:
        env = self._environment()
        ctx = connector_context(key, env=env, config=config, dotenv=None)
        generic = get_connector(str(config.get("use") or key)).configure is not None
        active = ctx.active_auth() or (ctx.spec.auth[0] if ctx.spec.auth else None)
        renamed = {v.name: ctx.env.name(v.name) for v in ctx.spec.variables()}
        present = Environment(env, dotenv=None)
        fields, optional = _spec_fields(
            ctx.spec,
            renamed,
            active,
            lambda name: bool(present.get(name)),
            allow_http=bool(config.get("allow_http")),
            system=key if generic or config.get("env") else None,
        )
        return _Entry(key, system, ctx.spec, config, step, fields, optional)

    def names(self) -> set[str]:
        """Every variable the forms of the project's systems take."""

        return {name for entry in self._entries() for name in entry.names}

    def systems(self) -> dict[str, Any]:
        """The Connect page: one card per system with its fields, status and what to do."""

        from awa.blueprints import read_declared_systems

        project = self._project
        if not project.blueprint_file.is_file():
            return {"exists": False, "systems": []}
        blueprint = project.blueprint()
        declared = {system.key for system in read_declared_systems(project.directory)}
        cards = []
        for entry in self._entries():
            switched = blueprint.connectors.get(entry.key, {}).get("use") != "workspace"
            was_sample = entry.step is not None
            card = self._card(entry, switched=switched, was_sample=was_sample)
            if entry.key in declared and card["status"] == "connected":
                card["status_text"] = (
                    f"Live: {entry.system} was declared under Your data, so awa reads it "
                    "directly, read-only."
                )
                card["declared"] = True
            cards.append(card)
        counts = {"connected": 0, "attention": 0, "not_set_up": 0}
        for card in cards:
            counts[card["status"]] += 1
        return {"exists": True, "systems": cards, "counts": counts}

    def _card(self, entry: _Entry, *, switched: bool, was_sample: bool) -> dict[str, Any]:
        needed = [item for item in entry.fields if item["required"]]
        missing = [item["label"] for item in needed if not item["set"]]
        any_set = any(item["set"] for item in (*entry.fields, *entry.optional))
        if switched and not missing:
            status, text = "connected", f"Live: awa talks to {entry.system} directly."
        elif missing and (any_set or (switched and was_sample)):
            status = "attention"
            text = "Still missing: " + ", ".join(missing) + "."
        elif missing:
            status = "not_set_up"
            text = (
                f"Not connected yet: until you connect {entry.system}, awa uses a stand-in with "
                "sample records it wrote from your description."
                if was_sample
                else f"Not set up: enter the settings below to connect {entry.system}."
            )
        else:
            status = "attention"
            text = "The settings are complete. Press Save and connect to switch to the live system."
        return {
            "key": entry.key,
            "system": entry.system,
            "connector": entry.spec.name,
            "connector_title": entry.spec.title,
            "summary": entry.spec.summary,
            "setup": entry.spec.setup.replace("<NAME>", entry.key.upper()),
            "docs": entry.spec.docs,
            "notes": list(entry.spec.notes),
            "status": status,
            "status_text": text,
            "mode": "real" if switched else "sample",
            "sample": was_sample,
            "declared": False,
            "tools": list(entry.step.get("tools", [])) if entry.step else [],
            "fields": entry.fields,
            "optional": entry.optional,
        }

    def _entry_of(self, key: str) -> _Entry:
        for entry in self._entries():
            if entry.key == key:
                return entry
        raise ValueError(f"This project has no system {key!r} to connect")

    def _clean(self, entry: _Entry, values: Mapping[str, str]) -> dict[str, str]:
        """The values of `values` that belong to `entry`'s form, checked; blanks are skipped."""

        clean = {name: str(value).strip() for name, value in values.items() if str(value).strip()}
        for name in values:
            if name not in entry.names:
                raise ValueError(f"{name!r} is not a setting of {entry.system}")
        check_values(clean, {item["name"]: item for item in (*entry.fields, *entry.optional)})
        return clean

    def test(self, key: str, values: Mapping[str, str]) -> dict[str, Any]:
        """Test one system with the typed values over the saved ones; `ok`, `message`, `advice`."""

        from awa.connectors import check_connector

        entry = self._entry_of(key)
        clean = self._clean(entry, values)
        env = self._environment(clean)
        result = check_connector(
            key, env=env, config=entry.config, transport=self._project.transport
        )
        message = self._project.redacted(redact_values(result.message, clean.values()))
        return {
            "ok": result.ok,
            "message": message,
            "advice": advice(message, result.ok),
            "seconds": round(result.seconds, 2),
        }

    async def save(self, key: str, values: Mapping[str, str]) -> dict[str, Any]:
        """Save the values, test the system and on success switch the blueprint to it.

        Files are written on the event loop's thread, so two saves never interleave; `removed`
        lists sample-data tools without a counterpart in the real system.
        """

        from awa.blueprints._connect import _switch_file

        project = self._project
        entry = self._entry_of(key)
        written = project.save_settings(self._clean(entry, values))
        outcome = await asyncio.to_thread(self.test, key, {})
        removed: list[str] = []
        step = entry.step
        on_sample = project.blueprint().connectors.get(key, {}).get("use") == "workspace"
        if outcome["ok"] and step is not None and on_sample:
            switch = {ref: tuple(targets) for ref, targets in step["switch"].items()}
            _switch_file(project.blueprint_file, switch, {key: dict(step["config"])})
            removed = [ref for ref, targets in switch.items() if not targets]
            project._system = None
        return {"saved": written, "test": outcome, "connected": outcome["ok"], "removed": removed}

    def back_to_stand_in(self, key: str) -> dict[str, Any]:
        """Switch a live system back to its sample records in `data/`; settings stay in `.env`."""

        from itertools import takewhile

        from awa.ui._cases import CaseEditor, on_stand_ins

        project = self._project
        if key not in CaseEditor(project).switchable():
            raise ValueError(f"{key!r} has no stand-in to go back to")
        text = project.blueprint_file.read_text(encoding="utf-8")
        header = "".join(takewhile(lambda line: line.startswith("#"), text.splitlines(True)))
        blueprint = on_stand_ins(project, [key])
        project.blueprint_file.write_text(
            header + blueprint.to_yaml(), encoding="utf-8", newline="\n"
        )
        project._system = None
        return {"stand_in": True}
