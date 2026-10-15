"""MCP server tools as agent tools: server config, sessions, tool wrapping and tool pinning.

Defenses: pinning with drift checks, framed output, description and tool-count caps, https for
remote hosts, `${NAME}` expansion only where allowed, and secrets hidden from reprs, logs and errors.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import ipaddress
import json
import os
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from types import TracebackType
from typing import Annotated, Any, Literal, TextIO, TypeVar, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from awa._compat.mcp import MCPSession, connect, convert_tool, require_adapters, tool_input_schema
from awa._internal.logging import get_logger
from awa._internal.upstream.tools import BaseTool, InjectedToolArg, StructuredTool, ToolException
from awa.core.config import env_value
from awa.errors import ConfigurationError, MCPError, MCPToolDriftError
from awa.kernel.redaction import redact_values
from awa.tools.hardening import FRAMED_METADATA_KEY, cap_text, frame_tool_content

_RawTool = Any
"""A tool as the MCP SDK lists it."""

_SERVER_METADATA_KEYS = frozenset(
    {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint", "_meta"}
)
"""The tool metadata a server may set: the MCP annotations and `_meta`, never awa's own keys."""

_T = TypeVar("_T")

_log = get_logger("mcp")

_SERVER_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,47}$"
_MAX_TOOL_NAME_LENGTH = 64
"""The tool name length most model providers accept."""

MCP_LOCK_FILENAME = "mcp.lock.json"
"""Default name of the lock file `awa mcp lock` writes next to a blueprint."""

_LOCK_VERSION = 1

_ENV_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_ENV_REFERENCE = re.compile(rf"\$\{{({_ENV_NAME})(?::-([^}}]*))?\}}")
_INVALID_TOOL_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_GLOB_CHARS = frozenset("*?[")
_MIN_REDACTED_LENGTH = 3
_MAX_LIST_PAGES = 100


class MCPToolPin(BaseModel):
    """The pinned definition of one MCP tool.

    `digest` is `"sha256:<hex>"` over the tool's name, description, input schema and annotations
    (canonical JSON); `description` and `input_schema` are set when the lock records definitions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    description: str | None = None
    input_schema: dict[str, Any] | None = None


class MCPServerLock(BaseModel):
    """The pinned tools of one MCP server (by MCP tool name) and the name and version it reported."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server_name: str | None = None
    server_version: str | None = None
    tools: dict[str, MCPToolPin] = Field(default_factory=dict)

    def drift(self, current: MCPServerLock) -> MCPDrift:
        """Compare these pins with `current` (as `lock_mcp_tools` reports it); empty when unchanged."""

        return MCPDrift(
            changed=tuple(
                sorted(
                    name
                    for name, pin in current.tools.items()
                    if name in self.tools and self.tools[name].digest != pin.digest
                )
            ),
            added=tuple(sorted(set(current.tools) - set(self.tools))),
            removed=tuple(sorted(set(self.tools) - set(current.tools))),
        )


@dataclass(frozen=True)
class MCPDrift:
    """How a server's tools differ from their pins (MCP tool names).

    `added` holds selected tools without a pin; `removed` pinned tools the server no longer offers.
    """

    changed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.changed or self.added or self.removed)

    def describe(self) -> str:
        """Return a one-line summary, such as `"changed: search; added: delete_all"`."""

        parts = [
            f"{label}: {', '.join(names)}"
            for label, names in (
                ("changed", self.changed),
                ("added", self.added),
                ("removed", self.removed),
            )
            if names
        ]
        return "; ".join(parts) or "no changes"


