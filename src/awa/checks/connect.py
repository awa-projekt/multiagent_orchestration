"""`awa connect microsoft [--teams]`: set up Microsoft 365 step by step, then check it.

Shows which values are set (never the values), asks for each missing one with hidden input and
saves it in `.env`, signs the Teams user in with the device code when asked (also for people
search with `--people` or `config/people.yaml`), then runs the read-only microsoft checks.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from awa.checks._engine import EXIT_CONFIGURATION, EXIT_FAILED, CheckReport, CheckRun

Ask = Callable[[str], str]

QUESTIONS: tuple[tuple[str, str], ...] = (
    ("AZURE_TENANT_ID", "Directory (tenant) ID, from the app registration's Overview page"),
    ("AZURE_CLIENT_ID", "Application (client) ID, from the same page"),
    (
        "AZURE_CLIENT_SECRET",
        "client secret VALUE (Certificates & secrets); empty to use a certificate instead",
    ),
    (
        "AZURE_CLIENT_CERTIFICATE_PATH",
        "certificate file (PEM with key, or .pfx); empty if you entered a secret",
    ),
    ("MS_MAILBOX", "the ONE mailbox awa works in (empty: no Outlook)"),
    (
        "MS_SITE_URL",
        "the ONE SharePoint site, https://company.sharepoint.com/sites/... (empty: none)",
    ),
    ("TEAMS_WEBHOOK_URL", "Teams Workflows webhook link (empty: none)"),
    ("TEAMS_TEAM_ID", "Teams team id for the signed-in user (empty: none)"),
    ("TEAMS_CHANNEL_ID", "Teams channel id 19:...@thread.tacv2 (empty: none)"),
)
"""What is asked, in order: the variable and the question."""

USER_SCOPES = ("User.Read", "ChannelMessage.Send", "ChannelMessage.Read.All")
"""The delegated permissions the Teams user is asked to approve."""
PEOPLE_SCOPE = "User.ReadBasic.All"
"""Added to the sign-in when people are used: find colleagues (no admin consent needed)."""


def sign_in_scopes(project: Path, *, people: bool = False) -> tuple[str, ...]:
    """The delegated permissions to ask for: `USER_SCOPES`, plus `PEOPLE_SCOPE` when `people`
    is asked for or the project has pinned people (`config/people.yaml`)."""

    from awa.people import PEOPLE_FILE

    wanted = people or (project / PEOPLE_FILE).is_file()
    return (*USER_SCOPES, PEOPLE_SCOPE) if wanted else USER_SCOPES


def _present(project: Path) -> dict[str, bool]:
    from awa.checks._env import project_env

    lookup = project_env(project)
    return {name: bool(lookup(name)) for name, _ in QUESTIONS}


def run_connect_microsoft(
    project: Path,
    *,
    teams: bool = False,
    people: bool = False,
    secret: Ask = getpass.getpass,
    out: TextIO | None = None,
    sign_in: Callable[..., object] | None = None,
    connector_options: Mapping[str, Any] | None = None,
) -> int:
    """The whole flow; returns the exit code of the checks (0, 1, or 2 when nothing could run).

    Args:
        people: Also sign a user in with User.ReadBasic.All (people search), without Teams.
        secret: Asks one question without echoing the answer.
        sign_in: The device-code sign-in (`awa.connectors._microsoft_auth.sign_in` by default).
        connector_options: HTTP options for a local test server (none in production).
    """

    from awa.checks import run_groups

    out = out if out is not None else sys.stdout
    project.mkdir(parents=True, exist_ok=True)
    present = _present(project)
    out.write("Microsoft 365 values in .env (values are never shown):\n")
    for name, _ in QUESTIONS:
        out.write(f"  {name:<32} {'set' if present[name] else 'not set'}\n")
    answers: dict[str, str] = {}
    for name, question in QUESTIONS:
        if present[name]:
            continue
        if name == "AZURE_CLIENT_CERTIFICATE_PATH" and (
            answers.get("AZURE_CLIENT_SECRET") or present["AZURE_CLIENT_SECRET"]
        ):
            continue
        if name in ("TEAMS_TEAM_ID", "TEAMS_CHANNEL_ID") and not teams:
            continue
        value = secret(f"{name}: {question} (hidden): ").strip()
        if value:
            answers[name] = value
    if answers:
        from awa.core.dotenv import write_env_file

        try:
            written = write_env_file(project / ".env", answers)
        except ValueError as error:
            out.write(f"Nothing was saved: {error}\n")
            return EXIT_CONFIGURATION
        out.write(f"\nSaved in {project / '.env'} (readable by you only): {', '.join(written)}\n")
    if teams or people:
        code = _sign_in_teams(project, out, sign_in, connector_options, people=people)
        if code is not None:
            return code
    out.write("\nChecking (read-only calls; nothing is sent):\n")
    report = run_groups(
        ["microsoft"],
        project=project,
        no_model=True,
        on_result=_printer(out),
        connector_options=connector_options,
    )
    return _finish(report, out)


def _sign_in_teams(
    project: Path,
    out: TextIO,
    sign_in: Callable[..., object] | None,
    connector_options: Mapping[str, Any] | None,
    *,
    people: bool = False,
) -> int | None:
    """Sign the Teams user in (with the people permission when people are used); `None` when it
    worked, else the exit code."""

    from awa.checks._env import project_env
    from awa.connectors._microsoft_errors import MicrosoftAuthError

    env = project_env(project)
    tenant, client = env("AZURE_TENANT_ID"), env("AZURE_CLIENT_ID")
    if not tenant or not client:
        out.write("\nThe Teams sign-in needs AZURE_TENANT_ID and AZURE_CLIENT_ID first.\n")
        return EXIT_CONFIGURATION
    if sign_in is None:
        from awa.connectors._microsoft_auth import sign_in as device_sign_in

        sign_in = device_sign_in
    out.write("\nSigning the Teams user in (device code):\n")

    def show(prompt: Any) -> None:
        out.write(prompt.text() + "\n")
        out.flush()

    try:
        done = sign_in(
            tenant=tenant,
            client_id=client,
            show=show,
            scopes=sign_in_scopes(project, people=people),
            authority=env("AZURE_AUTHORITY_HOST") or "https://login.microsoftonline.com",
            token_cache=str(project / ".awa" / "ms_token_cache.json"),
            options=dict(connector_options or {}),
        )
    except MicrosoftAuthError as error:
        out.write(f"The Teams sign-in did not work: {error}\n")
        return EXIT_FAILED
    username = getattr(done, "username", "?")
    out.write(f"Signed in as {username}.\n")
    return None


def _printer(out: TextIO) -> Callable[[CheckRun], None]:
    from awa.checks._render import print_line

    return lambda run: print_line(run, out)


def _finish(report: CheckReport, out: TextIO) -> int:
    from awa.checks._render import print_summary

    for note in report.notes:
        out.write(f"note: {note}\n")
    print_summary(report, out)
    return report.exit_code


__all__ = ["PEOPLE_SCOPE", "QUESTIONS", "USER_SCOPES", "run_connect_microsoft", "sign_in_scopes"]
