"""A small, correctly set up Microsoft 365 organization on FakeGraph for the `awa check` tests.

The app has least privilege (Sites.Selected granted on one site, Outlook scoped to one mailbox,
User.Read.All for people search); a second mailbox it must not read, a site whose library holds a
workbook, a Teams channel with a Workflows webhook and a user who signs in with the device code.
A scenario breaks one thing on purpose.
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _helpers.fakegraph import BUILTIN_PLUGINS, TENANT_DOMAIN, FakeGraph
from _helpers.fakegraph import directory as directory_plugin
from awa.checks import CheckContext, CheckReport, run_checks

EXPECTED: dict[str, tuple[str, str]] = {
    "missing-consent": ("microsoft.sign_in", "ms.consent_missing"),
    "site-not-granted": ("microsoft.sharepoint", "ms.site_not_granted"),
    "rbac-not-scoped": ("microsoft.outlook_scope", "ms.rbac_not_scoped"),
    "secret-expired": ("microsoft.sign_in", "ms.secret_expired"),
    "throttled": ("microsoft.outlook", "ms.throttled"),
    "old-webhook": ("microsoft.teams", "ms.webhook_old_host"),
}
"""Each scenario, the check it turns red and the advice code it shows."""

CLIENT_ID = "8f3c2a71-5d4e-4b6a-9c1f-2e7d0b9a4c35"
SECRET = "check~Secret.Value-0123456789abcdef"
SITE_HOST = "contoso.sharepoint.com"
SITE_PATH = "sites/sales"
USER_SCOPES = ("User.Read", "ChannelMessage.Send", "ChannelMessage.Read.All")
WORKBOOK = Path(__file__).resolve().parents[1] / "fixtures" / "microsoft" / "sample.xlsx"


@dataclass
class MicrosoftWorld:
    """The running organization: the env values that point at it and the connector options."""

    graph: FakeGraph
    env: dict[str, str]
    folder: Path
    options: dict[str, Any]

    def sign_in_user(self) -> str:
        """Sign the Teams user in with the device code (approved at once); returns the cache."""

        from awa.connectors._microsoft_auth import sign_in

        cache = self.folder / "ms_token_cache.json"
        if not cache.is_file():
            sign_in(
                tenant=self.env["AZURE_TENANT_ID"],
                client_id=self.env["AZURE_CLIENT_ID"],
                show=lambda prompt: None,
                scopes=USER_SCOPES,
                authority=self.env["AZURE_AUTHORITY_HOST"],
                token_cache=str(cache),
                options=self.options,
            )
        return str(cache)

    def check(self, project: Path, *, allow_post: frozenset[str] = frozenset()) -> CheckReport:
        """Run the Microsoft checks against this organization, without a model call."""

        from awa.checks.microsoft import CHECKS

        ctx = CheckContext(
            self.env.get,
            allow_post=allow_post,
            no_model=True,
            project=project,
            options=dict(self.options),
        )
        ctx.shared["token_cache"] = self.sign_in_user()
        return run_checks(CHECKS, ctx)


@contextlib.contextmanager
def microsoft_world(scenario: str | None = None) -> Iterator[MicrosoftWorld]:
    """Start the organization, broken as `scenario` says; stopped when the block ends."""

    graph = FakeGraph(plugins=(*BUILTIN_PLUGINS, directory_plugin))
    roles: tuple[str, ...] = ("Sites.Selected", "User.Read.All")
    if scenario == "missing-consent":
        roles = ()
    if scenario == "rbac-not-scoped":
        roles = (*roles, "Mail.ReadWrite")
    app = graph.add_app(
        CLIENT_ID,
        SECRET,
        roles=roles,
        scopes=USER_SCOPES,
        public_client=True,
        name="awa",
        secret_lifetime_days=-1 if scenario == "secret-expired" else 365,
    )
    mailbox = f"support@{TENANT_DOMAIN}"
    other = f"ceo@{TENANT_DOMAIN}"
    support = graph.add_user(mailbox, name="Support")
    graph.add_user(other, name="Chief Executive")
    bot = graph.add_user(f"awa-bot@{TENANT_DOMAIN}", name="awa bot")
    if scenario != "missing-consent":
        app.rbac["Application Mail.ReadWrite"] = {mailbox}
    mail = graph.store("mail")
    mail.add_message(support, "inbox", subject="Order A-1", body="Please send order A-1.")
    mail.add_message(
        support, "inbox", subject="Invoice 17", body="The invoice is attached.", is_read=True
    )
    files = graph.store("files")
    site = files.add_site(SITE_HOST, SITE_PATH, name="Sales")
    if scenario not in ("site-not-granted", "missing-consent"):
        files.grant(site, CLIENT_ID, "write")
    files.put_file(site, "Orders.xlsx", WORKBOOK.read_bytes())
    teams = graph.store("teams")
    team = teams.add_team("Sales", bot)
    channel = teams.add_channel(team, "General")
    teams.add_message(channel, "Welcome to the sales channel.", sender=bot)
    identity = graph.identity
    identity.device_pending_polls = 0
    identity.device_interval = 1
    identity.device_user = bot.upn
    if scenario == "throttled":
        graph.throttle("/users/*", retry_after=60, times=None)
    with tempfile.TemporaryDirectory(prefix="awa-msworld-") as folder, graph:
        hook = (
            "https://prod-12.westeurope.logic.azure.com:443/workflows/0a1b2c3d/triggers/manual/"
            "paths/invoke?api-version=2016-06-01&sig=TEST-SIGNATURE-0000"
            if scenario == "old-webhook"
            else f"{graph.url}/workflows/check?sig=TEST-SIGNATURE-0000"
        )
        env = {
            **graph.env(app),
            "MS_MAILBOX": mailbox,
            "AWA_CHECK_NEGATIVE_MAILBOX": other,
            "MS_SITE_URL": f"https://{SITE_HOST}/{SITE_PATH}",
            "TEAMS_WEBHOOK_URL": hook,
            "TEAMS_TEAM_ID": team.id,
            "TEAMS_CHANNEL_ID": channel.id,
        }
        yield MicrosoftWorld(graph, env, Path(folder), dict(graph.connector_options))
