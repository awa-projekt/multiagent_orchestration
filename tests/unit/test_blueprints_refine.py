"""Refinement: `describe:`, `refine:`, `refinements:` and `refine.py` hooks, and the post-build stage with its accept gate."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from langchain.agents.middleware import (
    ContextEditingMiddleware,
    ModelCallLimitMiddleware,
    ModelRequest,
)
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from test_blueprints_build_loop import CHECKED
from test_blueprints_project import BLUEPRINT as DESIGN

import awa.runtime._langgraph as engine
from awa.agents import Agent
from awa.agents._stack import with_caching
from awa.agents.middleware import RunTrackingMiddleware
from awa.blueprints import AcceptanceDesign, BlueprintDesign, BlueprintError, build_project
from awa.blueprints._project import ProjectDesign
from awa.blueprints._refine import RefineMiddleware
from awa.blueprints._refine_stage import (
    LineBatch,
    LineDraft,
    accepted_lines,
    analyze_traces,
    case_literals,
    literal_in,
    refine_stage,
    refiner_prompt,
)
from awa.blueprints.schema import Blueprint, RefineBlueprint
from awa.contracts.events import ToolCallEvent, ToolResultEvent
from awa.contracts.usage import Usage
from awa.evals.dataset import Dataset
from awa.evals.report import CaseResult, EvalReport
from awa.models import ModelRegistry
from awa.models.prompt_cache import PromptCacheMiddleware
from awa.models.resilience import ModelResilienceMiddleware
from awa.testing import FunctionChatModel, ModelCall, structured_output, tool_call


def blueprint(agent: dict[str, Any], path: Path | None = None) -> Blueprint:
    data = {"name": "desk", "agents": [{"name": "analyst", **agent}]}
    if path is None:
        return Blueprint.from_dict(data)

    file = path / "blueprint.yaml"
    file.write_text(yaml.safe_dump(data), encoding="utf-8")
    return Blueprint.from_yaml_file(file)


def request(model: Any) -> ModelRequest:
    return ModelRequest(model=model, messages=[], model_settings={})


REFINE = RefineBlueprint(
    model_kwargs={"temperature": 0.2},
    anthropic={"cache_ttl": "1h", "thinking": {"type": "enabled", "budget_tokens": 2048}},
    openai={"reasoning": {"effort": "low"}},
)


def test_a_provider_section_applies_only_to_attempts_of_that_provider() -> None:
    middleware = RefineMiddleware("analyst", REFINE)
    claude = ChatAnthropic(model="claude-sonnet-4-5", api_key="test")
    gpt = ChatOpenAI(model="gpt-5-mini", api_key="test")

    on_claude: Any = middleware.prepare(request(claude)).model
    on_gpt: Any = middleware.prepare(request(gpt)).model

    assert on_claude.thinking == {"type": "enabled", "budget_tokens": 2048}
    assert on_claude.temperature == 0.2 and claude.thinking is None  # a copy, not the original
    assert on_gpt.reasoning == {"effort": "low"} and on_gpt.temperature == 0.2
    assert not hasattr(on_gpt, "thinking")  # the fallback carries no Anthropic field


def test_a_key_the_model_class_lacks_goes_with_the_call() -> None:
    fake = FunctionChatModel(lambda call: "ok")
    prepared = RefineMiddleware("analyst", RefineBlueprint(model_kwargs={"seed": 7})).prepare(
        request(fake)
    )
    assert prepared.model is fake and prepared.model_settings == {"seed": 7}


def test_unknown_keys_fail_when_the_blueprint_loads() -> None:
    with pytest.raises(BlueprintError, match="reasonin is not a field of ChatOpenAI"):
        blueprint({"refine": {"openai": {"reasonin": {}}}})
    with pytest.raises(BlueprintError, match="'NoSuchMiddleware' is not a LangChain"):
        blueprint({"refine": {"middleware": [{"NoSuchMiddleware": {}}]}})
    with pytest.raises(BlueprintError, match="unexpected keyword argument 'bogus'"):
        blueprint({"refine": {"middleware": [{"ModelCallLimitMiddleware": {"bogus": 1}}]}})
    with pytest.raises(BlueprintError, match="cache_ttl must be 5m or 1h"):
        blueprint({"refine": {"anthropic": {"cache_ttl": "2h"}}})


def test_refine_builds_middleware_by_name_inside_the_resilience_layer() -> None:
    spec = {
        "model": "claude",
        "refine": {
            **REFINE.model_dump(),
            "middleware": [{"ModelCallLimitMiddleware": {"run_limit": 3}}],
        },
    }
    agent = blueprint(spec).build(model_registry=None)
    assert isinstance(agent, Agent)
    assert agent._prompt_cache == "1h"  # cache_ttl: 1h
    stack = with_caching(agent, [*agent._middleware, ModelResilienceMiddleware(), *_tracking()])
    kinds = [type(m) for m in stack]
    assert ModelCallLimitMiddleware in kinds
    resilience = kinds.index(ModelResilienceMiddleware)
    assert resilience < kinds.index(PromptCacheMiddleware) < kinds.index(RefineMiddleware)


async def test_server_side_context_editing_replaces_the_client_side_one_on_claude_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edits = {"edits": [{"type": "clear_tool_uses_20250919"}]}
    middleware = RefineMiddleware(
        "analyst", RefineBlueprint(anthropic={"context_management": edits})
    )
    claude = ChatAnthropic(model="claude-sonnet-4-5", api_key="test")
    on_claude: Any = middleware.prepare(request(claude)).model
    assert middleware.replaces_context_editing
    assert on_claude.context_management == edits
    assert on_claude.betas == ["context-management-2025-06-27"]  # the beta it needs

    seen: list[str] = []

    async def handler(req: ModelRequest) -> Any:
        seen.append(type(req.model).__name__)
        return "ok"

    edited: list[str] = []
    original = ContextEditingMiddleware.awrap_model_call

    async def spy(self: Any, req: ModelRequest, inner: Any) -> Any:
        edited.append(type(req.model).__name__)
        return await original(self, req, inner)

    monkeypatch.setattr(ContextEditingMiddleware, "awrap_model_call", spy)
    await middleware.awrap_model_call(request(claude), handler)
    await middleware.awrap_model_call(request(ChatOpenAI(model="gpt-5-mini", api_key="t")), handler)
    assert seen == ["ChatAnthropic", "ChatOpenAI"]
    assert edited == ["ChatOpenAI"]  # the fallback still gets the client-side clearing


def test_an_agent_with_server_side_editing_has_no_client_side_editing_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stacks: list[list[Any]] = []
    real = engine.create_agent

    def record(*args: Any, **kwargs: Any) -> Any:
        stacks.append(list(kwargs["middleware"]))
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, "create_agent", record)
    edits = {"edits": [{"type": "clear_tool_uses_20250919"}]}

    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(lambda call: "ok"))
    for refine in ({"anthropic": {"context_management": edits}}, {"openai": {"seed": 1}}):
        agent = blueprint({"model": "default", "refine": refine}).build(model_registry=registry)
        assert isinstance(agent, Agent)
        agent._compiled_graph(None)
    with_server, without = stacks
    assert not any(isinstance(m, ContextEditingMiddleware) for m in with_server)
    assert any(isinstance(m, ContextEditingMiddleware) for m in without)


def _tracking() -> list[Any]:
    return [RunTrackingMiddleware()]


async def test_describe_and_refinements_reach_the_model(tmp_path: Path) -> None:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "analyst.md").write_text("Answer from the ledger.", encoding="utf-8")
    seen: list[str] = []

    def respond(call: ModelCall) -> str:
        first = call.messages[0]
        seen.append(str(first.content) if isinstance(first, SystemMessage) else "")
        return "done"

    spec = {
        "model": "default",
        "describe": {"instructions_file": "prompts/analyst.md"},
        "refinements": [{"text": "Call lookup once per order.", "evidence": "2 duplicates"}],
    }

    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(respond))
    agent = blueprint(spec, tmp_path).build(model_registry=registry)
    await agent.run("Hello")
    assert "Answer from the ledger." in seen[0]
    assert "## Learned from test runs\n- Call lookup once per order." in seen[0]


def test_instructions_and_an_instructions_file_exclude_each_other() -> None:
    with pytest.raises(BlueprintError, match="not both"):
        blueprint({"instructions": "x", "describe": {"instructions_file": "a.md"}})


@pytest.mark.parametrize("file", ["../secret.txt", "/etc/passwd", "C:/secret.txt", "a/../../b"])
def test_an_instructions_file_stays_inside_the_blueprint_folder(file: str) -> None:
    with pytest.raises(BlueprintError, match="must be relative and stay inside"):
        blueprint({"describe": {"instructions_file": file}})


def test_tool_docs_replace_the_description_the_model_reads() -> None:
    def lookup(order: str) -> str:
        """Look an order up."""
        return order

    spec = {"tools": ["lookup"], "describe": {"tool_docs": {"lookup": "The order ledger."}}}
    agent = blueprint(spec).build(tools=[lookup])
    assert isinstance(agent, Agent)
    assert [tool.description for tool in agent._tools] == ["The order ledger."]
    with pytest.raises(BlueprintError, match="does not have: other"):
        blueprint({**spec, "describe": {"tool_docs": {"other": "x"}}}).build(tools=[lookup])


HOOKS = """
from awa.refine import refine_agent, refine_model


