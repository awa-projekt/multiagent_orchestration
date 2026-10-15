"""Compiler layer: a process described as data (YAML, JSON or Python) becomes a running system.

`Blueprint` validates (`check`) and builds (`build`) one; `load`/`aload` read a file and build
it; `design` drafts a blueprint from a description; `build_project` creates a project folder
evaluated on its own cases; `explain` shows what auto-matching gave each agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Unpack

from awa._internal.util import homes, lazy_attribute
from awa.blueprints.errors import BlueprintError, BlueprintIssue
from awa.blueprints.schema import (
    AgentBlueprint,
    AgentBuildOptions,
    Blueprint,
    BuildOptions,
    ToolSource,
)

if TYPE_CHECKING:
    from awa.blueprints._agent_facts import effective_instructions as effective_instructions
    from awa.blueprints._agent_facts import effective_skills as effective_skills
    from awa.blueprints._brief import BRIEF_FILE as BRIEF_FILE
    from awa.blueprints._brief import assistant_brief, write_brief
    from awa.blueprints._build_loop import (
        BuildProgress,
        BuildQuestions,
        BuildReport,
        build_project,
        evaluate_project,
    )
    from awa.blueprints._build_loop import BuildQuestion as BuildQuestion
    from awa.blueprints._data_files import LocalFiles as LocalFiles
    from awa.blueprints._data_files import LocalRunFiles as LocalRunFiles
    from awa.blueprints._data_files import prepare_data
    from awa.blueprints._data_map import DataMap
    from awa.blueprints._declare import read_declared_systems
    from awa.blueprints._dry_run import DryRun, dry_run
    from awa.blueprints._dry_run import DryRunLine as DryRunLine
    from awa.blueprints._patterns import DEFAULT_MAX_REQUESTS as DEFAULT_MAX_REQUESTS
    from awa.blueprints._patterns import MAX_PROPOSALS as MAX_PROPOSALS
    from awa.blueprints._patterns import PATTERNS as PATTERNS
    from awa.blueprints._patterns import Pattern as Pattern
    from awa.blueprints._patterns import PatternError as PatternError
    from awa.blueprints._patterns import PatternProposal as PatternProposal
    from awa.blueprints._patterns import PatternTest as PatternTest
    from awa.blueprints._patterns import PatternTrial as PatternTrial
    from awa.blueprints._patterns import suggest_patterns, try_patterns
    from awa.blueprints._project import TEST_DESIGNER_INSTRUCTIONS as TEST_DESIGNER_INSTRUCTIONS
    from awa.blueprints._project import AcceptanceDesign as AcceptanceDesign
    from awa.blueprints._project import ApprovalExample as ApprovalExample
    from awa.blueprints._project import ApprovalStepDesign as ApprovalStepDesign
    from awa.blueprints._project import CaseDesign as CaseDesign
    from awa.blueprints._project import ColumnDesign as ColumnDesign
    from awa.blueprints._project import ConnectionStep as ConnectionStep
    from awa.blueprints._project import DataNote as DataNote
    from awa.blueprints._project import DecisionDesign as DecisionDesign
    from awa.blueprints._project import Expectation as Expectation
    from awa.blueprints._project import ProjectDesign, design_project
    from awa.blueprints._project import RuleArgument as RuleArgument
    from awa.blueprints._project import RuleDesign as RuleDesign
    from awa.blueprints._project import RuleExample as RuleExample
    from awa.blueprints._project import SystemDesign as SystemDesign
    from awa.blueprints._project import TableDesign as TableDesign
    from awa.blueprints._project_data import ProjectData
    from awa.blueprints._resolve import ToolCatalog as ToolCatalog
    from awa.blueprints._resolve import load_tools
    from awa.blueprints._resolve import tools_files as tools_files
    from awa.blueprints._run_data import Decide as Decide
    from awa.blueprints._run_data import RunOutcome, run_project
    from awa.blueprints._systems import DeclaredSystem
    from awa.blueprints.architect import ARCHITECT_INSTRUCTIONS as ARCHITECT_INSTRUCTIONS
    from awa.blueprints.architect import AgentDesign as AgentDesign
    from awa.blueprints.architect import BlueprintDesign, design
    from awa.blueprints.architect import ConditionalApprovalDesign as ConditionalApprovalDesign
    from awa.blueprints.architect import EdgeDesign as EdgeDesign
    from awa.blueprints.architect import StepDesign as StepDesign
    from awa.blueprints.architect import TeamDesign as TeamDesign
    from awa.blueprints.auto import AgentAutoReport as AgentAutoReport
    from awa.blueprints.auto import AutoMatch as AutoMatch
    from awa.blueprints.auto import AutoReport, explain
    from awa.blueprints.errors import Severity as Severity
    from awa.blueprints.json_schema import MAX_SCHEMA_DEPTH as MAX_SCHEMA_DEPTH
    from awa.blueprints.json_schema import MAX_SCHEMA_NODES as MAX_SCHEMA_NODES
    from awa.blueprints.json_schema import model_from_json_schema
    from awa.blueprints.schema import BLUEPRINT_FORMAT_VERSION as BLUEPRINT_FORMAT_VERSION
    from awa.blueprints.schema import MAX_BLUEPRINT_BYTES as MAX_BLUEPRINT_BYTES
    from awa.blueprints.schema import ApprovalBlueprint as ApprovalBlueprint
    from awa.blueprints.schema import ApprovalCheckBlueprint as ApprovalCheckBlueprint
    from awa.blueprints.schema import BlueprintDefaults as BlueprintDefaults
    from awa.blueprints.schema import BlueprintModelSettings as BlueprintModelSettings
    from awa.blueprints.schema import DelegationBlueprint as DelegationBlueprint
    from awa.blueprints.schema import HandoffHistoryMode as HandoffHistoryMode
    from awa.blueprints.schema import LimitsBlueprint as LimitsBlueprint
    from awa.blueprints.schema import LoopStep as LoopStep
    from awa.blueprints.schema import MapStep as MapStep
    from awa.blueprints.schema import NetworkBlueprint as NetworkBlueprint
    from awa.blueprints.schema import NetworkLimitsBlueprint as NetworkLimitsBlueprint
    from awa.blueprints.schema import ParallelStep as ParallelStep
    from awa.blueprints.schema import PolicyBlueprint as PolicyBlueprint
    from awa.blueprints.schema import ProcessBlueprint as ProcessBlueprint
    from awa.blueprints.schema import ProcessStepBlueprint as ProcessStepBlueprint
    from awa.blueprints.schema import Role as Role
    from awa.blueprints.schema import RuleCheckBlueprint as RuleCheckBlueprint
    from awa.blueprints.schema import SchemaCheckBlueprint as SchemaCheckBlueprint
    from awa.blueprints.schema import SequentialStep as SequentialStep
    from awa.blueprints.schema import SkillSource as SkillSource
    from awa.blueprints.schema import StepCheckBlueprint as StepCheckBlueprint
    from awa.blueprints.schema import Structure as Structure
    from awa.blueprints.schema import TeamBlueprint as TeamBlueprint
    from awa.blueprints.schema import TeamLimitsBlueprint as TeamLimitsBlueprint
    from awa.blueprints.schema import WorkflowNode as WorkflowNode
    from awa.blueprints.schema import workflow_references as workflow_references
    from awa.contracts.protocols import AgentLike


# Defining module -> names. Names outside `__all__` stay importable for the reference docs.
_LAZY = homes(
    {
        "awa.blueprints._build_loop": (
            "BuildProgress BuildReport build_project BuildQuestion BuildQuestions evaluate_project"
        ),
        "awa.blueprints._data_files": "LocalFiles LocalRunFiles prepare_data",
        "awa.blueprints._run_data": "Decide RunOutcome run_project",
        "awa.blueprints._dry_run": "DryRun DryRunLine dry_run",
        "awa.blueprints._project_data": "ProjectData",
        "awa.blueprints._systems": "DeclaredSystem",
        "awa.blueprints._declare": "read_declared_systems",
        "awa.blueprints._data_map": "DataMap",
        "awa.blueprints._brief": "BRIEF_FILE assistant_brief write_brief",
        "awa.blueprints._patterns": (
            "DEFAULT_MAX_REQUESTS MAX_PROPOSALS PATTERNS Pattern PatternError PatternProposal"
            " PatternTest PatternTrial suggest_patterns try_patterns"
        ),
        "awa.blueprints._project": (
            "TEST_DESIGNER_INSTRUCTIONS AcceptanceDesign ApprovalExample ApprovalStepDesign"
            " CaseDesign ColumnDesign ConnectionStep DataNote DecisionDesign Expectation"
            " ProjectDesign RuleArgument RuleDesign RuleExample SystemDesign TableDesign"
            " design_project"
        ),
        "awa.blueprints._resolve": "load_tools ToolCatalog tools_files",
        "awa.blueprints._agent_facts": "effective_instructions effective_skills",
        "awa.blueprints.architect": (
            "ARCHITECT_INSTRUCTIONS AgentDesign BlueprintDesign ConditionalApprovalDesign EdgeDesign"
            " StepDesign TeamDesign design"
        ),
        "awa.blueprints.auto": "AgentAutoReport AutoMatch AutoReport explain",
        "awa.blueprints.errors": "Severity",
        "awa.blueprints.json_schema": "MAX_SCHEMA_DEPTH MAX_SCHEMA_NODES model_from_json_schema",
        "awa.blueprints.schema": (
            "BLUEPRINT_FORMAT_VERSION MAX_BLUEPRINT_BYTES ApprovalBlueprint ApprovalCheckBlueprint"
            " BlueprintDefaults BlueprintModelSettings DelegationBlueprint HandoffHistoryMode"
            " LimitsBlueprint LoopStep MapStep NetworkBlueprint NetworkLimitsBlueprint ParallelStep"
            " PolicyBlueprint ProcessBlueprint ProcessStepBlueprint Role RuleCheckBlueprint"
            " SchemaCheckBlueprint SequentialStep SkillSource StepCheckBlueprint Structure"
            " TeamBlueprint TeamLimitsBlueprint WorkflowNode workflow_references"
        ),
    }
)


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _LAZY, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})


def load(source: str | Path, **options: Unpack[BuildOptions]) -> AgentLike:
    """Build a project folder, a blueprint file or YAML text; see `Blueprint.build`.

    A folder or file brings the `tools.py` and `skills/` beside the blueprint unless `tools` or
    `skill_registry` is given. MCP tools (`mcp:<server>`) must be in `tools`; `aload` loads them.

    Raises:
        BlueprintError: If the blueprint cannot be read, validated or built.

    Example:
        ```python
        desk = load("invoice-desk", checkpointer=StateInMemorySaver())
        result = await desk.run("Check invoice 1001.")
        ```
    """

    from awa.blueprints._resolve import read_project

    blueprint, completed = read_project(source, options)
    return blueprint.build(**completed)


async def aload(source: str | Path, **options: Unpack[BuildOptions]) -> AgentLike:
    """Like `load`, but first loads the tools of the MCP servers the agents reference.

    Raises:
        BlueprintError: If the blueprint cannot be read, validated or built, or an MCP
            server cannot be reached.
        OptionalDependencyError: If MCP servers are used and the `mcp` extra is missing.
    """

    from awa.blueprints._resolve import read_project

    blueprint, completed = read_project(source, options)
    return await blueprint.abuild(**completed)


__all__ = [
    "AgentBlueprint",
    "AgentBuildOptions",
    "AutoReport",
    "BRIEF_FILE",
    "Blueprint",
    "BlueprintDesign",
    "BlueprintError",
    "BlueprintIssue",
    "BuildOptions",
    "BuildProgress",
    "BuildQuestions",
    "BuildReport",
    "DataMap",
    "DeclaredSystem",
    "DryRun",
    "PATTERNS",
    "Pattern",
    "PatternProposal",
    "PatternTest",
    "PatternTrial",
    "ProjectData",
    "ProjectDesign",
    "RunOutcome",
    "ToolSource",
    "aload",
    "assistant_brief",
    "build_project",
    "design",
    "design_project",
    "dry_run",
    "evaluate_project",
    "explain",
    "load",
    "load_tools",
    "model_from_json_schema",
    "prepare_data",
    "read_declared_systems",
    "run_project",
    "suggest_patterns",
    "try_patterns",
    "write_brief",
]
