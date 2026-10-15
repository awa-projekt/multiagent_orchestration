"""The project folder the UI works on (what `awa build` writes) and its write-only `.env`."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awa.blueprints import Blueprint, BlueprintError, BlueprintIssue
from awa.blueprints.triggers import KINDS, Step, add_trigger, trigger_entry, trigger_steps
from awa.core.dotenv import read_env_file, write_env_file
from awa.data import DataError
from awa.errors import (
    AwaError,
    ConfigurationError,
    MissingConfigurationError,
    OptionalDependencyError,
    UsageLimitExceededError,
)
from awa.models.errors import classify_model_error
from awa.ui._plain import as_text

if TYPE_CHECKING:
    from awa._internal.upstream.models import BaseChatModel
    from awa.blueprints import DeclaredSystem, ProjectDesign
    from awa.connectors import EnvironmentReport, Transport
    from awa.contracts.protocols import AgentLike
    from awa.contracts.usage import UsageLimits
    from awa.data import Limits
    from awa.evals import Dataset
    from awa.models import ModelRegistry
    from awa.ui._cases import CaseEditor
    from awa.ui._connections import Connections
    from awa.ui._data import DataStep
    from awa.ui._operate import Operation
    from awa.ui._systems import SystemsStep
    from awa.ui._tools import ToolInfo
    from awa.ui._try import Narrator

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_DATA_LISTED = 50
_TITLE_CHARS = 40
_EXAMPLE_CHARS = 240
_EXAMPLES = 5
_TRIGGER_SETTINGS = frozenset(
    {"input", "secret_env", "scheme", "cron", "every", "timezone", "directory", "pattern", "host"}
    | {"username_env", "password_env", "folder", "queue"}
)

Emit = Callable[[str, dict[str, Any]], None]
"""Reports progress: an event name and its JSON data."""


def env_names(path: Path) -> list[str]:
    """The variable names `path` assigns a non-empty value to, sorted (never the values)."""

    return sorted(name for name, value in read_env_file(path).items() if value)


def _load_env(
    path: Path, environ: MutableMapping[str, str], *, override: Iterable[str] = ()
) -> None:
    """Export the `.env` at `path` into `environ`: new names, and the names in `override`.

    Names on the deny-list of `awa.core.config` (such as `PATH` or `HTTPS_PROXY`) are never exported.
    """

    from awa.core.settings_registry import is_denied

    forced = set(override)
    for name, value in read_env_file(path).items():
        if is_denied(name):
            continue
        if value and (name in forced or not environ.get(name)):
            environ[name] = value


def _redact(text: str, path: Path) -> str:
    """`text` with every `.env` value at `path` (4+ characters) replaced by `***`."""

    from awa.kernel.redaction import redact_values

    return redact_values(text, read_env_file(path).values())


def environment(project: Path, environ: Mapping[str, str]) -> EnvironmentReport:
    """What is configured (names only), per `awa.connectors.discover`."""

    from awa import connectors

    return connectors.discover(environ, dotenv=project / ".env")


@dataclass(frozen=True)
class Explanation:
    """A failure in plain language: what happened and what to do about it."""

    message: str
    fix: str
    link: str = ""
    link_text: str = ""
    detail: str = ""


class Project:
    """The project folder: its files, checks, the built system and the export.

    Args:
        model: Model of the architect when designing (`None`: the `default` profile).
        model_registry: Registry of the blueprint's model profiles (`None`: a fresh one per
            build, so newly saved credentials apply).
        transport: Sends the requests of connection tests (default: the connectors' client).
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        model: str | BaseChatModel | None = None,
        model_registry: ModelRegistry | None = None,
        environ: MutableMapping[str, str] | None = None,
        transport: Transport | None = None,
        data_limits: Limits | None = None,
    ) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.model_registry = model_registry
        self.environ: MutableMapping[str, str] = os.environ if environ is None else environ
        self.transport = transport
        self.data_limits = data_limits
        self._system: tuple[tuple[float, ...], AgentLike] | None = None
        self._infos: tuple[object, dict[str, ToolInfo]] | None = None
        self._data: DataStep | None = None
        _load_env(self.env_file, self.environ)

    @property
    def env_file(self) -> Path:
        return self.directory / ".env"

    @property
    def blueprint_file(self) -> Path:
        return self.directory / "blueprint.yaml"

    @property
    def runs_file(self) -> Path:
        """The local data file `awa serve` and `awa worker` keep next to the blueprint."""

        return self.directory / ".awa" / "awa.db"

    @property
    def tools_file(self) -> Path:
        return self.directory / "tools.py"

    @property
    def cases_file(self) -> Path:
        return self.directory / "evals" / "cases.yaml"

    def allowed_settings(self) -> set[str]:
        """The names the Set up and Connect pages may save; never `PATH` or `HTTPS_PROXY`.

        The model setting, `.env.example` names, provider and connector variables and those of the
        project's own systems, less the deny-list of `awa.core.config`: this process exports what
        is saved.
        """

        from awa.core.settings_registry import is_denied
        from awa.ui._connections import provider_variables

        known = {"AWA_DEFAULT_MODEL", *self._needed_settings()}
        known.update(name for names in provider_variables().values() for name in names)
        known.update(
            name for c in environment(self.directory, self.environ).connectors for name in c.env
        )
        if self.blueprint_file.is_file():
            known.update(self.connections.names())
        return {name for name in known if not is_denied(name)}

    def save_settings(self, values: Mapping[str, str]) -> list[str]:
        """Write `values` (names of `allowed_settings`, valid links) to `.env` and export them."""

        from awa.ui._connections import check_values, setting_kinds

        allowed = self.allowed_settings()
        for name in values:
            if name not in allowed:
                raise ValueError(f"{name!r} is not a setting this page can save")
        check_values(values, setting_kinds(self))
        written = write_env_file(self.env_file, values)
        self._refresh(written)
        return written

    def _refresh(self, written: list[str]) -> None:
        from awa.core.settings import get_settings

        _load_env(self.env_file, self.environ, override=written)
        get_settings.cache_clear()
        self._system = None

    def setup(self) -> dict[str, Any]:
        """Providers, connectors and the names set in `.env`; never a value."""

        from awa.blueprints._project_files import parse_steps
        from awa.ui._connections import connector_forms, provider_forms

        report = environment(self.directory, self.environ)
        saved = set(env_names(self.env_file))

        def is_set(name: str) -> bool:
            return bool(self.environ.get(name)) or name in saved

        return {
            "providers": provider_forms(report, is_set),
            "connectors": connector_forms(report, is_set),
            "env_names": sorted(saved),
            "default_model_set": bool(self.environ.get("AWA_DEFAULT_MODEL")),
            "project": [{"name": name, "set": is_set(name)} for name in self._needed_settings()],
            "connect_steps": len(parse_steps(self.guide())),
        }

    @property
    def operation(self) -> Operation:
        """Long-term operation: the triggers in words, recent runs, health, a run on a sample."""

        from awa.ui._operate import Operation

        return Operation(self)

    @property
    def data(self) -> DataStep:
        """The Data step: the user's files in `data/files`, their profile, notes and masking."""

        if self._data is None:
            from awa.ui._data import DataStep
            from awa.ui._data_folder import UPLOAD_LIMITS, LocalFileStore

            folder = self.directory / "data"
            store = LocalFileStore(
                folder / "files",
                self.directory / ".awa" / "uploads",
                self.data_limits or UPLOAD_LIMITS,
            )
            self._data = DataStep(
                store, folder / "PROFILE.json", self.directory / ".awa" / "data.json"
            )
        return self._data

    @property
    def systems(self) -> SystemsStep:
        """The external systems declared on the Data step (live, read-only in a design)."""

        from awa.ui._systems import SystemsStep

        return SystemsStep(self)

    def data_map(self) -> dict[str, Any]:
        """Where the data of the built system comes from: per agent, which source it reads or
        writes, how, and whether it is live, a copy of the user's file or a stand-in."""

        from awa.blueprints._data_map import data_map, stand_ins_from_steps
        from awa.blueprints._declare import read_declared_systems
        from awa.blueprints._project_files import parse_steps

        if not self.blueprint_file.is_file():
            return {"exists": False, "sources": [], "agents": []}
        found = data_map(
            self.blueprint(),
            declared=read_declared_systems(self.directory),
            stand_ins=stand_ins_from_steps(parse_steps(self.guide())),
            files=self.files_in("data/files"),
        )
        return {"exists": True, **found.to_dict()}

    @property
    def case_editor(self) -> CaseEditor:
        """The Test step's editor of the eval cases (request, files, live or stand-in)."""

        from awa.ui._cases import CaseEditor

        return CaseEditor(self)

    @property
    def connections(self) -> Connections:
        """The Connect step: this project's systems, their settings forms, tests and connecting."""

        from awa.ui._connections import Connections

        return Connections(self)

    def _is_set(self, name: str) -> bool:
        return bool(self.environ.get(name)) or name in env_names(self.env_file)

    def _needed_settings(self) -> list[str]:
        """The names `.env.example` lists for this project (the model and each system)."""

        example = self.directory / ".env.example"
        if not example.is_file():
            return []
        names = [
            match.group(1)
            for line in example.read_text(encoding="utf-8").splitlines()
            if (match := _ASSIGNMENT.match(line)) and match.group(1) != "AWA_DEFAULT_MODEL"
        ]
        return list(dict.fromkeys(names))

    def design_inputs(
        self, process: str, emit: Emit
    ) -> tuple[EnvironmentReport, dict[str, Any], list[DeclaredSystem]]:
        """What a design starts from: the reachable systems, the user's files and the systems
        they declared (each reported to `emit`)."""

        from awa.blueprints._declare import merged_env, named_systems, read_declared_systems

        systems = environment(self.directory, self.environ)
        request = self.data.request()
        files: dict[str, Any] = {}
        if request is not None:
            files = {"data": request.paths, "notes": request.notes, "mask": request.mask}
            emit("progress", {"message": f"Using your {len(request.paths)} data file(s)"})
        declared = read_declared_systems(self.directory)
        declared += named_systems(
            process,
            merged_env(self.environ, self.directory / ".env", {}),
            taken=[s.key for s in declared] + [s.connector for s in declared],
        )
        if declared:
            names = ", ".join(system.name for system in declared)
            emit("progress", {"message": f"Using your external systems live, read-only: {names}"})
            files.setdefault("mask", self.data.mask())
        return systems, files, declared

    async def create(self, process: str, *, thorough: bool, emit: Emit) -> dict[str, Any]:
        """Design (with `thorough`, also test and repair) a system for `process` into the folder."""

        if not process.strip():
            raise ValueError(
                "Describe the process first: what comes in, who does what, what goes out"
            )
        from awa.blueprints import build_project, design_project

        self._system = None
        systems, files, declared = self.design_inputs(process, emit)
        if thorough:
            report = await build_project(
                process,
                out_dir=self.directory,
                model=self.model,
                environment=systems,
                force=True,  # the Describe page says that a new design replaces the old one
                on_progress=lambda progress: emit("progress", _progress(progress)),
                declared_systems=declared,
                **files,
            )
            facts = report.to_dict()
            return {
                "summary": report.summary(),
                "passed": report.passed,
                "pass_rate": report.pass_rate,
                "name": facts["name"],
                "cases": facts["cases"],
                "data_map": facts["data_map"],
            }
        emit("progress", {"message": "Designing the system and its eval cases"})
        project_design = await design_project(
            process, model=self.model, environment=systems, declared_systems=declared, **files
        )
        project_design.write(self.directory)
        return {"summary": f"Designed {project_design.blueprint.name}; the files are written."}

    def adopt(self, design: ProjectDesign) -> Path:
        """Write `design` into the folder as the project's system (replacing the files of the
        old one) and return the folder."""

        self._system = None
        return design.write(self.directory)

    def blueprint(self) -> Blueprint:
        """The project's blueprint.

        Raises:
            MissingConfigurationError: If there is none yet.
        """

        if not self.blueprint_file.is_file():
            raise MissingConfigurationError("There is no system in this folder yet")
        return Blueprint.from_yaml_file(self.blueprint_file)

    def usage_limits(self) -> UsageLimits | None:
        """The `limits:` of the blueprint, which hold for every run in the app (`None`: none)."""

        return self.blueprint().usage_limits()

    def redacted(self, text: str) -> str:
        """`text` without any credential saved in `.env`."""

        return _redact(text, self.env_file)

    def tools(self) -> dict[str, ToolInfo]:
        """The tools of the built system in plain words, by tool name (empty when it cannot be
        built: the page then falls back to the tool names)."""

        from awa.ui._tools import tool_infos

        try:
            system = self.system()
        except Exception:  # noqa: BLE001 - the page names tools by their name instead
            return {}
        if self._infos is None or self._infos[0] is not system:
            self._infos = (system, tool_infos(system, self._instance_titles()))
        return self._infos[1]

    def _instance_titles(self) -> dict[str, str]:
        """How each system is called in sentences: the project's own names, then the catalog's."""

        from awa.connectors import list_connectors

        titles = {spec.name: spec.title for spec in list_connectors()}
        for key, options in self.blueprint().connectors.items():
            head, colon, _ = str(options.get("description") or "").partition(":")
            if colon and 0 < len(head.strip()) <= _TITLE_CHARS:
                titles[key] = head.strip()
        return titles

    def approval_reason(self, tool: str, info: ToolInfo | None = None) -> str:
        """Why a call of `tool` waits for a person, in words: `because the total is 5,000 EUR or
        more` (from the blueprint's approval rule)."""

        from awa.ui._plain import condition_words

        generic = "because your team asked to be asked before this step"
        try:
            blueprint = self.blueprint()
        except MissingConfigurationError:
            return generic
        for rule in blueprint.approvals:
            if tool in _tool_names(rule.tool):
                if rule.condition:
                    return "because " + condition_words(rule.condition, info.fields if info else {})
                if rule.when:
                    return "because a rule of your team says this one needs a person"
                return "because this step always needs a person's OK"
        reasons = [policy.reason for policy in blueprint.policies if policy.ask and policy.reason]
        return f"because {reasons[0].rstrip('.')}" if reasons else generic

    def try_it(self) -> dict[str, Any]:
        """What the Try it page needs: whether a system exists, whether a model is set up, and
        example requests taken from the project's eval cases."""

        report = environment(self.directory, self.environ)
        ready = self.model is not None or self.model_registry is not None
        data: dict[str, Any] = {
            "exists": self.blueprint_file.is_file(),
            "model_ready": ready or any(p.configured for p in report.providers),
            "examples": [],
        }
        if data["exists"]:
            seen: list[str] = []
            for case in self.cases():
                text = " ".join(case["input"].split())
                if text and text not in seen and len(text) <= _EXAMPLE_CHARS:
                    seen.append(text)
            data["examples"] = seen[:_EXAMPLES]
        return data

    def review(self) -> dict[str, Any]:
        """Everything the Review page shows (a missing blueprint gives `exists: False`)."""

        from awa.ui._diagram import blueprint_svg

        if not self.blueprint_file.is_file():
            return {"exists": False, "folder": str(self.directory)}
        text = self.blueprint_file.read_text(encoding="utf-8")
        blueprint = self.blueprint()
        gated = {approval.tool for approval in blueprint.approvals}
        connectors = sorted(
            {
                tool.removeprefix("connector:").split(".", 1)[0]
                for agent in blueprint.agents
                for tool in agent.tools
                if tool.startswith("connector:")
            }
            - set(blueprint.connectors)  # the project's own instances have connection steps
        )
        known = {c["name"]: c for c in self.setup()["connectors"]}
        guide = self.guide()
        return {
            "exists": True,
            "folder": str(self.directory),
            "name": blueprint.name,
            "description": blueprint.description,
            "structure": blueprint.resolved_entrypoint(),
            "agents": [
                {
                    "name": agent.name,
                    "description": agent.description,
                    "tools": list(agent.tools),
                    "approvals": [tool for tool in agent.tools if tool in gated],
                }
                for agent in blueprint.agents
            ],
            "diagram": blueprint_svg(blueprint),
            "issues": [issue_json(issue) for issue in self.check(text)],
            "yaml": text,
            "cases": self.cases(),
            "connectors": [
                known.get(name, {"name": name, "title": name, "configured": False, "missing": []})
                for name in connectors
            ],
            "steps": self._steps(guide),
            "connect": "\n".join(
                line for line in guide.splitlines() if not line.startswith("<!-- awa:step")
            ),
            "data": self.data_files(),
            "data_map": self.data_map(),
        }

    def guide(self) -> str:
        """The text of `CONNECT.md` (empty when missing)."""

        connect = self.directory / "CONNECT.md"
        return connect.read_text(encoding="utf-8") if connect.is_file() else ""

    def _steps(self, guide: str) -> list[dict[str, Any]]:
        """The steps of `CONNECT.md`: `ready` once off the sample data, and missing settings."""

        from awa.blueprints._project_files import parse_steps

        try:
            connectors = self.blueprint().connectors
        except MissingConfigurationError:
            connectors = {}
        steps = []
        for step in parse_steps(guide):
            names = [str(name) for name in step.get("env_vars") or ()]
            sample = connectors.get(str(step.get("key")), {"use": "workspace"}).get("use")
            steps.append(
                {
                    "system": str(step.get("system", "")),
                    "status": "ready" if sample != "workspace" else str(step.get("status", "")),
                    "connector": step.get("connector"),
                    "missing": [name for name in names if not self._is_set(name)],
                    "tools": sorted(step.get("switch") or ()),
                }
            )
        return steps

    def data_files(self) -> dict[str, Any]:
        """The local sample-data workspace (`data/`) the system works on until it is connected."""

        root = self.directory / "data"
        found = self.files_in("data")
        return {
            "exists": root.is_dir(),
            "folder": str(root),
            "count": len(found),
            "files": found[:_DATA_LISTED],
        }

    def files_in(self, folder: str) -> list[str]:
        """The files under `folder` of the project, as sorted paths relative to it."""

        root = self.directory / folder
        if not root.is_dir():
            return []
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())

    def check(self, text: str) -> list[BlueprintIssue]:
        """Check blueprint YAML against the project's tools; parse errors become issues."""

        try:
            blueprint = Blueprint.from_yaml_text(text)
        except BlueprintError as exc:
            return list(exc.issues) or [BlueprintIssue("error", "$", str(exc))]
        from awa.blueprints._resolve import file_modules_scope

        with file_modules_scope():
            return blueprint.check(tools=self.loaded_tools())

    def save_yaml(self, text: str) -> tuple[list[BlueprintIssue], bool]:
        """Check `text` and save it as the blueprint when it has no errors."""

        issues = self.check(text)
        if any(issue.severity == "error" for issue in issues):
            return issues, False
        self.blueprint_file.write_text(text, encoding="utf-8")
        self._system = None
        return issues, True

    def cases(self) -> list[dict[str, Any]]:
        """The eval cases, as plain data (empty when there are none)."""

        if not self.cases_file.is_file():
            return []
        return [
            {
                "id": case.id,
                "input": as_text(case.input),
                "expected": as_text(case.expected) if case.expected is not None else "",
                "checks": list(case.scorers),
            }
            for case in self.dataset().cases
        ]

    def dataset(self) -> Dataset:
        """The project's eval cases.

        Raises:
            MissingConfigurationError: If the project has none.
        """

        from awa.evals import Dataset

        if not self.cases_file.is_file():
            raise MissingConfigurationError("This system has no eval cases yet")
        return Dataset.load(self.cases_file)

    def system(self) -> AgentLike:
        """The built system with an in-memory checkpointer (cached until its inputs change)."""

        stamp = tuple(
            path.stat().st_mtime if path.is_file() else 0.0
            for path in (self.blueprint_file, self.tools_file)
        )
        if self._system is not None and self._system[0] == stamp:
            return self._system[1]
        system = self.build_system()
        self._system = (stamp, system)
        return system

    def build_system(self, blueprint: Blueprint | None = None) -> AgentLike:
        """Build a fresh system from the blueprint (or a variant of it, such as an eval case's
        with a system on its stand-in) and the project's tools and skills."""

        from awa._internal.upstream.checkpoint import StateInMemorySaver
        from awa.blueprints._resolve import file_modules_scope, skill_registry

        blueprint = blueprint or self.blueprint()
        with file_modules_scope():
            return blueprint.build(
                tools=self.loaded_tools(),
                skill_registry=skill_registry([], self.blueprint_file),
                model_registry=self.model_registry,
                checkpointer=StateInMemorySaver(),
                allow_imports=True,
            )

    def loaded_tools(self) -> list[Any] | None:
        """The tools of `tools.py` (`None` without one)."""

        from awa.blueprints._resolve import load_tools

        if not self.tools_file.is_file():
            return None
        return load_tools([str(self.tools_file)])

    def deploy(self) -> dict[str, Any]:
        """The commands that serve, publish and evaluate the system, run in the project folder."""

        awa = _awa_command()
        # Relative paths: the commands run in the project folder (shorter, and portable).
        blueprint = "blueprint.yaml"
        commands = [
            {
                "title": "Serve it as a web API for other programs",
                "command": f"{awa} serve {blueprint}",
                "note": "Add --api-key-env NAME before other computers may call it.",
            },
            {
                "title": "Use it from a desktop assistant or IDE (MCP)",
                "command": f"{awa} mcp serve {blueprint}",
                "note": "Add this command as an MCP server in the assistant's settings.",
            },
            {
                "title": "Chat with it in a terminal",
                "command": f"{awa} chat {blueprint}",
                "note": "",
            },
        ]
        if self.cases_file.is_file():
            commands.append(
                {
                    "title": "Check it automatically (for example before each change)",
                    "command": f"{awa} eval {blueprint} evals/cases.yaml",
                    "note": "Exit code 1 when an eval case fails.",
                }
            )
        steps = self._steps(self.guide())
        return {
            "folder": str(self.directory),
            "commands": commands,
            "triggers": [asdict(step) for step in self._trigger_steps()],
            "trigger_kinds": list(KINDS),
            "automation": self.operation.triggers(),
            "keep_running": self.operation.keep_running(),
            "data": self.data_files(),
            "not_connected": [step["system"] for step in steps if step["status"] != "ready"],
        }

    def _trigger_steps(self) -> list[Step]:
        """How to run each trigger of the blueprint (empty without a blueprint or triggers)."""

        if not self.blueprint_file.is_file():
            return []
        return trigger_steps(self.blueprint(), "blueprint.yaml")

    def add_trigger(self, kind: str, name: str, settings: Mapping[str, str]) -> dict[str, Any]:
        """Write a trigger of `kind` (options of `trigger_entry`) into the blueprint.

        Raises:
            ConfigurationError: If a setting is unknown or the trigger is invalid.
        """

        unknown = sorted(set(settings) - _TRIGGER_SETTINGS)
        if unknown:
            raise ConfigurationError(f"Unknown trigger setting: {', '.join(unknown)}")
        entry = trigger_entry(
            kind, name.strip() or None, **{key: value for key, value in settings.items() if value}
        )
        self.blueprint()  # no system yet: say so
        add_trigger(self.blueprint_file, entry)
        self._system = None
        return {"name": entry["name"], "steps": [asdict(s) for s in self._trigger_steps()]}

    async def approvals(self) -> list[dict[str, Any]]:
        """The runs of this project's local store that wait for an approval, oldest first, each
        open approval as a card (what happens, the values with titles, why a person decides)."""

        from awa.ui._try import Narrator

        if not self.runs_file.is_file():
            return []
        runs = await self._with_runs(lambda store: store.list(status="waiting", limit=200))
        narrator = Narrator(self)
        return [_waiting(run, narrator) for run in sorted(runs, key=lambda run: run.created_at)]

    async def decide(
        self, run_id: str, interrupt_id: str, *, approve: bool, note: str, args: str
    ) -> dict[str, Any]:
        """Answer one open approval of a waiting run, as `awa runs decide` does.

        `args` is a JSON object of tool arguments to run the call with instead (`""`: none).
        """

        from awa.automation._decision import approval_decision
        from awa.automation._time import utcnow
        from awa.automation.automation import decide_run

        edited = _json_object(args) if args.strip() else None
        decision = approval_decision(approve, note.strip() or None, edited)
        run = await self._with_runs(
            lambda store: decide_run(
                store, run_id, decision, now=utcnow(), interrupt_id=interrupt_id, actor="web app"
            )
        )
        return {"run": run.id, "status": run.status, "open": len(_open_approvals(run))}

    async def _with_runs(self, action: Callable[[Any], Awaitable[Any]]) -> Any:
        from awa.ui._operate import local_runs

        async with local_runs(self.runs_file) as store:
            return await action(store)

    def export_zip(self) -> bytes:
        """The project folder as a zip of the files that may leave the machine (`export_files`).

        Raises:
            TooManyFilesError: If the project has more than 2000 files to export.
        """

        from awa.server._builds import export_files

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for relative in export_files(self.directory):
                archive.write(self.directory / relative, relative.as_posix())
        return buffer.getvalue()

    def explain(self, exc: BaseException) -> Explanation:
        """`exc` in plain language with the fix; credentials are redacted from the message."""

        message = _redact(str(exc) or type(exc).__name__, self.env_file)
        lowered, kind = message.lower(), type(exc).__name__.lower()
        missing = isinstance(exc, MissingConfigurationError)
        if missing and "no system" in lowered:
            return Explanation(message, "Go to Describe and create a system first.")
        if missing and "eval cases" in lowered:
            return Explanation(message, "Create the system on Describe with testing switched on.")
        if isinstance(exc, OptionalDependencyError):
            return Explanation(message, "Run the awa installer again; it installs every part.")
        if isinstance(exc, BlueprintError):
            return Explanation(message, "Open Review, correct the listed problems and save again.")
        if isinstance(exc, DataError):
            return Explanation(message, _DATA_FIXES.get(exc.code, _DATA_FIX))
        problem = classify_model_error(exc) if isinstance(exc, Exception) else None
        if problem is not None:
            return _model_problem(problem.kind, message)
        if "authentication" in kind or "permission" in kind or "api key" in lowered:
            return _model_problem("authentication", message)
        if missing:
            return Explanation(message, "Enter the missing setting on the Set up page.", *_SETUP)
        if isinstance(exc, UsageLimitExceededError):
            return Explanation(
                message,
                "The run reached the limits of this system (the `limits:` in the blueprint). "
                "Raise them on Review, or ask for something smaller.",
            )
        if isinstance(exc, ValueError | AwaError):
            return Explanation(message, "Correct the input and try again.")
        if "connect" in kind or "timeout" in kind:
            return _model_problem("connection", message)
        return Explanation(
            f"{type(exc).__name__}: {message}",
            "Try again; if it keeps failing, the awa window shows more details.",
        )


