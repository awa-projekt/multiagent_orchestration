"""Skills: reusable instructions from the builtin library and from your own SKILL.md files.

The code reviewer uses two builtin skills inline: their instructions are part
of its system prompt, and bundled reference files are readable with
`read_skill_resource`. The release manager uses a custom skill from
`examples/skills/release-notes/` on demand: its prompt lists only the skill
names and descriptions, and it loads a skill (and the style guide bundled
with it) with `load_skill` / `read_skill_resource` when the task needs it.
On-demand mode keeps prompts short when an agent has many skills.

Run:
    pip install -e ".[anthropic]"
    export ANTHROPIC_API_KEY=...
    python examples/02_skills.py
    # No model key? Run its offline tests instead:
    #   python -m pytest tests/examples/test_02_skills.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from _support import require_model_credentials
from awa import Agent, SkillRegistry

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

DIFF = """\
--- a/billing/export.py
+++ b/billing/export.py
@@ def export_invoices(customer_id):
-    rows = db.query("SELECT * FROM invoices WHERE customer = ?", customer_id)
+    rows = db.query(f"SELECT * FROM invoices WHERE customer = '{customer_id}'")
     return [to_csv(row) for row in rows]
"""

MERGED_CHANGES = [
    "#412 Export invoices as CSV from the billing page",
    "#415 Dashboard loads 40% faster by caching account summaries",
    "#418 Fix: totals were rounded twice for multi-currency invoices",
    "#420 Remove the deprecated /v1/export endpoint (use /v2/export)",
    "#421 CI: run tests on Python 3.13",
]


@dataclass(frozen=True)
class SkilledAgents:
    """The two agents of this example."""

    code_reviewer: Agent
    release_manager: Agent


def build_skill_registry() -> SkillRegistry:
    """The builtin skills plus every skill in `examples/skills/`."""

    return SkillRegistry.from_directory(SKILLS_DIR, include_builtin=True)


def build_agents() -> SkilledAgents:
    """Build one agent with inline builtin skills and one with on-demand skills."""

    code_reviewer = Agent(
        "code_reviewer",
        description="Reviews code changes for correctness and security.",
        instructions="Review the diff you are given. List blocking findings first.",
        skills=["code-review", "security-review"],
    )
    release_manager = Agent(
        "release_manager",
        description="Writes release notes and version announcements.",
        instructions="You prepare releases. Use your skills for any writing task.",
        skills=["release-notes", "writing"],
        skill_registry=build_skill_registry(),
        skill_mode="on_demand",
    )
    return SkilledAgents(code_reviewer=code_reviewer, release_manager=release_manager)


async def main() -> None:
    require_model_credentials()
    agents = build_agents()
    for agent in (agents.code_reviewer, agents.release_manager):
        print(f"{agent.name}: skills={list(agent.card.skills)} tools={list(agent.card.tools)}")

    review = await agents.code_reviewer.run(f"Review this change:\n\n{DIFF}")
    print(f"\n== Review\n{review.output}")

    changes = "\n".join(f"- {change}" for change in MERGED_CHANGES)
    notes = await agents.release_manager.run(
        f"Write the release notes for version 2.4.0, released 2026-10-01:\n{changes}"
    )
    print(f"\n== Release notes\n{notes.output}")


if __name__ == "__main__":
    asyncio.run(main())
