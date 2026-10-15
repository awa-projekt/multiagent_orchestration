"""Check what a newcomer meets: build the wheel, install it fresh, follow the keyless start path.

Run it from the repository root (needs uv and a package index or cache):

    python scripts/first_run_check.py                 # prints the report, exit 1 on a failure
    python scripts/first_run_check.py --report r.json # also writes the numbers as JSON
    python scripts/first_run_check.py --wheel dist/awa_agents-<version>-py3-none-any.whl

It builds the wheel into a temporary folder outside the repository (or takes the one `--wheel`
names), creates a fresh virtual environment there, and runs the keyless start of
docs/start/first-automation.md with no model key: install the wheel with its main extras,
`awa doctor`, `awa init`, install pytest, run the project's offline tests. It counts the commands
and the seconds, and fails when the path needs more than five commands, a command fails, a command
prints a traceback, or a warning or error has no `fix:` line. Scripted-model tests prove the flow,
not answer quality: a path that needs a model key (`awa build`) is not part of this check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_COMMANDS = 5
PYTHON_VERSION = "3.13"
EXTRAS = "all"
SECRET_PREFIXES = ("AWA_", "ANTHROPIC_", "OPENAI_", "AZURE_OPENAI_", "LANGSMITH_", "LANGCHAIN_")
WINDOWS_SHORT_PATH = 90
_PROBLEM_LINE = re.compile(r"^\s*(WARN|FAIL|ERROR)\b|^error:", re.IGNORECASE)


@dataclass
class Step:
    """One command a newcomer types, with what happened."""

    command: str
    seconds: float
    exit_code: int
    problems: list[str] = field(default_factory=list)


@dataclass
class Report:
    """The numbers of one run."""

    wheel: str = ""
    build_seconds: float = 0.0
    setup_seconds: float = 0.0
    steps: list[Step] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def commands(self) -> int:
        return len(self.steps)

    @property
    def path_seconds(self) -> float:
        return round(sum(step.seconds for step in self.steps), 1)

    @property
    def ok(self) -> bool:
        return not self.failures


def output_problems(text: str) -> list[str]:
    """What is wrong with the output of one command.

    A traceback is always a problem. A line that starts with WARN, FAIL, ERROR or `error:`
    needs a `fix:` line within the next three lines: the message must name the fix.
    """

    problems = []
    lines = text.splitlines()
    if any(line.startswith("Traceback (most recent call last)") for line in lines):
        problems.append("prints a traceback")
    for number, line in enumerate(lines):
        if _PROBLEM_LINE.match(line) and not any(
            "fix:" in near for near in lines[number : number + 4]
        ):
            problems.append(f"message without a fix: {line.strip()}")
    return problems


def judge(report: Report) -> Report:
    """Add the failure for a path that needs more than `MAX_COMMANDS` commands."""

    if report.commands > MAX_COMMANDS:
        report.failures.append(
            f"the path needs {report.commands} commands; the limit is {MAX_COMMANDS}"
        )
    return report


def clean_environment() -> dict[str, str]:
    """The process environment without any model key or awa setting (the keyless start)."""

    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(SECRET_PREFIXES)}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(
    command: list[str], cwd: Path, timeout: float
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.perf_counter()
    done = subprocess.run(
        command,
        cwd=cwd,
        env=clean_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return done, round(time.perf_counter() - started, 2)


def run_check(
    repo: Path,
    workdir: Path,
    python_version: str = PYTHON_VERSION,
    timeout: float = 900,
    wheel: Path | None = None,
) -> Report:
    """Build, install fresh and follow the path; see the module docstring.

    Args:
        repo: The awa folder (holds pyproject.toml).
        workdir: A new, empty folder outside `repo`; wheel, environment and project go here.
        python_version: The interpreter for the fresh environment (uv finds or fetches it).
        timeout: Seconds allowed for one command.
        wheel: A built wheel to install instead of building one from `repo`.

    Returns:
        The report; `report.ok` is false when the path breaks a rule.
    """

    report = Report()
    uv = shutil.which("uv")
    if uv is None:
        report.failures.append("uv is not installed: install it from https://docs.astral.sh/uv/")
        return report
    if workdir.resolve().is_relative_to(repo.resolve()):
        report.failures.append(f"the work folder {workdir} is inside the repository: pick another")
        return report
    if os.name == "nt" and len(str(workdir.resolve())) > WINDOWS_SHORT_PATH:
        report.failures.append(
            f"the work folder path is long ({len(str(workdir))} characters): a deep Windows path "
            "can break imports in the installed packages; pass a short --workdir such as C:/awa-fr"
        )
        return report
    dist, venv, project_root = workdir / "dist", workdir / "venv", workdir / "work"
    project_root.mkdir(parents=True, exist_ok=True)

    if wheel is None:
        build = [uv, "build", "--wheel", "--out-dir", str(dist)]
        done, report.build_seconds = _run(build, repo, timeout)
        wheels = sorted(dist.glob("awa_agents-*.whl"))
        if done.returncode != 0 or not wheels:
            report.failures.append(f"building the wheel failed: {done.stderr.strip()[-300:]}")
            return report
        wheel = wheels[-1]
    report.wheel = wheel.name
    make_venv = [uv, "venv", str(venv), "--python", python_version, "--quiet"]
    done, report.setup_seconds = _run(make_venv, workdir, timeout)
    if done.returncode != 0:
        report.failures.append(f"creating the environment failed: {done.stderr.strip()[-300:]}")
        return report

    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    exe = ".exe" if os.name == "nt" else ""
    python, awa = str(scripts / f"python{exe}"), str(scripts / f"awa{exe}")
    install = [uv, "pip", "install", "--python", python]
    path = [
        ("pip install awa", [*install, f"{wheel}[{EXTRAS}]"]),
        ("awa doctor", [awa, "doctor"]),
        ("awa init my-desk --template support", [awa, "init", "my-desk", "--template", "support"]),
        ("pip install pytest", [*install, "pytest"]),
        ("pytest my-desk", [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "my-desk"]),
    ]
    for label, command in path:
        done, seconds = _run(command, project_root, timeout)
        step = Step(label, seconds, done.returncode)
        text = f"{done.stdout}\n{done.stderr}"
        # Installers print progress on stderr: judge them by exit code and traceback only.
        found = output_problems(text)
        step.problems = [p for p in found if p == "prints a traceback"] if "pip" in label else found
        # On the keyless path `awa doctor` exits with 78 ("the model key is missing") by design.
        allowed = {0, 78} if label == "awa doctor" else {0}
        if done.returncode not in allowed:
            step.problems.append(f"exit code {done.returncode}: {text.strip()[-300:]}")
        report.steps.append(step)
        if step.problems:
            report.failures.extend(f"{label}: {problem}" for problem in step.problems)
            break
    return judge(report)


def render(report: Report) -> str:
    """The report as text for people."""

    lines = [
        f"wheel: {report.wheel or '-'} "
        f"(build {report.build_seconds}s, environment {report.setup_seconds}s)"
    ]
    for number, step in enumerate(report.steps, 1):
        state = "FAILED" if step.problems else "ok"
        lines.append(f"{number}. {step.command:<38} {step.seconds:>7.1f}s  {state}")
    lines.append(
        f"commands: {report.commands} (limit {MAX_COMMANDS}), path time: {report.path_seconds}s"
    )
    lines.extend(f"FAIL: {failure}" for failure in report.failures)
    lines.append("result: " + ("pass" if report.ok else "fail"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Follow the keyless start path, fresh install.")
    parser.add_argument(
        "--workdir", type=Path, help="folder outside the repo (default: a temp one)"
    )
    parser.add_argument("--python", default=PYTHON_VERSION, help="Python for the new environment")
    parser.add_argument("--report", type=Path, help="also write the numbers as JSON to this file")
    parser.add_argument("--wheel", type=Path, help="install this built wheel instead of building")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="awa-fr-"))
    wheel = args.wheel.resolve() if args.wheel else None
    try:
        report = run_check(repo, workdir, args.python, wheel=wheel)
    finally:
        if args.workdir is None:
            shutil.rmtree(workdir, ignore_errors=True)
    print(render(report))
    if args.report:
        numbers = {
            **asdict(report),
            "commands": report.commands,
            "path_seconds": report.path_seconds,
        }
        args.report.write_text(json.dumps(numbers, indent=2) + "\n", encoding="utf-8")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
