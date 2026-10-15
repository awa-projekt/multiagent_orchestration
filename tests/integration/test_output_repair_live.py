"""A real model fixes its answer after an output validator rejected it (run explicitly).

One scripted bad-then-good exchange per structured-output mode: the validator rejects the first
answer, the reason goes back to the model, and the second answer passes. Needs the Azure OpenAI
settings of the project (`AZURE_OPENAI_*`); it makes real model requests, so it never runs by
default (`-m integration`).
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from pydantic import BaseModel

from awa.agents import Agent, ModelRetry
from awa.models import resolve_model

pytestmark = [pytest.mark.integration, pytest.mark.model_requests_allowed]

MODEL = "azure_openai:gpt-5.6"


class Pick(BaseModel):
    color: str


def _model(structured_output: bool) -> Any:
    if not os.environ.get("AZURE_OPENAI_API_KEY"):
        pytest.skip("set the AZURE_OPENAI_* variables of the project to run a live test")
    model = resolve_model(MODEL)
    # `True`: the provider's native structured output (retried inside the model call);
    # `False`: structured output through a tool call (retried in the next model turn).
    model.profile = {**(model.profile or {}), "structured_output": structured_output}
    return model


@pytest.mark.parametrize("structured_output", [True, False], ids=["provider-mode", "tool-mode"])
async def test_the_model_fixes_a_rejected_answer(structured_output: bool) -> None:
    seen: list[str] = []

    def reject_the_first_pick(pick: Pick) -> None:
        seen.append(pick.color)
        if len(seen) == 1:
            raise ModelRetry("That color is taken. Pick a different color.")

    agent = Agent(
        "picker",
        model=_model(structured_output),
        instructions="Pick one color of the rainbow.",
        output_type=Pick,
        output_validators=[reject_the_first_pick],
    )

    result = await agent.run("Pick a color.")

    assert len(seen) == 2
    assert result.output.color == seen[1]
    assert result.usage.requests == 2
