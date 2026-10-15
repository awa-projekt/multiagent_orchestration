"""Extension: serve awa systems as an MCP server, so other AI clients can call company processes.

Each system `<name>` becomes the tools `run_<name>` (an `input`, optionally a `thread_id`) and
`resume_<name>` (a `thread_id` and a `decision`); pending approvals are elicited from the client's
user when it supports elicitation. `serve_as_mcp` runs over stdio or streamable HTTP, `awa mcp
serve blueprint.yaml` does the same for a blueprint. Needs the `mcp` extra; see the MCP guide.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from awa._internal.logging import get_logger
from awa._internal.upstream.tools import BaseTool
from awa.contracts.usage import Usage
from awa.errors import (
    AwaError,
    ConfigurationError,
    InvalidRequestError,
    PermissionDeniedError,
    ThreadNotFoundError,
)
from awa.persistence.threads import BaseThreadIndex, ThreadOwner
from awa.server._auth import (
    ANONYMOUS,
    LOOPBACK_HOSTS,
    ApiKeyAuthenticator,
    Authenticator,
    Principal,
)
from awa.server._config import (
    THREAD_ID_PATTERN,
    ServedSystem,
    SystemsArg,
    normalise_systems,
    stamp_reviewer,
    system_checkpointer,
    system_kind,
)
from awa.server._serialize import pending_interrupts, usage_json
from awa.server._stores import thread_index_for
from awa.server.errors import describe_error
from awa.server.jobs import JobRegistry, JobResult, JobSpec, execute

_log = get_logger("server.mcp")

Transport = Literal["stdio", "http"]
_MAX_APPROVAL_ROUNDS = 20
_SUMMARY_KEYS = ("status", "text", "output", "thread_id", "interrupts", "run_id")
_PRINCIPAL_SCOPE_KEY = "awa.principal"
_NO_TENANT_PREFIX = "t0000000000000000."
_MAX_THREAD_ID_CHARS = 128
_FOREIGN_SESSION = "This MCP session was opened by another caller; open a session of your own"
_MAX_SESSIONS = 100_000


class ApprovalAnswer(BaseModel):
    """What the client's user answers when a run asks for approval (elicitation)."""

    approve: bool = Field(description="Approve the action (false rejects it)")
    note: str = Field(default="", description="Optional note for the record")


class _RunArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str | dict[str, Any] | list[dict[str, Any]]
    thread_id: str | None = Field(default=None, pattern=THREAD_ID_PATTERN)


class _ResumeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(pattern=THREAD_ID_PATTERN)
    decision: dict[str, Any]


def create_mcp_server(
    systems: SystemsArg,
    *,
    name: str | None = None,
    instructions: str | None = None,
    tools: Sequence[BaseTool] = (),
    elicit_approvals: bool = True,
    host: str = "127.0.0.1",
    port: int = 8000,
    path: str = "/mcp",
    thread_index: BaseThreadIndex | None = None,
) -> Any:
    """Build an MCP server (FastMCP) whose tools run awa systems.

    Args:
        systems: One system, a list, or a mapping of name to system; wrap one in `ServedSystem`
            for server-side deps, a usage ceiling or a `run_timeout`. Clients never send deps.
        tools: Tools to expose directly as well; their calls bypass the systems' approvals.
        thread_index: Where every thread's owner is recorded; pass the HTTP server's index when
            both serve the same checkpointer, so a thread keeps one owner on both.

    Raises:
        OptionalDependencyError: If the `mcp` extra is not installed.
        ConfigurationError: If a system keeps its threads in a durable checkpointer and no
            `thread_index` is given (`InMemoryThreadIndex()` accepts losing them on restart).

    Example:
        ```python
        server = create_mcp_server({"invoices": invoice_team}, tools=[lookup_invoice])
        server.run("stdio")
        ```
    """

    from awa._compat.mcp_server import load_server_sdk

    sdk = load_server_sdk()
    served = normalise_systems(systems)
    index = thread_index_for(
        (system_checkpointer(entry.system) for entry in served.values()),
        thread_index,
        several_callers=True,
        where="The MCP server",
    )
    registry = JobRegistry(dict(served), thread_index=index)
    built: list[Any] = []
    for served_name, entry in served.items():
        threads = _Threads(index, served_name, entry)
        built.append(_run_tool(sdk, registry, threads, elicit_approvals))
        if _resumable(entry):
            built.append(_resume_tool(sdk, registry, threads))
    built.extend(_direct_tool(sdk, tool) for tool in tools)
    names = [tool.name for tool in built]
    duplicates = sorted({tool_name for tool_name in names if names.count(tool_name) > 1})
    if duplicates:
        raise ValueError(f"Two MCP tools are named {', '.join(duplicates)}")
    return sdk.FastMCP(
        name or next(iter(served)),
        instructions=instructions,
        tools=built,
        host=host,
        port=port,
        streamable_http_path=path,
    )


