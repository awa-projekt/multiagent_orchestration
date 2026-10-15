"""Offline tests for `examples/04_workflows.py`."""

from __future__ import annotations

from typing import Any

from _helpers.examples import load_example, tool_results
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, structured_output, tool_call


async def test_workflow_member_researches_in_parallel_and_loops_until_approved() -> None:
    example = load_example("04_workflows")
    team = example.build_team()
    [launch_copy] = team.members
    research, refine = launch_copy.steps
    audience, competitors = research.branches
    copywriter, editor = refine.body.steps
    verdict = example.EditorVerdict

    def lead(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call(
                "delegate_task",
                {"agent": "launch_copy", "task": "Launch copy for Tidy, automatic budgeting."},
            )
        result = tool_results(call.messages)[-1]
        assert 'status="ok"' in result and "Tidy sorts every expense" in result
        return "Approved copy: Tidy sorts every expense for you. Try it free."

    copywriter_model = FakeChatModel(
        ["Tidy is a budgeting app.", "Tidy sorts every expense for you."]
    )
    editor_model = FakeChatModel(
        [
            structured_output(
                verdict,
                {
                    "approved": False,
                    "copy_text": "Tidy is a budgeting app.",
                    "feedback": "Name the benefit and add a call to action.",
                },
            ),
            structured_output(
                verdict,
                {"approved": True, "copy_text": "Tidy sorts every expense for you. Try it free."},
            ),
        ]
    )
    with (
        team.orchestrator.override(model=FunctionChatModel(lead)),
        audience.override(model=FakeChatModel(["Busy young professionals."])),
        competitors.override(model=FakeChatModel(["Rivals stress manual control."])),
        copywriter.override(model=copywriter_model),
        editor.override(model=editor_model),
    ):
        result = await team.run("We need launch copy for Tidy.")

    assert result.output == "Approved copy: Tidy sorts every expense for you. Try it free."
    first_brief = str(copywriter_model.calls[0].last_message.content)
    assert "[research]: [audience_researcher]: Busy young professionals." in first_brief
    assert "[competitor_researcher]: Rivals stress manual control." in first_brief
    second_brief = str(copywriter_model.calls[1].last_message.content)
    assert "Name the benefit and add a call to action." in second_brief
    assert str(editor_model.calls[1].last_message.content) == "Tidy sorts every expense for you."
    assert launch_copy.card.role == "workflow"
    assert refine.max_iterations == example.MAX_EDITING_ROUNDS