@refine_model("analyst")
def tune(model, ctx):
    return model.model_copy(update={"name": ctx.project})


@refine_agent()
def extra(middleware, ctx):
    from langchain.agents.middleware import ModelCallLimitMiddleware

    return [*middleware, ModelCallLimitMiddleware(run_limit=2)]
"""


def test_refine_py_hooks_take_and_return_langchain_objects(tmp_path: Path) -> None:
    (tmp_path / "refine.py").write_text(HOOKS, encoding="utf-8")

    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(lambda call: "ok"))
    loaded = blueprint({"model": "default"}, tmp_path)
    with pytest.raises(BlueprintError, match="allow_imports=True"):
        loaded.build(model_registry=registry)
    agent = loaded.build(model_registry=registry, allow_imports=True)
    assert isinstance(agent, Agent)
    assert isinstance(agent._model, FunctionChatModel) and agent._model.name == "desk"
    assert any(isinstance(m, ModelCallLimitMiddleware) for m in agent._middleware)


def test_a_hook_that_returns_no_chat_model_is_a_blueprint_error(tmp_path: Path) -> None:
    source = (
        "from awa.refine import refine_model\n\n@refine_model()\n"
        "def tune(model, ctx):\n    return model.bind(max_tokens=5)\n"
    )
    (tmp_path / "refine.py").write_text(source, encoding="utf-8")

    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(lambda call: "ok"))
    with pytest.raises(BlueprintError, match="must return a chat model"):
        blueprint({"model": "default"}, tmp_path).build(model_registry=registry, allow_imports=True)


def test_the_refine_section_follows_the_provider_of_the_model() -> None:
    refine = RefineBlueprint(anthropic={"max_tokens": 1}, openai={"seed": 2}, google={"top_k": 3})

    assert refine.section("anthropic") == {"max_tokens": 1}
    assert refine.section("openai") == refine.section("azure_openai") == {"seed": 2}
    assert refine.section("google_genai") == {"top_k": 3}
    assert refine.section("groq") == refine.section(None) == {}


BLUEPRINT = Blueprint.from_dict(
    {
        "name": "desk",
        "agents": [
            {
                "name": "clerk",
                "description": "Answers order questions from the ledger.",
                "tools": ["lookup", "builtin:web_search", "builtin:calculator"],
            }
        ],
    }
)
EVALS = Dataset(
    cases=[
        {"id": "status", "input": "Where is order A-1002 for Mrs Huber?", "expected": "shipped"},
        {"id": "late", "input": "Order 4711 is late", "expected": "'apologise politely'"},
    ]
)


def run(
    case: str, calls: list[tuple[str, dict[str, Any]]], *, passed: bool = True, tokens: int = 100
) -> CaseResult:
    events = [
        ToolCallEvent(
            run_id="r",
            root_run_id="r",
            agent="clerk",
            depth=0,
            tool_name=name,
            call_id=str(i),
            arguments=args,
        )
        for i, (name, args) in enumerate(calls)
    ]
    result = CaseResult(
        case_id=case,
        status="passed" if passed else "failed",
        agent="clerk",
        tool_calls=[name for name, _ in calls],
        usage=Usage(requests=1, input_tokens=tokens),
    )
    return result.attach(trace=None, events=events)


def report(*runs: CaseResult, repetitions: int = 1) -> EvalReport:
    return EvalReport(
        name="e",
        system="s",
        dataset="d",
        started_at=datetime.now(UTC),
        cases=list(runs),
        repetitions=repetitions,
    )


RAW = report(
    run("status", [("lookup", {"order": "A-1002"}), ("lookup", {"order": "A-1002"})]),
    run("late", [("lookup", {"order": "4711"}), ("calculator", {"expression": "1+1"})]),
)


def test_traces_give_findings() -> None:
    analysis = analyze_traces(BLUEPRINT, RAW, flaky=["late"])

    kinds = {(f.kind, f.text) for f in analysis.findings}
    assert (
        "duplicate",
        "lookup was called again with identical arguments in 1 of 2 run(s)",
    ) in kinds
    assert ("first", "lookup was the first tool call in all 2 runs") in kinds
    assert ("flaky", "1 case(s) passed only on a re-run") in kinds
    assert not any(kind == "budget" for kind, _ in kinds)  # short runs need no budget
    assert all("A-1002" not in f.text and "4711" not in f.text for f in analysis.findings)


def test_case_values_are_literals_unless_the_blueprint_has_them() -> None:
    literals = case_literals(EVALS, BLUEPRINT)
    assert {"a-1002", "4711", "huber", "apologise politely"} <= literals
    assert "order" not in literals  # the blueprint itself speaks of orders
    assert literal_in("Call lookup once per order, then answer.", literals) is None
    assert literal_in("For Mrs Huber look the order up first.", literals) == "huber"
    assert literal_in("Check order 4711 twice.", literals) == "4711"


def test_case_values_with_umlauts_and_backslashes_stay_readable() -> None:
    evals = Dataset(
        cases=[
            {"id": "x", "input": "Mrs Gr\u00f6\u00dfer files to D:\\tmp\\shared", "expected": "ok"}
        ]
    )

    assert "gr\u00f6\u00dfer" in case_literals(evals, BLUEPRINT)


async def test_accepted_lines_carry_their_evidence_and_measurement() -> None:
    prompts: list[str] = []

    async def ask(prompt: str) -> LineBatch:
        prompts.append(prompt)
        return LineBatch(
            lines=[
                LineDraft(agent="clerk", finding="f1", text="Call lookup once per order."),
                LineDraft(agent="clerk", finding="f2", text="Start with lookup for A-1002."),
                LineDraft(agent="clerk", finding="f9", text="Unfounded line."),
                LineDraft(agent="ghost", finding="f1", text="Other agent."),
            ]
        )

    async def evaluate(design: Any, k: int) -> EvalReport:
        assert k == 2
        assert [r.text for r in design.blueprint.agent("clerk").refinements] == [
            "Call lookup once per order."
        ]
        good = [
            run("status", [("lookup", {})], tokens=90),
            run("late", [("lookup", {})], tokens=90),
        ]
        return report(*good, *[r.model_copy() for r in good], repetitions=2)

    project = _project()
    kept, measured, outcome = await refine_stage(project, RAW, ["late"], ask=ask, evaluate=evaluate)

    assert len(prompts) == 1 and "A-1002" not in prompts[0]
    assert outcome.accepted and outcome.lines == {"clerk": ["Call lookup once per order."]}
    line = kept.blueprint.agent("clerk").refinements[0]
    assert (line.evidence, line.pass_hat_k, line.input_tokens, line.raw_input_tokens) == (
        "lookup was called again with identical arguments in 1 of 2 run(s)",
        1.0,
        90,
        100,
    )
    assert measured.repetitions == 2


async def test_a_refinement_that_costs_more_tokens_is_discarded() -> None:
    async def ask(prompt: str) -> LineBatch:
        return LineBatch(lines=[LineDraft(agent="clerk", finding="f1", text="Call lookup once.")])

    async def evaluate(design: Any, k: int) -> EvalReport:
        runs = [run("status", [], tokens=500), run("late", [], tokens=500)]
        return report(*runs, *runs, repetitions=2)

    project = _project()
    kept, measured, outcome = await refine_stage(project, RAW, [], ask=ask, evaluate=evaluate)
    assert not outcome.accepted and kept is project and measured is RAW
    assert outcome.measurement is not None and outcome.measurement.input_tokens == 500


async def test_nothing_to_refine_makes_no_model_call() -> None:
    quiet = report(run("status", [("lookup", {"order": "1"})]), run("late", [("calculator", {})]))
    plain = Blueprint.from_dict(
        {"name": "desk", "agents": [{"name": "clerk", "tools": ["lookup"]}]}
    )

    async def never(*_: Any) -> Any:
        raise AssertionError("no call expected")

    project = dataclasses.replace(_project(), blueprint=plain)
    kept, _, outcome = await refine_stage(project, quiet, [], ask=never, evaluate=never)
    assert kept is project and outcome.skipped == "the traces show nothing to refine"


def _project() -> Any:
    return ProjectDesign(process="Orders.", blueprint=BLUEPRINT, evals=EVALS, connections=[])


async def test_build_project_keeps_a_refinement_that_saves_work(tmp_path: Path) -> None:
    prompts: list[str] = []

    def respond(call: ModelCall) -> Any:
        first = call.messages[0]
        system = str(first.content) if isinstance(first, SystemMessage) else ""
        if system.startswith("You write the acceptance tests"):
            return structured_output(AcceptanceDesign, CHECKED)
        if system.startswith("You design multi-agent systems"):
            return structured_output(BlueprintDesign, DESIGN)
        if system.startswith("You refine the instructions"):
            text = str(call.last_message.content)
            finding = next(line for line in text.splitlines() if "(duplicate)" in line)
            return structured_output(
                LineBatch,
                {
                    "lines": [
                        {
                            "agent": "desk",
                            "finding": finding.split()[1],
                            "text": "Call orders_query once per question.",
                        }
                    ]
                },
            )
        prompts.append(system)
        request = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
        results = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
        if not results:
            return tool_call("orders_query", {"query": "SELECT status FROM orders"})
        if "Refund" in request:
            if len(results) == 1:
                return tool_call("needs_approval", {"amount": 120})
            statement = "UPDATE orders SET status = 'refunded' WHERE order_id = '1003'"
            return tool_call("orders_execute", {"statement": statement})
        if len(results) == 1 and "Learned from test runs" not in system:
            return tool_call("orders_query", {"query": "SELECT status FROM orders"})  # again
        return f"Order 1002: {results[-1]}"

    report = await build_project(
        "Orders.", out_dir=tmp_path / "desk", model=FunctionChatModel(respond), environment=None
    )

    assert report.passed and report.refinement is not None
    assert report.refinement["accepted"], report.refinement
    assert report.refinement["lines"] == {"desk": ["Call orders_query once per question."]}
    assert report.stage_requests["refine"] >= 1 + 2 * 2
    assert any(
        "## Learned from test runs\n- Call orders_query once per question." in p for p in prompts
    )
    written = (tmp_path / "desk" / "blueprint.yaml").read_text(encoding="utf-8")
    assert "refinements:" in written and "Call orders_query once per question." in written
    assert report.eval_report is not None and report.eval_report.repetitions == 2


async def test_the_first_tool_alone_is_no_reason_for_a_model_call() -> None:
    steady = report(run("status", [("lookup", {"order": "1"})]), run("late", [("lookup", {})]))
    plain = Blueprint.from_dict(
        {"name": "desk", "agents": [{"name": "clerk", "tools": ["lookup"]}]}
    )

    async def never(*_: Any) -> Any:
        raise AssertionError("no call expected")

    project = dataclasses.replace(_project(), blueprint=plain)
    analysis = analyze_traces(plain, steady)
    assert [f.kind for f in analysis.findings] == ["first"] and not analysis.actionable
    kept, _, outcome = await refine_stage(project, steady, [], ask=never, evaluate=never)
    assert kept is project and outcome.skipped == "the traces show nothing to refine"


def test_failed_tool_calls_give_a_finding_and_the_matchers_missing_sources() -> None:
    sql = Blueprint.from_dict(
        {
            "name": "desk",
            "auto": True,
            "agents": [
                {"name": "clerk", "description": "Answers questions from the SQL database."}
            ],
        }
    )
    failing = run("status", [("lookup", {"order": "1"})])
    error = ToolResultEvent(
        run_id="r",
        root_run_id="r",
        agent="clerk",
        depth=0,
        tool_name="lookup",
        call_id="0",
        content="ValueError: no such table",
        is_error=True,
    )
    failing.attach(trace=None, events=[*failing.events, error])

    analysis = analyze_traces(sql, report(failing))

    assert ("errors", "lookup failed 1 time(s)") in {(f.kind, f.text) for f in analysis.findings}
    (suggestion,) = analysis.suggest["clerk"]
    assert suggestion.startswith("builtin:sql_query: ") and "toolkits.sql" in suggestion
    steady = analyze_traces(sql, report(run("status", [("lookup", {"order": "1"})])))
    assert not steady.suggest  # no trouble, no suggestion


def test_an_agent_that_runs_again_in_a_loop_reads_afresh_and_that_is_no_duplicate() -> None:
    looped = run("status", [("lookup", {"order": "1"}), ("lookup", {"order": "1"})])
    events = [e.model_copy(update={"run_id": f"round-{i}"}) for i, e in enumerate(looped.events)]
    looped.attach(trace=None, events=events)  # two runs of the agent, one call each

    analysis = analyze_traces(BLUEPRINT, report(looped))

    assert not any(f.kind == "duplicate" for f in analysis.findings)


def test_lines_on_context_findings_are_dropped_and_repeated_ids_stripped() -> None:
    analysis = analyze_traces(BLUEPRINT, RAW)
    by_kind = {f.kind: f.id for f in analysis.findings}
    batch = LineBatch(
        lines=[
            LineDraft(agent="clerk", finding=by_kind["first"], text="Call lookup first."),
            LineDraft(
                agent="clerk",
                finding=by_kind["duplicate"],
                text=f"Call lookup once per order. ({by_kind['duplicate']})",
            ),
        ]
    )

    kept, rejected = accepted_lines(batch, analysis, literals=set())

    assert [line.text for line in kept["clerk"]] == ["Call lookup once per order."]
    assert rejected == ["clerk: Call lookup first. (rests on a context finding only)"]
    assert f"- {by_kind['first']} (context): " in refiner_prompt(BLUEPRINT, analysis)


def test_a_long_run_without_waste_is_context_only() -> None:
    items = [("lookup", {"order": str(i)}) for i in range(8)]  # one call per item
    analysis = analyze_traces(BLUEPRINT, report(run("status", items), run("late", items)))

    assert {f.kind for f in analysis.findings} == {"first", "budget"}
    assert not analysis.actionable