def serve_as_mcp(
    systems: SystemsArg,
    *,
    transport: Transport = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
    path: str = "/mcp",
    api_key: str | None = None,
    authenticator: Authenticator | None = None,
    name: str | None = None,
    instructions: str | None = None,
    tools: Sequence[BaseTool] = (),
    elicit_approvals: bool = True,
    thread_index: BaseThreadIndex | None = None,
    log_level: str = "info",
    allow_unauthenticated: bool = False,
) -> None:
    """Serve systems as MCP tools until interrupted; see `create_mcp_server`.

    Over stdio nothing but the protocol goes to stdout. Over HTTP (`transport="http"`, local
    connections only by default) set `api_key` or an `authenticator` so only your clients can
    call it.

    Args:
        thread_index: Where every thread's owner is recorded; needed when a system keeps its
            threads in a durable checkpointer (see `create_mcp_server`).
        allow_unauthenticated: Serve HTTP on a non-loopback `host` without `api_key` or
            `authenticator`, so anyone who can reach the port can run the systems.

    Raises:
        ConfigurationError: If a system sets `approver_roles` but the callers are unknown (stdio)
            or have no roles (`api_key`): pass an `authenticator` whose principals carry them.
            Also if HTTP is served on a non-loopback `host` without authentication and without
            `allow_unauthenticated`.

    Example:
        ```python
        serve_as_mcp({"support": support_team}, transport="http", api_key=os.environ["MCP_KEY"])
        ```
    """

    if transport not in ("stdio", "http"):
        raise ValueError("transport must be 'stdio' or 'http'")
    if api_key is not None and authenticator is not None:
        raise ValueError("Pass either api_key or authenticator, not both")
    _require_callers_for_approver_roles(systems, transport, authenticator)
    open_host = transport == "http" and host not in LOOPBACK_HOSTS
    if open_host and api_key is None and authenticator is None and not allow_unauthenticated:
        raise ConfigurationError(
            f"Serving MCP on {host} without authentication would let anyone who can reach the "
            "port run the systems; pass api_key or an authenticator, or allow_unauthenticated=True"
        )
    server = create_mcp_server(
        systems,
        name=name,
        instructions=instructions,
        tools=tools,
        elicit_approvals=elicit_approvals,
        thread_index=thread_index,
        host=host,
        port=port,
        path=path,
    )
    if transport == "stdio":
        server.run("stdio")
        return
    import uvicorn

    if api_key is not None:
        authenticator = ApiKeyAuthenticator({api_key: Principal("mcp-client")})
    if authenticator is None and host not in LOOPBACK_HOSTS:
        _log.warning(
            "Serving MCP on %s without authentication: anyone who can reach the port can run the "
            "systems. Pass api_key or an authenticator.",
            host,
        )
    app = server.streamable_http_app()
    if authenticator is not None:
        app = BearerGate(app, authenticator)
    uvicorn.run(app, host=host, port=port, log_level=log_level)


