"""`ToolContract`: test a tool's schema, safety flags and behavior in a few lines."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal, TypedDict
from uuid import uuid4

from pydantic import BaseModel

from awa._internal import aio as asyncio_compat
from awa._internal.upstream.checkpoint import StateInMemorySaver
from awa._internal.upstream.graph import END, START, Command, StateGraph
from awa._internal.upstream.messages import ToolCall, ToolMessage
from awa._internal.upstream.tools import BaseTool
from awa.contracts.events import RunEvent
from awa.runtime.context import RunContext, new_run_id, use_run_context
from awa.tools.decorator import ToolSpec, tool_spec
from awa.tools.decorator import tool as _as_tool
from awa.tools.risk import RISK_NAMES, ToolRisk, infer_tool_risk

Decision = bool | Literal["approve", "reject"] | Mapping[str, Any]
"""An answer to an approval pause: `True`/`"approve"`, `False`/`"reject"`, or a
full resume payload such as `{"action": "edit", "args": {...}}`."""

_CALL_ID = "tool-contract-call"


class ToolApprovalRequested(AssertionError):
    """Raised by `ToolContract.call` when the tool paused for approval without an `approve=` decision.

    Attributes:
        payload: What a reviewer would see (tool, arguments, reason).
    """

    def __init__(self, tool: str, payload: Any) -> None:
        super().__init__(
            f"Tool {tool!r} asked for approval ({payload!r}); pass approve=True, "
            "approve=False or approve={'action': 'edit', 'args': {...}}"
        )
        self.payload = payload


class _State(TypedDict, total=False):
    message: Any


class ToolContract:
    """Checks one tool's contract: schema, flags, calls, errors and approvals.

    It runs the tool as an agent would: as a tool call in a `RunContext` with `deps`, in a small
    checkpointed graph so approval pauses can be answered. Assertions raise `AssertionError` and
    return the contract, so they chain.

    Args:
        tool: The tool, or a function (converted with `awa.tool`).
        deps: `RunContext.deps` of every call.
        metadata: `RunContext.metadata` (for example `{"awa.tenant": "acme"}`).
        agent: Name of the agent the calls run under.

    Attributes:
        events: Run events emitted during the calls (approvals, guardrail blocks).

    Example:
        ```python
        contract = ToolContract(issue_refund, deps=Shop(payments=FakePayments()))
        contract.expect_args("order_id", "amount").expect_risk(external_action=True)
        assert contract.call({"order_id": "A-1", "amount": 20}) == "refunded 20.0"
        payload = contract.expect_approval_request({"order_id": "A-1", "amount": 900})
        contract.expect_error({"order_id": "missing", "amount": 1}, contains="unknown order")
        ```
    """

    def __init__(
        self,
        tool: BaseTool | Callable[..., Any],
        *,
        deps: Any = None,
        metadata: Mapping[str, Any] | None = None,
        agent: str = "tool-contract",
    ) -> None:
        self.tool: BaseTool = tool if isinstance(tool, BaseTool) else _as_tool(tool)
        self.deps = deps
        self.metadata = dict(metadata or {})
        self.agent = agent
        self.events: list[RunEvent] = []

    @property
    def name(self) -> str:
        """The tool's name."""

        return self.tool.name

    @property
    def parameters(self) -> dict[str, Any]:
        """The JSON schema of the arguments the model sees (injected ones excluded)."""

        schema = self.tool.tool_call_schema
        if isinstance(schema, dict):
            return dict(schema)
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            return schema.model_json_schema()
        return {}

    @property
    def spec(self) -> ToolSpec | None:
        """The tool's `ToolSpec` (`None` for tools not built with `awa.tool`)."""

        return tool_spec(self.tool)

    @property
    def risk(self) -> ToolRisk:
        """The resolved risk flags (annotation, else name heuristics)."""

        return infer_tool_risk(self.tool)

    def snapshot(self) -> dict[str, Any]:
        """A stable summary for snapshot tests: name, description, arguments, flags."""

        parameters = self.parameters
        properties = parameters.get("properties") or {}
        required = set(parameters.get("required") or ())
        spec = self.spec
        return {
            "name": self.name,
            "description": self.tool.description,
            "args": {
                name: {
                    "type": _type_name(field),
                    "required": name in required,
                    "description": field.get("description", ""),
                }
                for name, field in properties.items()
            },
            "risk": list(self.risk.flags()),
            "approval": spec.approval if spec is not None else None,
        }

    def expect_args(
        self, *required: str, optional: Iterable[str] = (), exact: bool = True
    ) -> ToolContract:
        """Assert the model-visible arguments: `*required`, `optional`, and with `exact` no others."""

        parameters = self.parameters
        properties = set((parameters.get("properties") or {}).keys())
        actual_required = set(parameters.get("required") or ())
        missing = [name for name in (*required, *optional) if name not in properties]
        _check(not missing, f"{self.name}: missing argument(s) {missing}; has {sorted(properties)}")
        not_required = [name for name in required if name not in actual_required]
        _check(not not_required, f"{self.name}: argument(s) {not_required} are optional")
        now_required = [name for name in optional if name in actual_required]
        _check(not now_required, f"{self.name}: argument(s) {now_required} are required")
        if exact:
            extra = sorted(properties - set(required) - set(optional))
            _check(not extra, f"{self.name}: unexpected argument(s) {extra}")
        return self

    def expect_hidden(self, *names: str) -> ToolContract:
        """Assert these parameters (such as `ctx`) are not visible to the model."""

        visible = [name for name in names if name in (self.parameters.get("properties") or {})]
        _check(not visible, f"{self.name}: {visible} should be hidden from the model")
        return self

    def expect_description(self, contains: str | None = None) -> ToolContract:
        """Assert the tool has a description (containing `contains`, if given)."""

        description = self.tool.description or ""
        _check(bool(description.strip()), f"{self.name}: has no description")
        if contains is not None:
            _check(
                contains in description,
                f"{self.name}: description {description!r} lacks {contains!r}",
            )
        return self

    def expect_risk(self, **flags: bool) -> ToolContract:
        """Assert resolved risk flags, for example `expect_risk(external_action=True)`."""

        unknown = sorted(set(flags) - set(RISK_NAMES))
        _check(not unknown, f"unknown risk flag(s) {unknown}; use {list(RISK_NAMES)}")
        risk = self.risk
        wrong = {
            name: getattr(risk, name)
            for name, value in flags.items()
            if getattr(risk, name) != value
        }
        _check(not wrong, f"{self.name}: risk flags differ: {wrong} (expected {flags})")
        return self

    def expect_approval(
        self, mode: bool | Literal["always", "conditional", "never"] = True
    ) -> ToolContract:
        """Assert the tool's own approval: `True` (always or conditional), `False`, or a mode."""

        spec = self.spec
        actual = spec.approval if spec is not None else "never"
        if mode is True:
            ok = actual != "never"
        elif mode is False:
            ok = actual == "never"
        else:
            ok = actual == mode
        _check(ok, f"{self.name}: approval is {actual!r}, expected {mode!r}")
        return self

    def expect_spec(self, **fields: Any) -> ToolContract:
        """Assert fields of the tool's `ToolSpec`, for example `expect_spec(timeout=10, retries=2)`."""

        spec = self.spec
        if spec is None:
            raise AssertionError(f"{self.name}: was not built with awa.tool (no ToolSpec)")
        values = spec.to_metadata()
        unknown = sorted(set(fields) - set(values))
        _check(not unknown, f"unknown ToolSpec field(s) {unknown}")
        wrong = {name: values[name] for name, value in fields.items() if values[name] != value}
        _check(not wrong, f"{self.name}: spec differs: {wrong} (expected {fields})")
        return self

    def expect_snapshot(self, expected: Mapping[str, Any]) -> ToolContract:
        """Assert `snapshot()` equals `expected` (the keys given in `expected`)."""

        actual = self.snapshot()
        differences = {key: actual.get(key) for key in expected if actual.get(key) != expected[key]}
        _check(not differences, f"{self.name}: snapshot differs in {differences}")
        return self

    async def acall_message(
        self, args: Mapping[str, Any] | None = None, /, *, approve: Decision | None = None
    ) -> ToolMessage:
        """Call the tool as a model would and return the `ToolMessage`.

        Raises:
            ToolApprovalRequested: If the tool paused and `approve` is `None`.
        """

        graph = self._graph()
        config: Any = {"configurable": {"thread_id": f"contract-{uuid4().hex}"}}
        call = ToolCall(name=self.name, args=dict(args or {}), id=_CALL_ID, type="tool_call")
        with use_run_context(self._context()):
            state = await graph.ainvoke({"message": call}, config=config)
            interrupts = state.get("__interrupt__") or ()
            if interrupts:
                payload = interrupts[0].value
                if approve is None:
                    raise ToolApprovalRequested(self.name, payload)
                state = await graph.ainvoke(Command(resume=_decision(approve)), config=config)
        message = state["message"]
        if not isinstance(message, ToolMessage):
            raise AssertionError(f"{self.name}: returned {message!r} instead of a ToolMessage")
        return message

    def call_message(
        self, args: Mapping[str, Any] | None = None, /, *, approve: Decision | None = None
    ) -> ToolMessage:
        """Synchronous `acall_message` (not inside a running event loop)."""

        return asyncio_compat.run(self.acall_message(args, approve=approve))

    async def acall(
        self, args: Mapping[str, Any] | None = None, /, *, approve: Decision | None = None
    ) -> Any:
        """Call the tool and return the content of its result."""

        return (await self.acall_message(args, approve=approve)).content

    def call(
        self, args: Mapping[str, Any] | None = None, /, *, approve: Decision | None = None
    ) -> Any:
        """Synchronous `acall` (not inside a running event loop)."""

        return asyncio_compat.run(self.acall(args, approve=approve))

    async def aexpect_error(
        self,
        args: Mapping[str, Any] | None = None,
        /,
        *,
        contains: str | None = None,
        approve: Decision | None = None,
    ) -> str:
        """Assert the call fails as an error result the model can read; return its text."""

        message = await self.acall_message(args, approve=approve)
        text = str(message.text)
        _check(
            message.status == "error",
            f"{self.name}: expected an error result, got {message.status}: {text!r}",
        )
        if contains is not None:
            _check(contains in text, f"{self.name}: error {text!r} lacks {contains!r}")
        return text

    def expect_error(
        self,
        args: Mapping[str, Any] | None = None,
        /,
        *,
        contains: str | None = None,
        approve: Decision | None = None,
    ) -> str:
        """Synchronous `aexpect_error`."""

        return asyncio_compat.run(self.aexpect_error(args, contains=contains, approve=approve))

    async def aexpect_approval_request(self, args: Mapping[str, Any] | None = None, /) -> Any:
        """Assert the call pauses for approval; return the reviewer's payload."""

        try:
            message = await self.acall_message(args)
        except ToolApprovalRequested as request:
            return request.payload
        raise AssertionError(
            f"{self.name}: expected an approval pause, but it ran: {message.text!r}"
        )

    def expect_approval_request(self, args: Mapping[str, Any] | None = None, /) -> Any:
        """Synchronous `aexpect_approval_request`."""

        return asyncio_compat.run(self.aexpect_approval_request(args))

    def _context(self) -> RunContext[Any]:
        context: RunContext[Any] = RunContext(
            run_id=new_run_id(), agent=self.agent, deps=self.deps, metadata=dict(self.metadata)
        )
        context.events.subscribe(self.events.append)
        return context

    def _graph(self) -> Any:
        tool_ = self.tool

        async def call_tool(state: _State) -> dict[str, Any]:
            return {"message": await tool_.ainvoke(state["message"])}

        builder: StateGraph[_State] = StateGraph(_State)
        builder.add_node("call_tool", call_tool)
        builder.add_edge(START, "call_tool")
        builder.add_edge("call_tool", END)
        return builder.compile(checkpointer=StateInMemorySaver())


def _decision(approve: Decision) -> Mapping[str, Any]:
    if approve is True or approve == "approve":
        return {"action": "approve", "reviewer": "tool-contract"}
    if approve is False or approve == "reject":
        return {"action": "reject", "note": "rejected in a test", "reviewer": "tool-contract"}
    if isinstance(approve, Mapping):
        return approve
    raise ValueError(f"approve must be a bool, 'approve', 'reject' or a mapping, not {approve!r}")


def _type_name(field: Mapping[str, Any]) -> str:
    if "type" in field:
        return str(field["type"])
    if "$ref" in field:
        return str(field["$ref"]).rsplit("/", 1)[-1]
    options = field.get("anyOf") or field.get("oneOf")
    if options:
        return " | ".join(_type_name(option) for option in options)
    return "any"


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


__all__ = ["Decision", "ToolApprovalRequested", "ToolContract"]
