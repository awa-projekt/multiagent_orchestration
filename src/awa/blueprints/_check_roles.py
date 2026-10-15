"""The role rules of `Blueprint.check`: roles, sub-agent trees, judges and reviewers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from awa.blueprints._resolve import ToolCatalog
from awa.blueprints._risk import agent_tools
from awa.blueprints._roles import (
    bound_judge,
    bound_reviewers,
    parent_of,
    role_roots,
    tree_limits,
    uses_role_tree,
)
from awa.blueprints.errors import BlueprintIssue, Severity

if TYPE_CHECKING:
    from awa.blueprints.schema import AgentBlueprint, Blueprint

_GENERIC_WORDS = frozenset(
    {"good", "correct", "accurate", "fine", "ok", "okay", "right", "great", "quality", "high"}
)
_MIN_CRITERION_WORDS = 4
_RECOMMENDED_MAX_ITERATIONS = 5
_MAX_LISTED = 5


def role_issues(blueprint: Blueprint, catalog: ToolCatalog | None) -> list[BlueprintIssue]:
    """Return the issues of the roles, `sub_agents`, `judges` and `reviews` of `blueprint`."""

    return _RoleChecker(blueprint, catalog).run()


class _RoleChecker:
    def __init__(self, blueprint: Blueprint, catalog: ToolCatalog | None) -> None:
        self._bp = blueprint
        self._catalog = catalog
        self._by_name = {spec.name: spec for spec in blueprint.agents}
        self._issues: list[BlueprintIssue] = []

    def run(self) -> list[BlueprintIssue]:
        for index, spec in enumerate(self._bp.agents):
            self._check_agent(spec, f"agents[{index}]")
        if uses_role_tree(self._bp):
            self._check_structure_mix()
            self._check_tree()
            self._check_limits()
        return self._issues

    def _add(self, severity: Severity, path: str, message: str) -> None:
        self._issues.append(BlueprintIssue(severity, path, message))

    def _index(self, name: str) -> int:
        return next(i for i, spec in enumerate(self._bp.agents) if spec.name == name)

    def _check_agent(self, spec: AgentBlueprint, path: str) -> None:
        role = spec.role
        if spec.judges is not None and role != "judge":
            self._add("error", f"{path}.judges", "`judges` is for agents with role: judge")
        if spec.reviews is not None and role != "reviewer":
            self._add("error", f"{path}.reviews", "`reviews` is for agents with role: reviewer")
        if spec.rubric and role not in ("judge", "reviewer"):
            self._add("error", f"{path}.rubric", "`rubric` is for a judge or a reviewer")
        if role == "judge":
            self._check_judge(spec, path)
        if role == "reviewer":
            self._check_reviewer(spec, path)
        if role in ("judge", "reviewer"):
            self._check_read_only(spec, path)
            self._check_rubric(spec, path)
        if role == "orchestrator" and spec.tools:
            self._add(
                "warning",
                f"{path}.tools",
                f"orchestrator {spec.name!r} has tools of its own; an orchestrator plans and "
                "delegates, give the tools to a worker",
            )
        if (
            spec.max_iterations is not None
            and spec.max_iterations > _RECOMMENDED_MAX_ITERATIONS
            and spec.sub_agents
        ):
            self._add(
                "warning",
                f"{path}.max_iterations",
                f"{spec.max_iterations} judge rounds is a lot: loops that long tend to "
                "oscillate and cost many calls; improve the criteria or the worker instead",
            )
        if spec.max_iterations is not None and not spec.sub_agents:
            self._add(
                "error",
                f"{path}.max_iterations",
                "`max_iterations` belongs on the orchestrator (or worker) that lists the "
                "checked worker under `sub_agents`",
            )
        if spec.delegation is not None and not spec.sub_agents:
            self._add("error", f"{path}.delegation", "`delegation` needs `sub_agents`")

    def _check_judge(self, spec: AgentBlueprint, path: str) -> None:
        if not spec.rubric:
            self._add(
                "error",
                f"{path}.rubric",
                f"judge {spec.name!r} needs a rubric: a judge without criteria approves "
                "everything. List what the work must meet, one criterion per entry",
            )
        if spec.judges is None:
            self._add(
                "error",
                f"{path}.judges",
                f"judge {spec.name!r} needs `judges: <worker>`, the worker whose output it gates",
            )
            return
        target = self._by_name.get(spec.judges)
        if target is None:
            self._add("error", f"{path}.judges", f"unknown worker {spec.judges!r}")
            return
        if target.role not in ("worker", None) or target.name == spec.name:
            self._add(
                "error",
                f"{path}.judges",
                f"{spec.judges!r} is not a worker; a judge gates a worker's output",
            )
            return
        rivals = [s.name for s in self._bp.agents if s.judges == spec.judges and s.role == "judge"]
        if len(rivals) > 1 and rivals[0] != spec.name:
            self._add(
                "error",
                f"{path}.judges",
                f"worker {spec.judges!r} already has the judge {rivals[0]!r}; a worker has "
                "one judge",
            )
            return
        self._judge_independence(spec, target, path)

    def _judge_independence(self, judge: AgentBlueprint, worker: AgentBlueprint, path: str) -> None:
        defaults = self._bp.defaults.model
        judge_model = judge.model or defaults or "default"
        worker_model = worker.model or defaults or "default"
        if judge_model == worker_model:
            self._add(
                "warning",
                f"{path}.model",
                f"judge {judge.name!r} uses the same model as worker {worker.name!r} "
                f"({judge_model}); errors of one model tend to repeat in the other. Give the "
                "judge a different model",
            )
        if set(judge.tools) == set(worker.tools):
            self._add(
                "warning",
                f"{path}.tools",
                f"judge {judge.name!r} has the same tools as worker {worker.name!r}"
                + (" (none)" if not judge.tools else "")
                + ", so it cannot check the work against a source of its own; give the judge "
                "its own read tools (for example to open the cited sources)",
            )

    def _check_reviewer(self, spec: AgentBlueprint, path: str) -> None:
        if spec.reviews is None:
            return
        target = self._by_name.get(spec.reviews)
        if target is None:
            self._add("error", f"{path}.reviews", f"unknown worker {spec.reviews!r}")
        elif target.role not in ("worker", None):
            self._add(
                "error",
                f"{path}.reviews",
                f"{spec.reviews!r} is not a worker; a reviewer reads a worker's output",
            )

    def _check_read_only(self, spec: AgentBlueprint, path: str) -> None:
        what = spec.role or "role"
        if spec.sub_agents:
            self._add("error", f"{path}.sub_agents", f"a {what} cannot have sub-agents")
        if spec.memory is not None:
            self._add("error", f"{path}.memory", f"a {what} is read-only and cannot have memory")
        if spec.output_schema is not None:
            self._add(
                "error",
                f"{path}.output_schema",
                f"a {what} has a fixed output; remove its output_schema",
            )
        writers = [
            tool.label
            for tool in agent_tools(self._bp, spec, self._catalog)
            if tool.risk.external_action
        ]
        undeclared = [
            reference
            for reference in spec.tools
            if reference.startswith("plugin:")
            and (
                (declared := self._bp.tool_risks.get(reference)) is None
                or declared.external_action is not False
            )
        ]
        if undeclared:
            self._add(
                "error",
                f"{path}.tools",
                f"{what} {spec.name!r} must be read-only, but the tools of "
                f"{', '.join(undeclared[:_MAX_LISTED])} are only known when the plugin loads. "
                "Declare each one read-only under tool_risks (external_action: false) after "
                "checking that it sends and changes nothing, or give it to a worker",
            )
        if writers:
            shown = ", ".join(writers[:_MAX_LISTED])
            self._add(
                "error",
                f"{path}.tools",
                f"{what} {spec.name!r} must be read-only, but it has tools that send or change "
                f"something ({shown}). Remove them, or give them to a worker. The builtin web "
                "tools count as read-only once `toolkits.web.allowed_hosts` is set; for any "
                "other wrongly flagged tool, correct it under tool_risks",
            )

    def _check_rubric(self, spec: AgentBlueprint, path: str) -> None:
        for number, text in enumerate(spec.rubric, 1):
            words = text.split()
            if not words:
                self._add("error", f"{path}.rubric[{number - 1}]", "a criterion must not be empty")
            elif len(words) < _MIN_CRITERION_WORDS or {w.lower() for w in words} <= _GENERIC_WORDS:
                self._add(
                    "warning",
                    f"{path}.rubric[{number - 1}]",
                    f"criterion c{number} ({text!r}) is vague; say what exactly must hold, so it "
                    "can be checked",
                )

    def _check_structure_mix(self) -> None:
        for name in ("team", "workflow", "network", "process"):
            if getattr(self._bp, name) is not None:
                self._add(
                    "error",
                    name,
                    f"`sub_agents`, `judges` and `reviews` build a role tree, which cannot be "
                    f"combined with a `{name}:` section; use one or the other",
                )

    def _check_tree(self) -> None:
        for index, spec in enumerate(self._bp.agents):
            path = f"agents[{index}]"
            seen: set[str] = set()
            for position, child in enumerate(spec.sub_agents):
                child_path = f"{path}.sub_agents[{position}]"
                target = self._by_name.get(child)
                if target is None:
                    self._add("error", child_path, f"unknown sub-agent {child!r}")
                elif child == spec.name:
                    self._add("error", child_path, f"{child!r} cannot be its own sub-agent")
                elif child in seen:
                    self._add("error", child_path, f"sub-agent {child!r} is listed twice")
                elif target.role == "judge":
                    self._add(
                        "error",
                        child_path,
                        f"{child!r} is a judge; bind it with `judges: <worker>` instead of "
                        "listing it as a sub-agent",
                    )
                elif target.role == "reviewer" and target.reviews is not None:
                    self._add(
                        "error",
                        child_path,
                        f"{child!r} reads {target.reviews!r} (`reviews`), so it is not also a "
                        "sub-agent; drop `reviews` to let the orchestrator call it directly",
                    )
                seen.add(child)
        parents: dict[str, list[str]] = {}
        for spec in self._bp.agents:
            for child in dict.fromkeys(spec.sub_agents):
                parents.setdefault(child, []).append(spec.name)
        for child, listed_by in parents.items():
            if len(listed_by) > 1 and child in self._by_name:
                self._add(
                    "error",
                    f"agents[{self._index(child)}]",
                    f"{child!r} is a sub-agent of {' and '.join(listed_by)}; an agent has one "
                    "parent (copy the agent under another name to use it twice)",
                )
        self._check_cycles()
        self._check_roots()

    def _check_cycles(self) -> None:
        state: dict[str, int] = {}

        def visit(name: str, trail: list[str]) -> bool:
            if state.get(name) == 1:
                cycle = " -> ".join([*trail[trail.index(name) :], name])
                self._add(
                    "error", f"agents[{self._index(name)}]", f"sub_agents form a cycle: {cycle}"
                )
                return True
            if state.get(name) == 2 or name not in self._by_name:
                return False
            state[name] = 1
            found = any(visit(child, [*trail, name]) for child in self._by_name[name].sub_agents)
            state[name] = 2
            return found

        for spec in self._bp.agents:
            if visit(spec.name, []):
                return

    def _check_roots(self) -> None:
        roots = role_roots(self._bp)
        children = set(parent_of(self._bp))
        if not roots and not any(spec.sub_agents for spec in self._bp.agents):
            self._add(
                "error",
                "agents",
                "judges and reviewers need a tree: add an orchestrator with `sub_agents`",
            )
            return
        if len(roots) > 1 and self._bp.entrypoint is None:
            self._add(
                "error",
                "entrypoint",
                f"several agents lead sub-agents and nobody leads them ({', '.join(roots)}); "
                "give the system one root orchestrator, or set `entrypoint:`",
            )
        for root in roots:
            spec = self._by_name[root]
            if spec.role != "orchestrator":
                self._add(
                    "error",
                    f"agents[{self._index(root)}].role",
                    f"{root!r} is the root of the tree and must have role: orchestrator",
                )
        orphans = [
            spec.name
            for spec in self._bp.agents
            if spec.name not in children
            and spec.name not in roots
            and spec.judges is None
            and spec.reviews is None
        ]
        if len(roots) == 1 and orphans:
            self._add(
                "warning",
                "agents",
                f"agents outside the tree are never run: {', '.join(orphans)}. List them under "
                "`sub_agents`, or remove them",
            )
        self._check_bindings()
        self._check_generated_names()
        if len(roots) == 1:
            self._check_depth(roots[0])

    def _check_bindings(self) -> None:
        """A judge or reviewer bound to a worker the tree does not contain would never run."""

        children = set(parent_of(self._bp))
        for spec in self._bp.agents:
            target = spec.judges if spec.role == "judge" else spec.reviews
            if target and target in self._by_name and target not in children:
                self._add(
                    "error",
                    f"agents[{self._index(spec.name)}]",
                    f"{spec.name!r} checks {target!r}, which is not listed under any agent's "
                    "`sub_agents`, so it would never run",
                )

    def _check_depth(self, root: str) -> None:
        limit, _ = tree_limits(self._bp, root)
        deepest = {root: 0}
        pending = [root]
        while pending:
            name = pending.pop()
            for child in self._by_name[name].sub_agents:
                if child in self._by_name and child not in deepest:
                    deepest[child] = deepest[name] + 1
                    pending.append(child)
        for name, depth in list(deepest.items()):
            bound = [bound_judge(self._bp, name), *bound_reviewers(self._bp, name)]
            if any(bound):
                deepest[f"{name}+checks"] = depth + 1
        reached = max(deepest.values())
        if reached > limit:
            self._add(
                "error",
                f"agents[{self._index(root)}]",
                f"the tree is {reached} levels deep below {root!r}, more than the limit of "
                f"{limit}; flatten it, or raise `delegation.max_depth` on the root",
            )

    def _check_generated_names(self) -> None:
        taken = set(self._by_name)
        for spec in self._bp.agents:
            for suffix in ("_team", "_checked"):
                generated = f"{spec.name}{suffix}"
                if generated in taken:
                    self._add(
                        "error",
                        f"agents[{self._index(generated)}].name",
                        f"the name {generated!r} is used for the unit built around "
                        f"{spec.name!r}; rename the agent",
                    )
        roots = role_roots(self._bp)
        if len(roots) == 1 and self._bp.name in taken:
            self._add(
                "error",
                "name",
                f"the blueprint name {self._bp.name!r} is also an agent name; the root team "
                "is named after the blueprint, rename one of them",
            )

    def _check_limits(self) -> None:
        if self._bp.limits is None:
            self._add(
                "warning",
                "limits",
                "a role tree runs several agents and judge loops, which use several times the "
                "model calls of one agent; add `limits:` (max_requests, max_total_tokens)",
            )