_DATA_FIX = "Check the file, or remove it and drop it again."
_DATA_FIXES = {
    "awa.data.too_large": "Use a smaller file, split it, or remove files you no longer need.",
    "awa.data.too_many_files": "Remove files you no longer need, or put them in a zip archive.",
    "awa.data.type_mismatch": "Rename the file so its ending says what it is, or export it again "
    "from the program that made it.",
    "awa.data.unsupported_type": "Use one of the supported types: Excel, CSV, PDF, Word, email "
    "(.eml, .mbox), JSON, text, or a SQLite database.",
    "awa.data.zip_unsafe": "Open the archive yourself and drop the files instead.",
}
_SETUP = ("#setup", "Check your model settings")
_MODEL_PROBLEMS: dict[str, tuple[str, str, tuple[str, str]]] = {
    "rate_limit": (
        "The AI service is busy right now (too many requests).",
        "Wait a minute and try again. If it keeps happening, ask your IT team for a higher limit.",
        ("", ""),
    ),
    "timeout": ("The AI service took too long to answer.", "Try again in a moment.", ("", "")),
    "connection": (
        "awa could not reach the AI service.",
        "Check your internet connection and, for a company endpoint, its address on the Set up page.",
        _SETUP,
    ),
    "server_error": (
        "The AI service reported a problem on its side.",
        "Try again later.",
        ("", ""),
    ),
    "permission_denied": (
        "Your key is not allowed to use this model.",
        "Ask your administrator to allow it, or choose another model on the Set up page.",
        _SETUP,
    ),
    "not_found": (
        "The AI service does not know this model.",
        "Check the model name (for Azure also the deployment name) on the Set up page.",
        _SETUP,
    ),
    "context_overflow": (
        "That was too much text for the model at once.",
        "Use a shorter message or smaller files and try again.",
        ("", ""),
    ),
}


