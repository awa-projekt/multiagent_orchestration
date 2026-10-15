"""`resume` takes the `thread_id` of a result as it is, and refuses `None` like a thread that waits for nothing.

Before: `resume(None, ...)` started a new run on a fresh thread (the tool call ran again) and the
annotation `thread_id: str` rejected `result.thread_id` (typed `str | None`) under mypy.
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from awa import Agent, Sequential, Team
from awa.agents.graph_agent import GraphAgent
from awa.errors import NoPendingInterruptError
from awa.team import AgentNetwork
from awa.testing import FakeChatModel


def agent(name: str) -> Agent[Any, str]:
    return Agent(name, model=FakeChatModel(["ok"]), checkpointer=InMemorySaver())


def graph_agent() -> GraphAgent:
    graph: StateGraph[MessagesState] = StateGraph(MessagesState)
    graph.add_node("echo", lambda state: {"messages": []})
    graph.add_edge(START, "echo")
    graph.add_edge("echo", END)
    return GraphAgent(graph.compile(checkpointer=InMemorySaver()), name="graph")


SYSTEMS = {
    "agent": lambda: agent("solo"),
    "graph": graph_agent,
    "team": lambda: Team(
        "desk", orchestrator=agent("lead"), members=[agent("m")], checkpointer=InMemorySaver()
    ),
    "network": lambda: AgentNetwork(
        "net", lead=agent("boss"), teammates=[agent("mate")], checkpointer=InMemorySaver()
    ),
    "workflow": lambda: Sequential("seq", [agent("a")], checkpointer=InMemorySaver()),
}


@pytest.mark.parametrize("kind", sorted(SYSTEMS))
async def test_resume_without_a_thread_is_refused(kind: str) -> None:
    system = SYSTEMS[kind]()

    with pytest.raises(NoPendingInterruptError, match="None|no thread to resume"):
        await system.resume(None, {"action": "approve"})
