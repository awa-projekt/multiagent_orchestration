"""Static checks of the public API types: `python -m mypy tests/typing` fails when one changes.

The functions are never called; mypy checks every `assert_type` in them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any, assert_type

from langchain_core.tools import BaseTool
from pydantic import BaseModel

import awa
from awa import Agent, AgentLike, RunEvent, RunResult, tool
from awa.evals import EvalReport, evaluate, evaluate_sync
from awa.runtime import RunHandle
from awa.testing import FakeChatModel, ModelCall, expect_scripts_consumed


class Invoice(BaseModel):
    number: str
    total: float


def lookup(order_id: str) -> str:
    return order_id


async def check_agent_results() -> None:
    plain = Agent("plain", model=FakeChatModel(["ok"]))
    assert_type(plain, Agent[Any, str])
    assert_type(await plain.run("hi"), RunResult[str])
    assert_type(plain.run_sync("hi"), RunResult[str])
    assert_type(plain.run_sync("hi").output, str)
    assert_type(plain.start("hi"), RunHandle[RunResult[str]])
    assert_type(plain.stream("hi"), AsyncIterator[RunEvent])
    paused = await plain.run("hi")
    assert_type(await plain.resume(paused.thread_id, {"action": "approve"}), RunResult[str])

    typed = Agent("typed", model=FakeChatModel(["ok"]), output_type=Invoice)
    assert_type(typed, Agent[Any, Invoice])
    assert_type((await typed.run("hi")).output, Invoice)
    assert_type(typed.run_sync("hi").output, Invoice)
    assert_type(await typed.start("hi").result(), RunResult[Invoice])
    assert_type(typed.clone(name="copy"), Agent[Any, Invoice])
    assert_type(typed.as_tool(), BaseTool)


def check_tool_decorator() -> None:
    assert_type(tool(lookup), BaseTool)
    assert_type(tool("find_order"), Callable[[Callable[..., Any] | BaseTool], BaseTool])
    assert_type(tool(approval=True)(lookup), BaseTool)


def check_blueprints() -> None:
    assert_type(awa.load("agents/triage.yaml"), AgentLike)


async def check_blueprints_async() -> None:
    assert_type(await awa.aload("agents/triage.yaml"), AgentLike)


async def check_evals(system: AgentLike) -> None:
    report = await evaluate(system, [])
    assert_type(report, EvalReport)
    assert_type(evaluate_sync(system, []), EvalReport)
    assert_type(report.passed(), bool)
    assert_type(report.to_markdown(), str)


def check_testing() -> None:
    with expect_scripts_consumed() as created:
        assert_type(created, list[FakeChatModel])
    model = FakeChatModel(["ok"], allow_unconsumed=True)
    assert_type(model.remaining, int)
    assert_type(model.calls, list[ModelCall])
