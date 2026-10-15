"""Catalog entry for Microsoft 365 over Microsoft Graph (data only)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from awa._internal.version import __version__
from awa.connectors._spec import (
    BearerAuth,
    ConnectorSpec,
    EnvVar,
    OAuth2ClientCredentials,
    Operation,
    Pagination,
    Param,
    TokenAuth,
    body_param,
    path_param,
    plain_var,
)

if TYPE_CHECKING:
    from awa.connectors._engine import ConnectorContext
    from awa.connectors.ports import TokenProvider


def _recipients(addresses: Any) -> list[dict[str, Any]]:
    return [{"emailAddress": {"address": str(address)}} for address in addresses]


_TENANT = plain_var("AZURE_TENANT_ID", "Directory (tenant) id of the Entra ID app registration.")
_CLIENT_ID = plain_var("AZURE_CLIENT_ID", "Application (client) id of the app registration.")
_CLIENT_SECRET = EnvVar("AZURE_CLIENT_SECRET", "A client secret of the app registration.")
_AUTHORITY = EnvVar(
    "AZURE_AUTHORITY_HOST",
    "Sign-in host of Microsoft Entra ID; set it for a sovereign cloud "
    "(https://login.microsoftonline.us) or a local test server.",
    secret=False,
    required=False,
    default="https://login.microsoftonline.com",
)
_GRAPH_URL = EnvVar(
    "MS_GRAPH_URL",
    "Host of Microsoft Graph; set it for a sovereign cloud (https://graph.microsoft.us) "
    "or a local test server.",
    secret=False,
    required=False,
    default="https://graph.microsoft.com",
)


def microsoft_user_agent() -> str:
    """`NONISV|awa|awa/<version>`: how Microsoft asks third-party apps to identify themselves."""

    return f"NONISV|awa|awa/{__version__}"


def _entra(scope: str) -> OAuth2ClientCredentials:
    return OAuth2ClientCredentials(
        "{AZURE_AUTHORITY_HOST}/{AZURE_TENANT_ID}/oauth2/v2.0/token",
        _CLIENT_ID,
        _CLIENT_SECRET,
        scope=scope,
        extra=(_AUTHORITY, _TENANT),
    )


CERTIFICATE_PATH = EnvVar(
    "AZURE_CLIENT_CERTIFICATE_PATH",
    "A PEM file with the private key and the certificate (or a .pfx/.p12 file) whose public part "
    "is uploaded to the app registration (Certificates & secrets, Certificates).",
    secret=False,
)
CERTIFICATE_PASSWORD = EnvVar(
    "AZURE_CLIENT_CERTIFICATE_PASSWORD",
    "The password of the certificate's private key, if it has one.",
    required=False,
)


def _certificate_provider(scope: str) -> Callable[[ConnectorContext], TokenProvider]:
    def build(ctx: ConnectorContext) -> TokenProvider:
        from awa.connectors._microsoft_cert import CertificateTokenProvider

        return CertificateTokenProvider(scope)

    return build


def _app_sign_in(scope: str) -> tuple[OAuth2ClientCredentials, TokenAuth]:
    """The client secret and the preferred certificate; the secret comes first for "missing"."""

    certificate = TokenAuth(
        _certificate_provider(scope),
        kind="app",
        env=(_AUTHORITY, _TENANT, _CLIENT_ID, CERTIFICATE_PATH, CERTIFICATE_PASSWORD),
        token_urls=("{AZURE_AUTHORITY_HOST}/{AZURE_TENANT_ID}/oauth2/v2.0/token",),
        preferred=True,
    )
    return _entra(scope), certificate


def _signed_in(error: Exception) -> Any:
    """Health: a 403 on `/organization` still proves the sign-in (least-privilege apps lack it)."""

    if "denied access (HTTP 403)" in str(error):
        return {"signed_in": True}
    raise error


_ODATA_PAGES = Pagination("next_url", next="@odata.nextLink", size_param="$top", size_max=100)
_USER = Param(
    "user",
    "User id or user principal name (name@company.com) of the mailbox.",
    location="path",
    required=True,
)
_TEAM = path_param("team_id", "Id of the team.")
_CHANNEL = path_param("channel_id", "Id of the channel.")
_SITE = path_param("site_id", "Id of the SharePoint site (from search_sites).")

MICROSOFT_GRAPH = ConnectorSpec(
    name="microsoft_graph",
    title="Microsoft 365 (Graph)",
    category="collaboration",
    summary="SharePoint files, Outlook mail and Teams channels through Microsoft Graph.",
    base_url="{MS_GRAPH_URL}/v1.0",
    env=(_GRAPH_URL,),
    auth=(
        *_app_sign_in("https://graph.microsoft.com/.default"),
        BearerAuth(
            EnvVar("MS_GRAPH_TOKEN", "A Graph access token (delegated sign-in)."), identity="user"
        ),
    ),
    headers={"User-Agent": microsoft_user_agent()},
    setup_steps={
        "user": "set MS_GRAPH_TOKEN to a delegated Graph access token (a signed-in user's)",
        "app": "set AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET (or "
        "AZURE_CLIENT_CERTIFICATE_PATH)",
    },
    hosts=("*.sharepoint.com",),
    operations=(
        Operation(
            "search_sites",
            "Find SharePoint sites by name or keyword.",
            path="/sites",
            params=(Param("query", "Words to search for.", key="search", required=True),),
            items="value",
            pages=Pagination("next_url", next="@odata.nextLink"),
        ),
        Operation(
            "list_files",
            "List the files and folders of a SharePoint site's document library folder.",
            path="/sites/{site_id}/drive/items/{item_id}/children",
            params=(
                _SITE,
                Param(
                    "item_id",
                    "Folder id; 'root' for the top folder.",
                    location="path",
                    default="root",
                ),
            ),
            query={"$select": "id,name,size,webUrl,lastModifiedDateTime,folder,file"},
            items="value",
            pages=_ODATA_PAGES,
        ),
        Operation(
            "read_file",
            "Read a text file (txt, csv, md, json, ...) from a SharePoint site.",
            path="/sites/{site_id}/drive/items/{item_id}/content",
            params=(_SITE, path_param("item_id", "Id of the file.")),
            response="text",
        ),
        Operation(
            "list_messages",
            "List the newest emails in a mailbox folder (subject, sender, preview).",
            path="/users/{user}/mailFolders/{folder}/messages",
            params=(
                _USER,
                Param("folder", "Mail folder, e.g. inbox.", location="path", default="inbox"),
                Param("filter", "OData filter, e.g. isRead eq false.", key="$filter"),
            ),
            query={"$select": "id,subject,from,receivedDateTime,bodyPreview,isRead"},
            items="value",
            pages=_ODATA_PAGES,
        ),
        Operation(
            "read_message",
            "Read one email with its plain-text body.",
            path="/users/{user}/messages/{message_id}",
            params=(_USER, path_param("message_id", "Id of the message.")),
            query={"$select": "subject,from,toRecipients,ccRecipients,receivedDateTime,body"},
            headers={"Prefer": 'outlook.body-content-type="text"'},
        ),
        Operation(
            "send_mail",
            "Send an email from a mailbox.",
            method="POST",
            path="/users/{user}/sendMail",
            params=(
                _USER,
                body_param(
                    "to",
                    "Recipient addresses.",
                    "message.toRecipients",
                    type="array",
                    items="string",
                    required=True,
                    convert=_recipients,
                ),
                body_param("subject", "Subject line.", "message.subject", required=True),
                body_param("body", "Plain-text message.", "message.body.content", required=True),
                body_param(
                    "cc",
                    "Copy recipients.",
                    "message.ccRecipients",
                    type="array",
                    items="string",
                    convert=_recipients,
                ),
            ),
            body={"message.body.contentType": "Text", "saveToSentItems": True},
            write=True,
        ),
        Operation(
            "list_channels",
            "List the channels of a Teams team.",
            path="/teams/{team_id}/channels",
            params=(_TEAM,),
            items="value",
        ),
        Operation(
            "read_channel",
            "Read the newest messages of a Teams channel.",
            path="/teams/{team_id}/channels/{channel_id}/messages",
            params=(_TEAM, _CHANNEL),
            items="value",
            pages=Pagination("next_url", next="@odata.nextLink", size_param="$top", size_max=50),
            max_limit=100,
        ),
        Operation(
            "post_channel_message",
            "Post a message to a Teams channel (needs a delegated token).",
            method="POST",
            path="/teams/{team_id}/channels/{channel_id}/messages",
            params=(
                _TEAM,
                _CHANNEL,
                body_param("text", "The message.", "body.content", required=True),
            ),
            write=True,
            needs="user",
        ),
    ),
    health=Operation(
        "health",
        "Ask Graph for the organization; a refusal for a missing permission still proves the sign-in.",
        path="/organization",
        query={"$select": "id"},
        fallback=_signed_in,
    ),
    docs="https://learn.microsoft.com/graph/api/overview",
    setup=(
        "Register an app in Microsoft Entra ID, add the application permissions the tools need "
        "(Sites.Read.All, Mail.Read, Mail.Send, ChannelMessage.Read.All), grant admin consent, "
        "and create a client secret. The connection check needs no extra permission: it counts "
        "as healthy when Graph accepts the sign-in, even if it refuses /organization "
        "(Organization.Read.All)."
    ),
)


def builtin_connectors() -> list[tuple[str, ConnectorSpec, str]]:
    """The connectors for Microsoft 365 as `(name, spec, summary)` rows, a provider of the registry."""

    from awa.connectors._saas_microsoft_outlook import OUTLOOK
    from awa.connectors._saas_microsoft_sharepoint import SHAREPOINT
    from awa.connectors._saas_microsoft_teams import TEAMS

    specs = (MICROSOFT_GRAPH, OUTLOOK, SHAREPOINT, TEAMS)
    return [(spec.name, spec, spec.summary) for spec in specs]
