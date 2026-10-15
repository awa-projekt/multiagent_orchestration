"""Blueprint models for approvals and policies (data only)."""

from __future__ import annotations

from pydantic import Field

from awa.blueprints._schema_spec import _Spec
from awa.tools.risk import RiskName


class ApprovalBlueprint(_Spec):
    """A human-in-the-loop gate on a tool, for every call or only when a condition holds.

    Attributes:
        tool: The tool as written in `agents[].tools`; `"mcp:<server>/<tool>"` gates one MCP
            tool, `"mcp:<server>"` every tool of the server.
        when: When approval is needed; default always. An expression over the call's
            arguments (`"amount >= 5000"`, see `awa.tools.ApprovalCondition`; evaluated without
            `eval` and checked by `check()`), or a `"package.module:predicate"` reference
            (imported only with `allow_imports=True`).
    """

    tool: str
    when: str | None = None

    @property
    def condition(self) -> str | None:
        """`when` as an expression over the arguments, or `None` (always, or an import reference)."""

        from awa.blueprints._resolve import is_valid_import_reference

        if self.when is None or is_valid_import_reference(self.when.strip()):
            return None
        return self.when


class PolicyBlueprint(_Spec):
    """A run-time tool policy for some or all agents, applied in declaration order; see `awa.tools.policy`.

    Attributes:
        agents: The agents it applies to; empty for every agent.
        deny: Tool names whose calls are refused (the model reads `reason`).
        ask: Risk flags whose tools pause for a human decision; needs a checkpointer.
        use: `"package.module:policy"`, a `ToolPolicy` function (imported only with
            `allow_imports=True`).
    """

    agents: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    ask: list[RiskName] = Field(default_factory=list)
    reason: str | None = None
    use: str | None = None

    def applies_to(self, agent: str) -> bool:
        """Return whether the policy applies to the agent `agent`."""

        return not self.agents or agent in self.agents