def _model_problem(kind: str, message: str) -> Explanation:
    """A failed model request in plain words, with the fix and, when settings are the cause, the
    link to the Set up page."""

    lowered = message.lower()
    if kind == "authentication":
        if "no api key" in lowered or "could not resolve" in lowered or "not set" in lowered:
            return Explanation(
                "No key for the AI model is set up yet.",
                "Enter the key of your AI provider on the Set up page.",
                "#setup",
                "Set up your model first",
                detail=message,
            )
        return Explanation(
            "Your AI provider did not accept the key.",
            "Check the key (and the endpoint) on the Set up page.",
            *_SETUP,
            detail=message,
        )
    known = _MODEL_PROBLEMS.get(kind)
    if known is None:
        return Explanation(message, "Try again; if it keeps failing, the awa window shows more.")
    return Explanation(known[0], known[1], *known[2], detail=message)


def _tool_names(reference: str) -> set[str]:
    """The names a blueprint tool reference goes by in a run: `connector:mail.send` -> `mail_send`."""

    names = {reference}
    if reference.startswith("connector:"):
        names.add(reference.removeprefix("connector:").replace(".", "_"))
    elif reference.startswith("mcp:"):
        names.add(reference.rsplit("/", 1)[-1])
    elif ":" in reference:
        names.add(reference.split(":", 1)[1])
    return names


