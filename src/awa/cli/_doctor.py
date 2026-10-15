"""`awa doctor`: check that this machine can run awa, and say how to fix it (exit 0 usable,
1 blocked, 78 a needed key or variable missing)."""

from __future__ import annotations

import argparse
import os
import platform
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urlsplit

from awa._internal.install import Install, safe_find_spec
from awa._internal.util import is_within
from awa.cli._common import EXIT_CONFIG, EXIT_ERROR, EXIT_OK, Commands, add_command, print_json

if TYPE_CHECKING:
    from awa.connectors import EnvironmentReport
    from awa.core.config import LoadedEnvironment
    from awa.core.settings import Settings
    from awa.models import ModelRegistry, ModelSettings

LIVE_POINTER = (
    "doctor checks this machine and the settings only; `--check-models` calls the models, "
    "`awa check` Microsoft 365."
)
"""The last line of the doctor report."""

PYTHON_FLOOR = (3, 11)
"""The oldest Python awa supports (`requires-python = ">=3.11"`)."""

DoctorStatus = Literal["ok", "info", "warn", "fail"]
"""Outcome of one check: `fail` blocks use, `warn` needs attention, `info` is a note."""

EXTRAS: dict[str, tuple[str, ...]] = {
    "anthropic": ("langchain_anthropic",),
    "openai": ("langchain_openai",),
    "postgres": ("langgraph.checkpoint.postgres", "psycopg", "psycopg_pool"),
    "mcp": ("langchain_mcp_adapters", "mcp"),
    "server": ("fastapi", "uvicorn"),
    "otel": ("opentelemetry.trace",),
    "langfuse": ("opentelemetry.sdk.trace", "opentelemetry.exporter.otlp.proto.http"),
    "docs": ("mkdocs", "material", "mkdocstrings"),
}
"""The optional extras of `awa-agents` and the modules that prove each is installed."""

_CORE_MODULES = (
    "langchain", "langchain_core", "langgraph", "langgraph.checkpoint", "pydantic",
    "pydantic_settings", "yaml", "typing_extensions",
)  # fmt: skip
_SETUP_HINT = 'delete it, then run: uv venv && uv pip install -e ".[dev]" (in the awa folder)'
_PROBE_PROMPT = "Reply with the single word: OK"
_MODEL_USER = "model profile "


