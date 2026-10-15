"""Shared fakes for the run-trace tests (no tests here): a team that delegates, consults,
hands over and asks for an approval, and helpers to record and scrub its trace."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from _helpers.fake_secrets import fake_secret
from awa import Agent, tool
from awa.contracts import RunResult
from awa.observability import EventRecorder, TraceRecorder
from awa.runtime import use_global_subscriber
from awa.team import Team
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call

SECRET = fake_secret("sk-", "abcdefghijklmnopqrstuvwxyz123456")

PUBLISHED: list[str] = []


@tool
def web_search(query: str) -> str:
    """Search the web.

    Args:
        query: What to search for.
    """

    return f"3 results for {query}"


@tool(approval=True)
def publish(title: str, api_key: str = "") -> str:
    """Publish the brief.

    Args:
        title: Title of the brief.
        api_key: Key of the publishing service.
    """

    PUBLISHED.append(title)
    return f"published {title}"


def _tool_results(call: ModelCall) -> int:
    return sum(isinstance(m, ToolMessage) for m in call.messages)


def _researcher(call: ModelCall) -> Any:
    done = _tool_results(call)
    if done == 0:
        return tool_call("web_search", {"query": "debian first release"})
    if done == 1:
        return tool_call("publish", {"title": "Debian 1993", "api_key": SECRET})
    return "researcher findings"


def _analyst(call: ModelCall) -> Any:
    if _tool_results(call) == 0:
        return tool_call("consult_peer", {"peer": "researcher", "question": "is 1993 right?"})
    return "analyst check"


def _lead(call: ModelCall) -> Any:
    done = _tool_results(call)
    if done == 0:
        return tool_call("delegate_task", {"agent": "researcher", "task": "research the topic"})
    if done == 1:
        return tool_call("delegate_task", {"agent": "analyst", "task": "check the findings"})
    return tool_call("transfer_to_writer", {"reason": "write it up"})


def desk_team() -> Team:
    """lead -> researcher (tool + approval), lead -> analyst -> researcher (consult), lead >> writer."""

    lead = Agent("lead", description="Leads.", model=FunctionChatModel(_lead))
    researcher = Agent(
        "researcher",
        description="Researches.",
        model=FunctionChatModel(_researcher),
        tools=[web_search, publish],
    )
    analyst = Agent("analyst", description="Checks.", model=FunctionChatModel(_analyst))
    writer = Agent(
        "writer", description="Writes.", model=FakeChatModel(["final summary"], repeat_last=True)
    )
    return Team(
        "desk",
        orchestrator=lead,
        members=[researcher, analyst, writer],
        peers={"analyst": ["researcher"]},
        handoffs={"lead": ["writer"]},
        checkpointer=InMemorySaver(),
    )


async def run_desk(
    team: Team,
    *,
    recorder: TraceRecorder,
    events: EventRecorder | None = None,
    reviewer: str = "alice",
    thread_id: str = "t-1",
) -> RunResult[Any]:
    """Run the team to its end, approving every request, with the recorders subscribed."""

    watching = events if events is not None else EventRecorder()
    with use_global_subscriber(recorder), use_global_subscriber(watching):
        result = await team.run("Brief on Debian", thread_id=thread_id)
        while result.is_interrupted:
            result = await team.resume(thread_id, {"action": "approve", "reviewer": reviewer})
    return result


def scrub(text: str, path: Path) -> str:
    """The rendering without what changes from run to run: the file and the times."""

    text = text.replace(str(path), "<file>")
    return re.sub(r"\b\d+(\.\d+)? (ms|s)\b", "<time>", text)