class MCPLock(BaseModel):
    """Pinned MCP tool definitions of several servers: the content of `mcp.lock.json`.

    Create one with `lock_mcp_tools` (or `awa mcp lock`), commit it, and let
    servers verify against it with `MCPServer(lock="mcp.lock.json")` or
    `MCPServer(pin=lock.servers["crm"])`. `servers` is keyed by `MCPServer.name`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    lock_version: int = _LOCK_VERSION
    servers: dict[str, MCPServerLock] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known_version(self) -> MCPLock:
        if self.lock_version != _LOCK_VERSION:
            raise ValueError(
                f"unsupported lock_version {self.lock_version}; this version of awa reads "
                f"version {_LOCK_VERSION}"
            )
        return self

    @classmethod
    def read(cls, path: str | os.PathLike[str]) -> MCPLock:
        """Read a lock file.

        Raises:
            MCPError: If the file is missing, not JSON, or not a lock file.
        """

        file = Path(path)
        try:
            text = file.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise MCPError(
                f"MCP lock file '{file}' not found; create it with 'awa mcp lock'"
            ) from None
        except OSError as exc:
            raise MCPError(f"Cannot read MCP lock file '{file}': {exc.strerror or exc}") from None
        try:
            return cls.model_validate_json(text)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"]) or "$"
            raise MCPError(f"Invalid MCP lock file '{file}': {where}: {first['msg']}") from None

    def to_json(self) -> str:
        """Return the lock as stable, indented JSON (sorted keys, trailing newline)."""

        data = self.model_dump(mode="json", exclude_none=True)
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    def write(self, path: str | os.PathLike[str]) -> Path:
        """Write the lock to `path` and return the path."""

        file = Path(path)
        file.write_text(self.to_json(), encoding="utf-8")
        return file

    def merged(self, other: MCPLock) -> MCPLock:
        """Return this lock with the servers of `other` added or replaced."""

        return MCPLock(servers={**self.servers, **other.servers})


class MCPServer(BaseModel):
    """How to reach one MCP server, which of its tools to use, and how far to trust it.

    `transport` is inferred when omitted: `"stdio"` if `command` is set,
    `"http"` if `url` is set. Values may reference environment variables as
    `${NAME}` or `${NAME:-default}` (the default also replaces an empty
    value, as in the shell); they are expanded once, when a connection opens.
    Without `allow_env`, references are allowed only in `env` and `headers`
    values; `command`, `args`, `cwd` and `url` may reference only the
    variables listed in `allow_env`, which then also limits `env` and
    `headers`. A disallowed reference is a validation error, so a config
    cannot smuggle an unrelated secret into a URL or a command line.

    Attributes:
        name: Unique server name (a letter, then letters, digits, `_`, `-`); also the default
            tool name prefix and the `<server>` in blueprint references `mcp:<server>`.
        env: Extra variables of the stdio process, added to a safe default environment;
            hidden from `repr` like `headers`.
        url: Endpoint of an HTTP server; `https://` unless the host is local or
            `allow_insecure` is set.
        tool_prefix: `None` uses `"<name>_"`; `""` keeps the server's names.
        include: MCP tool names or glob patterns to use (empty: all); a name without
            wildcards must exist on the server.
        timeout: Seconds for any single request (connecting, listing, one tool call).
        pin: The expected tool definitions (from an `MCPLock`); not serialized; excludes `lock`.
        lock: Lock file holding this server's pins; relative to the working directory (in
            blueprints loaded from a file: to the blueprint's folder).
        on_drift: `"refuse"` raises `MCPToolDriftError`; `"warn"` logs and loads the tools.
        frame_output: Wrap tool results and server errors in a `<tool_output>` frame.
        max_description_chars: Longer descriptions are truncated and flagged
            (`metadata["mcp_description_truncated"]`); `None` disables the cap.
        max_tools: A server offering more selected tools is refused; `None` disables the cap.

    Example:
        ```python
        files = MCPServer(
            name="files",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "${DATA_DIR:-./data}"],
            allow_env=["DATA_DIR"],
            exclude=["write_*", "move_file"],
            lock="mcp.lock.json",
        )
        ```
    """

    # Validation errors must not echo the input: it can hold headers and env values.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    name: str = Field(pattern=_SERVER_NAME_PATTERN)
    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = Field(default=None, repr=False)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    tool_prefix: str | None = None
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    timeout: float = Field(default=30.0, gt=0)
    allow_env: list[str] | None = None
    allow_insecure: bool = False
    pin: MCPServerLock | None = Field(default=None, exclude=True, repr=False)
    lock: str | None = None
    on_drift: Literal["refuse", "warn"] = "refuse"
    frame_output: bool = True
    max_description_chars: int | None = Field(default=2000, ge=1)
    max_tools: int | None = Field(default=128, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _infer_transport(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and data.get("transport") is None:
            data = {key: value for key, value in data.items() if key != "transport"}
            if data.get("url") is not None and data.get("command") is None:
                data["transport"] = "http"
        return data

    @model_validator(mode="after")
    def _check_config(self) -> MCPServer:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("a stdio MCP server needs a 'command'")
            if self.url is not None or self.headers or self.allow_insecure:
                raise ValueError(
                    "'url', 'headers' and 'allow_insecure' only apply to the http transport"
                )
        else:
            if not self.url:
                raise ValueError(f"an {self.transport} MCP server needs a 'url'")
            if self.command is not None or self.args or self.env is not None or self.cwd:
                raise ValueError("'command', 'args', 'env' and 'cwd' only apply to stdio servers")
            if "${" not in self.url:
                problem = self._url_problem(self.url)
                if problem is not None:
                    raise ValueError(problem)
        for variable in self.allow_env or ():
            if not re.fullmatch(_ENV_NAME, variable):
                raise ValueError(f"allow_env: {variable!r} is not an environment variable name")
        problem = next(iter(self._env_problems()), None)
        if problem is not None:
            raise ValueError(problem)
        if self.pin is not None and self.lock is not None:
            raise ValueError("set either 'pin' or 'lock', not both")
        return self

    @property
    def prefix(self) -> str:
        """The effective tool name prefix (`tool_prefix`, or `"<name>_"`)."""

        return f"{self.name}_" if self.tool_prefix is None else self.tool_prefix

    def selects(self, tool_name: str) -> bool:
        """Return whether the MCP tool `tool_name` passes `include` and `exclude`."""

        if self.include and not any(fnmatchcase(tool_name, p) for p in self.include):
            return False
        return not any(fnmatchcase(tool_name, p) for p in self.exclude)

    def tool_name(self, mcp_name: str) -> str:
        """Return the agent-facing name of the MCP tool `mcp_name`."""

        name = _INVALID_TOOL_CHARS.sub("_", f"{self.prefix}{mcp_name}")
        return name[:_MAX_TOOL_NAME_LENGTH]

    def connection(self) -> dict[str, Any]:
        """Return the transport configuration with environment references expanded.

        The result contains secrets (headers, environment values); do not log it.

        Raises:
            MCPError: If a reference is unset without default or not allowed, or the URL is not.
        """

        return self._resolve()[0]

    def pinned(self) -> MCPServerLock | None:
        """Return the pins this server verifies against (`pin`, or its entry in `lock`).

        Raises:
            MCPError: If the lock file cannot be read or has no entry for this server.
        """

        if self.pin is not None or self.lock is None:
            return self.pin
        entry = MCPLock.read(self.lock).servers.get(self.name)
        if entry is None:
            raise MCPError(
                f"MCP lock file '{self.lock}' has no entry for server {self.name!r}; "
                "run 'awa mcp lock' to pin its tools"
            )
        return entry

    def _templated(self) -> list[tuple[str, str, bool]]:
        """Return `(label, value, is_secret_field)` for every value that may hold references."""

        values: list[tuple[str, str, bool]] = []
        if self.command is not None:
            values.append(("command", self.command, False))
        values.extend((f"args[{i}]", arg, False) for i, arg in enumerate(self.args))
        if self.cwd is not None:
            values.append(("cwd", self.cwd, False))
        if self.url is not None:
            values.append(("url", self.url, False))
        values.extend((f"env[{key!r}]", value, True) for key, value in (self.env or {}).items())
        values.extend((f"headers[{key!r}]", value, True) for key, value in self.headers.items())
        return values

    def _env_problems(self) -> list[str]:
        allowed = None if self.allow_env is None else set(self.allow_env)
        problems: list[str] = []
        for label, value, secret_field in self._templated():
            for match in _ENV_REFERENCE.finditer(value):
                variable = match.group(1)
                if allowed is None and not secret_field:
                    problems.append(
                        f"{label} references the environment variable {variable!r}; outside "
                        "'env' and 'headers' values, list the variables to expand in "
                        "'allow_env'"
                    )
                elif allowed is not None and variable not in allowed:
                    problems.append(
                        f"{label} references the environment variable {variable!r}, which is "
                        "not listed in 'allow_env'"
                    )
        return problems

    def _url_problem(self, url: str) -> str | None:
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError:
            return "'url' is not a valid URL"
        if parts.scheme not in ("http", "https") or not host:
            return "'url' must be an https:// URL (or http:// for a server on this machine)"
        if parts.scheme == "http" and not self.allow_insecure and not _is_local(host):
            return (
                f"'url' uses plain http to the remote host {host!r}; use https, or set "
                "allow_insecure=True if the connection is protected otherwise"
            )
        return None

    def _resolve(self) -> tuple[dict[str, Any], frozenset[str]]:
        """Return the connection config and the secret values to redact from messages."""

        problems = self._env_problems()
        if problems:
            raise MCPError(f"MCP server {self.name!r}: {problems[0]}")
        secrets: set[str] = set()
        expand = functools.partial(self._expand, secrets=secrets)
        session_kwargs = {"read_timeout_seconds": timedelta(seconds=self.timeout)}
        if self.transport == "stdio":
            assert self.command is not None
            env = None
            if self.env is not None:
                env = {key: expand(value, f"env[{key!r}]") for key, value in self.env.items()}
                secrets.update(env.values())
            connection: dict[str, Any] = {
                "transport": "stdio",
                "command": expand(self.command, "command"),
                "args": [expand(arg, f"args[{i}]") for i, arg in enumerate(self.args)],
                "env": env,
                "cwd": expand(self.cwd, "cwd") if self.cwd is not None else None,
                "session_kwargs": session_kwargs,
            }
        else:
            assert self.url is not None
            headers = {
                key: expand(value, f"headers[{key!r}]") for key, value in self.headers.items()
            }
            secrets.update(headers.values())
            url = expand(self.url, "url")
            problem = self._url_problem(url)
            if problem is not None:
                raise MCPError(f"MCP server {self.name!r}: {problem}")
            connection = {
                "transport": "streamable_http",
                "url": url,
                "headers": headers or None,
                "timeout": self.timeout,
                "session_kwargs": session_kwargs,
            }
        return connection, frozenset(s for s in secrets if len(s) >= _MIN_REDACTED_LENGTH)

    def _expand(self, value: str, what: str, *, secrets: set[str]) -> str:
        """Expand the `${NAME}` references of `value` in one pass and collect the values used."""

        def substitute(match: re.Match[str]) -> str:
            variable, default = match.group(1), match.group(2)
            resolved = env_value(variable)
            if default is not None and not resolved:
                resolved = default
            if resolved is None:
                raise MCPError(
                    f"MCP server {self.name!r}: {what} references the environment variable "
                    f"{variable!r}, which is not set; set it or add a default as ${{NAME:-value}}"
                )
            secrets.add(resolved)
            return resolved

        return _ENV_REFERENCE.sub(substitute, value)


ServerSpec = MCPServer | Mapping[str, Any]
"""An `MCPServer`, or its fields as a mapping (for example parsed from YAML)."""


async def load_mcp_tools(servers: ServerSpec | Sequence[ServerSpec]) -> list[BaseTool]:
    """Connect to MCP servers once, list their tools, and return them as agent tools.

    Every call of a returned tool opens its own short-lived session (for a
    stdio server: starts the process), so the tools stay usable for as long
    as they are referenced and need no cleanup. Prefer `MCPToolset` for stdio
    servers or chatty workloads. Pinned servers are verified while listing. Tools come server by
    server in the given order, sorted by MCP tool name within a server.

    Raises:
        OptionalDependencyError: If the `mcp` extra is not installed.
        MCPToolDriftError: If a pinned server's tools changed and `on_drift` is `"refuse"`.
        MCPError: If a server cannot be reached or listed, an `include` name does not exist,
            a server exceeds `max_tools`, or two tools end up with the same name.

    Example:
        ```python
        tools = await load_mcp_tools(
            {"name": "weather", "url": "https://weather.example.com/mcp"}
        )
        forecaster = Agent("forecaster", tools=tools)
        ```
    """

    specs = _normalize(servers)
    require_adapters()

    results = await _gather_all(
        [_in_new_session(spec, functools.partial(_server_tools, spec)) for spec in specs]
    )
    return _finalize(list(zip(specs, results, strict=True)))


async def lock_mcp_tools(
    servers: ServerSpec | Sequence[ServerSpec], *, definitions: bool = False
) -> MCPLock:
    """Connect to MCP servers and pin the definitions of their selected tools.

    Existing pins are not checked, so this is also how a reviewed change is
    accepted. The selection (`include`, `exclude`, `max_tools`) is applied
    first: only tools an agent can get are pinned. Save the lock with `MCPLock.write`.

    Args:
        servers: One server or several, as `MCPServer`s or mappings of its fields.
        definitions: Also record each tool's description and input schema, so a change shows
            up in the lock file's diff.

    Raises:
        OptionalDependencyError: If the `mcp` extra is not installed.
        MCPError: If a server cannot be reached or listed, an `include` name does not exist,
            or a server exceeds `max_tools`.
    """

    specs = _normalize(servers)
    require_adapters()

    async def lock(spec: MCPServer) -> MCPServerLock:
        listing = await _in_new_session(spec, functools.partial(_list_server, spec))
        return listing.lock(definitions=definitions)

    results = await _gather_all([lock(spec) for spec in specs])
    return MCPLock(servers={spec.name: result for spec, result in zip(specs, results, strict=True)})


class MCPToolset:
    """Keeps one session per MCP server open and provides their tools.

    Sessions open, one server after the other, when the `async with` block
    is entered and close when it exits, also on errors and cancellation;
    stdio server processes are stopped. The tools share the sessions, so
    calls are fast. A tool called after the block exited returns an error
    result instead of reconnecting. Enter and exit the block in the same task.

    Example:
        ```python
        async with MCPToolset(MCPServer(name="crm", command="crm-mcp")) as tools:
            result = await Agent("sales", tools=tools).run("Summarize ACME's open deals.")
        ```
    """

    def __init__(self, servers: ServerSpec | Sequence[ServerSpec]) -> None:
        self._servers = _normalize(servers)
        self._stack: AsyncExitStack | None = None
        self._tools: list[BaseTool] | None = None
        self._state = _OpenState()

    @property
    def servers(self) -> tuple[MCPServer, ...]:
        """The servers this toolset connects to."""

        return self._servers

    @property
    def is_open(self) -> bool:
        """Whether the sessions are open."""

        return self._stack is not None

    @property
    def tools(self) -> list[BaseTool]:
        """The tools of every server (only while the toolset is open).

        Raises:
            MCPError: If the toolset is not open.
        """

        if self._tools is None or self._stack is None:
            raise MCPError("The MCP toolset is not open; use 'async with MCPToolset(...)'")
        return list(self._tools)

    async def __aenter__(self) -> list[BaseTool]:
        """Open a session to every server and return their tools.

        Raises:
            OptionalDependencyError: If the `mcp` extra is not installed.
            MCPToolDriftError: If a pinned server's tools changed and `on_drift` is `"refuse"`.
            MCPError: If the toolset is already open or loading fails as in `load_mcp_tools`.
        """

        if self._stack is not None:
            raise MCPError("The MCP toolset is already open")
        require_adapters()
        stack = AsyncExitStack()
        state = _OpenState()
        try:
            listed: list[tuple[MCPServer, list[BaseTool]]] = []
            for spec in self._servers:
                session, server_info = await stack.enter_async_context(_open_session(spec))
                listed.append((spec, await _server_tools(spec, session, server_info, state)))
            all_tools = _finalize(listed)
        except BaseException:
            await stack.aclose()
            raise
        state.open = True
        self._stack, self._tools, self._state = stack, all_tools, state
        _log.debug("Opened %d MCP session(s) with %d tool(s)", len(self._servers), len(all_tools))
        return list(all_tools)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close every session (stopping stdio server processes)."""

        stack, self._stack, self._tools = self._stack, None, None
        self._state.open = False
        if stack is not None:
            await stack.aclose()
            _log.debug("Closed %d MCP session(s)", len(self._servers))

    def __repr__(self) -> str:
        names = ", ".join(spec.name for spec in self._servers)
        return f"MCPToolset(servers=[{names}], open={self.is_open})"


