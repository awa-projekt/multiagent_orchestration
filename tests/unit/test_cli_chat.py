"""`awa chat`: a multi-turn terminal conversation driven by scripted stdin and fake models."""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.memory import InMemorySaver

from awa import Agent
from awa.cli import ChatSession, main
from awa.models import ModelRegistry
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper

TOOLS_MODULE = '''
CALLS = []


def lookup(order_id: str) -> str:
    """Look up an order."""
    CALLS.append(order_id)
    return f"order {order_id}: shipped"


def pay(order_id: str) -> str:
    """Pay an order."""
    CALLS.append(f"pay:{order_id}")
    return f"paid {order_id}"


__all__ = ["lookup", "pay"]
'''

CLERK = """
name: desk
agents:
  - name: clerk
    description: Looks up and pays orders.
    tools: [lookup, pay]
approvals:
  - tool: pay
"""

TEAM = """
name: office
agents:
  - name: lead
    description: Leads.
  - name: helper
    description: Looks orders up.
    tools: [lookup]
  - name: closer
    description: Closes conversations.
team:
  orchestrator: lead
  members: [helper, closer]
  handoffs:
    lead: [closer]
"""

PIPELINE = """
name: pipeline
agents:
  - name: first
  - name: second
workflow:
  sequential: [first, second]
"""


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> ModelRegistry:
    registry = ModelRegistry()
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    return registry


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    files = {
        "chat_tools.py": TOOLS_MODULE,
        "clerk.yaml": CLERK,
        "team.yaml": TEAM,
        "pipeline.yaml": PIPELINE,
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    monkeypatch.delitem(sys.modules, "chat_tools", raising=False)
    return tmp_path


def type_lines(monkeypatch: pytest.MonkeyPatch, *lines: str) -> None:
    """Script stdin: `input()` reads these lines, then hits end of file."""

    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(f"{line}\n" for line in lines)))


def calls() -> list[str]:
    module: Any = sys.modules["chat_tools"]
    return list(module.CALLS)


def humans(call: ModelCall) -> list[str]:
    return [message.text for message in call.messages if isinstance(message, HumanMessage)]


def chat(*args: str) -> int:
    return main(["chat", *args, "--tools", "chat_tools.py"])


# --------------------------------------------------------------------------- conversation