@dataclass(frozen=True)
class DoctorCheck:
    """One finding of `awa doctor`.

    Attributes:
        name: Short identifier, for example `python`, `extra:postgres` or `credentials:anthropic`.
        status: `ok`, `info`, `warn` or `fail` (`fail` blocks use).
        fix: The command or action that resolves the finding, if any.
        data: Machine-readable facts for `--json` (never secret values).
    """

    name: str
    status: DoctorStatus
    summary: str
    details: tuple[str, ...] = ()
    fix: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DoctorReport:
    """Everything `awa doctor` found, in the order it was checked."""

    checks: tuple[DoctorCheck, ...]

    @property
    def usable(self) -> bool:
        """Whether nothing blocks use (no check failed)."""

        return not self.failures

    @property
    def failures(self) -> tuple[DoctorCheck, ...]:
        """The checks with status `fail`."""

        return tuple(check for check in self.checks if check.status == "fail")

    @property
    def warnings(self) -> tuple[DoctorCheck, ...]:
        """The checks with status `warn`."""

        return tuple(check for check in self.checks if check.status == "warn")

    @property
    def missing_required(self) -> tuple[DoctorCheck, ...]:
        """Warnings about a required part that is missing: a model key, or a variable an
        enabled trigger reads (`awa doctor` exits with 78 for them)."""

        return tuple(c for c in self.warnings if c.data.get("required"))

    def check(self, name: str) -> DoctorCheck | None:
        """Return the check called `name`, or `None`."""

        return next((check for check in self.checks if check.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        """The report as JSON-compatible data (what `awa doctor --json` prints)."""

        return {
            "usable": self.usable,
            "failures": len(self.failures),
            "warnings": len(self.warnings),
            "missing_required": [check.name for check in self.missing_required],
            "checks": [{**asdict(check), "details": list(check.details)} for check in self.checks],
        }

    def render(self) -> str:
        """The report as plain ASCII text for a terminal."""

        width = max((len(check.name) for check in self.checks), default=0)
        lines = ["awa doctor", ""]
        for check in self.checks:
            lines.append(f"  {check.status.upper():<5} {check.name:<{width}}  {check.summary}")
            indent = " " * (width + 10)
            lines.extend(f"{indent}{detail}" for detail in check.details)
            if check.fix:
                lines.append(f"{indent}fix: {check.fix}")
        failures, warnings = len(self.failures), len(self.warnings)
        lines.append("")
        if failures:
            lines.append(
                f"Result: NOT usable - {failures} problem(s) block use, {warnings} warning(s)."
            )
        else:
            suffix = f" with {warnings} warning(s)" if warnings else ""
            lines.append(f"Result: usable{suffix}.")
        lines.append(LIVE_POINTER)
        return "\n".join(lines)


def run_doctor(
    *,
    check_models: bool = False,
    check_postgres: bool = False,
    check_langfuse: bool = False,
    settings: Settings | None = None,
    registry: ModelRegistry | None = None,
    cwd: Path | None = None,
    environment: EnvironmentReport | None = None,
) -> DoctorReport:
    """Check the interpreter, environment, installation and configuration.

    Args:
        check_models: Make one tiny real model call per model profile (costs tokens).
        check_postgres: Connect to `AWA_POSTGRES_DSN` and run `SELECT version()`.
        check_langfuse: Ask the Langfuse server for its health and check the keys; sends no trace.
        settings: Settings to check; by default loaded from the environment and `cwd/.env`,
            reporting validation errors instead of raising.
        registry: Model profiles for `check_models`; defaults to the `default`, `fast` and
            `strong` profiles of `settings`.
        environment: What `awa.connectors.discover` found; discovered from `cwd` by default.
    """

    from awa.core.config import current

    if cwd is None:
        cwd = Path.cwd()
        loaded = current()
        env_file = loaded.dotenv_file() or loaded.project_dir / ".env"
    else:
        env_file = cwd / ".env"
    project = env_file.parent  # where the settings come from, also with `awa --project`
    install = _Install.detect()
    core = _check_core(install)
    checks = [
        _check_python(sys.version_info[:3], platform.python_implementation(), sys.executable),
        *_check_environment(Path(sys.prefix), Path(sys.base_prefix), project),
        install.check(),
        core,
    ]
    if core.status == "ok":
        checks.append(_check_upstream())
    checks.append(_check_env_file(project))
    problems: list[str] = []
    if settings is None:
        settings, problems = _load_settings(env_file if env_file.is_file() else None)
    checks.append(_settings_check(settings, problems))
    checks += configuration_checks(env_file, settings)
    providers = _profile_providers(settings)
    checks += _check_extras(install, _needed_extras(settings, providers))
    checks += _check_providers(settings, providers, install, env_file)
    if "azure_openai" in providers and (azure := _check_azure(settings)) is not None:
        checks.append(azure)
    checks += _check_systems(environment, env_file)
    checks.append(_check_preview())
    if (postgres := _check_postgres(settings, connect=check_postgres)) is not None:
        checks.append(postgres)
    if (langfuse := _check_langfuse(settings, connect=check_langfuse)) is not None:
        checks.append(langfuse)
    checks += _check_security(project)
    if check_models:
        checks.extend(_check_models(settings, registry))
    return DoctorReport(tuple(checks))


def configuration_checks(env_file: Path, settings: Settings) -> list[DoctorCheck]:
    """Where each value comes from, the provider, typos, overrides, skipped checks, `.env`
    permissions and trigger variables; a foreign `env_file`'s folder counts as the project."""

    loaded = _environment_of(env_file)
    checks = [_sources_check(loaded), _provider_check(settings)]
    checks += [
        DoctorCheck(
            f"typo:{unknown.name}",
            "warn",
            unknown.message(),
            fix=f"rename it to {unknown.suggestion}" if unknown.suggestion else "remove it",
            data={"suggestion": unknown.suggestion},
        )
        for unknown in loaded.unknown
    ]
    checks += _environment_notes(loaded)
    if (exposure := _env_file_exposure(loaded.dotenv_file())) is not None:
        checks.append(exposure)
    checks += _trigger_checks(loaded)
    return checks


def _environment_of(env_file: Path) -> LoadedEnvironment:
    """The loaded environment, or the one of `env_file`'s folder when that is another project."""

    from awa.core import config

    loaded = config.current()
    loaded_file = loaded.dotenv_file()
    if loaded_file is not None:
        same = _same_path(loaded_file, env_file)
    else:
        same = _same_path(loaded.project_dir, env_file.parent)
    if same:
        return loaded
    return config.LoadedEnvironment(
        project_dir=env_file.parent, dotenv_path=env_file if env_file.is_file() else None
    )


def _sources_check(loaded: LoadedEnvironment) -> DoctorCheck:
    from awa.cli._env import setting_row, setting_rows
    from awa.core.config import current

    rows = [
        (row["name"], row["value"], row["source"], ", ".join(row["used_by"]))
        for row in (setting_row(name, loaded) for name in _shown_settings(loaded))
    ]
    width = max((len(row[0]) for row in rows), default=4)
    source_width = max((len(row[2]) for row in rows), default=6)
    table = [f"{'NAME':<{width}}  {'VALUE':<24}  {'SOURCE':<{source_width}}  USED BY"]
    table += [f"{n:<{width}}  {v[:24]:<24}  {s:<{source_width}}  {u}" for n, v, s, u in rows]
    from_dotenv = sum(1 for row in rows if row[2].startswith((".env", "~/")))
    return DoctorCheck(
        "sources",
        "info",
        f"{len(rows)} setting(s) shown, {from_dotenv} from .env (secrets show only set)",
        tuple(table),
        data={"settings": setting_rows() if loaded is current() else []},
    )


def _provider_check(settings: Settings) -> DoctorCheck:
    from awa.core.providers import complete_providers, detect

    lookup = settings.credential
    chosen = settings.detected_provider
    complete = [item.name for item in complete_providers(lookup)]
    first = detect(lookup)
    if chosen is not None:
        summary = f"provider: {chosen} (chosen from the credentials found: {', '.join(complete)})"
    elif first is not None:
        summary = f"provider: {first.name} (credentials found: {', '.join(complete)})"
    else:
        summary = "provider: none (no model key is set)"
    return DoctorCheck(
        "provider",
        "ok" if first is not None else "warn",
        summary,
        fix=None if first is not None else "run awa setup, or set a key in .env (awa env set)",
        data={"provider": chosen or (first.name if first else None), "complete": complete},
    )


def _environment_notes(loaded: LoadedEnvironment) -> list[DoctorCheck]:
    """Overrides of `.env`, names not exported, LangSmith tracing from `.env`, skipped checks."""

    from awa.core.config import preflight_skipped

    notes: list[DoctorCheck] = []
    shadowed = sorted(name for name in loaded.shadowed if name not in loaded.exported)
    if shadowed:
        notes.append(
            DoctorCheck(
                "overrides",
                "info",
                f"env overrides .env for {', '.join(shadowed)}",
                data={"names": shadowed},
            )
        )
    if loaded.not_exported:
        notes.append(
            DoctorCheck(
                "not_exported",
                "info",
                f"not exported (unknown): {len(loaded.not_exported)} name(s) in .env",
                data={"count": len(loaded.not_exported)},
            )
        )
    langsmith = str(loaded.get("LANGSMITH_TRACING") or "").lower() in {"1", "true", "yes"}
    if langsmith and loaded.source_of("LANGSMITH_TRACING").origin == "dotenv":
        notes.append(DoctorCheck("langsmith", "info", "LANGSMITH_TRACING from .env is active"))
    if preflight_skipped():
        notes.append(
            DoctorCheck("preflight", "warn", "--skip-preflight used: configuration checks skipped")
        )
    return notes


def _shown_settings(loaded: LoadedEnvironment) -> list[str]:
    """The model profiles and run settings, plus every setting that has a value."""

    from awa.core.settings_registry import DOCTOR_SHOWN, REGISTRY

    names = list(DOCTOR_SHOWN)
    names += [n for n in REGISTRY if n not in names and (loaded.get(n) or "").strip()]
    names += [n for n in sorted(loaded.declared) if n not in names]
    return names


def _env_file_exposure(path: Path | None) -> DoctorCheck | None:
    """A warning when others can read the `.env` (POSIX mode, or a broad Windows ACL)."""

    if path is None or not path.is_file():
        return None
    if os.name == "posix":
        mode = path.stat().st_mode & 0o077
        if not mode:
            return None
        return DoctorCheck(
            "env_permissions",
            "warn",
            ".env readable by others",
            fix=f"chmod 600 {path.name}",
            data={"mode": oct(path.stat().st_mode & 0o777)},
        )
    import subprocess

    icacls = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "icacls.exe"
    try:
        result = subprocess.run(  # noqa: S603 - a fixed system tool, no shell
            [str(icacls), str(path)], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    broad = ("Everyone", "BUILTIN\\Users", "NT AUTHORITY\\Authenticated Users", "Jeder")
    if result.returncode == 0 and any(name in result.stdout for name in broad):
        return DoctorCheck(
            "env_permissions",
            "warn",
            ".env readable by others (its ACL lists a group of all users)",
            fix="awa env set rewrites it for your account only; or remove the group with icacls",
        )
    return None


def _trigger_checks(loaded: LoadedEnvironment) -> list[DoctorCheck]:
    """The variables the triggers of the project's blueprint read, when one is missing."""

    blueprint = loaded.project_dir / "blueprint.yaml"
    if not blueprint.is_file():
        return []
    from awa.cli._preflight import trigger_env

    needed = trigger_env(blueprint)
    missing = {name: who for name, who in needed.items() if not (loaded.get(name) or "").strip()}
    if not missing:
        return []
    return [
        DoctorCheck(
            "triggers",
            "warn",
            f"{len(missing)} variable(s) the triggers need are not set",
            tuple(f"{name}: needed by {who}" for name, who in missing.items()),
            fix=f"awa env set {next(iter(missing))}",
            data={"missing": sorted(missing), "required": True},
        )
    ]


def _check_python(version: Sequence[int], implementation: str, executable: str) -> DoctorCheck:
    text = ".".join(map(str, version))
    floor = ".".join(map(str, PYTHON_FLOOR))
    data = {"version": text, "implementation": implementation, "executable": executable}
    details = (executable, f"{platform.system()} {platform.release()} ({platform.machine()})")
    if tuple(version[:2]) < PYTHON_FLOOR:
        summary = f"{implementation} {text} is older than the required {floor}"
        fix = (
            "uv python install 3.13 (no admin rights needed), then run the installer again "
            "(scripts/install-windows.cmd, scripts/install-linux.sh or "
            "scripts/install-macos.command)"
        )
        return DoctorCheck("python", "fail", summary, details, fix, data)
    summary = f"{implementation} {text} (needs >= {floor})"
    return DoctorCheck("python", "ok", summary, details, data=data)


def read_pyvenv_cfg(venv: Path) -> dict[str, str] | None:
    """Return the `key = value` pairs of `venv/pyvenv.cfg` (keys lower-cased), or `None`."""

    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip().lower()] = value.strip()
    return values


def venv_problem(venv: Path) -> str | None:
    """Explain why the virtual environment at `venv` cannot work here (its `pyvenv.cfg` base
    interpreter is missing), or return `None`."""

    config = read_pyvenv_cfg(venv)
    if config is None:
        return "it has no pyvenv.cfg, so it is not a working virtual environment"
    home = config.get("home")
    if not home:
        return "its pyvenv.cfg has no 'home' entry"
    home_path = Path(home)
    if not home_path.exists():
        return (
            f"its base interpreter directory {home} does not exist here "
            "(the environment was created on another machine or by another user)"
        )
    users = Path.home().parent
    if (
        users != Path(users.anchor)
        and is_within(home_path, users)
        and not is_within(home_path, Path.home())
    ):
        return f"its base interpreter {home} belongs to another user's profile"
    interpreter = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not interpreter.exists():
        return f"its interpreter {interpreter} is missing or a dangling link"
    return None


def _check_environment(prefix: Path, base_prefix: Path, cwd: Path) -> list[DoctorCheck]:
    checks = [_check_active_environment(prefix, base_prefix)]
    local = cwd / ".venv"
    if local.is_dir() and not _same_path(local, prefix):
        problem = venv_problem(local)
        data = {"path": str(local), "healthy": problem is None}
        if problem is not None:
            summary, fix = f".venv in this directory is broken: {problem}", _SETUP_HINT
            checks.append(DoctorCheck("local-venv", "warn", summary, (str(local),), fix, data))
        else:
            activate = (
                r".venv\Scripts\Activate.ps1" if os.name == "nt" else "source .venv/bin/activate"
            )
            summary = ".venv in this directory is healthy but not the one running awa doctor"
            fix = f"activate it: {activate}"
            checks.append(DoctorCheck("local-venv", "info", summary, (str(local),), fix, data))
    return checks


def _check_active_environment(prefix: Path, base_prefix: Path) -> DoctorCheck:
    in_venv = prefix != base_prefix
    data: dict[str, Any] = {"prefix": str(prefix), "virtual_environment": in_venv}
    if in_venv:
        config = read_pyvenv_cfg(prefix) or {}
        creator = "uv" if "uv" in config else "venv"
        data.update(creator=creator, home=config.get("home"))
        problem = venv_problem(prefix)
        running = ".".join(map(str, sys.version_info[:2]))
        recorded = config.get("version_info") or config.get("version") or ""
        if problem is None and recorded and not recorded.startswith(running):
            problem = f"it was created for Python {recorded}, but runs {running}"
        if problem is None:
            summary = f"virtual environment {prefix} ({creator})"
            return DoctorCheck("environment", "ok", summary, data=data)
        summary = f"the active virtual environment is unhealthy: {problem}"
        return DoctorCheck("environment", "warn", summary, (str(prefix),), _SETUP_HINT, data)
    conda = os.environ.get("CONDA_PREFIX")
    if conda and _same_path(Path(conda), prefix):
        return DoctorCheck("environment", "ok", f"conda environment {prefix}", data=data)
    return DoctorCheck(
        "environment",
        "warn",
        "not running in a virtual environment (packages go into the global interpreter)",
        (str(prefix),),
        fix="create a project environment: uv venv (or python -m venv .venv), then install awa into it",
        data=data,
    )


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


WINDOWS_PATH_LIMIT = 260
"""Windows refuses file paths longer than this unless long paths are enabled."""

LONGEST_PACKAGE_PATH = 140
"""Characters of the longest file path below site-packages in a full awa install (measured: 133)."""


def _long_path_problem(location: Path | None) -> str | None:
    """A sentence when awa sits so deep on Windows that dependency files may exceed the path limit."""

    if sys.platform != "win32" or location is None:
        return None
    deepest = len(str(location.parent)) + 1 + LONGEST_PACKAGE_PATH
    if deepest <= WINDOWS_PATH_LIMIT:
        return None
    return (
        f"awa is installed in a deep folder ({location.parent}); its longest file paths can pass "
        f"the Windows limit of {WINDOWS_PATH_LIMIT} characters, which breaks `awa` commands "
        "with an ImportError"
    )


@dataclass(frozen=True)
class _Install(Install):
    """The detected install (`awa._internal.install`) plus its `awa doctor` check."""

    def check(self) -> DoctorCheck:
        data: dict[str, Any] = {
            "version": self.version,
            "location": str(self.location) if self.location else None,
            "kind": "editable" if self.editable_root else "wheel" if self.version else None,
        }
        if self.version is None:
            return DoctorCheck(
                "awa",
                "warn",
                f"awa is imported from {self.location} without being installed "
                "(no package metadata, no `awa` command)",
                fix=self.command('-e ".[dev,docs]"') + " (in the awa folder)",
                data=data,
            )
        details: tuple[str, ...] = (f"imported from {self.location}",)
        if self.editable_root is not None:
            details += (f"editable source {self.editable_root}",)
            expected = self.editable_root / "src" / "awa"
        else:
            expected = self.installed_at or Path()
        if self.location is not None and not _same_path(self.location, expected):
            return DoctorCheck(
                "awa",
                "warn",
                f"awa {self.version} is installed at {expected}, but Python imports {self.location}",
                ("a local folder or PYTHONPATH entry named 'awa' shadows the installed package",),
                fix="run from another directory or remove the shadowing 'awa' folder",
                data=data,
            )
        long_path = _long_path_problem(self.location)
        if long_path is not None:
            return DoctorCheck(
                "awa",
                "warn",
                long_path,
                details,
                fix="install awa into a short folder (for example C:\\awa), or enable Windows "
                "long paths (https://learn.microsoft.com/windows/win32/fileio/maximum-file-path-limitation)",
                data=data,
            )
        summary = f"awa {self.version} ({data['kind']} install)"
        return DoctorCheck("awa", "ok", summary, details, data=data)


def _check_core(install: _Install) -> DoctorCheck:
    missing = [name for name in _CORE_MODULES if safe_find_spec(name) is None]
    if not missing:
        return DoctorCheck(
            "dependencies", "ok", "core dependencies importable", data={"missing": []}
        )
    summary = f"core dependencies are missing: {', '.join(missing)}"
    fix = install.awa_command(options="--force-reinstall ")
    return DoctorCheck("dependencies", "fail", summary, fix=fix, data={"missing": missing})


PREVIEW_MODULES = (
    "awa.automation", "awa.checks", "awa.data", "awa.directory", "awa.ext", "awa.kernel",
    "awa.messaging", "awa.people", "awa.server", "awa.tasks", "awa.team.network",
    "awa.testing.contracts", "awa.ui",
)  # fmt: skip
"""The modules that declare `__stability__ = "preview"` (a test keeps this list exact)."""


def _check_preview() -> DoctorCheck:
    """Which parts carry no stability promise yet (`docs/about/stability.md`)."""

    from awa.cli import PREVIEW_COMMANDS, PREVIEW_SUBCOMMANDS

    commands = sorted([*PREVIEW_COMMANDS, *PREVIEW_SUBCOMMANDS])
    data = {"modules": list(PREVIEW_MODULES), "commands": commands}
    details = (
        "modules: " + ", ".join(PREVIEW_MODULES),
        "commands: " + (", ".join(commands) or "none"),
    )
    return DoctorCheck(
        "preview", "info", "preview parts: no stability promise yet", details, data=data
    )


def _check_upstream() -> DoctorCheck:
    """The LangChain/LangGraph releases in use and whether the MCP SDK is installed."""

    try:
        from awa._internal.upstream import upstream_versions

        versions = upstream_versions()
    except ImportError as exc:  # an API awa needs is missing
        return DoctorCheck(
            "upstream",
            "fail",
            "the installed LangChain/LangGraph lack an API awa needs",
            (str(exc),),
            fix='pip install "langchain>=1.4.3,<2" "langchain-core>=1.6.7,<2" "langgraph>=1.2.14,<2"',
            data={"error": str(exc)},
        )
    mcp_installed = all(safe_find_spec(module) is not None for module in EXTRAS["mcp"])
    shown = ("langchain", "langchain-core", "langgraph", "langgraph-checkpoint")
    summary = ", ".join(f"{name} {versions[name]}" for name in shown if versions.get(name))
    details = (f"MCP: {'installed' if mcp_installed else 'not installed'}",)
    data = {"versions": versions, "mcp": mcp_installed}
    return DoctorCheck("upstream", "ok", summary, details, data=data)


def _check_extras(install: _Install, needed: dict[str, list[str]]) -> list[DoctorCheck]:
    """A missing extra fails when a configured backend needs it; a model provider's extra is
    only a warning, since awa starts without it and the fix is one install."""
    checks = []
    for extra, modules in EXTRAS.items():
        name = f"extra:{extra}"
        missing = [module for module in modules if safe_find_spec(module) is None]
        data = {
            "installed": not missing,
            "missing_modules": missing,
            "needed_by": ", ".join(needed[extra]) if extra in needed else None,
        }
        fix = install.awa_command(extra)
        if not missing:
            checks.append(DoctorCheck(name, "ok", "installed", data=data))
        elif extra in needed:
            summary = f"not installed, but needed by {data['needed_by']}"
            models_only = all(user.startswith(_MODEL_USER) for user in needed[extra])
            status: DoctorStatus = "warn" if models_only else "fail"
            checks.append(DoctorCheck(name, status, summary, fix=fix, data=data))
        else:
            checks.append(DoctorCheck(name, "info", "not installed", fix=fix, data=data))
    return checks


def _check_env_file(cwd: Path) -> DoctorCheck:
    env_file = cwd / ".env"
    if env_file.is_file():
        return DoctorCheck(".env", "ok", f"found {env_file}", data={"path": str(env_file)})
    fix = None
    if (cwd / ".env.example").is_file():
        fix = "copy .env.example .env" if os.name == "nt" else "cp .env.example .env"
    summary = f"no .env in {cwd}; settings come from environment variables only"
    return DoctorCheck(".env", "info", summary, fix=fix, data={"path": None})


def _load_settings(env_file: Path | None) -> tuple[Settings, list[str]]:
    from pydantic import ValidationError

    from awa.core.settings import Settings

    try:
        return Settings(_env_file=env_file), []
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
    fields = Settings.model_fields
    problems: list[str] = []
    overrides: dict[str, Any] = {}
    for error in errors:
        location = str(error["loc"][0]) if error["loc"] else ""
        name = next(
            (
                field_name
                for field_name, info in fields.items()
                if location.lower() in {field_name.lower(), (info.alias or "").lower()}
            ),
            None,
        )
        if name is None:
            problems.append(f"{location}: {error['msg']}")
            continue
        variable = fields[name].alias or f"AWA_{name.upper()}"
        problems.append(f"{variable}: {error['msg']}")
        overrides[name] = fields[name].default
    try:
        # Check everything else with the defaults in place of the invalid values.
        return Settings(_env_file=env_file, **overrides), problems
    except ValidationError:
        return Settings.model_construct(), problems


def _settings_check(settings: Settings, problems: list[str]) -> DoctorCheck:
    shown = (
        "env", "default_model", "fast_model", "strong_model", "checkpointer_backend",
        "store_backend", "artifact_backend",
    )  # fmt: skip
    data: dict[str, Any] = {name: getattr(settings, name) for name in shown}
    data["errors"] = problems
    if problems:
        summary = f"{len(problems)} invalid setting(s); awa refuses to start with them"
        fix = "correct the values in .env or the environment (docs/develop/configuration.md)"
        return DoctorCheck("settings", "fail", summary, tuple(problems), fix, data)
    summary = f"valid (env={settings.env}, default model {settings.default_model})"
    backends = (
        f"backends: checkpointer={settings.checkpointer_backend}, "
        f"store={settings.store_backend}, artifacts={settings.artifact_backend}"
    )
    return DoctorCheck("settings", "ok", summary, (backends,), data=data)


def _profile_providers(settings: Settings) -> dict[str, list[str]]:
    """Map each provider to the profiles (`default`, `fast`, `strong`) that use it."""

    from awa.core.providers import provider_of

    providers: dict[str, list[str]] = {}
    for profile, model in (
        ("default", settings.default_model),
        ("fast", settings.fast_model),
        ("strong", settings.strong_model),
    ):
        providers.setdefault(provider_of(model) or "unknown", []).append(profile)
    return providers


def _needed_extras(settings: Settings, providers: dict[str, list[str]]) -> dict[str, list[str]]:
    from awa.core.providers import provider

    needed: dict[str, list[str]] = {}
    for name, profiles in providers.items():
        known = provider(name)
        if known is not None and known.extra is not None:
            needed.setdefault(known.extra, []).extend(f"{_MODEL_USER}{p}" for p in profiles)
    for backend in _postgres_backends(settings):
        needed.setdefault("postgres", []).append(f"the {backend} backend")
    if settings.langfuse_tracing:
        needed.setdefault("langfuse", []).append("Langfuse tracing (langfuse_tracing)")
    return needed


def _check_azure(settings: Settings) -> DoctorCheck | None:
    """Which route and sign-in Azure OpenAI models use, and the usual mistakes in them."""

    endpoint = (settings.azure_openai_endpoint or "").strip()
    if not endpoint:
        return None  # the credentials check already reports the missing endpoint
    from awa.models._azure import v1_base_url

    version = (settings.azure_api_version or "").strip()
    route = f"dated API version {version}" if version else "v1 API (no API version needed)"
    details = [f"route: {route}", f"auth: {settings.azure_auth}"]
    notes: list[str] = []
    endpoint_path = urlsplit(endpoint).path.strip("/")
    if endpoint_path and endpoint_path != "openai":
        notes.append(
            f"AZURE_OPENAI_ENDPOINT has a path ({endpoint_path}); "
            "use only https://<resource>.openai.azure.com/"
        )
    if version:
        details.append(f"to use the v1 API ({v1_base_url(endpoint)}) remove OPENAI_API_VERSION")
        if version.endswith("-preview"):
            notes.append(f"{version} is a preview version; Azure retires those regularly")
    if settings.azure_auth == "key" and not settings.azure_openai_api_key:
        entra = (settings.azure_tenant_id, settings.azure_client_id, settings.azure_client_secret)
        if all(entra):
            notes.append("Entra variables are set but AZURE_OPENAI_AUTH is not entra: set it")
    data = {"route": "dated" if version else "v1", "auth": settings.azure_auth, "notes": notes}
    summary = (
        f"Azure OpenAI: {'dated route' if version else 'v1 route'}, "
        f"sign-in with {settings.azure_auth}"
    )
    if notes:
        return DoctorCheck(
            "azure_openai", "warn", f"{summary}; {notes[0]}", tuple(details + notes), data=data
        )
    return DoctorCheck("azure_openai", "ok", summary, tuple(details), data=data)


def _check_providers(
    settings: Settings, providers: dict[str, list[str]], install: _Install, env_file: Path
) -> list[DoctorCheck]:
    from awa.core.config import LoadedEnvironment
    from awa.core.providers import provider as provider_spec

    loaded = LoadedEnvironment(env_file.parent, env_file if env_file.is_file() else None)

    def lookup(variable: str) -> str | None:
        return settings.credential(variable) or (loaded.get(variable) or "").strip() or None

    checks = []
    for provider, profiles in providers.items():
        used_by = f"profile{'s' if len(profiles) > 1 else ''} {', '.join(profiles)}"
        known = provider_spec(provider)
        name = f"credentials:{provider}"
        if known is None:
            summary = f"provider not known to awa doctor; credentials not checked ({used_by})"
            checks.append(DoctorCheck(name, "info", summary, data={"profiles": profiles}))
            continue
        if known.extra is None and safe_find_spec(known.module) is None:
            checks.append(
                DoctorCheck(
                    f"provider:{provider}",
                    "fail",
                    f"{known.package} is not installed ({used_by})",
                    fix=install.command(known.package),
                    data={"profiles": profiles, "package": known.package},
                )
            )
        needed = () if known.local else known.needed(lookup)
        state = {variable: lookup(variable) is not None for variable in needed}
        details = tuple(f"{var}: {'PRESENT' if ok else 'ABSENT'}" for var, ok in state.items())
        data = {"profiles": profiles, "credentials": state}
        if all(state.values()):
            summary = "credentials present" if state else "no credentials needed"
            checks.append(DoctorCheck(name, "ok", f"{summary} ({used_by})", details, data=data))
            continue
        missing = [variable for variable, present in state.items() if not present]
        where = "in .env (awa env set, or awa setup) or the environment"
        checks.append(
            DoctorCheck(
                name,
                "warn",
                f"credentials ABSENT ({used_by}); real model calls will fail, "
                "offline tests still work",
                details,
                fix=f"set {', '.join(missing)} {where}",
                data={**data, "required": True},
            )
        )
    return checks


def _check_systems(environment: EnvironmentReport | None, env_file: Path) -> list[DoctorCheck]:
    """ "External systems": connected connectors, and those with only some variables set."""

    from awa.connectors import discover, get_connector

    report = environment if environment is not None else discover(dotenv=env_file)
    needs_setup = {
        status.name
        for status in report.connectors
        if any(v.required for v in get_connector(status.name).variables())
    }
    connected = [s for s in report.connectors if s.configured and s.name in needs_setup]
    partial = [s for s in report.connectors if s.partial]
    summary = (
        f"external systems: {len(connected)} connected"
        + (f" ({', '.join(s.name for s in connected)})" if connected else "")
        + (f", {len(partial)} incomplete" if partial else "")
        + f"; {len(report.connectors)} connectors in the catalog"
    )
    fix = None if connected else "awa connectors list  (then awa connectors show <name>)"
    data = {"connected": [s.name for s in connected], "incomplete": [s.name for s in partial]}
    checks = [DoctorCheck("systems", "info", summary, fix=fix, data=data)]
    for status in connected:
        summary = f"{status.title}: variables set ({len(status.tools)} tool(s))"
        fix = f"awa connectors test {status.name}  (one real call)"
        data = {"tools": list(status.tools)}
        checks.append(DoctorCheck(f"system:{status.name}", "ok", summary, fix=fix, data=data))
    for status in partial:
        missing = ", ".join(status.missing)
        summary = f"{status.title}: set up in part; missing {missing}"
        fix = f"set {missing} (awa connectors show {status.name})"
        data = {"missing": list(status.missing)}
        checks.append(DoctorCheck(f"system:{status.name}", "warn", summary, fix=fix, data=data))
    return checks


def _postgres_backends(settings: Settings) -> list[str]:
    return [
        name
        for name, value in (
            ("checkpointer", settings.checkpointer_backend),
            ("store", settings.store_backend),
            ("artifact", settings.artifact_backend),
        )
        if value == "postgres"
    ]


def _check_postgres(settings: Settings, *, connect: bool) -> DoctorCheck | None:
    backends = _postgres_backends(settings)
    if not backends and not connect:
        return None
    dsn = settings.postgres_dsn.get_secret_value() if settings.postgres_dsn else ""
    details: tuple[str, ...] = ()
    if backends:
        details += (f"used by: {', '.join(backends)}",)
        if sys.platform == "win32":
            details += (
                "Windows: run async programs with awa.ext.run_sync(main(), selector_loop=True)",
            )
    data: dict[str, Any] = {"backends": backends, "dsn_set": bool(dsn), "connected": None}
    if not dsn:
        return DoctorCheck(
            "postgres",
            "fail",
            "AWA_POSTGRES_DSN is not set",
            details,
            fix="set AWA_POSTGRES_DSN=postgresql://user:password@host:5432/db",
            data=data,
        )
    if not connect:
        summary = "AWA_POSTGRES_DSN is set (not contacted; pass --check-postgres)"
        return DoctorCheck("postgres", "ok", summary, details, data=data)
    error = _connect_postgres(dsn)
    data["connected"] = error is None
    if error is not None:
        return DoctorCheck("postgres", "fail", f"cannot connect: {error}", details, data=data)
    return DoctorCheck("postgres", "ok", "connected to AWA_POSTGRES_DSN", details, data=data)


def _check_security(cwd: Path) -> list[DoctorCheck]:
    """A governance warning for every security part the project switched off."""

    from awa.plugins.switches import in_effect

    return [
        DoctorCheck(
            f"security:{key}",
            "warn",
            f"SECURITY OFF: {key} (governance finding)",
            fix=f"remove {key} from the blueprint's components:",
        )
        for key in in_effect(cwd)["security_off"]
    ]


def _check_langfuse(settings: Settings, *, connect: bool) -> DoctorCheck | None:
    """Whether `AWA_LANGFUSE_TRACING` can send traces: keys, address and (on request) the server."""

    if not settings.langfuse_tracing and not connect:
        return None
    from awa.observability.langfuse import DEFAULT_LANGFUSE_URL

    base = (settings.langfuse_base_url or DEFAULT_LANGFUSE_URL).rstrip("/")
    public = settings.langfuse_public_key.get_secret_value() if settings.langfuse_public_key else ""
    secret = settings.langfuse_secret_key.get_secret_value() if settings.langfuse_secret_key else ""
    data: dict[str, Any] = {
        "tracing": settings.langfuse_tracing,
        "base_url": base,
        "keys_set": bool(public and secret),
        "reachable": None,
    }
    details = (f"server: {base}", f"content captured: {settings.langfuse_capture_content}")
    if not public or not secret:
        fix = (
            "set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (Langfuse project settings, API keys)"
        )
        fix += "; until then runs go without tracing (AWA_LANGFUSE_STRICT=true stops them)"
        status: DoctorStatus = "fail" if settings.langfuse_strict else "warn"
        return DoctorCheck("langfuse", status, "Langfuse keys are not set", details, fix, data)
    if not connect:
        summary = "keys and server set (not contacted; pass --check-langfuse)"
        return DoctorCheck("langfuse", "ok", summary, details, data=data)
    error = _contact_langfuse(base, public, secret)
    data["reachable"] = error is None
    if error is not None:
        message = _redact(error, [public, secret])
        fix = "start Langfuse (docker compose up -d) or correct LANGFUSE_BASE_URL and the keys"
        return DoctorCheck(  # asked for explicitly (--check-langfuse): a failed contact fails
            "langfuse", "fail", f"cannot use Langfuse: {message}", details, fix, data
        )
    return DoctorCheck(
        "langfuse", "ok", "server answered and accepted the keys", details, data=data
    )


def _contact_langfuse(base: str, public: str, secret: str) -> str | None:
    """`None` when the server is healthy and accepts the keys, else what went wrong."""

    import base64
    import urllib.error
    import urllib.request

    if not base.startswith(("http://", "https://")):
        return "LANGFUSE_BASE_URL must start with http:// or https://"

    class Stay(urllib.request.HTTPRedirectHandler):
        """Follows no redirect: the keys never go to another address."""

        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    opener = urllib.request.build_opener(Stay)
    token = base64.b64encode(f"{public}:{secret}".encode()).decode("ascii")
    for path, headers in (
        ("/api/public/health", {}),
        ("/api/public/projects", {"Authorization": f"Basic {token}"}),
    ):
        request = urllib.request.Request(base + path, headers=headers)
        try:
            with opener.open(request, timeout=10) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            reason = "the keys were rejected" if exc.code in (401, 403) else f"HTTP {exc.code}"
            return f"{path}: {reason}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return f"{path}: {exc}"
    return None


def _connect_postgres(dsn: str) -> str | None:
    """Connect once and run `SELECT version()`; return a redacted error message or `None`."""

    try:
        import psycopg
    except ImportError:
        return "psycopg is not installed (install the postgres extra)"
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            connection.execute("SELECT version()").fetchone()
    except Exception as exc:
        return _redact(f"{type(exc).__name__}: {exc}", _dsn_secrets(dsn))
    return None


def _dsn_secrets(dsn: str) -> list[str]:
    secrets = [dsn]
    try:
        password = urlsplit(dsn).password
    except ValueError:
        password = None
    if password:
        secrets += [password, unquote(password)]
    return secrets


def _redact(text: str, secrets: Iterable[str]) -> str:
    """`text` without the secret values, on one line, at most 300 characters."""

    from awa._internal.util import preview
    from awa.kernel.redaction import redact_values

    return preview(redact_values(text, secrets), 300, flatten=True)


def _check_models(settings: Settings, registry: ModelRegistry | None) -> list[DoctorCheck]:
    from awa.models import ModelRegistry, ModelSettings

    probe_settings = None
    if registry is None:
        registry = ModelRegistry(settings)
        # The profiles are model strings: cap the reply and skip SDK retries.
        probe_settings = ModelSettings(max_tokens=16, timeout=60, max_retries=0)
    secrets = [
        value.get_secret_value()
        for value in (
            settings.anthropic_api_key,
            settings.openai_api_key,
            settings.azure_openai_api_key,
            settings.azure_client_secret,
            settings.langsmith_api_key,
            settings.langfuse_secret_key,
            settings.postgres_dsn,
        )
        if value is not None
    ]
    return [_probe(registry, profile, probe_settings, secrets) for profile in registry.profiles()]


def _probe(
    registry: ModelRegistry, profile: str, probe_settings: ModelSettings | None, secrets: list[str]
) -> DoctorCheck:
    from awa.models.errors import azure_error_hint, model_provider

    name = f"model:{profile}"
    started = time.perf_counter()
    model = None
    try:
        model = registry.get(profile, settings=probe_settings)
        reply = model.invoke(_PROBE_PROMPT)
    except Exception as exc:
        message = _redact(f"{type(exc).__name__}: {exc}", secrets)
        provider = model_provider(model) if model is not None else None
        hint = azure_error_hint(exc, provider=provider)
        return DoctorCheck(
            name, "fail", f"model call failed: {message}", fix=hint, data={"ok": False}
        )
    elapsed = time.perf_counter() - started
    from awa.models.errors import model_identity

    # What the service says it ran, not what the deployment or profile is called.
    metadata = getattr(reply, "response_metadata", None) or {}
    reported = metadata.get("model_name") or metadata.get("model")
    summary = f"{model_identity(model)} replied in {elapsed:.1f} s"
    data: dict[str, Any] = {"ok": True, "seconds": round(elapsed, 3)}
    if isinstance(reported, str) and reported:
        summary += f" (the service reports model {reported})"
        data["service_model"] = reported
    return DoctorCheck(name, "ok", summary, data=data)


def add_doctor(commands: Commands) -> None:
    command = add_command(
        commands,
        "doctor",
        description="Check Python, the virtual environment, the awa install, extras, "
        "credentials and settings, and show where each setting comes from. Exit code 1 when "
        "something blocks use, 78 when a required part is missing (a model key, or a "
        "variable a trigger needs).",
    )
    command.add_argument("--json", action="store_true", help="print the report as JSON")
    command.add_argument(
        "--check-models",
        action="store_true",
        help="make one tiny real call per model profile (costs tokens)",
    )
    command.add_argument(
        "--check-postgres", action="store_true", help="connect to AWA_POSTGRES_DSN"
    )
    command.add_argument(
        "--check-langfuse",
        action="store_true",
        help="ask the Langfuse server for its health and check the keys (sends no trace)",
    )
    command.set_defaults(handler=_doctor)


def _doctor(args: argparse.Namespace) -> int:
    report = run_doctor(
        check_models=args.check_models,
        check_postgres=args.check_postgres,
        check_langfuse=args.check_langfuse,
    )
    if args.json:
        print_json(report.to_dict())
    else:
        print(report.render())
    if not report.usable:
        return EXIT_ERROR
    return EXIT_CONFIG if report.missing_required else EXIT_OK