class BearerGate:
    """ASGI middleware refusing HTTP requests its `Authenticator` rejects (401).

    Mount `create_mcp_server(...).streamable_http_app()` behind it in your own ASGI server: it
    hands each request's `Principal` to the tools (approver roles, tenant scoping). An MCP
    session belongs to the principal that opened it; another principal naming it gets 403.
    """

    def __init__(self, app: Any, authenticator: Authenticator) -> None:
        self._app = app
        self._authenticator = authenticator
        self._sessions: OrderedDict[str, tuple[str, str | None]] = OrderedDict()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            from starlette.requests import Request
            from starlette.responses import JSONResponse

            try:
                principal = await self._authenticator.authenticate(Request(scope, receive))
            except AwaError as exc:
                info = describe_error(exc)
                response = JSONResponse(
                    {"error": info.body()}, status_code=info.status, headers=info.headers()
                )
                await response(scope, receive, send)
                return
            identity = (principal.id, principal.tenant)
            session = _header(scope, b"mcp-session-id")
            if session is not None and self._sessions.get(session, identity) != identity:
                refused = JSONResponse(
                    {"error": {"type": "PermissionDenied", "message": _FOREIGN_SESSION}},
                    status_code=403,
                )
                await refused(scope, receive, send)
                return
            # A copy of the scope, so the tools of this request see exactly this caller.
            scope = {**scope, _PRINCIPAL_SCOPE_KEY: principal}
            send = self._binding(send, identity)
        await self._app(scope, receive, send)

    def _binding(self, send: Any, identity: tuple[str, str | None]) -> Any:
        """`send` that records the principal of every session a response opens."""

        async def binding_send(message: Any) -> None:
            if message.get("type") == "http.response.start":
                for name, value in message.get("headers") or ():
                    if bytes(name).lower() == b"mcp-session-id":
                        session = bytes(value).decode("latin-1")
                        self._sessions.setdefault(session, identity)
                        self._sessions.move_to_end(session)
                        while len(self._sessions) > _MAX_SESSIONS:
                            self._sessions.popitem(last=False)
            await send(message)

        return binding_send