class _OpenState:
    """Whether the sessions behind a toolset's tools are still open."""

    __slots__ = ("open",)

    def __init__(self) -> None:
        self.open = False


@dataclass(frozen=True)
class _Listing:
    """The selected tools of one server, sorted by MCP name, and what it reported."""

    server_name: str | None
    server_version: str | None
    tools: tuple[_RawTool, ...]

    def lock(self, *, definitions: bool = False) -> MCPServerLock:
        return MCPServerLock(
            server_name=self.server_name,
            server_version=self.server_version,
            tools={
                tool.name: MCPToolPin(
                    digest=_digest(tool),
                    description=(tool.description or "") if definitions else None,
                    input_schema=tool_input_schema(tool) if definitions else None,
                )
                for tool in self.tools
            },
        )


def _digest(tool: _RawTool) -> str:
    definition: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool_input_schema(tool),
    }
    annotations = getattr(tool, "annotations", None)
    if annotations is not None:
        dumped = annotations.model_dump(mode="json", exclude_none=True)
        if dumped:
            definition["annotations"] = dumped
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_local(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _normalize(servers: ServerSpec | Sequence[ServerSpec]) -> tuple[MCPServer, ...]:
    items: Sequence[ServerSpec] = (
        [servers] if isinstance(servers, MCPServer | Mapping) else list(servers)
    )
    specs = tuple(
        item if isinstance(item, MCPServer) else MCPServer.model_validate(dict(item))
        for item in items
    )
    names = [spec.name for spec in specs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigurationError(f"MCP server names must be unique: {', '.join(duplicates)}")
    return specs


def _server_errlog() -> TextIO | None:
    """Return a stderr with a real file descriptor for a stdio server, or `None` to inherit."""

    for stream in (sys.stderr, sys.__stderr__):
        try:
            if stream is not None:
                stream.fileno()
                return stream
        except (AttributeError, OSError, ValueError):
            continue
    return None


@asynccontextmanager
async def _open_session(spec: MCPServer) -> AsyncIterator[tuple[MCPSession, Any]]:
    """Open and initialize a session; yield it with the server's `Implementation` info."""

    connection, secrets = spec._resolve()
    _log.debug("Connecting to MCP server %r over %s", spec.name, spec.transport)
    in_body = False
    try:
        async with connect(connection, errlog=_server_errlog()) as session:
            initialized = await session.initialize()
            in_body = True
            yield session, getattr(initialized, "serverInfo", None)
            in_body = False
    except Exception as exc:
        if in_body:
            raise  # raised by the code using the session, not by the connection
        raise MCPError(
            f"MCP session with server {spec.name!r} failed: {_describe(exc, secrets)}"
        ) from None


async def _in_new_session(spec: MCPServer, work: Callable[[MCPSession, Any], Awaitable[_T]]) -> _T:
    """Run `work(session, server_info)` in a short-lived session of `spec`."""

    result: Any = None
    error: Exception | None = None
    async with _open_session(spec) as (session, server_info):
        try:
            result = await work(session, server_info)
        except Exception as exc:  # re-raised outside, where the session is closed
            error = exc
    if error is not None:
        raise error
    return cast(_T, result)


async def _gather_all(calls: Sequence[Awaitable[_T]]) -> list[_T]:
    """Await all `calls` concurrently, then raise the first error.

    Every call runs to completion, so no session is left half-open.
    """

    results = await asyncio.gather(*calls, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return cast(list[_T], results)


async def _list_server(spec: MCPServer, session: MCPSession, server_info: Any) -> _Listing:
    """List, select and cap the tools of `spec` over an open session."""

    _, secrets = spec._resolve()
    tools: list[_RawTool] = []
    try:
        page = await session.list_tools()
        tools.extend(page.tools)
        pages = 1
        while page.nextCursor:
            pages += 1
            if pages > _MAX_LIST_PAGES:
                raise MCPError(f"the server returned more than {_MAX_LIST_PAGES} pages of tools")
            page = await session.list_tools(cursor=page.nextCursor)
            tools.extend(page.tools)
    except Exception as exc:
        raise MCPError(
            f"Cannot list the tools of MCP server {spec.name!r}: {_describe(exc, secrets)}"
        ) from None
    names = {tool.name for tool in tools}
    for pattern in spec.include:
        if not _GLOB_CHARS.intersection(pattern) and pattern not in names:
            available = ", ".join(sorted(names)) or "none"
            raise MCPError(
                f"MCP server {spec.name!r} has no tool {pattern!r} (available: {available})"
            )
    selected = sorted((tool for tool in tools if spec.selects(tool.name)), key=lambda t: t.name)
    if spec.max_tools is not None and len(selected) > spec.max_tools:
        raise MCPError(
            f"MCP server {spec.name!r} offers {len(selected)} tools, more than max_tools="
            f"{spec.max_tools}; select the tools agents need with include/exclude, or raise "
            "max_tools"
        )
    return _Listing(
        server_name=getattr(server_info, "name", None),
        server_version=getattr(server_info, "version", None),
        tools=tuple(selected),
    )


async def _server_tools(
    spec: MCPServer, session: MCPSession, server_info: Any, state: _OpenState | None = None
) -> list[BaseTool]:
    """List, verify and wrap the tools of `spec`; with `state` the tools share `session`."""

    _, secrets = spec._resolve()
    listing = await _list_server(spec, session, server_info)
    _verify(spec, listing)
    shared = session if state is not None else None
    return [await _wrap(spec, tool, secrets, state=state, session=shared) for tool in listing.tools]


def _verify(spec: MCPServer, listing: _Listing) -> None:
    """Compare the listed tools with the server's pins; refuse or warn on drift."""

    pin = spec.pinned()
    if pin is None:
        return
    drift = pin.drift(listing.lock())
    version = ""
    if pin.server_version is not None and pin.server_version != listing.server_version:
        version = (
            f" (server version {pin.server_version} when pinned, "
            f"{listing.server_version or 'unknown'} now)"
        )
    if not drift:
        if version:
            _log.info("MCP server %r matches its pins%s", spec.name, version)
        return
    message = (
        f"The tools of MCP server {spec.name!r} changed since they were pinned{version}: "
        f"{drift.describe()}. Review the change, then run 'awa mcp lock' to accept it"
    )
    if spec.on_drift == "warn":
        _log.warning("%s", message)
        return
    raise MCPToolDriftError(
        message, server=spec.name, changed=drift.changed, added=drift.added, removed=drift.removed
    )


def _finalize(listed: Sequence[tuple[MCPServer, list[BaseTool]]]) -> list[BaseTool]:
    """Check that tool names are unique across servers."""

    tools: list[BaseTool] = []
    owners: dict[str, str] = {}
    for spec, server_tools in listed:
        for tool in server_tools:
            if tool.name in owners:
                raise MCPError(
                    f"Two MCP tools are named {tool.name!r} (servers {owners[tool.name]!r} and "
                    f"{spec.name!r}); give the servers distinct tool_prefix values or exclude one"
                )
            owners[tool.name] = spec.name
            tools.append(tool)
        _log.debug("MCP server %r provides %d tool(s)", spec.name, len(server_tools))
    return tools


async def _wrap(
    spec: MCPServer,
    raw: _RawTool,
    secrets: frozenset[str],
    *,
    state: _OpenState | None,
    session: MCPSession | None,
) -> BaseTool:
    """Convert a listed tool; rename it, cap its description, frame its output and
    turn transport failures into tool errors the model sees.

    Of the server's metadata only the MCP annotations and `_meta` are kept, so a server
    cannot set awa's own keys (risk, tool spec, output limit, framing)."""

    async def call_in_new_session(name: str, arguments: dict[str, Any]) -> Any:
        return await _in_new_session(spec, lambda session, _: session.call_tool(name, arguments))

    converted = await convert_tool(
        raw, session=session, call_in_new_session=call_in_new_session, server_name=spec.name
    )
    inner = converted.call
    mcp_name = raw.name
    name = spec.tool_name(mcp_name)
    source = f"mcp:{spec.name}"
    frame = spec.frame_output
    metadata: dict[str, Any] = {
        **{key: value for key, value in converted.metadata.items() if key in _SERVER_METADATA_KEYS},
        "mcp_server": spec.name,
        "mcp_tool": mcp_name,
        FRAMED_METADATA_KEY: frame,
    }
    description = _capped_description(spec, raw, metadata)

    async def call_mcp_tool(
        runtime: Annotated[object | None, InjectedToolArg()] = None, **arguments: Any
    ) -> Any:
        if state is not None and not state.open:
            raise ToolException(
                f"The MCP session with server {spec.name!r} is closed; the tool "
                f"{mcp_name!r} is no longer available"
            )
        try:
            content, artifact = await inner(runtime, arguments)
        except ToolException as exc:
            text = redact_values(str(exc), secrets, min_len=_MIN_REDACTED_LENGTH)
            raise ToolException(
                frame_tool_content(text, tool=name, source=source) if frame else text
            ) from None
        except Exception as exc:
            raise ToolException(
                f"MCP server {spec.name!r} failed to run {mcp_name!r}: {_describe(exc, secrets)}"
            ) from None
        if frame:
            content = frame_tool_content(content, tool=name, source=source)
        return content, artifact

    return StructuredTool(
        name=name,
        description=description,
        args_schema=converted.args_schema,
        coroutine=call_mcp_tool,
        response_format="content_and_artifact",
        metadata=metadata,
        handle_tool_error=True,
    )


def _capped_description(spec: MCPServer, raw: _RawTool, metadata: dict[str, Any]) -> str:
    """Return the tool description, truncated and flagged in `metadata` above the server's cap."""

    description: str = raw.description or f"The {raw.name!r} tool of the {spec.name!r} MCP server."
    limit = spec.max_description_chars
    if limit is None or len(description) <= limit:
        return description
    _log.warning(
        "MCP server %r: the description of tool %r has %d characters; truncated to %d "
        "(max_description_chars)",
        spec.name,
        raw.name,
        len(description),
        limit,
    )
    metadata["mcp_description_truncated"] = True
    return cap_text(description, limit)


def _describe(exc: BaseException, secrets: frozenset[str]) -> str:
    """Describe the innermost cause of `exc` (unwrapping exception groups), with secrets redacted."""

    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc) or "no details"
    return redact_values(f"{type(exc).__name__}: {text}", secrets, min_len=_MIN_REDACTED_LENGTH)
