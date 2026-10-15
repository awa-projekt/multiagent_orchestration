"""Graphs, control flow and graph errors (`langgraph.graph`, `langgraph.types`, `langgraph.errors`)."""

from __future__ import annotations

from typing import Any, TypeAlias

from langgraph._internal._constants import CONFIG_KEY_SCRATCHPAD
from langgraph.channels import BaseChannel
from langgraph.errors import GraphBubbleUp, GraphInterrupt, GraphRecursionError, NodeTimeoutError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.graph.state import CompiledStateGraph
from langgraph.managed import RemainingSteps
from langgraph.pregel import Pregel
from langgraph.runtime import Runtime
from langgraph.types import (
    Checkpointer,
    Command,
    Interrupt,
    RetryPolicy,
    Send,
    StateSnapshot,
    TracePolicy,
    interrupt,
)

CompiledGraph: TypeAlias = CompiledStateGraph[Any, Any, Any, Any]
"""A compiled LangGraph graph with its state type parameters erased."""


__all__ = [
    "CONFIG_KEY_SCRATCHPAD",
    "END",
    "REMOVE_ALL_MESSAGES",
    "START",
    "BaseChannel",
    "Checkpointer",
    "Command",
    "CompiledGraph",
    "CompiledStateGraph",
    "GraphBubbleUp",
    "GraphInterrupt",
    "GraphRecursionError",
    "Interrupt",
    "NodeTimeoutError",
    "Pregel",
    "RemainingSteps",
    "RetryPolicy",
    "Runtime",
    "Send",
    "StateGraph",
    "StateSnapshot",
    "TracePolicy",
    "interrupt",
]