def test_streams_the_reply_and_prints_tool_calls(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    models.register(
        "default",
        FakeChatModel(
            [tool_call("lookup", {"order_id": "7"}), "Order 7 shipped."], stream_chunk_size=3
        ),
    )
    type_lines(monkeypatch, "Where is 7?", "/exit")

    assert chat("clerk.yaml") == 0

    out = capsys.readouterr().out
    assert "chatting with agent 'clerk' on thread chat-" in out
    assert '  clerk -> lookup({"order_id": "7"})' in out
    assert "clerk: Order 7 shipped.\n" in out
    assert out.count("Order 7 shipped.") == 1  # streamed once, not repeated
    assert calls() == ["7"]


@pytest.mark.parametrize("stream", [True, False])
def test_control_characters_never_reach_the_terminal(
    stream: bool,
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    models.register(
        "default",
        FakeChatModel(
            [tool_call("lookup", {"order_id": "7\x1b[2J"}), "Done\x1b]0;owned\x07."],
            stream_chunk_size=3,
        ),
    )
    type_lines(monkeypatch, "Where is 7?", "/exit")

    assert chat("clerk.yaml", *([] if stream else ["--no-stream"])) == 0

    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out
    assert "Done]0;owned." in out


def test_no_stream_prints_complete_replies(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    models.register("default", FakeChatModel([tool_call("lookup", {"order_id": "7"}), "Shipped."]))
    type_lines(monkeypatch, "Where is 7?")

    assert chat("clerk.yaml", "--no-stream") == 0  # end of input ends the chat

    out = capsys.readouterr().out
    assert "clerk: Shipped." in out
    assert "-> lookup" not in out


def test_turns_share_one_thread_until_reset(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[list[str]] = []

    def respond(call: ModelCall) -> str:
        seen.append(humans(call))
        return f"reply {len(seen)}"

    models.register("default", FunctionChatModel(respond))
    type_lines(monkeypatch, "first", "second", "/history", "/reset", "third", "/exit")

    assert chat("clerk.yaml", "--thread-id", "t-1") == 0

    assert seen == [["first"], ["first", "second"], ["third"]]
    out = capsys.readouterr().out
    assert "on thread t-1" in out
    assert "you: first\nclerk: reply 1\nyou: second\nclerk: reply 2\n" in out
    assert "(new thread chat-" in out


def test_usage_help_and_unknown_commands(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    models.register("default", FakeChatModel([tool_call("lookup", {"order_id": "7"}), "Shipped."]))
    type_lines(monkeypatch, "/usage", "Where is 7?", "/usage", "/help", "/nope", "/quit")

    assert chat("clerk.yaml") == 0

    out = capsys.readouterr().out
    assert "last turn:   0 model call(s)" in out
    assert "last turn:   2 model call(s)" in out
    assert "this thread: 2 model call(s)" in out
    assert "session:     2 model call(s)" in out
    assert "  clerk: 2 model call(s)" in out
    assert "1 tool call(s)" in out
    assert "/reset     start a new thread" in out
    assert "unknown command /nope" in out


@pytest.mark.parametrize(
    ("answers", "expected_calls", "note"),
    [(["y"], ["pay:9"], None), (["n", "too much"], [], "too much"), (["", ""], [], "Rejected")],
)
def test_approvals_are_asked_for_inline(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: list[str],
    expected_calls: list[str],
    note: str | None,
) -> None:
    results: list[str] = []

    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("pay", {"order_id": "9"})
        results.append(call.last_message.text)
        return "Handled."

    models.register("default", FunctionChatModel(respond))
    type_lines(monkeypatch, "Pay order 9.", *answers, "/exit")

    assert chat("clerk.yaml") == 0

    out = capsys.readouterr().out
    assert 'approval needed: pay({"order_id": "9"})' in out
    assert "approve? [y/N]" in out
    assert "clerk: Handled." in out
    assert calls() == expected_calls
    if note is not None:
        assert note in results[0]
        assert "note for the agent" in out


def test_team_turns_show_delegations_and_handoffs(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def respond(call: ModelCall) -> Any:
        tools = set(call.tool_names)
        if "delegate_task" in tools:  # the lead
            if call.index == 0:
                return tool_call("delegate_task", {"agent": "helper", "task": "Find order 7."})
            return tool_call("transfer_to_closer", {"reason": "Wrap up."})
        if "lookup" in tools and not any(m.type == "tool" for m in call.messages):
            return tool_call("lookup", {"order_id": "7"})
        return "Order 7 has shipped." if "lookup" in tools else "All done, goodbye."

    models.register("default", FunctionChatModel(respond, stream_chunk_size=4))
    type_lines(monkeypatch, "Where is order 7?", "/exit")

    assert chat("team.yaml") == 0

    out = capsys.readouterr().out
    assert "chatting with team 'office'" in out
    assert "  lead => helper: Find order 7." in out
    assert '    helper -> lookup({"order_id": "7"})' in out
    assert "  lead <= helper [ok]" in out
    assert "  lead >> closer: Wrap up." in out
    assert "closer: All done, goodbye.\n" in out
    assert "helper: Order 7" not in out  # members' text is summarised, not streamed


def test_workflows_run_single_turn_with_a_note(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    models.register("default", FakeChatModel(["one", "two", "three", "four"]))
    type_lines(monkeypatch, "Go.", "Again.", "/history", "/reset", "/exit")

    assert chat("pipeline.yaml") == 0

    out = capsys.readouterr().out
    assert "each message runs on its own (single turn)" in out
    assert "second: two" in out
    assert "second: four" in out
    assert "you: Go.\npipeline: two\nyou: Again.\npipeline: four\n" in out
    assert "nothing to reset" in out

    assert chat("pipeline.yaml", "--thread-id", "t") == 1
    assert "drop --thread-id" in capsys.readouterr().err


def test_a_failing_turn_does_not_end_the_chat(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def respond(call: ModelCall) -> str:
        if call.index == 0:
            raise RuntimeError("provider unavailable")
        return "Back again."

    models.register("default", FunctionChatModel(respond))
    type_lines(monkeypatch, "Hello?", "Hello again?", "/exit")

    assert chat("clerk.yaml") == 0

    captured = capsys.readouterr()
    assert "error: RuntimeError: provider unavailable" in captured.err
    assert "clerk: Back again." in captured.out


def test_blueprint_errors_are_reported_before_the_chat_starts(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["chat", "clerk.yaml"]) == 1  # no --tools: the tools are unknown
    assert "unknown tool 'lookup'" in capsys.readouterr().err


# --------------------------------------------------------------------------- Ctrl-C


class _Keys:
    """Scripted keyboard: returns lines, runs callables, raises exceptions."""

    def __init__(self, *items: Any) -> None:
        self._items = list(items)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._items:
            raise EOFError
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return str(item())
        return str(item)


def build_clerk(model: Any, *, gated: bool = False) -> Agent[Any, Any]:
    def lookup(order_id: str) -> str:
        """Look up an order."""
        return f"order {order_id}: shipped"

    tool: BaseTool = StructuredTool.from_function(lookup)
    if gated:
        tool = create_tool_approval_wrapper(tool)
    return Agent("clerk", model=model, tools=[tool], checkpointer=InMemorySaver())


def pending_tasks(session: ChatSession) -> list[asyncio.Task[Any]]:
    runner = session._runner
    assert runner is not None
    return [task for task in asyncio.all_tasks(runner.get_loop()) if not task.done()]


def test_ctrl_c_cancels_the_turn_without_leaking_tasks() -> None:
    seen: list[list[str]] = []

    def respond(call: ModelCall) -> str:
        seen.append(humans(call))
        if len(seen) == 2:
            raise KeyboardInterrupt  # the user presses Ctrl-C while the model works
        return f"reply {len(seen)}"

    out, err = io.StringIO(), io.StringIO()
    leftovers: list[int] = []

    def third() -> str:
        leftovers.append(len(pending_tasks(session)))
        return "third"

    keys = _Keys("first", "second", third)
    session: ChatSession = ChatSession(
        build_clerk(FunctionChatModel(respond)), input=keys, out=out, err=err, thread_id="t-1"
    )

    assert session.run() == 0

    assert leftovers == [0]
    assert "(turn cancelled; thread t-1 may be left mid-step, continuing on chat-" in err.getvalue()
    assert session.thread_id != "t-1"
    # The new thread continues from the last complete transcript.
    assert seen[2] == ["first", "third"]
    assert "clerk: reply 3" in out.getvalue()


def test_ctrl_c_at_the_approval_prompt_moves_to_a_new_thread() -> None:
    def respond(call: ModelCall) -> Any:
        if not any(m.type == "tool" for m in call.messages) and "Look" in humans(call)[-1]:
            return tool_call("lookup", {"order_id": "1"})
        return "Fine."

    out, err = io.StringIO(), io.StringIO()
    keys = _Keys("Look up order 1.", KeyboardInterrupt(), "hello")
    session = ChatSession(
        build_clerk(FunctionChatModel(respond), gated=True), input=keys, out=out, err=err
    )

    assert session.run() == 0

    assert "approval needed: lookup" in out.getvalue()
    assert "was left paused for approval" in err.getvalue()
    assert "clerk: Fine." in out.getvalue()


def test_end_of_input_at_the_approval_prompt_leaves_the_run_paused() -> None:
    model = FunctionChatModel(lambda call: tool_call("lookup", {"order_id": "1"}))
    err = io.StringIO()
    keys = _Keys("Look up order 1.")  # then Ctrl-D: input ends at "approve? [y/N]"
    session = ChatSession(build_clerk(model, gated=True), input=keys, out=io.StringIO(), err=err)

    assert session.run() == 0

    assert keys.prompts == ["you> ", "approve? [y/N] ", "you> "]
    assert "was left paused for approval" in err.getvalue()
    assert "EOFError" not in err.getvalue()


def test_ctrl_c_at_the_prompt_ends_the_chat() -> None:
    keys = _Keys(KeyboardInterrupt(), "never read")
    session = ChatSession(build_clerk(FakeChatModel(["unused"])), input=keys, out=io.StringIO())

    assert session.run() == 0
    assert keys.prompts == ["you> "]
