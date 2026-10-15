"""The connector catalog, configured instances, tools and health checks."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awa._internal.upstream.tools import BaseTool, ToolException
from awa._internal.util import name_taken
from awa.connectors._engine import (
    COMMON_OPTIONS,
    ConnectorContext,
    Transport,
    call_operation,
    http_tools,
)
from awa.connectors._generic import GRAPHQL, ODATA, REST
from awa.connectors._local import LOCAL
from awa.connectors._openapi import OPENAPI
from awa.connectors._saas import SAAS
from awa.connectors._spec import CATEGORIES, ConnectorSpec, Operation
from awa.core.dotenv import Environment
from awa.errors import AwaError, ConfigurationError
from awa.plugins import REGISTRY, KindView, current_project

_PREFIX = "connector:"

_BUILTIN: tuple[ConnectorSpec, ...] = (OPENAPI, REST, ODATA, GRAPHQL, *LOCAL, *SAAS)
CORE_CONNECTORS = frozenset(
    {"openapi", "rest", "odata", "graphql", "sql", "web", "smtp", "imap", "webhook", "workspace"}
)  # fmt: skip
"""The generic connectors with a stability promise; every other one is preview (tested against
fake connections; the connectors for Microsoft 365 also read live on one tenant; see
docs/develop/connectors.md)."""
_REGISTRY: MutableMapping[str, ConnectorSpec] = KindView(REGISTRY, "connectors", current_project)
_INSTANCE = re.compile(r"^[a-z][a-z0-9_]*$")
_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def list_connectors() -> list[ConnectorSpec]:
    """The catalog: every connector, by category, then in catalog order."""

    order = {category: index for index, category in enumerate(CATEGORIES)}
    shown = [spec for spec in _REGISTRY.values() if not _switched_off(spec.name)]
    position = {name: index for index, name in enumerate(_shipped())}
    return sorted(
        shown, key=lambda s: (order.get(s.category, 99), position.get(s.name, len(position)))
    )


def builtin_connectors() -> list[tuple[str, ConnectorSpec, str]]:
    """The generic connectors awa ships as `(name, spec, summary)` rows, a provider of the registry."""
    return [(spec.name, spec, spec.summary) for spec in _BUILTIN]


def _shipped() -> dict[str, ConnectorSpec]:
    # What the built-in providers return, in provider order; a registration never changes it.
    return {entry.name: entry.loader() for entry in REGISTRY.builtins("connectors")}


def get_connector(name: str) -> ConnectorSpec:
    """The connector called `name`, as the project's switches and plugins have it.

    A connector switched off in the blueprint's `components:` is refused; a project or installed
    connector with a built-in's name is used when it says `replace`.

    Raises:
        ConfigurationError: If there is none, or it is switched off.
    """

    if _INSTANCE.fullmatch(name):
        from awa.plugins.switches import choice, replacement

        if (picked := choice(f"connector.{name}")).is_off:
            raise ConfigurationError(
                f"the connector {name!r} is switched off for this project; remove "
                f"`connector: {{{name}: off}}` from the `components:` of the blueprint"
            )
        own: ConnectorSpec | None = replacement(picked)
        if own is not None:
            return own
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise ConfigurationError(f"unknown connector {name!r} (known: {known})") from None


def builtin_connector(name: str) -> ConnectorSpec | None:
    """The connector awa ships under `name` (whatever is registered or switched), or `None`."""

    return _shipped().get(name)


def _switched_off(name: str) -> bool:
    if not _INSTANCE.fullmatch(name):
        return False
    from awa.plugins.switches import is_off

    return is_off(f"connector.{name}")


def register_connector(spec: ConnectorSpec, *, replace: bool = False) -> ConnectorSpec:
    """Add a connector of your own to the catalog (for `discover`, blueprints and the CLI).

    Raises:
        ConfigurationError: If the name is taken and `replace` is not set.

    Example:
        ```python
        spec = ConnectorSpec("inventory", "Inventory", "data", "Stock levels.", operations=OPS)
        register_connector(spec)
        tools = connector_tools("inventory")
        ```
    """

    if spec.name in _REGISTRY and not replace:
        raise ConfigurationError(name_taken("connector", spec.name))
    shipped = _shipped().get(spec.name)
    REGISTRY.register("connectors", spec.name, spec, replace=True, summary=spec.summary)
    if _INSTANCE.fullmatch(spec.name):
        from awa.plugins.switches import Entry, set_python

        # Python wins over the project file: the registered spec is what this process uses.
        replaces = shipped is not None and spec is not shipped
        entry = Entry(use="python:register_connector") if replaces else Entry(state="on")
        set_python(f"connector.{spec.name}", entry, spec if replaces else None)
    return spec


def split_reference(reference: str) -> tuple[str, str | None]:
    """`"connector:jira.search_issues"` (or `"jira.search_issues"`, `"jira"`) as
    `(instance, operation)`."""

    body = reference.removeprefix(_PREFIX)
    instance, _, operation = body.partition(".")
    return instance, operation or None


def option_problems(spec: ConnectorSpec, options: Mapping[str, Any]) -> list[str]:
    """What is wrong with an instance's options (unknown names, wrong shapes), offline."""

    known = set(COMMON_OPTIONS) | set(spec.options)
    problems = [
        f"unknown option {name!r} of the {spec.name} connector (known: {', '.join(sorted(known))})"
        for name in sorted(set(options) - known)
    ]
    renames = options.get("env")
    if renames is not None and not (
        isinstance(renames, Mapping)
        and all(
            _VARIABLE.fullmatch(str(k)) and _VARIABLE.fullmatch(str(v)) for k, v in renames.items()
        )
    ):
        problems.append("env must map variable names to the names to read instead")
    include = options.get("include")
    if include is not None and not (
        isinstance(include, str)
        or (isinstance(include, list | tuple) and all(isinstance(item, str) for item in include))
    ):
        problems.append("include must be an operation name or a list of them")
    for flag in ("read_only", "requires_approval", "allow_private_networks", "allow_http"):
        if flag in options and not isinstance(options[flag], bool):
            problems.append(f"{flag} must be true or false")
    return problems


