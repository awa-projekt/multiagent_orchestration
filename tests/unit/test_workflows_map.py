"""`Map` and workflow loops that end cleanly."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from awa._internal.upstream.checkpoint import InMemorySaver
from awa._internal.upstream.graph import interrupt
from awa.agents import Agent
from awa.blueprints import Blueprint
from awa.contracts import AgentCard, AgentInput, RunResult, UsageLimits
from awa.errors import AgentRunError
from awa.models import ModelRegistry
from awa.runtime.context import new_run_id
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, structured_output, tool_call
from awa.workflows import Checked, Loop, Map, Sequential, Verdict
from awa.workflows.map import split_items


@dataclass
class Worker:
    """An `AgentLike` double: `reply` maps the part input to the output."""

    name: str
    reply: Callable[[str], Any]
    delay: float = 0.0
    inputs: list[str] = field(default_factory=list)
    running: int = 0
    peak: int = 0

    @property
    def card(self) -> AgentCard:
        return AgentCard(name=self.name, description="worker double")

    async def run(
        self,
        input: AgentInput,
        *,
        deps: Any = None,
        usage_limits: UsageLimits | None = None,
        thread_id: str | None = None,
        history: Any = None,
    ) -> RunResult[Any]:
        assert isinstance(input, str)
        self.inputs.append(input)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(self.delay)
            output = self.reply(input)
        finally:
            self.running -= 1
        text = output if isinstance(output, str) else json.dumps(output)
        return RunResult(run_id=new_run_id(), agent=self.name, output=output, text=text)


def test_split_items_reads_arrays_single_list_objects_and_lines() -> None:
    assert split_items('["a", {"b": 1}]') == ["a", {"b": 1}]
    assert split_items('{"invoices": [1, 2], "note": "x"}') == [1, 2]
    assert split_items("one\n\n two \n") == ["one", "two"]
    assert split_items("") == []


async def test_each_item_gets_one_result_in_input_order() -> None:
    worker = Worker("upper", lambda text: text.upper(), delay=0.01)
    result = await Map("shout", worker).run('["c", "a", "b"]')

    assert result.output == ["C", "A", "B"]
    assert result.text == "[item 1]: C\n\n[item 2]: A\n\n[item 3]: B"
    assert result.metadata["missing"] == []
    assert result.metadata["items"] == 3
    assert sorted(worker.inputs) == ["a", "b", "c"]


async def test_parts_run_concurrently_and_max_concurrency_bounds_them() -> None:
    free = Worker("free", lambda text: text, delay=0.05)
    await Map("free-map", free).run("\n".join("abcdef"))
    assert free.peak == 6

    bounded = Worker("bounded", lambda text: text, delay=0.02)
    result = await Map("bounded-map", bounded, max_concurrency=2).run("\n".join("abcdef"))
    assert bounded.peak == 2
    assert result.output == list("abcdef")


async def test_parts_of_several_items_take_a_json_array_and_return_a_list() -> None:
    worker = Worker("lengths", lambda text: [len(item) for item in json.loads(text)])
    result = await Map("lengths-map", worker, part_size=2).run('["a", "bb", "ccc"]')

    assert result.output == [1, 2, 3]
    assert sorted(worker.inputs) == ['["a","bb"]', '["ccc"]']


async def test_a_part_with_the_wrong_number_of_results_is_retried_once() -> None:
    calls: list[str] = []

    def reply(text: str) -> Any:
        calls.append(text)
        items = json.loads(text)
        # The first answer for this part drops one result; the retry is complete.
        if "x" in items and calls.count(text) == 1:
            return items[:-1]
        return items

    worker = Worker("echo", reply)
    result = await Map("echo-map", worker, part_size=2).run('["x", "y", "z"]')

    assert result.output == ["x", "y", "z"]
    assert result.metadata["missing"] == []
    assert result.metadata["rounds"] == 2
    assert calls.count('["x","y"]') == 2


async def test_items_still_failing_after_the_retry_are_reported() -> None:
    def reply(text: str) -> str:
        if text == "bad":
            raise ValueError("cannot read it")
        return text

    worker = Worker("reader", reply)
    result = await Map("read-map", worker).run("good\nbad\nfine")

    assert result.output == ["good", None, "fine"]
    assert result.metadata["missing"] == [2]
    assert result.metadata["errors"] == {"2": "ValueError: cannot read it"}
    assert "[item 2]: (no result: ValueError: cannot read it)" in result.text
    assert worker.inputs.count("bad") == 2
    assert worker.inputs.count("good") == 1


async def test_every_item_failing_raises() -> None:
    def reply(text: str) -> str:
        raise ValueError("down")

    with pytest.raises(AgentRunError, match="Every item"):
        await Map("down-map", Worker("down", reply)).run("a\nb")


async def test_an_empty_list_gives_an_empty_result_without_runs() -> None:
    worker = Worker("idle", lambda text: text)
    result = await Map("idle-map", worker).run("[]")

    assert result.output == []
    assert worker.inputs == []


async def test_map_feeds_a_following_step() -> None:
    worker = Worker("upper", lambda text: text.upper())
    writer = Worker("writer", lambda text: f"summary of {text.count('[item')} items")
    result = await Sequential("pipeline", [Map("each", worker), writer]).run("a\nb")

    assert result.text == "summary of 2 items"


async def test_resume_runs_only_the_part_that_paused() -> None:
    @dataclass
    class Approving(Worker):
        async def run(self, input: AgentInput, **kwargs: Any) -> RunResult[Any]:
            assert isinstance(input, str)
            self.inputs.append(input)
            if input == "needs-ok":
                decision = interrupt({"question": "approve?"})
                return RunResult(
                    run_id=new_run_id(), agent=self.name, output=decision, text=decision
                )
            return RunResult(run_id=new_run_id(), agent=self.name, output=input, text=input)

    worker = Approving("approver", lambda text: text)
    workflow = Map("approve-map", worker, checkpointer=InMemorySaver())
    paused = await workflow.run("a\nneeds-ok\nb", thread_id="t1")
    assert paused.is_interrupted

    resumed = await workflow.resume("t1", "approved")
    assert resumed.output == ["a", "approved", "b"]
    assert worker.inputs.count("a") == 1
    assert worker.inputs.count("b") == 1


def test_invalid_settings_raise() -> None:
    worker = Worker("w", lambda text: text)
    with pytest.raises(ValueError, match="part_size"):
        Map("m", worker, part_size=0)
    with pytest.raises(ValueError, match="max_concurrency"):
        Map("m", worker, max_concurrency=0)


# --------------------------------------------------------------------------- real agents, blueprints


@tool
async def look_up(name: str) -> str:
    """Look up a name."""

    await asyncio.sleep(0.01)
    return f"{name}: found"


def _two_lookups_then_answer(call: ModelCall) -> Any:
    found = [m.content for m in call.messages if isinstance(m, ToolMessage)]
    if not found:
        item = str(call.last_message.content)
        return [tool_call("look_up", {"name": item}), tool_call("look_up", {"name": item + "-b"})]
    return " / ".join(str(text) for text in found)


async def test_real_agent_with_parallel_tool_calls_under_max_concurrency_one() -> None:
    agent = Agent("reader", model=FunctionChatModel(_two_lookups_then_answer), tools=[look_up])
    result = await asyncio.wait_for(Map("read", agent, max_concurrency=1).run("a\nb\nc"), 30)

    assert result.output == [
        "a: found / a-b: found",
        "b: found / b-b: found",
        "c: found / c-b: found",
    ]


def _map_blueprint(**step: Any) -> Blueprint:
    return Blueprint.from_dict(
        {
            "name": "invoices",
            "agents": [
                {"name": "reader", "model": "reader"},
                {"name": "writer", "model": "writer"},
            ],
            "workflow": {"sequential": [{"map": "reader", **step}, "writer"]},
        }
    )


async def test_blueprint_map_step_builds_and_runs() -> None:
    models = ModelRegistry()
    models.register("reader", FunctionChatModel(lambda call: f"read {call.last_message.content}"))
    writer = FakeChatModel(["report"])
    models.register("writer", writer)
    bp = _map_blueprint(max_concurrency=2)

    assert [issue for issue in bp.check() if issue.severity == "error"] == []
    workflow = bp.build(model_registry=models)
    assert isinstance(workflow, Sequential)
    step = workflow.steps[0]
    assert isinstance(step, Map)
    assert step.name == "map-1"
    result = await workflow.run('["inv-1", "inv-2"]')

    assert result.text == "report"
    prompt = str(writer.calls[0].last_message.content)
    assert "[item 1]: read inv-1" in prompt
    assert "[item 2]: read inv-2" in prompt


def test_blueprint_map_step_is_checked_and_drawn() -> None:
    unknown = Blueprint.from_dict(
        {"name": "x", "agents": [{"name": "writer"}], "workflow": {"map": "nobody"}}
    )
    assert any(
        issue.path == "workflow.map" and issue.severity == "error" for issue in unknown.check()
    )

    mermaid = _map_blueprint(part_size=5).to_mermaid()
    assert 'n1[["for parts of 5"]]' in mermaid
    assert "n1 --> n2" in mermaid
    assert "input --> n1" in mermaid


RUBRIC = ["Every claim has a URL."]


class Draft(BaseModel):
    text: str
    done: bool


def verdict(decision: str, passed: bool = False, feedback: str = "") -> Any:
    return structured_output(
        Verdict,
        {
            "decision": decision,
            "criteria": [{"id": "c1", "passed": passed, "evidence": "looked"}],
            "feedback": feedback,
        },
    )


def writer(*texts: str) -> tuple[Agent[Any, Any], FakeChatModel]:
    model = FakeChatModel(list(texts))
    return Agent("writer", model=model), model


def judge(*script: Any) -> Agent[Any, Any]:
    return Agent("judge", model=FakeChatModel(list(script)), output_type=Verdict)


# ---------------------------------------------------------------- step budget


async def test_loop_ends_with_step_limit_instead_of_recursion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, model = writer("one", "two")
    loop = Loop("refine", body, until=lambda _: False, max_iterations=5)
    monkeypatch.setattr(loop, "_recursion_limit", lambda: 3)

    result = await loop.run("go")

    assert result.metadata["stopped_reason"] == "step_limit"
    assert result.metadata["iterations"] == 2
    assert result.text == "two"
    assert result.metadata["stop"]["bound"] == "recursion_limit"
    assert result.metadata["stop"]["stop_reason"] == "stopped:steps"
    assert len(model.calls) == 2


async def test_checked_ends_exhausted_before_the_recursion_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, model = writer("a")
    checked = Checked(
        "unit", body, judge=judge(verdict("revise", feedback="no")), rubric=RUBRIC, max_iterations=3
    )
    monkeypatch.setattr(checked, "_recursion_limit", lambda: 4)

    result = await checked.run("t")

    assert result.metadata["status"] == "exhausted"
    assert result.metadata["stop_reason"] == "step_limit"
    assert result.metadata["stop"]["bound"] == "recursion_limit"
    assert result.metadata["rounds"] == 1
    assert len(model.calls) == 1


# ---------------------------------------------------------------- typed stop signal


async def test_loop_stops_on_a_typed_output_field() -> None:
    model = FakeChatModel(
        [
            structured_output(Draft, {"text": "rough", "done": False}),
            structured_output(Draft, {"text": "final", "done": True}),
        ]
    )
    editor = Agent("editor", model=model, output_type=Draft)

    result = await Loop("refine", editor, until="done", max_iterations=3).run("draft it")

    assert result.metadata == {"iterations": 2, "stopped_reason": "condition"}
    assert isinstance(result.output, Draft) and result.output.text == "final"


def test_loop_rejects_an_unknown_output_field() -> None:
    editor = Agent("editor", model=FakeChatModel(["x"]), output_type=Draft)
    with pytest.raises(ValueError, match="names no field"):
        Loop("refine", editor, until="approved", max_iterations=3)
    plain = Agent("plain", model=FakeChatModel(["x"]))
    with pytest.raises(ValueError, match="names no field"):
        Loop("refine", plain, until="done", max_iterations=3)


# ---------------------------------------------------------------- escalation pause


async def test_escalation_without_checkpointer_keeps_the_status() -> None:
    body, _ = writer("draft")
    checked = Checked(
        "unit", body, judge=judge(verdict("escalate", feedback="legal")), rubric=RUBRIC
    )

    result = await checked.run("t")

    assert result.metadata["status"] == "escalated"
    assert not result.interrupts


async def test_escalation_pauses_and_a_revise_runs_the_worker_again() -> None:
    body, model = writer("first", "second")
    checked = Checked(
        "unit",
        body,
        judge=judge(verdict("escalate", feedback="needs a lawyer"), verdict("accept", True)),
        rubric=RUBRIC,
        checkpointer=InMemorySaver(),
    )

    paused = await checked.run("t")

    assert paused.is_interrupted and paused.thread_id is not None
    request = paused.interrupts[0]["value"]
    assert request["type"] == "checked_escalation"
    assert request["reason"] == "needs a lawyer" and request["draft"] == "first"

    result = await checked.resume(
        paused.thread_id, {"decision": "revise", "feedback": "drop the contract clause"}
    )

    assert result.metadata["status"] == "accepted"
    assert result.text == "second"
    assert result.metadata["rounds"] == 2
    person = result.metadata["verdicts"][1]
    assert person["by"] == "person" and person["decision"] == "revise"
    assert "drop the contract clause" in str(model.calls[1].last_message.content)


async def test_escalation_accept_and_reject_by_a_person() -> None:
    for answer, status in (("accept", "accepted"), ({"decision": "reject"}, "rejected")):
        body, model = writer("only")
        checked = Checked(
            "unit",
            body,
            judge=judge(verdict("escalate", feedback="unsure")),
            rubric=RUBRIC,
            checkpointer=InMemorySaver(),
        )
        paused = await checked.run("t")
        assert paused.thread_id is not None

        result = await checked.resume(paused.thread_id, answer)

        assert result.metadata["status"] == status
        assert len(model.calls) == 1
        if status == "rejected":
            assert result.text.startswith("REJECTED after 1 round(s)")
        else:
            assert result.text == "only"


async def test_an_invalid_answer_is_asked_again() -> None:
    body, _ = writer("only")
    checked = Checked(
        "unit",
        body,
        judge=judge(verdict("escalate", feedback="unsure")),
        rubric=RUBRIC,
        checkpointer=InMemorySaver(),
    )
    paused = await checked.run("t")
    assert paused.thread_id is not None

    again = await checked.resume(paused.thread_id, {"decision": "maybe"})

    assert again.is_interrupted
    assert "not a decision" in again.interrupts[0]["value"]["error"]
    result = await checked.resume(paused.thread_id, "accept")
    assert result.metadata["status"] == "accepted"


async def test_an_item_or_branch_without_progress_fails_alone() -> None:
    from awa.workflows import Parallel

    @tool
    def lookup(key: str) -> str:
        """Look a key up."""
        return "pending"

    def respond(call: ModelCall) -> Any:
        text = str(next(m for m in call.messages if m.type == "human").content)
        return tool_call("lookup", {"key": "x"}) if "bad" in text else f"result for {text}"

    def worker(name: str) -> Agent[Any, Any]:
        return Agent(name, model=FunctionChatModel(respond), tools=[lookup])

    mapped = await Map("each", worker("worker")).run(json.dumps(["good-1", "bad-2"]))
    assert mapped.output == ["result for good-1", None]
    assert "NoProgressError" in mapped.metadata["errors"]["2"]

    stuck = Agent(
        "stuck",
        model=FunctionChatModel(lambda call: tool_call("lookup", {"key": "x"})),
        tools=[lookup],
    )
    both = await Parallel("both", [worker("fine"), stuck], fail_fast=False).run("good")
    assert both.output == {"fine": "result for good"}
    assert "NoProgressError" in both.metadata["errors"]["stuck"]
