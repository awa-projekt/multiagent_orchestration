"""The "Suggest patterns" part of the Describe page: suggestions, their test and "Use this one"."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from awa.blueprints import (
    MAX_PROPOSALS,
    PatternError,
    PatternProposal,
    ProjectDesign,
    suggest_patterns,
    try_patterns,
)
from awa.contracts.usage import UsageLimits
from awa.ui._diagram import blueprint_svg

if TYPE_CHECKING:
    from awa._internal.upstream.models import BaseChatModel
    from awa.ui._project import Project

MAX_REQUEST_CAP = 200
"""The most model calls a person can allow for one test of the patterns."""


class Patterns:
    """Suggestions and tests of patterns for one project folder.

    A model ranks and tests only when scripted or allowed by the person (with a call cap); nothing
    is written to the project until a pattern is used.
    """

    def __init__(self, project: Project) -> None:
        self.project = project
        self._designs: dict[str, ProjectDesign] = {}

    def model(self) -> str | BaseChatModel | None:
        """The model that would be used: the project's, else the default profile of its
        registry; `None` when only the process-wide default exists (a real model)."""

        if self.project.model is not None:
            return self.project.model
        registry = self.project.model_registry
        return registry.get("default") if registry is not None and "default" in registry else None

    def scripted(self) -> bool:
        """Whether the model is a scripted test double, which never costs anything."""

        return type(self.model()).__module__.startswith("awa.testing")

    def live(self, allow_model: bool) -> str | BaseChatModel | None:
        """The model for this request (or `default`) when scripted or allowed by the person."""

        return (self.model() or "default") if self.scripted() or allow_model else None

    async def suggest(self, process: str, *, allow_model: bool) -> dict[str, Any]:
        """Suggest patterns for `process`: `proposals` cards and `ranked_by` (`words` or `model`).

        Raises:
            PatternError: If the description is empty.
        """

        proposals = await suggest_patterns(
            process, model=self.live(allow_model), count=MAX_PROPOSALS
        )
        return {
            "proposals": [_proposal_card(proposal) for proposal in proposals],
            "ranked_by": proposals[0].source,
        }

    async def test(
        self,
        process: str,
        ids: list[str],
        *,
        approval_gate: bool,
        allow_model: bool,
        max_requests: int,
        emit: Callable[[str, dict[str, Any]], None],
    ) -> dict[str, Any]:
        """Draft a blueprint per pattern, run the shared cases through each and compare (no write).

        Raises:
            PatternError: If a live model is needed but not allowed, or the ids are wrong.
        """

        if not self.scripted() and not allow_model:
            raise PatternError(
                "Testing patterns uses your model and costs tokens: tick the box that allows it"
            )
        if not 1 <= max_requests <= MAX_REQUEST_CAP:
            raise PatternError(f"The cap must be between 1 and {MAX_REQUEST_CAP} model calls")
        proposals = [PatternProposal(pid, "", "", approval_gate) for pid in dict.fromkeys(ids)]
        for proposal in proposals:
            proposal.pattern  # noqa: B018 - an unknown id is refused here, before any model call
        systems, files, declared = self.project.design_inputs(process, emit)
        self._designs = {}
        emit("started", {"patterns": [p.pattern.title for p in proposals], "cap": max_requests})
        result = await try_patterns(
            process,
            proposals,
            model=self.model(),
            usage_limits=UsageLimits(max_requests=max_requests),
            environment=systems,
            design_options={**files, "declared_systems": declared},
            on_progress=lambda event, facts: emit(event, dict(facts)),
        )
        self._designs = {t.pattern_id: t.design for t in result.trials if t.design is not None}
        return result.to_dict()

    def use(self, pattern_id: str) -> dict[str, Any]:
        """Write the tested design of `pattern_id` into the project.

        Raises:
            PatternError: If that pattern was not tested (or failed) in this session.
        """

        design = self._designs.get(pattern_id)
        if design is None:
            raise PatternError("Test this pattern first: there is nothing to use yet")
        self.project.adopt(design)
        return {"used": pattern_id, "name": design.blueprint.name}


def _proposal_card(proposal: PatternProposal) -> dict[str, Any]:
    pattern = proposal.pattern
    return {
        "id": pattern.id,
        "title": pattern.title,
        "structure": pattern.structure,
        "when_to_use": pattern.when_to_use,
        "agents": pattern.agents_sketch,
        "cost": pattern.cost_note,
        "diagram": blueprint_svg(pattern.blueprint()),
        "why": proposal.why,
        "risks": proposal.risks,
        "approval_gate": proposal.approval_gate,
    }