def connector_context(
    name: str | ConnectorSpec,
    *,
    env: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
    transport: Transport | None = None,
    dotenv: str | Path | None = ".env",
    ungated: frozenset[str] = frozenset(),
) -> ConnectorContext:
    """A configured instance of a connector.

    Args:
        name: Instance name (a catalog name, or any name with a `use` option naming the
            connector), or a `ConnectorSpec`.
        env: Variables to use; `None` reads the process environment, then `dotenv`.
        transport: Sends the requests (a `MockTransport` in tests).
        ungated: Operations a blueprint approval gates instead.

    Raises:
        ConfigurationError: If the connector is unknown or an option is invalid.
    """

    options = dict(config or {})
    if isinstance(name, ConnectorSpec):
        spec, instance = name, name.name
    else:
        instance = name
        spec = get_connector(str(options.get("use") or name))
    if not _INSTANCE.fullmatch(instance):
        raise ConfigurationError(f"connector instance name {instance!r} is not snake_case")
    problems = option_problems(spec, options)
    if problems:
        raise ConfigurationError(f"connector {instance!r}: " + "; ".join(problems))
    environment = Environment(
        env, dotenv=dotenv if env is None else None, renames=options.get("env") or {}
    )
    if spec.configure is not None:
        spec = spec.configure(spec, instance, options)
    return ConnectorContext(spec, instance, environment, options, transport, ungated)


def build_tools(ctx: ConnectorContext) -> list[BaseTool]:
    """The tools of a configured instance."""

    return ctx.spec.factory(ctx) if ctx.spec.factory is not None else http_tools(ctx)


def connector_tools(
    name: str | ConnectorSpec,
    *,
    env: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
    transport: Transport | None = None,
) -> list[BaseTool]:
    """The tools of a connector: reads run directly, writes wait for approval by default.

    HTTP connectors read their variables on every call, so the tools can be built before the
    credentials are set; a call then says which variable is missing. Connectors wrapping a toolkit
    or protocol (`workspace`, `sql`, `smtp`, `imap`, `webhook`) need them when the tools are built.

    Args:
        name: `"jira"`, one operation `"jira.search_issues"`, an instance name with a `use`
            option, or a `ConnectorSpec`.
        env: Variables to use instead of the process environment and `.env`.
        config: Options (`base_url`, `include`, `read_only`, `auth`, ...).
        transport: Sends the requests instead of the network (tests).

    Raises:
        ConfigurationError: If the connector, an option or an operation is unknown.

    Example:
        ```python
        tools = connector_tools("jira", config={"read_only": True})
        agent = Agent("triage", tools=tools)
        ```
    """

    options = dict(config or {})
    if isinstance(name, str):
        name, operation = split_reference(name)
        if operation is not None:
            options["include"] = [operation]
    return build_tools(connector_context(name, env=env, config=options, transport=transport))


@dataclass(frozen=True)
class ConnectorCheck:
    """The outcome of `check_connector`, without credentials; `missing` names unset variables."""

    name: str
    ok: bool
    message: str
    missing: tuple[str, ...] = ()
    seconds: float = 0.0


def health_arguments(operation: Operation) -> dict[str, Any]:
    """The arguments of a health call: every default, and one item for a listing."""

    arguments = {param.name: param.default for param in operation.params}
    if operation.items is not None:
        arguments["limit"] = 1
    return arguments


def check_connector(
    name: str | ConnectorSpec,
    *,
    env: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
    transport: Transport | None = None,
) -> ConnectorCheck:
    """Check that a connector reaches its system: one small read (or a login).

    This contacts the system (unless `transport` answers instead).
    """

    started = time.monotonic()
    label = name.name if isinstance(name, ConnectorSpec) else split_reference(name)[0]
    try:
        ctx = connector_context(
            label if isinstance(name, str) else name, env=env, config=config, transport=transport
        )
    except ConfigurationError as exc:
        return ConnectorCheck(label, False, str(exc))
    missing = ctx.missing()
    if missing:
        return ConnectorCheck(label, False, f"not configured: set {', '.join(missing)}", missing)
    spec = ctx.spec
    try:
        if spec.check is not None:
            message = spec.check(ctx)
        elif spec.health is not None:
            operation = spec.operation(spec.health) if isinstance(spec.health, str) else spec.health
            call_operation(ctx, operation, health_arguments(operation))
            message = f"{spec.title} answered ({operation.name})"
        else:
            tools = build_tools(ctx)
            message = (
                f"{len(tools)} tool(s) ready; no health operation to call "
                "(set the health option to an operation name)"
            )
    except (ToolException, AwaError, OSError, ValueError) as exc:
        return ConnectorCheck(
            label, False, ctx.redact(str(exc)), seconds=time.monotonic() - started
        )
    if spec.notes:
        message += "; " + "; ".join(spec.notes)
    return ConnectorCheck(label, True, ctx.redact(message), seconds=time.monotonic() - started)


def config_problems(name: str, config: Mapping[str, Any] | None = None) -> list[str]:
    """Why the instance `name` with `config` cannot be built, checked offline (empty when fine)."""

    try:
        ctx = connector_context(name, env={}, config=config or {})
        if not ctx.spec.dynamic:
            ctx.operations()
    except AwaError as exc:
        return [str(exc)]
    return []
