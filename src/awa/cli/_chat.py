"""`awa chat`: an interactive, multi-turn terminal chat with a built system (`ChatSession`)."""

from __future__ import annotations

import argparse
import asyncio
import builtins
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from uuid import uuid4

from awa.cli._common import (
    Commands,
    add_blueprint_argument,
    add_command,
    add_deps_options,
    add_limit_options,
    add_tools_options,
    deps_from_args,
    limit_option,
    limits_from_args,
    output_text,
    preview,
)
from awa.cli._terminal_text import terminal_safe

if TYPE_CHECKING:
    from awa._internal.upstream.messages import BaseMessage
    from awa.contracts.events import RunEvent, TextDeltaEvent
    from awa.contracts.protocols import AgentLike
    from awa.contracts.results import RunResult
    from awa.contracts.usage import Usage, UsageLimits

HELP = """\
Commands:
  /help      show this help
  /reset     start a new thread
  /usage     model calls and tokens of the last turn, this thread and the session
  /history   the messages of this thread
  /exit      end the chat (also /quit, Ctrl-D, or Ctrl-C at the prompt)
Ctrl-C while the system is working cancels the current turn."""


class ChatSession:
    """A terminal conversation with an `Agent`, a `Team` or a workflow.

    Streams the reply, prints short lines for delegations, handoffs and tool calls, and asks for
    approval-gated calls inline. Commands: `/exit`, `/reset`, `/usage`, `/history`, `/help`.

    Args:
        system: Agents and teams need a checkpointer to keep the conversation and pause for
            approvals.
        stream: Print the reply as it is written, with event lines.
        thread_id: Thread to start on; a new `chat-...` id by default.
        conversational: Whether messages continue one thread; defaults to `True` for systems
            that can resume a run (agents and teams). A paused run is resumed either way when the
            system can resume and the run has a thread.
        usage_limits: Limits of every turn; a turn that reaches one shows the error and the
            chat goes on.
        input: Reads one line from the user (default: `input`); `EOFError` ends the chat.

    Example:
        ```python
        desk = load("support.yaml", tools=[lookup_order], checkpointer=StateInMemorySaver())
        ChatSession(desk).run()
        ```
    """

    def __init__(
        self,
        system: AgentLike,
        *,
        stream: bool = True,
        thread_id: str | None = None,
        conversational: bool | None = None,
        deps: Any = None,
        usage_limits: UsageLimits | None = None,
        input: Callable[[str], str] | None = None,
        out: TextIO | None = None,
        err: TextIO | None = None,
    ) -> None:
        self._system = system
        self._stream = stream and callable(getattr(system, "stream", None))
        if conversational is None:
            conversational = callable(getattr(system, "resume", None))
        self._conversational = conversational
        self._deps = deps
        self._limits = usage_limits
        self._thread_id = thread_id or _new_thread_id()
        self._input = input if input is not None else builtins.input
        self._out = out if out is not None else sys.stdout
        self._err = err if err is not None else sys.stderr
        from awa.agents import Agent
        from awa.contracts.usage import Usage

        self._answer_depth = 0 if isinstance(system, Agent) else 1
        self._runner: asyncio.Runner | None = None
        self._last_result: RunResult[Any] | None = None
        self._carry_history: list[BaseMessage] | None = None
        self._turns: list[tuple[str, str]] = []
        self._last_usage = Usage()
        self._thread_usage = Usage()
        self._session_usage = Usage()

    @property
    def thread_id(self) -> str | None:
        """The current thread, or `None` when messages run on their own."""

        return self._thread_id if self._conversational else None

    def run(self) -> int:
        """Read messages until the user exits; returns the exit code (0)."""

        self._print_banner()
        with asyncio.Runner() as runner:
            self._runner = runner
            try:
                while True:
                    try:
                        line = self._input("you> ")
                    except (EOFError, KeyboardInterrupt):
                        self._write("")
                        break
                    text = line.strip()
                    if not text:
                        continue
                    if text.startswith("/"):
                        if not self._command(text):
                            break
                        continue
                    self._turn(text)
            finally:
                self._cancel_leftovers()
                self._runner = None
        return 0

    def _command(self, text: str) -> bool:
        """Handle a slash command; returns `False` to end the chat."""

        command = text.split()[0].lower()
        if command in ("/exit", "/quit"):
            return False
        if command == "/help":
            self._write(HELP)
        elif command == "/reset":
            self._reset()
        elif command == "/usage":
            self._print_usage()
        elif command == "/history":
            self._print_history()
        else:
            self._write(f"unknown command {command}; /help lists the commands")
        return True

    def _reset(self) -> None:
        from awa.contracts.usage import Usage

        self._last_result = None
        self._carry_history = None
        self._turns = []
        self._thread_usage = Usage()
        if not self._conversational:
            self._write("(each message already runs on its own; nothing to reset)")
            return
        self._thread_id = _new_thread_id()
        self._write(f"(new thread {self._thread_id})")

    def _print_usage(self) -> None:
        self._write(f"last turn:   {_usage_line(self._last_usage)}")
        if self._conversational:
            self._write(f"this thread: {_usage_line(self._thread_usage)}")
        self._write(f"session:     {_usage_line(self._session_usage)}")
        for agent, usage in sorted(self._session_usage.by_agent.items()):
            self._write(f"  {agent}: {_usage_line(usage)}")

    def _print_history(self) -> None:
        if not self._conversational:
            if not self._turns:
                self._write("(no messages yet)")
            for text, reply in self._turns:
                self._write(f"you: {preview(text, 100)}")
                self._write(f"{self._system.name}: {preview(reply, 100)}")
            return
        messages = self._last_result.messages if self._last_result is not None else []
        lines = [_describe_message(message, self._system.name) for message in messages]
        shown = [line for line in lines if line is not None]
        for line in shown:
            self._write(line)
        if not shown:
            self._write("(no messages on this thread yet)")

    def _turn(self, text: str) -> None:
        from awa.contracts.usage import Usage
        from awa.errors import ModelProviderError

        runner = self._runner
        assert runner is not None
        stage = "run"
        self._last_usage = Usage()
        try:
            result = runner.run(self._start(text))
            self._account(result)
            resume = getattr(self._system, "resume", None)
            while result.is_interrupted and result.thread_id is not None and callable(resume):
                stage = "approval"
                decision = self._ask_for_decisions(result.interrupts)
                stage = "run"
                result = runner.run(resume(result.thread_id, decision, **self._run_options()))
                self._account(result)
                self._print_result(result, streamed="")
        except (KeyboardInterrupt, EOFError):  # Ctrl-C, or Ctrl-D at the approval prompt
            self._cancel_leftovers()
            self._after_cancel(paused=stage == "approval")
            return
        except Exception as exc:
            self._cancel_leftovers()
            if isinstance(exc, ModelProviderError) and exc.kind == "authentication":
                raise  # a missing or rejected key fails every turn: end the chat with one line
            self._error(f"error: {type(exc).__name__}: {exc}")
            return
        if result.is_interrupted:
            self._error(
                f"(the run is paused for approval on thread {result.thread_id}; this system "
                "cannot be resumed here)"
            )
        self._last_result = result if self._conversational else None
        self._turns.append((text, output_text(result.output)))

    def _run_options(self) -> dict[str, Any]:
        deps = {"deps": self._deps} if self._deps is not None else {}
        return {**deps, **limit_option(self._limits)}

    async def _start(self, text: str) -> RunResult[Any]:
        from awa.contracts.events import RunCompletedEvent
        from awa.errors import AgentRunError

        options: dict[str, Any] = self._run_options()
        if self._conversational:
            options["thread_id"] = self._thread_id
            if self._carry_history:
                options["history"] = self._carry_history
        self._carry_history = None
        if not self._stream:
            result = await self._system.run(text, **options)
            self._print_result(result, streamed="")
            return result
        printer = _ChatPrinter(self._out, self._answer_depth)
        stream = self._system.stream  # type: ignore[attr-defined]  # the system streams (checked above)
        completed: RunResult[Any] | None = None
        async for event in stream(text, **options):
            if isinstance(event, RunCompletedEvent):
                completed = event.result
            else:
                printer(event)
        printer.close()
        if completed is None:
            raise AgentRunError("The run ended without a result")
        self._print_result(completed, streamed=printer.last_segment)
        return completed

    def _print_result(self, result: RunResult[Any], *, streamed: str) -> None:
        if result.is_interrupted:
            return
        text = output_text(result.output)
        if streamed.strip() and streamed.strip() == text.strip():
            return
        self._write(f"{result.agent}: {text}")

    def _account(self, result: RunResult[Any]) -> None:
        self._last_usage.merge(result.usage)
        self._thread_usage.merge(result.usage)
        self._session_usage.merge(result.usage)

    def _ask_for_decisions(self, interrupts: Sequence[dict[str, Any]]) -> Any:
        decisions = {pending["id"]: self._ask_for_decision(pending) for pending in interrupts}
        # A single pending approval takes the plain decision; several (parallel
        # tool calls) are resumed together, keyed by interrupt id.
        return next(iter(decisions.values())) if len(decisions) == 1 else decisions

    def _ask_for_decision(self, pending: dict[str, Any]) -> dict[str, str]:
        value = pending.get("value")
        if isinstance(value, dict) and "tool" in value:
            arguments = json.dumps(value.get("args", {}), default=str)
            self._write(f"approval needed: {value['tool']}({arguments})")
        else:
            self._write(f"approval needed: {json.dumps(value, default=str)}")
        answer = self._input("approve? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            return {"action": "approve"}
        note = self._input("note for the agent (optional): ").strip()
        return {"action": "reject", "note": note or "Rejected by the operator."}

    def _after_cancel(self, *, paused: bool) -> None:
        if not self._conversational:
            self._error("(turn cancelled)")
            return
        # A cancelled run can leave its thread mid-step (or paused for approval),
        # so the conversation continues on a new thread with the last complete
        # transcript as history.
        previous = self._thread_id
        self._thread_id = _new_thread_id()
        if self._last_result is not None:
            self._carry_history = _plain_transcript(self._last_result.messages)
        reason = "was left paused for approval" if paused else "may be left mid-step"
        self._error(
            f"(turn cancelled; thread {previous} {reason}, continuing on {self._thread_id})"
        )

    def _cancel_leftovers(self) -> None:
        """Cancel and await every task a cancelled turn left on the loop."""

        runner = self._runner
        if runner is None:
            return
        loop = runner.get_loop()
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    def _print_banner(self) -> None:
        kind = type(self._system).__name__.lower()
        if self._conversational:
            self._write(
                f"chatting with {kind} {self._system.name!r} on thread {self._thread_id}; "
                "/help for commands"
            )
        else:
            self._write(
                f"chatting with {self._system.name!r}; note: it keeps no conversation state, so "
                "each message runs on its own (single turn). /help for commands"
            )

    def _write(self, text: str) -> None:
        self._out.write(f"{terminal_safe(text)}\n")
        self._out.flush()

    def _error(self, text: str) -> None:
        self._err.write(f"{terminal_safe(text)}\n")
        self._err.flush()


class _ChatPrinter:
    """Streams the answering agents' text and prints short lines for other events."""

    def __init__(self, out: TextIO, answer_depth: int) -> None:
        self._out = out
        self._answer_depth = answer_depth
        self._base: int | None = None
        self._segment_run: str | None = None
        self._segment: list[str] = []
        self.last_segment = ""

    def __call__(self, event: RunEvent) -> None:
        from awa.contracts.events import ModelResponseEvent, TextDeltaEvent

        if self._base is None:
            self._base = event.depth
        depth = event.depth - self._base
        if isinstance(event, TextDeltaEvent):
            if depth == self._answer_depth:
                self._delta(event)
            return
        if isinstance(event, ModelResponseEvent) and depth == self._answer_depth:
            if event.run_id == self._segment_run:
                self._end_segment()
            elif event.content.strip():
                # Text that arrived without deltas (for example a workflow step).
                self._end_segment()
                self._out.write(terminal_safe(f"{event.agent}: {event.content}") + "\n")
                self._out.flush()
                self.last_segment = event.content
            return
        from awa.observability.events import describe_event

        line = describe_event(event, style="chat")
        if line is None:
            return
        self._end_segment()
        indent = "  " * max(1, depth - self._answer_depth + 1)
        self._out.write(f"{indent}{terminal_safe(line)}\n")
        self._out.flush()

    def close(self) -> None:
        self._end_segment()

    def _delta(self, event: TextDeltaEvent) -> None:
        if event.run_id != self._segment_run:
            self._end_segment()
            self._segment_run = event.run_id
            self._out.write(terminal_safe(f"{event.agent}: "))
        self._segment.append(event.delta)
        self._out.write(terminal_safe(event.delta))
        self._out.flush()

    def _end_segment(self) -> None:
        if self._segment_run is None:
            return
        self._out.write("\n")
        self._out.flush()
        self.last_segment = "".join(self._segment)
        self._segment_run = None
        self._segment = []


def _describe_message(message: BaseMessage, default_agent: str) -> str | None:
    from awa._internal.upstream.messages import AIMessage, HumanMessage, ToolMessage

    if isinstance(message, HumanMessage):
        return f"you: {preview(message.text, 100)}"
    if isinstance(message, AIMessage):
        speaker = message.name or default_agent
        parts: list[str] = []
        if message.text.strip():
            parts.append(f"{speaker}: {preview(message.text, 100)}")
        for call in message.tool_calls:
            parts.append(f"  {speaker} -> {call['name']}({preview(call.get('args', {}), 100)})")
        return "\n".join(parts) or None
    if isinstance(message, ToolMessage):
        return f"  <- {message.name or 'tool'}: {preview(message.text, 100)}"
    return None


def _plain_transcript(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """The user messages and final agent texts of a transcript (no tool traffic)."""

    from awa._internal.upstream.messages import AIMessage, HumanMessage

    kept: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            kept.append(HumanMessage(content=message.text))
        elif isinstance(message, AIMessage) and not message.tool_calls and message.text.strip():
            kept.append(AIMessage(content=message.text, name=message.name))
    return kept


def _usage_line(usage: Usage) -> str:
    return (
        f"{usage.requests} model call(s), {usage.total_tokens} tokens "
        f"({usage.input_tokens} in, {usage.output_tokens} out), {usage.tool_calls} tool call(s)"
    )


def _new_thread_id() -> str:
    return f"chat-{uuid4().hex[:8]}"


def add_chat(commands: Commands) -> None:
    chat = add_command(commands, "chat")
    add_blueprint_argument(chat)
    chat.add_argument("--thread-id", help="thread id to start on (kept in memory for this chat)")
    chat.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="print replies as they are written, with event lines (default: on)",
    )
    add_tools_options(chat)
    add_deps_options(chat)
    add_limit_options(chat, "turn")
    chat.set_defaults(handler=_chat)


def _chat(args: argparse.Namespace) -> int:
    from awa._internal import aio as asyncio_compat
    from awa._internal.upstream.checkpoint import StateInMemorySaver
    from awa.blueprints import Blueprint, BlueprintError
    from awa.blueprints._resolve import load_tools, skill_registry, tools_files
    from awa.cli._preflight import preflight

    preflight(args.blueprint)
    blueprint = Blueprint.from_yaml(Path(args.blueprint))
    tools = load_tools(tools_files(args.tools, args.blueprint))
    deps = deps_from_args(args)
    limits = limits_from_args(args, blueprint)
    # Workflows and networks keep no conversation: every message is a run of its
    # own (on a thread of its own, so approvals can pause and resume it).
    conversational = blueprint.resolved_entrypoint() not in ("workflow", "network")
    if args.thread_id is not None and not conversational:
        raise BlueprintError(
            "A workflow or network entrypoint keeps no conversation; drop --thread-id"
        )
    build_options: dict[str, Any] = {
        "tools": tools,
        "skill_registry": skill_registry(args.skills, args.blueprint),
        "checkpointer": StateInMemorySaver(),
        "allow_imports": True,
    }
    if blueprint.active_mcp_servers():
        system = asyncio_compat.run(blueprint.abuild(**build_options))
    else:
        system = blueprint.build(**build_options)
    return ChatSession(
        system,
        stream=args.stream,
        thread_id=args.thread_id,
        conversational=conversational,
        deps=deps,
        usage_limits=limits,
    ).run()
