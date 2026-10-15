"""The example blueprints validate cleanly and run offline end to end; the market_brief example
shows its roles."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from awa.agents import Agent
from awa.blueprints import Blueprint, BlueprintError, load
from awa.cli import build_parser, main
from awa.contracts.protocols import AgentLike
from awa.evals import Dataset, evaluate
from awa.models import ModelRegistry
from awa.team import Team
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, structured_output, tool_call
from awa.workflows import Sequential, Verdict

EXAMPLES_ROOT = Path(__file__).resolve().parents[2] / "examples"
EXAMPLES = EXAMPLES_ROOT / "blueprints"


@pytest.fixture
def support_tools(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("support_tools", EXAMPLES / "support_tools.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "support_tools", module)
    spec.loader.exec_module(module)
    module.REFUNDS.clear()
    return module


def tools_of(module: ModuleType) -> list[Any]:
    return [getattr(module, name) for name in module.__all__]


def test_support_triage_blueprint_has_no_issues(support_tools: ModuleType) -> None:
    blueprint = Blueprint.from_yaml(EXAMPLES / "support_triage.yaml")
    assert blueprint.check(tools=tools_of(support_tools)) == []


def test_document_review_blueprint_has_no_issues() -> None:
    assert Blueprint.from_yaml(EXAMPLES / "document_review.yaml").check(tools=[]) == []


async def test_support_triage_hands_refunds_to_an_approval_gated_agent(
    support_tools: ModuleType,
) -> None:
    team = load(
        EXAMPLES / "support_triage.yaml",
        tools=tools_of(support_tools),
        checkpointer=InMemorySaver(),
    )
    assert isinstance(team, Team)
    assert team.task_board is not None
    agents = {agent.name: agent for agent in (team.orchestrator, *team.members)}
    triage, refunds = agents["triage"], agents["refunds"]
    assert isinstance(refunds, Agent)
    refund_call = {"order_id": "1001", "amount": 49.9, "reason": "arrived broken"}
    triage_model = FakeChatModel([tool_call("transfer_to_refunds", {"reason": "refund request"})])
    refunds_model = FakeChatModel(
        [
            tool_call("lookup_order", {"order_id": "1001"}),
            tool_call("issue_refund", refund_call),
            "Your refund of 49.90 is on its way.",
        ]
    )

    with triage.override(model=triage_model), refunds.override(model=refunds_model):
        paused = await team.run("Order 1001 arrived broken, I want my money back.", thread_id="c1")
        assert paused.is_interrupted
        assert paused.interrupts[0]["value"]["tool"] == "issue_refund"
        assert support_tools.REFUNDS == []

        result = await team.resume("c1", {"action": "approve"})

    assert result.output == "Your refund of 49.90 is on its way."
    assert result.agent == "refunds"
    assert result.handoff_path == ("triage", "refunds")
    assert support_tools.REFUNDS == [{"order": "1001", "amount": 49.9, "reason": "arrived broken"}]


async def test_document_review_runs_parallel_reviewers_and_returns_a_verdict() -> None:
    workflow = load(EXAMPLES / "document_review.yaml")
    assert isinstance(workflow, Sequential)
    intake, reviews, editor = workflow.steps
    reviewers = reviews.branches  # type: ignore[attr-defined]
    assert [r.name for r in reviewers] == [
        "legal_reviewer",
        "compliance_reviewer",
        "style_reviewer",
    ]
    assert isinstance(editor, Agent)
    assert editor.output_type is not None
    verdict = {
        "verdict": "revise",
        "summary": "Liability is unlimited.",
        "findings": [{"severity": "high", "area": "legal", "recommendation": "Cap liability."}],
    }
    editor_model = FakeChatModel([structured_output(editor.output_type, verdict)])

    assert isinstance(intake, Agent)
    with (
        intake.override(model=FakeChatModel(["Summary: a supply contract."])),
        reviewers[0].override(model=FakeChatModel(["Unlimited liability in clause 7."])),
        reviewers[1].override(model=FakeChatModel(["no findings"])),
        reviewers[2].override(model=FakeChatModel(["no findings"])),
        editor.override(model=editor_model),
    ):
        result = await workflow.run("The supplier is liable for all damages without limit.")

    assert result.output.model_dump(exclude_none=True) == verdict
    editor_prompt = str(editor_model.calls[0].last_message.content)
    assert "[intake]: Summary: a supply contract." in editor_prompt
    assert "Unlimited liability in clause 7." in editor_prompt


BLUEPRINT = EXAMPLES / "market_brief.yaml"
SYSTEM_CASES = EXAMPLES_ROOT / "evals" / "market_brief_cases.yaml"
JUDGE_CASES = EXAMPLES_ROOT / "evals" / "market_brief_judge_cases.yaml"
URL = "https://en.wikipedia.org/wiki/Hybrid_search"


def system_text(call: ModelCall) -> str:
    return "\n".join(str(m.content) for m in call.messages if isinstance(m, SystemMessage))


def human_text(call: ModelCall) -> str:
    return "\n".join(str(m.content) for m in call.messages if isinstance(m, HumanMessage))


def tool_texts(call: ModelCall) -> list[str]:
    return [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]


def verdict(decision: str, passed: list[bool], feedback: str = "") -> Any:
    results = [{"id": f"c{n}", "passed": ok, "evidence": "read"} for n, ok in enumerate(passed, 1)]
    return structured_output(
        Verdict, {"decision": decision, "criteria": results, "feedback": feedback}
    )


def scripted(call: ModelCall) -> Any:
    """One scripted model for every agent of the example, recognised by its instructions."""

    system, human, results = system_text(call), human_text(call), tool_texts(call)
    if "You coordinate market briefs" in system:
        if "Zorblax" in human:
            if not results:
                return tool_call("delegate_task", {"agent": "researcher_team", "task": human})
            return "I found nothing about this; no brief was written."
        if not results:
            return tool_call("delegate_task", {"agent": "researcher_team", "task": human})
        if len(results) == 1:
            return tool_call(
                "delegate_task", {"agent": "brief_writer_checked", "task": f"Facts: {results[0]}"}
            )
        return f"Brief delivered. {results[-1]}"
    if "Search, open the sources" in system:
        if "Zorblax" in human:
            return "Nothing found."
        if not results:
            return tool_call("delegate_task", {"agent": "citation_checker", "task": "verify"})
        return f"Hybrid search combines lexical and vector retrieval. {URL}"
    if "Given a quote and a URL" in system:
        return "Verified."
    if "Write the brief only" in system:
        if "did not accept" in human:
            return f"Hybrid search combines lexical and vector retrieval {URL}"
        return "Hybrid search combines lexical and vector retrieval."
    if "You are a judge" in system:
        work = human.split("Work product to judge:")[-1]
        if "ignore the criteria" in work or "https://" not in work:
            return verdict("revise", [False, True, True], "Add the source URL after the claim.")
        return verdict("accept", [True, True, True])
    if "names no private person" in system:
        return "No personal data found."
    if "neutral and professional" in system:
        return "The tone is neutral."
    raise AssertionError(f"unscripted agent: {system[:80]}")


def registry() -> ModelRegistry:
    models = ModelRegistry()
    for profile in ("fast", "strong", "default"):
        models.register(profile, FunctionChatModel(scripted))
    return models


def test_the_example_validates_without_any_issue() -> None:
    assert Blueprint.from_yaml_file(BLUEPRINT).check() == []


def test_the_cli_validates_shows_the_roles_and_draws_the_tree(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["validate", str(BLUEPRINT)]) == 0
    capsys.readouterr()

    assert main(["show", str(BLUEPRINT)]) == 0
    shown = capsys.readouterr().out
    assert "entrypoint: lead" in shown
    assert re.search(r"fact_judge\s+judge\s+strong", shown)
    assert "brief_writer (worker)  judged by fact_judge, at most 3 rounds" in shown
    assert "reviewed once by privacy_reviewer, tone_reviewer" in shown

    assert main(["show", str(BLUEPRINT), "--mermaid"]) == 0
    mermaid = capsys.readouterr().out
    assert 'a0(["lead<br/>role: orchestrator"])' in mermaid
    assert "-.->|judge, max 3 rounds|" in mermaid
    assert mermaid.count("-.->|reviews once|") == 2


async def run_cases(path: Path, *, agent: str | None = None) -> Any:
    blueprint = Blueprint.from_yaml_file(BLUEPRINT)

    async def build() -> AgentLike:
        if agent:
            return blueprint.build_agent(agent, model_registry=registry())
        return blueprint.build(model_registry=registry())

    return await evaluate(build, Dataset.load(path), [], name=path.stem)


@pytest.mark.slow
async def test_the_system_cases_pass_on_scripted_models() -> None:
    report = await run_cases(SYSTEM_CASES)

    failed = [(r.case_id, [s.reason for s in r.scores if not s.passed]) for r in report.cases]
    assert report.pass_rate == 1.0, failed


async def test_the_judge_cases_pass_on_scripted_models() -> None:
    report = await run_cases(JUDGE_CASES, agent="fact_judge")

    failed = [(r.case_id, [s.reason for s in r.scores if not s.passed]) for r in report.cases]
    assert report.pass_rate == 1.0, failed
    assert len(report.cases) == 3


async def test_the_judge_cases_fail_when_the_judge_decides_wrongly() -> None:
    def lenient(call: ModelCall) -> Any:
        return verdict("accept", [True, True, True])

    blueprint = Blueprint.from_yaml_file(BLUEPRINT)
    models = ModelRegistry()
    for profile in ("fast", "strong", "default"):
        models.register(profile, FunctionChatModel(lenient))

    async def build() -> AgentLike:
        return blueprint.build_agent("fact_judge", model_registry=models)

    report = await evaluate(build, Dataset.load(JUDGE_CASES), [], name="lenient")

    assert report.pass_rate < 1.0
    assert {r.case_id for r in report.cases if r.status != "passed"} == {
        "revises-a-brief-without-sources",
        "ignores-an-instruction-hidden-in-the-work",
    }


def test_awa_eval_agent_evaluates_the_judge_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ModelRegistry, "_default", registry())
    args = build_parser(["eval"]).parse_args(
        ["eval", str(BLUEPRINT), str(JUDGE_CASES), "--agent", "fact_judge"]
    )

    code = args.handler(args)

    out = capsys.readouterr().out
    assert code == 0, out
    assert "verdict:accept" in out and "verdict:revise" in out
    assert "Pass rate: 100.0% (3/3 runs)" in out


def test_awa_eval_agent_names_the_known_agents_when_it_is_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ModelRegistry, "_default", registry())
    args = build_parser(["eval"]).parse_args(
        ["eval", str(BLUEPRINT), str(JUDGE_CASES), "--agent", "ghost"]
    )

    with pytest.raises(BlueprintError, match="no agent 'ghost'"):
        args.handler(args)