def _header(scope: Mapping[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if bytes(key).lower() == name:
            return bytes(value).decode("latin-1")
    return None


@cache
def _tool_class() -> Any:
    from awa._compat.mcp_server import load_server_sdk

    sdk = load_server_sdk()

    class AwaTool(sdk.Tool):  # type: ignore[misc, name-defined]  # the SDK is loaded lazily
        """A FastMCP tool with an exact JSON schema, run by an awa callable."""

        async def run(
            self, arguments: dict[str, Any], context: Any = None, convert_result: bool = False
        ) -> Any:
            try:
                return await self.fn(arguments, context)
            except sdk.ToolError:
                raise
            except ValidationError as exc:
                raise sdk.ToolError(f"Invalid arguments for {self.name}: {exc}") from None
            except Exception as exc:  # noqa: BLE001 - reported to the client, details hidden
                info = describe_error(exc)
                if info.internal:
                    _log.error("MCP tool %s failed", self.name, exc_info=exc)
                raise sdk.ToolError(f"{info.type}: {info.message}") from None

    return AwaTool


@cache
def _no_output_metadata() -> Any:
    from awa._compat.mcp_server import load_server_sdk

    def _stub() -> None:
        return None

    return load_server_sdk().func_metadata(_stub, structured_output=False)


def _make_tool(
    sdk: Any,
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    fn: Any,
    title: str | None = None,
    read_only: bool | None = None,
) -> Any:
    return _tool_class()(
        fn=fn,
        name=name,
        title=title,
        description=description,
        parameters=parameters,
        fn_metadata=_no_output_metadata(),
        is_async=True,
        context_kwarg=None,
        annotations=sdk.ToolAnnotations(title=title, readOnlyHint=read_only, openWorldHint=True),
    )


def _keeps_threads(entry: ServedSystem) -> bool:
    """Whether clients may continue the system's threads (agents and teams with a checkpointer)."""

    return (
        system_kind(entry.system) in ("agent", "team")
        and system_checkpointer(entry.system) is not None
    )


def _resumable(entry: ServedSystem) -> bool:
    """Whether paused runs can be answered: the system resumes, and keeps its threads in a
    checkpointer the server can read (to check the thread's owner and its pending decision)."""

    return (
        callable(getattr(entry.system, "resume", None))
        and system_checkpointer(entry.system) is not None
    )


def _run_tool(sdk: Any, registry: JobRegistry, threads: _Threads, elicit: bool) -> Any:
    name, entry = threads.name, threads.entry
    card = entry.system.card
    schema = card.input_schema
    if not (isinstance(schema, Mapping) and schema.get("type") == "object"):
        schema = {"type": "string", "description": "The request, in plain language"}
    properties: dict[str, Any] = {"input": schema}
    if _keeps_threads(entry):
        properties["thread_id"] = {
            "type": "string",
            "pattern": THREAD_ID_PATTERN,
            "description": "Continue this conversation thread (from an earlier result)",
        }
    parameters = {"type": "object", "properties": properties, "required": ["input"]}

    async def run(arguments: dict[str, Any], context: Any) -> Any:
        parsed = _RunArguments.model_validate(arguments)
        if parsed.thread_id is not None and not _keeps_threads(entry):
            raise InvalidRequestError(f"System {name!r} keeps no threads")
        value = parsed.input
        caller = _caller(context)
        scope = _ThreadScope(caller)
        thread_id = await threads.open(scope, parsed.thread_id)
        spec = JobSpec(
            system=name,
            input=json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value,
            thread_id=thread_id,
            metadata=_metadata(context),
            **_caller_fields(caller),
        )
        result = await _execute(spec, registry)
        if elicit and result.status == "interrupted" and _may_approve(entry, caller):
            result = await _ask_for_approvals(threads, registry, result, context, caller)
        return _tool_result(sdk, result, scope)

    description = (card.description or f"Run the {name} system.").strip()
    description += (
        " Returns JSON with status, text, output and thread_id; a status of "
        "'interrupted' lists the approvals the run waits for"
        + (f" (answer them with resume_{name})." if _resumable(entry) else ".")
    )
    return _make_tool(
        sdk,
        name=f"run_{name}",
        title=f"Run {name}",
        description=description,
        parameters=parameters,
        fn=run,
        read_only=False,
    )


def _resume_tool(sdk: Any, registry: JobRegistry, threads: _Threads) -> Any:
    name, entry = threads.name, threads.entry
    parameters = {
        "type": "object",
        "properties": {
            "thread_id": {
                "type": "string",
                "pattern": THREAD_ID_PATTERN,
                "description": "The thread of the paused run",
            },
            "decision": {
                "type": "object",
                "description": (
                    'The decision: {"action": "approve"}, {"action": "reject", "note": "..."}, '
                    'or {"action": "edit", "args": {...}}; with several pending approvals, '
                    "a mapping of interrupt id to decision"
                ),
            },
        },
        "required": ["thread_id", "decision"],
    }

    async def resume(arguments: dict[str, Any], context: Any) -> Any:
        parsed = _ResumeArguments.model_validate(arguments)
        caller = _caller(context)
        if not _may_approve(entry, caller):
            raise PermissionDeniedError(
                f"Answering approvals of system {name!r} needs one of the roles "
                f"{', '.join(sorted(entry.approver_roles or ()))}; "
                + (
                    "the caller is not known (serve it over HTTP with an authenticator)"
                    if caller is None
                    else "the caller has none of them"
                )
            )
        scope = _ThreadScope(caller)
        thread_id, owner = await threads.existing(scope, parsed.thread_id)
        spec = JobSpec(
            system=name,
            thread_id=thread_id,
            decision=await threads.stamped(thread_id, parsed.decision, scope.caller),
            metadata=_metadata(context),
            owner=owner.principal,
            **_caller_fields(scope.caller),
        )
        return _tool_result(sdk, await _execute(spec, registry), scope)

    return _make_tool(
        sdk,
        name=f"resume_{name}",
        title=f"Resume {name}",
        description=f"Answer the approvals a paused run of {name} waits for, and continue it.",
        parameters=parameters,
        fn=resume,
        read_only=False,
    )


def _direct_tool(sdk: Any, tool: BaseTool) -> Any:
    schema: Any = tool.tool_call_schema
    parameters = schema if isinstance(schema, dict) else schema.model_json_schema()
    parameters = {key: value for key, value in parameters.items() if key != "title"}
    parameters.setdefault("type", "object")
    parameters.setdefault("properties", {})

    async def call(arguments: dict[str, Any], context: Any) -> Any:
        try:
            output = await tool.ainvoke(arguments)
        except (ValueError, ValidationError) as exc:
            raise sdk.ToolError(f"{type(exc).__name__}: {exc}") from None
        text = output if isinstance(output, str) else json.dumps(output, default=str)
        return sdk.CallToolResult(content=[sdk.TextContent(type="text", text=text)])

    return _make_tool(
        sdk,
        name=tool.name,
        description=tool.description or tool.name,
        parameters=parameters,
        fn=call,
    )


async def _execute(spec: JobSpec, registry: JobRegistry) -> JobResult:
    from awa._compat.mcp_server import load_server_sdk

    result = await execute(spec, registry)
    if not result.ok:
        error = result.error or {}
        message = f"{error.get('type', 'Error')}: {error.get('message', '')}"
        raise load_server_sdk().ToolError(message)
    return result


def _tool_result(sdk: Any, result: JobResult, scope: _ThreadScope) -> Any:
    body = scope.public(result.result or {})
    summary = {key: body.get(key) for key in _SUMMARY_KEYS}
    return sdk.CallToolResult(
        content=[sdk.TextContent(type="text", text=json.dumps(summary, ensure_ascii=False))],
        structuredContent=body,
        isError=False,
    )


async def _ask_for_approvals(
    threads: _Threads,
    registry: JobRegistry,
    result: JobResult,
    context: Any,
    caller: Principal | None,
) -> JobResult:
    """Elicit every pending approval and resume until done; the result sums every round's usage."""

    from awa._compat.mcp_server import client_supports_elicitation

    if context is None or not client_supports_elicitation(context):
        return result
    name = threads.name
    used = _usage_of(result)
    for _ in range(_MAX_APPROVAL_ROUNDS):
        if result.status != "interrupted" or result.thread_id is None:
            return _with_usage(result, used)
        decisions: dict[str, Any] = {}
        for interrupt in result.interrupts:
            answer = await context.elicit(message=_describe(name, interrupt), schema=ApprovalAnswer)
            if answer.action == "accept":
                decisions[str(interrupt.get("id"))] = {
                    "action": "approve" if answer.data.approve else "reject",
                    "note": answer.data.note or "answered in the MCP client",
                }
            elif answer.action == "decline":
                decisions[str(interrupt.get("id"))] = {
                    "action": "reject",
                    "note": "declined in the MCP client",
                }
            else:  # cancelled: the run stays paused for resume_<name>
                return _with_usage(result, used)
        decision: Any = next(iter(decisions.values())) if len(decisions) == 1 else decisions
        owner = await threads.owner(result.thread_id)
        reviewer = caller if caller is not None else ANONYMOUS
        spec = JobSpec(
            system=name,
            thread_id=result.thread_id,
            decision=await threads.stamped(result.thread_id, decision, reviewer),
            metadata=_metadata(context),
            owner=owner.principal if owner is not None else None,
            **_caller_fields(reviewer),
        )
        result = await _execute(spec, registry)
        used.merge(_usage_of(result))
    return _with_usage(result, used)


def _usage_of(result: JobResult) -> Usage:
    return Usage.model_validate((result.result or {}).get("usage") or {})


def _with_usage(result: JobResult, used: Usage) -> JobResult:
    body = {**(result.result or {}), "usage": usage_json(used)}
    return result.model_copy(update={"result": body})


def _describe(name: str, interrupt: Mapping[str, Any]) -> str:
    value = interrupt.get("value")
    if isinstance(value, Mapping) and "tool" in value:
        args = json.dumps(value.get("args", {}), ensure_ascii=False, default=str)
        return f"{name} wants to call {value['tool']} with {args}. Approve?"
    text = json.dumps(value, ensure_ascii=False, default=str)
    return f"{name} asks for approval: {text[:2000]}"


def _caller(context: Any) -> Principal | None:
    """The caller `BearerGate` put into the ASGI scope; `None` over stdio or ungated HTTP."""

    try:
        request = context.request_context.request
    except (AttributeError, LookupError, ValueError):
        return None
    scope = getattr(request, "scope", None)
    principal = scope.get(_PRINCIPAL_SCOPE_KEY) if isinstance(scope, Mapping) else None
    return principal if isinstance(principal, Principal) else None


def _caller_fields(caller: Principal | None) -> dict[str, Any]:
    if caller is None or caller is ANONYMOUS:
        return {}
    return {"actor": caller.id, "tenant": caller.tenant}


def _may_approve(entry: ServedSystem, caller: Principal | None) -> bool:
    """Whether `caller` may answer approvals; with `approver_roles` set, an unknown caller may not."""

    if entry.approver_roles is None:
        return True
    return caller is not None and entry.may_approve(caller)


class _ThreadScope:
    """The namespace of a caller's threads: every id a client names gets its tenant's prefix.

    Callers without a tenant share one prefix, so no MCP caller can name a thread made over HTTP,
    by a job or by another tenant.
    """

    def __init__(self, caller: Principal | None) -> None:
        self.caller = caller if caller is not None else ANONYMOUS
        tenant = self.caller.tenant
        digest = hashlib.sha256(tenant.encode()).hexdigest()[:16] if tenant else None
        self.prefix = f"t{digest}." if digest else _NO_TENANT_PREFIX

    def internal(self, thread_id: str) -> str:
        """The thread id the system sees for a client's `thread_id`."""

        scoped = self.prefix + thread_id
        if len(scoped) > _MAX_THREAD_ID_CHARS:
            raise InvalidRequestError(
                "thread_id is too long: at most "
                f"{_MAX_THREAD_ID_CHARS - len(self.prefix)} characters"
            )
        return scoped

    def new(self) -> str:
        """A new thread id in the caller's namespace."""

        return self.prefix + uuid.uuid4().hex

    def owner(self) -> ThreadOwner:
        """The ownership record of a thread this caller creates."""

        return ThreadOwner(tenant=self.caller.tenant, principal=self.caller.id)

    def public(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """The result body with the prefix taken off its `thread_id`."""

        thread = body.get("thread_id")
        if isinstance(thread, str) and thread.startswith(self.prefix):
            return {**body, "thread_id": thread[len(self.prefix) :]}
        return dict(body)


class _Threads:
    """Who owns the threads of one served system: a caller may use its own tenant's threads.

    A thread without an ownership record is refused to everyone.
    """

    def __init__(self, index: BaseThreadIndex, name: str, entry: ServedSystem) -> None:
        self.index = index
        self.name = name
        self.entry = entry

    async def open(self, scope: _ThreadScope, thread_id: str | None) -> str | None:
        """The client's thread (if it may use it) or a new one, recorded as the caller's.

        Raises:
            ThreadNotFoundError: If the thread exists and is not the caller's tenant's.
        """

        if thread_id is not None:
            return (await self._check(scope, thread_id, create=True))[0]
        if system_checkpointer(self.entry.system) is None:
            return None
        internal = scope.new()
        await self.index.claim(self.name, internal, scope.owner())
        return internal

    async def existing(self, scope: _ThreadScope, thread_id: str) -> tuple[str, ThreadOwner]:
        """The internal id and owner of an existing thread the caller may answer.

        Raises:
            ThreadNotFoundError: If there is no such thread of the caller's tenant.
        """

        return await self._check(scope, thread_id, create=False)

    async def stamped(self, internal: str, decision: Any, reviewer: Principal) -> Any:
        """`decision` with the reviewer recorded as the server knows it (like the HTTP API)."""

        checkpointer = system_checkpointer(self.entry.system)
        pending = None if checkpointer is None else await pending_interrupts(checkpointer, internal)
        ids = [item.get("id") for item in pending or ()]
        return stamp_reviewer(decision, self.entry.reviewer_key, ids, reviewer)

    async def owner(self, internal: str) -> ThreadOwner | None:
        """The recorded owner of an internal thread id."""

        return await self.index.owner(self.name, internal)

    async def _check(
        self, scope: _ThreadScope, thread_id: str, *, create: bool
    ) -> tuple[str, ThreadOwner]:
        internal = scope.internal(thread_id)
        owner = await self.index.owner(self.name, internal)
        if owner is None and create and not await self._exists(internal):
            owner = await self.index.claim(self.name, internal, scope.owner())
        if owner is None or owner.tenant != scope.caller.tenant:
            raise ThreadNotFoundError(f"System {self.name!r} has no thread {thread_id!r}")
        return internal, owner

    async def _exists(self, internal: str) -> bool:
        checkpointer = system_checkpointer(self.entry.system)
        return (
            checkpointer is not None
            and await pending_interrupts(checkpointer, internal) is not None
        )


def _require_callers_for_approver_roles(
    systems: SystemsArg, transport: Transport, authenticator: Authenticator | None
) -> None:
    """Refuse to serve a system with approver roles when no caller can ever hold one."""

    roled = [
        served_name
        for served_name, entry in normalise_systems(systems).items()
        if entry.approver_roles is not None
    ]
    if roled and (transport == "stdio" or authenticator is None):
        reason = (
            "not identified on stdio"
            if transport == "stdio"
            else "unknown or without roles (an api_key maps to one principal without roles)"
        )
        raise ConfigurationError(
            f"System {', '.join(roled)} sets approver_roles, but over MCP the callers are "
            f"{reason}. Serve it over HTTP with an authenticator whose principals carry the "
            "roles (`serve_as_mcp(..., transport='http', authenticator=...)`), or drop "
            "approver_roles."
        )


def _metadata(context: Any) -> dict[str, Any]:
    from awa._compat.mcp_server import client_name

    client = client_name(context) if context is not None else None
    return {"mcp_client": client} if client else {}


def build_blueprint_system(
    blueprint: str | Path, *, tools: Sequence[str] = (), skills: Sequence[str] = ()
) -> tuple[ServedSystem, list[BaseTool | Any], Any]:
    """Build a blueprint for serving: the system, the loaded tools and the blueprint.

    Tools load like `awa serve` does (only serve trusted blueprints); blueprints with approvals
    get an in-memory checkpointer.

    Raises:
        BlueprintError: If the blueprint cannot be loaded or built.
    """

    from awa.server._blueprint_app import build_blueprint

    spec, system, loaded, _ = build_blueprint(blueprint, tools, skills, local_imports=True)
    served = ServedSystem(system, diagram=spec.to_mermaid(), usage_limits=spec.usage_limits())
    return served, loaded, spec


def exposable_tools(
    tools: Sequence[BaseTool | Any], blueprint: Any
) -> tuple[list[BaseTool], list[str]]:
    """The loaded tools safe to expose directly, and notes on the ones held back.

    Held back: tools behind an approval and tools that act on the outside world
    (`awa.tools.risk`); call those through `run_<system>`, where approvals apply.
    """

    from awa.tools.approval import APPROVAL_GATE_METADATA_KEY, APPROVAL_METADATA_KEY
    from awa.tools.decorator import tool_spec
    from awa.tools.risk import infer_tool_risk

    gated = {approval.tool for approval in getattr(blueprint, "approvals", ())}
    exposed: list[BaseTool] = []
    notes: list[str] = []
    for tool in tools:
        if not isinstance(tool, BaseTool):
            continue
        spec = tool_spec(tool)
        metadata = tool.metadata or {}
        gates_itself = (spec is not None and spec.approval != "never") or bool(
            metadata.get(APPROVAL_METADATA_KEY) or metadata.get(APPROVAL_GATE_METADATA_KEY)
        )
        if tool.name in gated or gates_itself:
            notes.append(f"not exposed: {tool.name} (needs approval)")
        elif infer_tool_risk(tool).external_action:
            notes.append(f"not exposed: {tool.name} (acts on the outside world)")
        else:
            exposed.append(tool)
    return exposed, notes


__all__ = [
    "ApprovalAnswer",
    "BearerGate",
    "Transport",
    "build_blueprint_system",
    "create_mcp_server",
    "exposable_tools",
    "serve_as_mcp",
]
