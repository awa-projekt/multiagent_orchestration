"""Offline tests for `examples/02_skills.py`."""

from __future__ import annotations

from typing import Any

from _helpers.examples import load_example, system_prompt, tool_results
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call


async def test_inline_skills_are_in_the_prompt_with_their_resources() -> None:
    example = load_example("02_skills")
    reviewer = example.build_agents().code_reviewer
    model = FakeChatModel(
        [
            tool_call(
                "read_skill_resource",
                {"name": "code-review", "path": "references/review-checklist.md"},
            ),
            "Blocking: the query is built with an f-string (SQL injection); keep parameters.",
        ]
    )

    with reviewer.override(model=model):
        result = await reviewer.run(f"Review this change:\n\n{example.DIFF}")

    prompt = system_prompt(model.calls[0])
    assert "## Skill: code-review" in prompt and "## Skill: security-review" in prompt
    [checklist] = tool_results(result.messages)
    assert checklist and not checklist.startswith("Error")
    assert reviewer.card.skills == ("code-review", "security-review")
    assert reviewer.card.tools == ("read_skill_resource",)


async def test_on_demand_skill_is_loaded_with_its_style_guide() -> None:
    example = load_example("02_skills")
    manager = example.build_agents().release_manager

    def respond(call: ModelCall) -> Any:
        last = str(call.last_message.content)
        if call.index == 0:
            prompt = system_prompt(call)
            assert "## Skills" in prompt and "`release-notes`:" in prompt
            assert "# Release Notes" not in prompt
            return tool_call("load_skill", {"name": "release-notes"})
        if call.index == 1:
            assert "# Skill: release-notes (v1.0.0)" in last
            assert "- references/style-guide.md" in last
            return tool_call(
                "read_skill_resource",
                {"name": "release-notes", "path": "references/style-guide.md"},
            )
        assert "Footer: `Version X.Y.Z - released YYYY-MM-DD`" in last
        return "**Breaking changes**\n- The /v1/export endpoint is removed (#420)."

    with manager.override(model=FunctionChatModel(respond)):
        result = await manager.run("Write the release notes for 2.4.0.")

    assert result.output.startswith("**Breaking changes**")
    assert manager.card.skills == ("release-notes", "writing")
    assert manager.card.tools == ("load_skill", "read_skill_resource")
    assert "release-notes" in example.build_skill_registry()
