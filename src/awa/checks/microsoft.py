"""The Microsoft 365 checks of `awa check microsoft`, in order; each later one needs the earlier.

Values, sign-in, Outlook, SharePoint, Excel (read locally: the workbook API takes no application
permissions), Teams, people search and Azure OpenAI's v1 route. Every call reads, except the Teams
card `--allow-post teams` allows; "Connect Microsoft 365" gives the Microsoft facts behind them.
"""

from __future__ import annotations

import ipaddress
import json
import re
import tempfile
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awa.checks._engine import Check, CheckContext, Result

DOCS = "https://learn.microsoft.com/graph/permissions-selected-overview"
SETUP_DOCS = "docs/guide/microsoft-365.md"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
DEFAULT_AUTHORITY = "https://login.microsoftonline.com"
DEFAULT_GRAPH = "https://graph.microsoft.com"

NAMES = (
    "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH",
    "AZURE_CLIENT_CERTIFICATE_PASSWORD", "AZURE_AUTHORITY_HOST", "MS_GRAPH_URL", "MS_MAILBOX",
    "MS_SITE_URL", "MS_CHECK_WORKBOOK", "TEAMS_WEBHOOK_URL", "TEAMS_TEAM_ID", "TEAMS_CHANNEL_ID",
    "AWA_CHECK_NEGATIVE_MAILBOX",
)  # fmt: skip
"""The variables the Microsoft checks read."""

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DOMAIN = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_MAIL = re.compile(r"^[^@\s]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_SITE_PATH = re.compile(r"^/(sites|teams)/[^/?#]+/?$")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_WIDE_SITES = {"Sites.Read.All", "Sites.ReadWrite.All", "Sites.Manage.All", "Sites.FullControl.All"}
_MAX_WORKBOOK = 20_000_000
# The v1 route documents openai.azure.com and services.ai.azure.com; Azure AI services and Foundry
# resources also show their endpoint as <resource>.cognitiveservices.azure.com, which serves the
# same /openai/v1/ route. The live probe is what proves a route works.
_V1_HOSTS = (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")

Problem = tuple[str, str, str]
"""Something wrong with a value, as `(said, fix, code)`."""


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() in _LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class _Answer:
    status: int
    data: Any
    text: str


class _Graph:
    """Read-only Graph calls with the app's token, through the connector engine's HTTP layer
    (host allowlist, timeouts, redaction, plain HTTP only to 127.0.0.1 for a local test server)."""

    def __init__(self, ctx: CheckContext) -> None:
        from awa.connectors._catalog import connector_context

        env = {name: value for name in NAMES if (value := ctx.value(name)) is not None}
        self.authority = (env.get("AZURE_AUTHORITY_HOST") or DEFAULT_AUTHORITY).rstrip("/")
        self.graph = (env.get("MS_GRAPH_URL") or DEFAULT_GRAPH).rstrip("/")
        env.setdefault("AZURE_AUTHORITY_HOST", self.authority)
        env.setdefault("MS_GRAPH_URL", self.graph)
        config = {**ctx.options, "retries": 0, "timeout": 20.0}
        self.connector = connector_context("microsoft_graph", env=env, config=config, dotenv=None)
        self.env = env
        self.token: str | None = None

    def sign_in(self) -> str:
        """The app's access token (certificate, else secret); `MicrosoftAuthError` if refused."""

        from awa.connectors._microsoft_errors import MicrosoftAuthError, explain

        if self.env.get("AZURE_CLIENT_CERTIFICATE_PATH"):  # a certificate wins over a secret
            from awa.connectors._microsoft_cert import CertificateTokenProvider

            self.token = CertificateTokenProvider(GRAPH_SCOPE).token(self.connector)
            return self.token
        from awa.connectors._engine import send

        tenant = urllib.parse.quote(self.env["AZURE_TENANT_ID"], safe="")
        response = send(
            self.connector,
            "POST",
            f"{self.authority}/{tenant}/oauth2/v2.0/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urllib.parse.urlencode(
                {
                    "grant_type": "client_credentials",
                    "client_id": self.env["AZURE_CLIENT_ID"],
                    "client_secret": self.env.get("AZURE_CLIENT_SECRET", ""),
                    "scope": GRAPH_SCOPE,
                }
            ).encode(),
            authenticate=False,
        )
        data = _json(response.text())
        token = data.get("access_token") if isinstance(data, dict) else None
        if response.status != 200 or not isinstance(token, str):
            advice = explain(response.status, data, service="entra", operation="client_credentials")
            raise MicrosoftAuthError(f"{advice.text} {advice.fix}", advice.code)
        self.token = token
        return token

    def get(self, path: str, *, headers: Mapping[str, str] | None = None) -> _Answer:
        """`GET {MS_GRAPH_URL}/v1.0{path}` with the token; any status is returned."""

        from awa.connectors._engine import send

        assert self.token is not None, "sign_in first"
        url = path if path.startswith(("https://", "http://")) else f"{self.graph}/v1.0{path}"
        response = send(
            self.connector,
            "GET",
            url,
            headers={"Authorization": f"Bearer {self.token}", **(headers or {})},
            authenticate=False,
        )
        return _Answer(response.status, _json(response.text()), response.text())

    def download(self, url: str, limit: int) -> bytes:
        """A pre-authenticated download URL (no token is sent)."""

        from awa.connectors._engine import send

        response = send(self.connector, "GET", url, authenticate=False, max_bytes=limit)
        if response.status != 200:
            raise OSError(f"the download answered HTTP {response.status}")
        if response.truncated:
            raise OSError(f"the file is larger than {limit} bytes")
        return response.body


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _graph(ctx: CheckContext) -> _Graph:
    found = ctx.shared.get("graph")
    if not isinstance(found, _Graph):
        found = _Graph(ctx)
        ctx.shared["graph"] = found
    return found


def _advice(answer: _Answer, service: str, operation: str) -> Any:
    from awa.connectors._microsoft_errors import explain

    return explain(answer.status, answer.data or answer.text, service=service, operation=operation)


def _failed(answer: _Answer, service: str, operation: str, said: str, **more: Any) -> Result:
    advice = _advice(answer, service, operation)
    return Result.fail(
        f"{said}: {advice.text.rstrip('.')} (HTTP {answer.status}).",
        advice.fix,
        code=advice.code,
        **more,
    )


def _admin_consent(client_id: str, permissions: str) -> str:
    return (
        f"In the Microsoft Entra admin center open Entra ID > App registrations > the app "
        f"{client_id} > API permissions, add the Microsoft Graph application permission(s) "
        f"{permissions}, then select 'Grant admin consent for <your organization>'."
    )


def _admin_rbac(client_id: str, mailbox: str) -> str:
    return (
        f"Limit the app {client_id} to the mailbox {mailbox} with Exchange RBAC for Applications "
        "(Exchange Online PowerShell; use the Object ID from Entra ID > Enterprise applications, "
        "not from App registrations):\n"
        f'   New-ServicePrincipal -AppId {client_id} -ObjectId <enterprise app object id> -DisplayName "awa"\n'
        f'   New-ManagementScope -Name "awa mailbox" -RecipientRestrictionFilter "PrimarySmtpAddress -eq \'{mailbox}\'"\n'
        f'   New-ManagementRoleAssignment -App {client_id} -Role "Application Mail.ReadWrite" -CustomResourceScope "awa mailbox"\n'
        f"   Test-ServicePrincipalAuthorization -Identity {client_id} -Resource {mailbox}\n"
        "   Then remove the tenant-wide Mail.* permissions of the app in Entra ID (API "
        "permissions): otherwise the scope has no effect. Changes take 30 minutes to 2 hours."
    )


def _admin_site(client_id: str, site: str) -> str:
    return (
        f"Grant the app {client_id} access to the one site {site} (it has Sites.Selected, which "
        "gives no access until a site is granted). With Microsoft Graph (needs "
        "Sites.FullControl.All, for example in Graph Explorer): look up the site id with "
        f"GET https://graph.microsoft.com/v1.0/sites/<host>:/<path>, then\n"
        "   POST https://graph.microsoft.com/v1.0/sites/<site id>/permissions\n"
        f'   {{"roles": ["write"], "grantedToIdentities": [{{"application": {{"id": "{client_id}", '
        '"displayName": "awa"}}]}\n'
        "   (or New-MgSitePermission in Microsoft Graph PowerShell). Use 'read' instead of 'write' "
        "if awa may only read."
    )


def check_values(ctx: CheckContext) -> Result:
    if not ctx.value("AZURE_TENANT_ID") and not ctx.value("AZURE_CLIENT_ID"):
        return Result.skip(
            "Microsoft 365 is not set up (AZURE_TENANT_ID and AZURE_CLIENT_ID are not set).",
            "Run awa connect microsoft, or fill in the Microsoft block of .env.example.",
            code="ms.not_configured",
            docs=SETUP_DOCS,
        )
    credential_errors, credential_warnings, days = _credential_problems(ctx)
    teams_errors, teams_warnings = _teams_problems(ctx)
    errors = [*_identity_problems(ctx), *credential_errors, *_address_problems(ctx), *teams_errors]
    warnings = [*credential_warnings, *teams_warnings]
    used = [n for n in NAMES if ctx.value(n) and n != "AZURE_CLIENT_CERTIFICATE_PASSWORD"]
    data = {"set": used, "certificate_days_left": None if days is None else int(days)}
    if errors:
        said, fix, code = errors[0]
        more = "; ".join(e[0] for e in errors[1:])
        return Result.fail(
            said[0].upper() + said[1:] + "." + (f" Also: {more}." if more else ""),
            fix,
            code=code,
            data={**data, "problems": [e[2] for e in errors]},
            docs=SETUP_DOCS,
        )
    if warnings:
        said, fix, code = warnings[0]
        return Result.warn(
            f"The values look right, but {said}.", fix, code=code, data=data, docs=SETUP_DOCS
        )
    return Result.ok(f"The values look right ({len(used)} set).", data=data)


def _identity_problems(ctx: CheckContext) -> list[Problem]:
    problems: list[Problem] = []
    tenant, client = ctx.value("AZURE_TENANT_ID"), ctx.value("AZURE_CLIENT_ID")
    if not tenant or not (_GUID.fullmatch(tenant) or _DOMAIN.fullmatch(tenant)):
        problems.append(
            (
                "AZURE_TENANT_ID is not a tenant id or domain",
                "Copy the Directory (tenant) ID from the app's Overview page.",
                "ms.tenant_shape",
            )
        )
    if not client or not _GUID.fullmatch(client):
        problems.append(
            (
                "AZURE_CLIENT_ID is not an application id",
                "Copy the Application (client) ID (a GUID) from the app's Overview page.",
                "ms.client_shape",
            )
        )
    return problems


def _credential_problems(ctx: CheckContext) -> tuple[list[Problem], list[Problem], float | None]:
    errors: list[Problem] = []
    warnings: list[Problem] = []
    secret = ctx.value("AZURE_CLIENT_SECRET")
    certificate = ctx.value("AZURE_CLIENT_CERTIFICATE_PATH")
    if secret and _GUID.fullmatch(secret):
        errors.append(
            (
                "AZURE_CLIENT_SECRET looks like the Secret ID, not the secret",
                "In Certificates & secrets copy the VALUE column (shown only once after "
                "creating the secret); create a new secret if it is hidden.",
                "ms.secret_id_copied",
            )
        )
    if not secret and not certificate:
        errors.append(
            (
                "neither AZURE_CLIENT_SECRET nor AZURE_CLIENT_CERTIFICATE_PATH is set",
                "Set a client secret, or (recommended) a certificate file.",
                "ms.credential_missing",
            )
        )
    if secret and certificate:
        warnings.append(
            (
                "both a client secret and a certificate are set; the certificate is used",
                "Remove AZURE_CLIENT_SECRET from .env once the certificate works.",
                "ms.secret_and_certificate",
            )
        )
    if not certificate:
        return errors, warnings, None
    from awa.connectors._microsoft_cert import expires_in_days, load_certificate
    from awa.connectors._microsoft_errors import MicrosoftAuthError

    try:
        loaded = load_certificate(certificate, ctx.value("AZURE_CLIENT_CERTIFICATE_PASSWORD"))
    except MicrosoftAuthError as error:
        errors.append((str(error), "Check AZURE_CLIENT_CERTIFICATE_PATH.", error.code))
        return errors, warnings, None
    days = expires_in_days(loaded)
    ctx.shared["certificate_sha1"] = loaded.sha1
    if days <= 0:
        errors.append(
            (
                f"the certificate expired on {loaded.not_after:%Y-%m-%d}",
                "Create a new certificate, upload its public part to the app and update "
                "AZURE_CLIENT_CERTIFICATE_PATH.",
                "ms.certificate_expired",
            )
        )
    elif days < 30:
        warnings.append(
            (
                f"the certificate expires in {int(days)} days",
                "Upload a new certificate to the app registration before then.",
                "ms.certificate_expires_soon",
            )
        )
    return errors, warnings, days


def _address_problems(ctx: CheckContext) -> list[Problem]:
    problems: list[Problem] = []
    for name, kind in (("AZURE_AUTHORITY_HOST", "sign-in host"), ("MS_GRAPH_URL", "Graph host")):
        value = ctx.value(name)
        if value:
            parts = urllib.parse.urlsplit(value)
            if parts.scheme != "https" and not (
                parts.scheme == "http" and _loopback(parts.hostname)
            ):
                problems.append(
                    (
                        f"{name} must be an https:// {kind}",
                        f"Leave {name} empty (it is only for sovereign clouds and local tests).",
                        "ms.host_shape",
                    )
                )
    mailbox = ctx.value("MS_MAILBOX")
    if mailbox and not _MAIL.fullmatch(mailbox):
        problems.append(
            (
                "MS_MAILBOX is not an email address",
                "Set the full address of the one mailbox.",
                "ms.mailbox_shape",
            )
        )
    negative = ctx.value("AWA_CHECK_NEGATIVE_MAILBOX")
    if negative and not _MAIL.fullmatch(negative):
        problems.append(
            (
                "AWA_CHECK_NEGATIVE_MAILBOX is not an email address",
                "Set the address of a mailbox the app must not read.",
                "ms.mailbox_shape",
            )
        )
    if negative and mailbox and negative.lower() == mailbox.lower():
        problems.append(
            (
                "AWA_CHECK_NEGATIVE_MAILBOX is the mailbox awa works in",
                "Choose another mailbox, one the app must not be able to read.",
                "ms.negative_is_mailbox",
            )
        )
    site = ctx.value("MS_SITE_URL")
    if site:
        parts = urllib.parse.urlsplit(site)
        host = (parts.hostname or "").lower()
        if (
            parts.scheme != "https"
            or not host.endswith(".sharepoint.com")
            or not _SITE_PATH.fullmatch(parts.path)
        ):
            problems.append(
                (
                    "MS_SITE_URL is not a SharePoint site address",
                    "Use the site's address, such as https://company.sharepoint.com/sites/sales.",
                    "ms.site_shape",
                )
            )
    return problems


def _teams_problems(ctx: CheckContext) -> tuple[list[Problem], list[Problem]]:
    errors: list[Problem] = []
    warnings: list[Problem] = []
    hook = ctx.value("TEAMS_WEBHOOK_URL")
    problem = webhook_problem(hook) if hook else None
    if problem is not None:
        (errors if problem[2] == "ms.webhook_retired" else warnings).append(problem)
    team, channel = ctx.value("TEAMS_TEAM_ID"), ctx.value("TEAMS_CHANNEL_ID")
    if not team and not channel:
        return errors, warnings
    from awa.connectors._saas_microsoft_teams import CHANNEL_PATTERN, TEAM_PATTERN

    if not team or not TEAM_PATTERN.fullmatch(team):
        errors.append(
            (
                "TEAMS_TEAM_ID is not a team id",
                "Copy the groupId= part of the team's link.",
                "ms.team_shape",
            )
        )
    if not channel or not CHANNEL_PATTERN.fullmatch(urllib.parse.unquote(channel)):
        errors.append(
            (
                "TEAMS_CHANNEL_ID is not a channel id",
                "Copy the 19:...@thread.tacv2 part of the channel's link.",
                "ms.channel_shape",
            )
        )
    return errors, warnings


def webhook_problem(url: str) -> Problem | None:
    """What is wrong with a Teams webhook address, as `(said, fix, code)`, or `None`.

    `ms.webhook_retired` (a Microsoft 365 connector address, switched off in May 2026) is an
    error; the old Power Automate host and an unknown host are warnings.
    """

    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    # Refuse the connector webhooks Microsoft retired in May 2026 (switched off).
    if host.endswith(".webhook.office.com") or (
        host in ("outlook.office.com", "outlook.office365.com")
        and parts.path.lower().startswith("/webhook")
    ):
        return (
            "TEAMS_WEBHOOK_URL is a Microsoft 365 (Office 365) connector address, which Microsoft "
            "switched off in May 2026",
            "In Teams open the channel menu (...) > Workflows > 'Send webhook alerts to a "
            "channel' and copy the new link into TEAMS_WEBHOOK_URL.",
            "ms.webhook_retired",
        )
    if host.endswith(".logic.azure.com"):
        return (
            "TEAMS_WEBHOOK_URL uses the old Power Automate host logic.azure.com, whose trigger "
            "links stopped working on 30 November 2025",
            "Open the workflow (Teams > Workflows, or Power Automate), copy the trigger's new "
            "link (host ...environment.api.powerplatform.com) into TEAMS_WEBHOOK_URL.",
            "ms.webhook_old_host",
        )
    if parts.scheme == "http" and _loopback(host):
        return None
    if parts.scheme != "https":
        return (
            "TEAMS_WEBHOOK_URL is not an https:// address",
            "Copy the whole link again.",
            "ms.webhook_shape",
        )
    if not host.endswith(".api.powerplatform.com"):
        return (
            f"TEAMS_WEBHOOK_URL points at {host}, not at a Teams Workflows address",
            "Use the link of a Teams workflow (host ...environment.api.powerplatform.com).",
            "ms.webhook_unknown_host",
        )
    if "sig=" not in parts.query:
        return (
            "TEAMS_WEBHOOK_URL has no sig= part",
            "Copy the whole link; without sig= only flows set to 'Anyone' can be called.",
            "ms.webhook_no_signature",
        )
    return None


def check_sign_in(ctx: CheckContext) -> Result:
    from awa._internal.upstream.tools import ToolException
    from awa.connectors._microsoft_auth import decode_claims
    from awa.connectors._microsoft_errors import MicrosoftAuthError

    graph = _graph(ctx)
    how = "certificate" if ctx.value("AZURE_CLIENT_CERTIFICATE_PATH") else "client secret"
    try:
        token = graph.sign_in()
    except MicrosoftAuthError as error:
        message = str(error)
        text, _, fix = message.partition(". ")
        return Result.fail(
            f"The app could not sign in with its {how}: {text}.",
            fix or "Check AZURE_TENANT_ID, AZURE_CLIENT_ID and the credential.",
            code=error.code,
            data={"authority": graph.authority},
        )
    except ToolException as error:
        return Result.fail(
            f"Microsoft's sign-in host {graph.authority} could not be reached: {error}",
            "Check the network, a proxy, or AZURE_AUTHORITY_HOST.",
            code="ms.unreachable",
            data={"authority": graph.authority},
        )
    roles = sorted(decode_claims(token).roles)
    ctx.shared["roles"] = roles
    client = ctx.value("AZURE_CLIENT_ID") or "<client id>"
    data = {"authority": graph.authority, "credential": how, "permissions": roles}
    shown = ", ".join(roles) or "none"
    if ctx.value("MS_SITE_URL") and not any(r.startswith("Sites.") for r in roles):
        return Result.fail(
            f"The app signed in, but its token holds no SharePoint permission (granted: {shown}).",
            "An administrator must add Sites.Selected and grant admin consent.",
            admin=_admin_consent(client, "Sites.Selected"),
            code="ms.consent_missing",
            data=data,
        )
    mail_roles = [r for r in roles if r.startswith("Mail.")]
    if mail_roles:
        return Result.warn(
            f"The app signed in ({how}). Permissions granted: {shown}. {', '.join(mail_roles)} is "
            "granted tenant-wide: the app can read every mailbox of the organization.",
            "Scope the app to one mailbox with Exchange RBAC for Applications, then remove the "
            "tenant-wide Mail permissions.",
            admin=_admin_rbac(client, ctx.value("MS_MAILBOX") or "<mailbox>"),
            code="ms.mail_tenant_wide",
            data=data,
        )
    wide = sorted(set(roles) & _WIDE_SITES)
    if wide and ctx.value("MS_SITE_URL"):
        return Result.warn(
            f"The app signed in ({how}). Permissions granted: {shown}. {', '.join(wide)} reaches "
            "every site of the organization.",
            "Use Sites.Selected and grant only the one site instead.",
            admin=_admin_site(client, ctx.value("MS_SITE_URL") or "<site>"),
            code="ms.sites_tenant_wide",
            data=data,
        )
    note = "" if roles else " (none in the token: Outlook then works only through RBAC)"
    return Result.ok(f"The app signed in ({how}). Permissions granted: {shown}{note}.", data=data)


def _inbox(mailbox: str) -> str:
    return (
        f"/users/{urllib.parse.quote(mailbox, safe='@')}/mailFolders/inbox"
        "?$select=totalItemCount,unreadItemCount"
    )


def check_outlook(ctx: CheckContext) -> Result:
    graph, mailbox = _graph(ctx), ctx.value("MS_MAILBOX") or ""
    answer = graph.get(_inbox(mailbox))
    client = ctx.value("AZURE_CLIENT_ID") or "<client id>"
    if answer.status != 200:
        admin = _admin_rbac(client, mailbox) if answer.status == 403 else None
        return _failed(
            answer, "outlook", "read", f"The inbox of {mailbox} could not be read", admin=admin
        )
    folder = answer.data if isinstance(answer.data, dict) else {}
    newest = graph.get(
        f"/users/{urllib.parse.quote(mailbox, safe='@')}/mailFolders/inbox/messages"
        "?$top=1&$select=subject,receivedDateTime"
    )
    received = None
    if newest.status == 200 and isinstance(newest.data, dict) and newest.data.get("value"):
        received = newest.data["value"][0].get("receivedDateTime")
    when = f"; newest message received {received}" if received else ""
    return Result.ok(
        f"The app can read the mailbox {mailbox}: {folder.get('totalItemCount', '?')} messages, "
        f"{folder.get('unreadItemCount', '?')} unread{when}.",
        data={"total": folder.get("totalItemCount"), "unread": folder.get("unreadItemCount")},
    )


def check_outlook_scope(ctx: CheckContext) -> Result:
    other = ctx.value("AWA_CHECK_NEGATIVE_MAILBOX")
    if not other:
        return Result.skip(
            "The negative mailbox test is not set up.",
            "Set AWA_CHECK_NEGATIVE_MAILBOX to a mailbox the app must NOT read; the check then "
            "proves the RBAC scope.",
            code="ms.negative_not_set",
        )
    answer = _graph(ctx).get(_inbox(other))
    client = ctx.value("AZURE_CLIENT_ID") or "<client id>"
    if answer.status == 403:
        return Result.ok(f"The app is refused for {other}, as it should be (HTTP 403).")
    if answer.status == 200:
        return Result.fail(
            f"The app can read the mailbox {other} too: it is not limited to one mailbox.",
            "Scope the app with Exchange RBAC for Applications and remove the tenant-wide Mail "
            "permissions (changes take up to 2 hours).",
            admin=_admin_rbac(client, ctx.value("MS_MAILBOX") or "<mailbox>"),
            code="ms.rbac_not_scoped",
        )
    if answer.status == 404:
        return Result.warn(
            f"The mailbox {other} does not exist, so the test proves nothing.",
            "Set AWA_CHECK_NEGATIVE_MAILBOX to an existing mailbox.",
            code="ms.negative_unknown",
        )
    return _failed(answer, "outlook", "read", f"The negative test on {other} did not answer")


def _site_path(site: str) -> str:
    parts = urllib.parse.urlsplit(site)
    return f"/sites/{parts.hostname}:{urllib.parse.quote(parts.path.rstrip('/'))}"


def check_sharepoint(ctx: CheckContext) -> Result:
    graph, site = _graph(ctx), ctx.value("MS_SITE_URL") or ""
    client = ctx.value("AZURE_CLIENT_ID") or "<client id>"
    answer = graph.get(_site_path(site))
    if answer.status == 403:
        return Result.fail(
            f"The app has no access to the site {site} yet (HTTP 403).",
            "Ask a SharePoint administrator to grant the app 'write' (or 'read') on this one "
            "site (text below).",
            admin=_admin_site(client, site),
            code="ms.site_not_granted",
            docs=DOCS,
        )
    if answer.status != 200:
        return _failed(answer, "sharepoint", "read", f"The site {site} could not be read")
    site_id = answer.data.get("id") if isinstance(answer.data, dict) else None
    drive = graph.get(f"/sites/{urllib.parse.quote(str(site_id), safe=',')}/drive")
    if drive.status != 200:
        return _failed(drive, "sharepoint", "read", "The document library could not be read")
    drive_id = drive.data.get("id") if isinstance(drive.data, dict) else None
    children = graph.get(
        f"/drives/{urllib.parse.quote(str(drive_id), safe='!')}/root/children?$top=5"
    )
    if children.status != 200:
        return _failed(children, "sharepoint", "read", "The library's files could not be listed")
    items = children.data.get("value", []) if isinstance(children.data, dict) else []
    ctx.shared["drive_id"] = drive_id
    return Result.ok(
        f"The app can read the site {site} and its document library ({len(items)} item(s) at "
        "the top shown).",
        data={"site_id": site_id, "drive_id": drive_id},
    )


def check_sharepoint_scope(ctx: CheckContext) -> Result:
    site = ctx.value("MS_SITE_URL") or ""
    if urllib.parse.urlsplit(site).path.strip("/") == "":
        return Result.skip("MS_SITE_URL is the root site; the negative test needs another site.")
    answer = _graph(ctx).get("/sites/root")
    if answer.status == 403:
        return Result.ok(
            "The app is refused for the organization's root site, as it should be (HTTP 403)."
        )
    if answer.status == 200:
        return Result.fail(
            "The app can read the organization's root site: it can reach every site.",
            "Replace Sites.Read.All (or higher) with Sites.Selected and grant only the one site.",
            admin=_admin_site(ctx.value("AZURE_CLIENT_ID") or "<client id>", site),
            code="ms.sites_not_scoped",
        )
    return _failed(answer, "sharepoint", "read", "The negative test on /sites/root did not answer")


def check_excel(ctx: CheckContext) -> Result:
    graph = _graph(ctx)
    drive = urllib.parse.quote(str(ctx.shared.get("drive_id")), safe="!")
    wanted = ctx.value("MS_CHECK_WORKBOOK")
    if wanted:
        answer = graph.get(f"/drives/{drive}/root:/{urllib.parse.quote(wanted.strip('/'))}")
        item = answer.data if answer.status == 200 and isinstance(answer.data, dict) else None
        if item is None:
            return _failed(
                answer, "sharepoint", "read", f"The workbook {wanted} could not be found"
            )
    else:
        answer = graph.get(f"/drives/{drive}/root/children?$top=200")
        values = answer.data.get("value", []) if isinstance(answer.data, dict) else []
        item = next((v for v in values if str(v.get("name", "")).lower().endswith(".xlsx")), None)
        if item is None:
            return Result.warn(
                "No .xlsx workbook is at the top of the library, so Excel was not checked.",
                "Put a workbook there, or set MS_CHECK_WORKBOOK to its path in the library.",
                code="ms.no_workbook",
            )
    link = item.get("@microsoft.graph.downloadUrl")
    if not isinstance(link, str):
        return Result.fail(
            f"Microsoft gave no download link for {item.get('name')}.",
            "Check that the app has read access to the file.",
            code="ms.no_download",
        )
    from awa._internal.install import require_module
    from awa.errors import OptionalDependencyError

    try:
        load_workbook = require_module(
            "openpyxl", "openpyxl", feature="Reading Excel files"
        ).load_workbook
    except OptionalDependencyError:
        return Result.fail(
            "Reading Excel files needs openpyxl.",
            'pip install "awa-agents[m365]"',
            code="check.dependency_missing",
        )
    size = item.get("size")
    if isinstance(size, int) and size > _MAX_WORKBOOK:
        return Result.fail(
            f"The workbook {item.get('name')} is larger than {_MAX_WORKBOOK:,} bytes, too large "
            "for this check.",
            "Set MS_CHECK_WORKBOOK to the path of a smaller workbook in the library.",
            code="ms.workbook_too_large",
        )
    data = graph.download(link, _MAX_WORKBOOK)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "workbook.xlsx"
        path.write_bytes(data)
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook.worksheets[0]
            cells = [
                list(row)
                for row in sheet.iter_rows(min_row=1, max_row=5, max_col=5, values_only=True)
            ]
            title = sheet.title
        finally:
            workbook.close()
    filled = sum(1 for row in cells for cell in row if cell is not None)
    return Result.ok(
        f"The workbook {item.get('name')} was downloaded and read on this computer (sheet "
        f"{title!r}, {filled} of the cells A1:E5 filled). The Excel web API is not used: it "
        "does not work with app permissions.",
        data={"workbook": item.get("name"), "sheet": title, "filled": filled},
    )


def _teams_connector(ctx: CheckContext) -> Any:
    from awa.connectors._catalog import connector_context

    config = {**ctx.options, "retries": 0}
    cache = ctx.shared.get("token_cache")
    config["token_cache"] = (
        cache if isinstance(cache, str) else str(ctx.project / ".awa" / "ms_token_cache.json")
    )
    return connector_context("teams", env=dict(_graph(ctx).env), config=config, dotenv=None)


def check_teams(ctx: CheckContext) -> Result:
    from awa._internal.upstream.tools import ToolException
    from awa.connectors._microsoft_errors import MicrosoftAuthError

    hook = ctx.value("TEAMS_WEBHOOK_URL")
    lines: list[str] = []
    if hook:
        problem = webhook_problem(hook)
        if problem is not None and problem[2] in ("ms.webhook_retired", "ms.webhook_old_host"):
            return Result.fail(problem[0] + ".", problem[1], code=problem[2])
        host = urllib.parse.urlsplit(hook).hostname
        lines.append(f"the Workflows address ({host}) is accepted; no card was sent")
    if not (ctx.value("AZURE_TENANT_ID") and ctx.value("AZURE_CLIENT_ID")):
        if not lines:
            return _teams_not_set_up()
        return Result.ok("Teams: " + "; ".join(lines) + " (webhook only, no user sign-in set up).")
    connector = _teams_connector(ctx)
    try:
        from awa.connectors import Operation
        from awa.connectors._engine import call_operation
        from awa.connectors._microsoft_auth import UserTokenProvider, decode_claims

        provider = UserTokenProvider(connector)
        token = provider.token(connector)
        me = call_operation(
            connector,
            Operation(
                "whoami", "", path="/me", query={"$select": "userPrincipalName"}, needs="user"
            ),
            {},
        )
        scopes = ", ".join(decode_claims(token).scopes) or "unknown"
        lines.append(f"signed in as {me.get('userPrincipalName', '?')} (permissions: {scopes})")
    except MicrosoftAuthError as error:
        if error.code != "ms.not_signed_in":
            text, _, fix = str(error).partition(". ")
            return Result.fail(f"The Teams sign-in does not work: {text}.", fix, code=error.code)
        if not lines:
            return _teams_not_set_up()
        return Result.ok(
            "Teams: " + "; ".join(lines) + "; no user is signed in (only needed to post as a user)."
        )
    except ToolException as error:
        return Result.fail(
            f"The Teams user could not be read: {error}",
            "Sign in again: awa connect microsoft --teams",
            code="ms.sign_in_rejected",
        )
    team, channel = ctx.value("TEAMS_TEAM_ID"), ctx.value("TEAMS_CHANNEL_ID")
    if not team or not channel:
        return Result.warn(
            "Teams: " + "; ".join(lines) + ". TEAMS_TEAM_ID and TEAMS_CHANNEL_ID are not set, so "
            "the user cannot post or read anywhere.",
            "Set the one team and channel the agents may use (the channel's link).",
            code="ms.channel_not_pinned",
        )
    tools = {t.name: t for t in _tools(connector)}
    answer = str(tools["teams_list_channel_messages"].invoke({"limit": 1}))
    if not answer.startswith("<tool_output"):
        return Result.fail(
            "Teams: the pinned channel could not be read: " + answer.removeprefix("Error: "),
            "Check TEAMS_TEAM_ID and TEAMS_CHANNEL_ID, and that the user is a member.",
            code="ms.channel_unreadable",
        )
    lines.append("the pinned channel is readable")
    return Result.ok("Teams: " + "; ".join(lines) + ".")


def _teams_not_set_up() -> Result:
    return Result.skip(
        "Teams is not set up (no Workflows webhook, no signed-in user).",
        "Set TEAMS_WEBHOOK_URL, or sign the Teams user in: awa connect microsoft --teams.",
        code="ms.teams_not_set_up",
    )


def _tools(connector: Any) -> list[Any]:
    return list(connector.spec.factory(connector))


def check_teams_card(ctx: CheckContext) -> Result:
    connector = _teams_connector(ctx)
    # `--allow-post teams` is the person's approval of this one message: the tool must not wait
    # for another one (there is no run to pause here).
    connector.config = {**connector.config, "requires_approval": False}
    tools = {t.name: t for t in _tools(connector)}
    text = "awa connection test, you can ignore this."
    if ctx.value("TEAMS_WEBHOOK_URL"):
        answer = str(tools["teams_post_card"].invoke({"title": "awa", "text": text}))
        where = "through the Workflows webhook"
    elif ctx.value("TEAMS_TEAM_ID"):
        answer = str(tools["teams_post_message"].invoke({"text": text}))
        where = "to the pinned channel as the signed-in user"
    else:
        return Result.skip("No Teams destination is set up.")
    if not answer.startswith("<tool_output"):
        return Result.fail(
            "The test message was not accepted: " + answer.removeprefix("Error: "),
            "Check the workflow (turned on, 'When a Teams webhook request is received').",
            code="ms.card_rejected",
        )
    return Result.ok(f"One test message was sent {where}; look for it in the channel.")


def check_people(ctx: CheckContext) -> Result:
    answer = _graph(ctx).get(
        '/users?$search="displayName:a"&$count=true&$top=1&$select=displayName',
        headers={"ConsistencyLevel": "eventual"},
    )
    if answer.status == 200:
        return Result.ok("People search works (one person found by name).")
    if answer.status == 403:
        return Result.warn(
            "People search is not allowed for the app (optional: only needed to choose people "
            "by name).",
            "Add the application permission User.Read.All if awa should search people.",
            admin=_admin_consent(ctx.value("AZURE_CLIENT_ID") or "<client id>", "User.Read.All"),
            code="ms.people_not_allowed",
        )
    advice = _advice(answer, "graph", "search")
    return Result.warn(
        f"People search did not work: {advice.text} (HTTP {answer.status})",
        advice.fix,
        code=advice.code,
    )


def check_azure_openai(ctx: CheckContext) -> Result:
    endpoint = ctx.value("AZURE_OPENAI_ENDPOINT") or ""
    parts = urllib.parse.urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(_V1_HOSTS):
        return Result.fail(
            f"AZURE_OPENAI_ENDPOINT ({host or endpoint}) is not an Azure OpenAI resource address.",
            "Use https://<resource>.openai.azure.com/, https://<resource>.services.ai.azure.com/ "
            "or https://<resource>.cognitiveservices.azure.com/ (the endpoint shown on the resource).",
            code="ms.aoai_endpoint",
        )
    from awa.models._azure import v1_base_url

    route = v1_base_url(endpoint)
    if ctx.value("OPENAI_API_VERSION"):
        return Result.warn(
            f"Azure OpenAI uses the dated route (OPENAI_API_VERSION={ctx.value('OPENAI_API_VERSION')}).",
            f"Remove OPENAI_API_VERSION to use the v1 route {route} (no API version to update).",
            code="ms.aoai_dated_route",
        )
    if ctx.no_model:
        return Result.ok(
            f"Azure OpenAI uses the v1 route {route} (no model call made).", data={"route": route}
        )
    return _probe_azure_openai(ctx, route)


def _probe_azure_openai(ctx: CheckContext, route: str) -> Result:
    """One tiny call (at most 16 tokens back) to the first profile that uses `azure_openai`."""

    from awa.core.settings import Settings
    from awa.kernel.redaction import redact_values
    from awa.models import ModelRegistry, ModelSettings
    from awa.models.errors import azure_error_hint, model_identity

    env_file = ctx.project / ".env"
    settings = Settings(_env_file=env_file if env_file.is_file() else None)
    for profile in ("default", "fast", "strong"):
        if (
            str(getattr(settings, f"{profile}_model", "") or "").split(":", 1)[0].strip().lower()
            == "azure_openai"
        ):
            break
    else:
        return Result.skip(
            f"No model profile uses azure_openai, so no call was made (route {route}).",
            "Set AWA_DEFAULT_MODEL=azure_openai:<deployment> to use it.",
        )
    try:
        model = ModelRegistry(settings).get(
            profile, settings=ModelSettings(max_tokens=16, timeout=60, max_retries=0)
        )
        model.invoke("Reply with the single word: OK")
    except Exception as error:
        secrets = [
            settings.credential(name) for name in ("AZURE_OPENAI_API_KEY", "AZURE_CLIENT_SECRET")
        ]
        message = redact_values(f"{type(error).__name__}: {error}", secrets, min_len=6)
        return Result.fail(
            f"Azure OpenAI on the v1 route {route}: the {profile} model does not answer: {message}",
            azure_error_hint(error, provider="azure_openai")
            or "Check the model name and the credentials in .env (awa doctor lists them).",
            code="model.failed",
        )
    label = model_identity(model)
    return Result.ok(
        f"Azure OpenAI on the v1 route {route}: {label} answered.", data={"model": label}
    )


CHECKS: tuple[Check, ...] = (
    Check("microsoft.values", "microsoft", "Microsoft values", check_values),
    Check(
        "microsoft.sign_in", "microsoft", "app sign-in", check_sign_in, after=("microsoft.values",)
    ),
    Check(
        "microsoft.outlook",
        "microsoft",
        "Outlook mailbox",
        check_outlook,
        needs=("MS_MAILBOX",),
        after=("microsoft.sign_in",),
    ),
    Check(
        "microsoft.outlook_scope",
        "microsoft",
        "Outlook negative test",
        check_outlook_scope,
        after=("microsoft.outlook",),
    ),
    Check(
        "microsoft.sharepoint",
        "microsoft",
        "SharePoint site",
        check_sharepoint,
        needs=("MS_SITE_URL",),
        after=("microsoft.sign_in",),
    ),
    Check(
        "microsoft.sharepoint_scope",
        "microsoft",
        "SharePoint negative test",
        check_sharepoint_scope,
        after=("microsoft.sharepoint",),
    ),
    Check(
        "microsoft.excel",
        "microsoft",
        "Excel workbook",
        check_excel,
        after=("microsoft.sharepoint",),
    ),
    # Teams needs no app sign-in: a Workflows webhook alone works (and a user sign-in alone).
    Check("microsoft.teams", "microsoft", "Teams", check_teams),
    Check(
        "microsoft.teams_card",
        "microsoft",
        "Teams test message",
        check_teams_card,
        after=("microsoft.teams",),
        writes=True,
        allow="teams",
    ),
    Check(
        "microsoft.people", "microsoft", "people search", check_people, after=("microsoft.sign_in",)
    ),
    Check(
        "microsoft.azure_openai",
        "microsoft",
        "Azure OpenAI",
        check_azure_openai,
        needs=("AZURE_OPENAI_ENDPOINT",),
    ),
)
"""The Microsoft checks in the order they run."""

__all__ = ["CHECKS", "NAMES", "webhook_problem"]
