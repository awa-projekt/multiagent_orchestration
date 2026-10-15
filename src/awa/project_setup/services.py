"""This project's local services in Docker: PostgreSQL and Langfuse.

Every command runs under this project's own Compose project name, so `down --volumes` never
touches another project's containers. Ports bind to 127.0.0.1; generated passwords live only in
the project `.env` and reach Compose through `--env-file`; the images are pinned.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from awa.errors import ConfigurationError

Profile = Literal["db", "tracing"]
"""A group of services that start together."""

PROFILES: tuple[Profile, ...] = ("db", "tracing")

DEFAULT_PORTS: Mapping[Profile, int] = {"db": 5432, "tracing": 3000}
"""Where the search for a free port starts, per profile."""

COMPOSE_FILE = ".awa/compose.services.yaml"
"""The Compose file, relative to the project folder."""

STATE_FILE = ".awa/setup_state.json"
"""States, times, ports and the Compose project name (never a secret)."""

PASSWORD_ALPHABET = string.ascii_letters + string.digits
_SLUG = re.compile(r"[^a-z0-9]+")
_MIN_COMPOSE = (2, 20)
_MEMORY_FOR_LANGFUSE = 4 * 1024**3


ServicesError = ConfigurationError
"""Docker cannot be used for this project's services (a `ConfigurationError`)."""


def password(length: int = 32) -> str:
    """A random password of letters and digits (safe in `.env`, URLs and Compose)."""

    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def project_name(project: Path) -> str:
    """`awa-<folder slug>-<first 8 hex of sha256(resolved path)>`: unique per project folder."""

    resolved = str(project.resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
    slug = _SLUG.sub("-", project.resolve().name.lower()).strip("-")[:24] or "project"
    return f"awa-{slug}-{digest}"


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Whether nothing listens on `host:port` (a bind test)."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def free_port(
    start: int, *, taken: Iterable[int] = (), is_free: Callable[[int], bool] = port_free
) -> int:
    """The first free port from `start` (1024-65535), skipping `taken`."""

    skip = set(taken)
    for port in range(max(start, 1024), 65536):
        if port not in skip and is_free(port):
            return port
    raise ServicesError(f"no free port from {start} on 127.0.0.1")


def check_port(port: int) -> int:
    """`port` if it is an int from 1024 to 65535."""

    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ServicesError(f"port {port!r} must be a number from 1024 to 65535")
    return port


def compose_text(project: str, ports: Mapping[Profile, int]) -> str:
    """The Compose file of `project` (`project_name`) with the host port of each profile."""

    db, tracing = (check_port(ports[p]) for p in PROFILES)
    template = (Path(__file__).parent / "compose.services.yaml").read_text(encoding="utf-8")
    for token, value in (
        ("__PROJECT_ID__", project.rsplit("-", 1)[-1]),
        ("__PROJECT__", project),
        ("__DB_PORT__", str(db)),
        ("__TRACING_PORT__", str(tracing)),
    ):
        template = template.replace(token, value)
    return template


Runner = Callable[[Sequence[str], float], "CommandResult"]
"""Runs a command (a list, never a shell string) with a timeout in seconds."""


