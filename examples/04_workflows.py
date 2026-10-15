"""Deterministic workflows: Sequential, Parallel and Loop, used as a team member.

The launch-copy workflow researches the audience and the competition in
parallel, then loops a copywriter and an editor until the editor approves
(at most three rounds). The control flow is fixed Python; only the content
comes from models. The whole workflow is one member of a team, so the
marketing lead can delegate to it like to any agent.

Run:
    pip install -e ".[anthropic]"
    export ANTHROPIC_API_KEY=...
    python examples/04_workflows.py
    # No model key? Run its offline tests instead:
    #   python -m pytest tests/examples/test_04_workflows.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from _support import require_model_credentials
from awa import Agent, Loop, Parallel, RunResult, Sequential, Team

MAX_EDITING_ROUNDS = 3


class EditorVerdict(BaseModel):
    """The editor's decision on one version of the copy."""

    approved: bool = Field(description="True if the copy can be published as is.")
    copy_text: str = Field(description="The copy, with small edits applied.")
    feedback: str = Field(default="", description="What the copywriter must change, if anything.")


def is_approved(result: RunResult[Any]) -> bool:
    """Stop the editing loop once the editor approves."""

    return isinstance(result.output, EditorVerdict) and result.output.approved


def revision_brief(result: RunResult[Any]) -> str:
    """Turn a rejected version and its feedback into the copywriter's next brief."""

    verdict = EditorVerdict.model_validate(result.output)
    return (
        "Revise this launch copy.\n\n"
        f"Current copy:\n{verdict.copy_text}\n\nEditor feedback:\n{verdict.feedback}"
    )


def build_launch_workflow() -> Sequential:
    """Build research (parallel) followed by the write-and-edit loop."""

    research = Parallel(
        "research",
        [
            Agent(
                "audience_researcher",
                description="Describes the target audience and what it cares about.",
                instructions="Describe the audience of the product in three bullet points.",
            ),
            Agent(
                "competitor_researcher",
                description="Summarizes how competitors position similar products.",
                instructions="Summarize competitor positioning in three bullet points.",
            ),
        ],
    )
    copywriter = Agent(
        "copywriter",
        description="Writes product launch copy.",
        instructions="Write launch copy of at most 60 words. Return only the copy.",
    )
    editor = Agent(
        "editor",
        description="Edits launch copy and approves it for publication.",
        instructions=(
            "Check the copy for clarity, a concrete benefit and a call to action. Approve "
            "it only if all three are present; otherwise give specific feedback."
        ),
        output_type=EditorVerdict,
    )
    refine = Loop(
        "refine",
        Sequential("write_and_edit", [copywriter, editor]),
        until=is_approved,
        max_iterations=MAX_EDITING_ROUNDS,
        next_input=revision_brief,
    )
    return Sequential(
        "launch_copy",
        [research, refine],
        carry="all",
        description="Researches the market, then writes and edits launch copy until approved.",
    )


def build_team() -> Team:
    """Build the marketing team with the launch-copy workflow as a member."""

    lead = Agent(
        "marketing_lead",
        description="Plans product launches.",
        instructions=(
            "Delegate launch copy to launch_copy with a brief naming the product and its "
            "key benefit. Report the approved copy."
        ),
    )
    return Team("marketing", orchestrator=lead, members=[build_launch_workflow()])


async def main() -> None:
    require_model_credentials()
    team = build_team()
    result = await team.run(
        "We launch 'Tidy', a budgeting app that sorts expenses automatically, next month. "
        "We need launch copy."
    )
    print(result.output)
    print(f"\n{result.usage.requests} model calls across {len(result.usage.by_agent)} agents")


if __name__ == "__main__":
    asyncio.run(main())
