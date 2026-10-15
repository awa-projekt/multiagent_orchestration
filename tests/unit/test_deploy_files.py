"""The deployment and install files agree with the library and can be started."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from awa.core.settings_registry import spec

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"


def _defaults_block() -> dict[str, str]:
    text = (DEPLOY / "awa.env.example").read_text(encoding="utf-8")
    block = text.split("(defaults shown)", 1)[1].split("\n\n", 1)[0]
    return dict(re.findall(r"^#(AWA_\w+)=(\S+)$", block, re.MULTILINE))


def test_env_example_shows_the_real_defaults() -> None:
    shown = _defaults_block()
    assert shown
    for name, value in shown.items():
        setting = spec(name)
        assert setting is not None, name
        assert str(setting.default) == value, name


def test_every_awa_name_the_containers_receive_is_a_known_setting() -> None:
    # awa.env.example turns AWA_STRICT_SETTINGS on, so an unknown AWA_* name stops every
    # container of compose.yaml at its start.
    example = (DEPLOY / "awa.env.example").read_text(encoding="utf-8")
    compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    names = set(re.findall(r"^#?(AWA_\w+)=", example, re.MULTILINE))
    shared = compose.split("x-awa:", 1)[1].split("\nservices:", 1)[0]
    names |= set(re.findall(r"^\s+(AWA_\w+):", shared, re.MULTILINE))
    names |= set(re.findall(r"--api-key-env=(AWA_\w+)", compose))

    assert "AWA_STRICT_SETTINGS=true" in example
    assert {"AWA_API_KEY", "AWA_MCP_API_KEY", "AWA_POSTGRES_DSN"} <= names
    assert sorted(name for name in names if spec(name) is None) == []


def test_the_langfuse_services_listen_where_their_healthchecks_ask() -> None:
    # Both Langfuse images bind to $HOSTNAME, which Docker sets to the container id (its network
    # address), and busybox wget resolves localhost to ::1 only; either way the healthcheck never
    # succeeds and the services stay unhealthy.
    tracing = yaml.safe_load((DEPLOY / "compose.tracing.yaml").read_text(encoding="utf-8"))
    for name in ("langfuse-web", "langfuse-worker"):
        service = tracing["services"][name]
        assert "http://127.0.0.1:" in " ".join(service["healthcheck"]["test"]), name
        assert service["environment"].get("HOSTNAME") == "0.0.0.0", name


def test_documented_commands_in_the_containers_go_through_the_entrypoint() -> None:
    # `docker compose exec` skips the entrypoint, so without it a command does not trust the
    # certificates in deploy/ca/ and a check of Langfuse behind the TLS edge fails.
    text = (REPO / "docs" / "develop" / "production.md").read_text(encoding="utf-8")
    commands = re.findall(r"^docker compose .* exec (awa-\S+ .*)$", text, re.MULTILINE)

    assert commands
    for command in commands:
        assert command.split()[1] == "awa-entrypoint", command


def test_the_shell_entry_points_are_executable() -> None:
    names = ["deploy/entrypoint.sh", "scripts/install-linux.sh", "scripts/install-macos.command"]
    try:
        staged = subprocess.run(
            ["git", "ls-files", "-s", *names], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("needs a git checkout")
    modes = {line.split("	")[1]: line.split()[0] for line in staged.splitlines()}
    assert modes == dict.fromkeys(names, "100755")


def test_the_build_context_holds_only_what_the_dockerfile_copies() -> None:
    ignored = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    rules = [line for line in ignored if line and not line.startswith("#")]
    copied = re.findall(r"^COPY (\S+)", (DEPLOY / "Dockerfile").read_text("utf-8"), re.MULTILINE)

    assert rules == ["*", *(f"!{source}" for source in copied)]


def test_the_env_examples_hold_no_placeholder_that_would_work_as_a_secret() -> None:
    for name in ("awa.env.example", ".env.example"):
        text = (DEPLOY / name).read_text(encoding="utf-8")
        assert "replace-with" not in text, name