def _open_approvals(run: Any) -> list[Any]:
    return [wait for wait in run.waits if not wait.answered and wait.kind == "approval"]


def _waiting(run: Any, narrator: Narrator) -> dict[str, Any]:
    """A waiting run for the Approvals page: what it wants to do, one entry per open approval."""

    asked = []
    for wait in _open_approvals(run):
        call = wait.value if isinstance(wait.value, dict) else {"request": wait.value}
        asked.append(
            {
                "interrupt_id": wait.interrupt_id,
                "tool": str(call.get("tool", "an action")),
                "args": json.dumps(call.get("args", call), ensure_ascii=False, indent=2)[:2000],
                "card": narrator.card(wait.value, wait.interrupt_id),
            }
        )
    return {
        "id": run.id,
        "automation": run.automation,
        "trigger": run.trigger or "",
        "created": run.created_at.astimezone().strftime("%Y-%m-%d %H:%M"),
        "input": as_text(run.input)[:300],
        "approvals": asked,
    }


def _json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ConfigurationError(f"The changed arguments are not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise ConfigurationError('The changed arguments must be a JSON object, e.g. {"amount": 90}')
    return value


def issue_json(issue: BlueprintIssue) -> dict[str, str]:
    return {"severity": issue.severity, "path": issue.path, "message": issue.message}


_PROGRESS_FACTS = ("cases", "id", "status", "flaky", "passed", "total", "name", "files")


def _progress(progress: Any) -> dict[str, Any]:
    """A build step for the page: the message, the stage and event, and the few facts the
    progress view shows (case ids and outcomes, counts); never a file's content."""

    message = getattr(progress, "message", None) or str(progress)
    data: dict[str, Any] = {"message": message}
    for name in ("stage", "round", "pass_rate", "event"):
        value = getattr(progress, name, None)
        if isinstance(value, str | int | float) and value != "":
            data[name] = value
    facts = getattr(progress, "data", None) or {}
    for name in _PROGRESS_FACTS:
        value = facts.get(name)
        if isinstance(value, str | int | float | bool) or (
            isinstance(value, list) and all(isinstance(item, str) for item in value)
        ):
            data[name] = value
    return data


def _awa_command() -> str:
    if shutil.which("awa"):
        return "awa"
    scripts = Path(sys.executable).parent
    for candidate in (scripts / "awa.exe", scripts / "awa"):
        if candidate.is_file():
            return _quote(candidate)
    return f"{_quote(Path(sys.executable))} -m awa"


def _quote(path: Path) -> str:
    text = str(path)
    return f'"{text}"' if any(c in text for c in " &()'") else text
