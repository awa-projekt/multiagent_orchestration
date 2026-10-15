"""`discover` and its report: which model providers and connectors are configured, by variable names only."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from awa.connectors._catalog import connector_context, list_connectors
from awa.core.dotenv import Environment
from awa.core.providers import PROVIDERS


@dataclass(frozen=True)
class ProviderStatus:
    """A model provider and whether its credentials are set.

    Attributes:
        name: Provider name, e.g. `"openai"`.
        configured: Whether every variable it needs is set.
        missing: Names of the variables that are not set.
    """

    name: str
    configured: bool
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConnectorStatus:
    """A catalog connector, whether it is configured, and the tools it offers.

    Attributes:
        tools: Tool references, `<name>.<operation>`; empty when the system lists them (an
            OpenAPI document).
        approval: The references among `tools` that need a person's approval before they run.
        partial: Some of its variables are set, but not all it needs.
        env: Names of every variable it may read (required or optional).
    """

    name: str
    title: str
    category: str
    configured: bool
    missing: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    approval: tuple[str, ...] = ()
    partial: bool = False
    env: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnvironmentReport:
    """Providers and connectors found in the environment (names only, never values).

    Attributes:
        providers: The model providers and whether each is configured.
        connectors: The catalog connectors and whether each is configured.
    """

    providers: tuple[ProviderStatus, ...] = ()
    connectors: tuple[ConnectorStatus, ...] = ()

    def available(self) -> tuple[ConnectorStatus, ...]:
        """The connectors whose required variables are all set."""
        return tuple(connector for connector in self.connectors if connector.configured)

    def brief(self) -> str:
        """A compact listing of the reachable systems and their tools, for the architect.

        Tools marked `[approval]` wait for a human decision; reference a tool
        as `connector:<name>.<operation>`.
        """
        lines = [
            "Model providers: "
            + (", ".join(p.name for p in self.providers if p.configured) or "none")
        ]
        available = self.available()
        if not available:
            lines.append("Connected systems: none")
        else:
            lines.append("Connected systems (reference tools as connector:<tool>):")
        for connector in available:
            tools = ", ".join(
                f"{tool} [approval]" if tool in connector.approval else tool
                for tool in connector.tools
            )
            lines.append(
                f"- {connector.name} ({connector.title}): "
                + (tools or f"tools listed by the system (connector:{connector.name})")
            )
        unconfigured = [c.name for c in self.connectors if not c.configured]
        if unconfigured:
            lines.append("Available but not configured: " + ", ".join(unconfigured))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """The report as JSON-compatible data (names only)."""
        return asdict(self)


def discover(
    env: Mapping[str, str] | None = None, *, dotenv: str | Path | None = ".env"
) -> EnvironmentReport:
    """Report which model providers and catalog connectors are configured, with missing variables.

    Reads variable names and whether they are set from `env` (default: the process environment)
    and the `dotenv` file (read, not exported; `None` for none). The report never contains a value.

    Example:
        ```python
        report = discover()
        print(report.brief())
        ```
    """

    environment = Environment(env, dotenv=dotenv)
    providers = tuple(
        ProviderStatus(
            spec.name, configured=not (missing := spec.missing(environment.get)), missing=missing
        )
        for spec in PROVIDERS
    )
    connectors = []
    for spec in list_connectors():
        ctx = connector_context(spec.name, env={})
        ctx.env = environment
        missing = ctx.missing()
        connectors.append(
            ConnectorStatus(
                name=spec.name,
                title=spec.title,
                category=spec.category,
                configured=not missing,
                missing=missing,
                tools=ctx.spec.references(),
                approval=tuple(f"{spec.name}.{op.name}" for op in ctx.spec.writes()),
                partial=bool(missing)
                and any(environment.is_set(v.name) for v in ctx.spec.variables()),
                env=tuple(v.name for v in ctx.spec.variables()),
            )
        )
    return EnvironmentReport(providers=providers, connectors=tuple(connectors))