@dataclass(frozen=True)
class CommandResult:
    """What a command printed and its exit code."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def run_command(command: Sequence[str], timeout: float) -> CommandResult:
    """Run `command` without a shell and return its result (the default `Runner`)."""

    try:
        done = subprocess.run(  # noqa: S603 - a list from docker_binary, never a shell
            list(command), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timed out after {timeout:.0f} s")
    except OSError as exc:
        return CommandResult(127, "", str(exc))
    return CommandResult(done.returncode, done.stdout or "", done.stderr or "")


def docker_binary(
    which: Callable[[str], str | None] = shutil.which, platform: str = sys.platform
) -> str:
    """The `docker` program: an `.exe` on Windows, no suffix elsewhere (a `.cmd` shim is refused).

    Raises:
        ServicesError: When Docker is not installed (or only a shim is found).
    """

    found = which("docker")
    if not found:
        raise ServicesError(
            "Docker is not installed: install Docker Desktop (or Docker Engine), or use "
            "--database url / --tracing url with a server you already have"
        )
    suffix = Path(found).suffix.lower()
    if (platform == "win32" and suffix != ".exe") or (platform != "win32" and suffix):
        raise ServicesError(f"{found} is not the docker program (a script shim); install Docker")
    return found


@dataclass
class Services:
    """This project's local services.

    Args:
        env_file: The project `.env` (Compose reads the passwords from it).
        runner: Runs `docker` commands (tests pass a fake).
        docker: The `docker` program (default: found with `docker_binary`).
    """

    project: Path
    env_file: Path
    runner: Runner = run_command
    docker: str | None = None

    @property
    def name(self) -> str:
        """The Compose project name."""

        return project_name(self.project)

    @property
    def compose_file(self) -> Path:
        return self.project / COMPOSE_FILE

    def _docker(self) -> str:
        if self.docker is None:
            self.docker = docker_binary()
        return self.docker

    def check_docker(self) -> None:
        """Raise `ServicesError` with one sentence when Docker cannot be used."""

        docker = self._docker()
        info = self.runner([docker, "info", "--format", "{{.ServerVersion}}"], 30)
        if info.returncode != 0:
            raise ServicesError(
                "Docker is installed but not running: start Docker Desktop (or the docker "
                "service), then run the command again"
            )
        version = self.runner([docker, "compose", "version", "--short"], 30)
        found = tuple(int(part) for part in re.findall(r"\d+", version.stdout)[:2])
        if version.returncode != 0 or len(found) < 2 or found < _MIN_COMPOSE:
            raise ServicesError(
                f"Docker Compose {'.'.join(map(str, _MIN_COMPOSE))} or newer is needed: update "
                "Docker"
            )

    def memory(self) -> int | None:
        """The memory Docker may use, in bytes (`None` when unknown)."""

        result = self.runner([self._docker(), "info", "--format", "{{.MemTotal}}"], 30)
        text = result.stdout.strip()
        return int(text) if result.returncode == 0 and text.isdigit() else None

    def langfuse_memory_warning(self) -> str | None:
        """One sentence when Docker has less than the 4 GB Langfuse needs."""

        total = self.memory()
        if total is not None and total < _MEMORY_FOR_LANGFUSE:
            return (
                f"Docker has {total / 1024**3:.1f} GB of memory; Langfuse needs at least 4 GB "
                "(Docker Desktop: Settings, Resources)"
            )
        return None

    def write_compose(self, ports: Mapping[Profile, int]) -> Path:
        """Write the Compose file for `ports`; return its path."""

        path = self.compose_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(compose_text(self.name, ports), encoding="utf-8")
        return path

    def _base(self, profiles: Iterable[Profile]) -> list[str]:
        command = [
            self._docker(),
            "compose",
            "-p",
            self.name,
            "-f",
            str(self.compose_file),
            "--env-file",
            str(self.env_file),
        ]
        for profile in profiles:
            if profile not in PROFILES:
                raise ServicesError(f"unknown profile {profile!r} (db or tracing)")
            command += ["--profile", profile]
        return command

    def up(self, profiles: Sequence[Profile], *, wait_timeout: float = 300) -> CommandResult:
        """Start the services of `profiles` and wait until they are healthy."""

        self.check_docker()
        if not self.compose_file.is_file():
            raise ServicesError("no services are set up yet: run awa setup first")
        command = [
            *self._base(profiles),
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            str(int(wait_timeout)),
        ]
        return self.runner(command, wait_timeout + 120)

    def down(
        self, profiles: Sequence[Profile] = PROFILES, *, volumes: bool = False
    ) -> CommandResult:
        """Stop the services of `profiles`; with `volumes`, also delete their data."""

        self.check_docker()
        if not self.compose_file.is_file():
            return CommandResult(0, "", "")
        command = [*self._base(profiles), "down"]
        if volumes:
            command.append("--volumes")
        return self.runner(command, 300)

    def status(self) -> list[dict[str, Any]]:
        """This project's containers: service, state, health and ports (none before setup)."""

        if not self.compose_file.is_file():
            return []
        self.check_docker()
        result = self.runner([*self._base(PROFILES), "ps", "--all", "--format", "json"], 60)
        if result.returncode != 0:
            raise ServicesError(f"docker compose ps failed: {result.stderr.strip()[:200]}")
        text = result.stdout.strip()
        if text.startswith("["):
            items: list[Any] = json.loads(text)
        else:
            items = [json.loads(line) for line in text.splitlines() if line.strip()]
        return [
            {
                "service": item.get("Service", ""),
                "state": item.get("State", ""),
                "health": item.get("Health", "") or "-",
                "ports": [
                    f"{p.get('URL')}:{p.get('PublishedPort')}"
                    for p in item.get("Publishers") or []
                    if p.get("PublishedPort")
                ],
            }
            for item in items
        ]


__all__ = [
    "COMPOSE_FILE",
    "DEFAULT_PORTS",
    "PROFILES",
    "STATE_FILE",
    "CommandResult",
    "Profile",
    "Runner",
    "Services",
    "ServicesError",
    "check_port",
    "compose_text",
    "docker_binary",
    "free_port",
    "password",
    "port_free",
    "project_name",
    "run_command",
]
