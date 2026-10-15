"""Plain-language advice for Microsoft Graph and Entra ID errors.

`explain` turns a failed Microsoft request into an `Advice` whose stable code tests can assert.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from awa._internal.upstream.tools import ToolException

SIGN_IN_COMMAND = "awa connect microsoft --teams"
"""The command that signs the Teams user in (named by every advice that asks for a new sign-in)."""


class MicrosoftAuthError(ToolException):
    """A sign-in or token problem worded for the person who fixes it; `code` is a stable code."""

    def __init__(self, message: str, code: str = "ms.error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Advice:
    """What went wrong (`text`) and the step that fixes it (`fix`); `code` is stable."""

    code: str
    text: str
    fix: str


_SIGN_IN = {
    "7000215": (
        "ms.secret_wrong",
        "The client secret is wrong.",
        "Copy the secret VALUE (not its id) into AZURE_CLIENT_SECRET.",
    ),
    "7000222": (
        "ms.secret_expired",
        "The client secret has expired.",
        "Create a new client secret in the app registration and update AZURE_CLIENT_SECRET.",
    ),
    "700027": (
        "ms.certificate_rejected",
        "Microsoft did not accept the certificate (it is not uploaded to this app, or it expired).",
        "Upload the certificate's public part to the app registration (Certificates & secrets, "
        "Certificates), or point AZURE_CLIENT_CERTIFICATE_PATH at the certificate you uploaded.",
    ),
    "700024": (
        "ms.assertion_time",
        "The certificate sign-in was outside its valid time; this computer's clock may be wrong.",
        "Set this computer's clock to the correct time (automatic time synchronization).",
    ),
    "700016": (
        "ms.app_unknown",
        "The application id is not known in this tenant.",
        "Check AZURE_CLIENT_ID and AZURE_TENANT_ID.",
    ),
    "90002": (
        "ms.tenant_unknown",
        "The tenant was not found.",
        "Check AZURE_TENANT_ID (a GUID or the tenant domain).",
    ),
    "65001": (
        "ms.consent_missing",
        "The app has not been consented to.",
        "An administrator must grant admin consent for the app's permissions.",
    ),
    "53003": (
        "ms.blocked_by_policy",
        "A Conditional Access policy blocked the sign-in.",
        "Ask your administrator to allow this app or sign-in method.",
    ),
    "7000218": (
        "ms.public_client_off",
        "The app registration does not allow sign-in without a client secret.",
        "In the app registration open Authentication, Advanced settings, and set 'Allow public client flows' to Yes.",
    ),
    "70008": (
        "ms.sign_in_expired",
        "The saved Microsoft sign-in has expired (it was not used for 90 days).",
        f"Sign in again: {SIGN_IN_COMMAND}",
    ),
    "70043": (
        "ms.sign_in_expired",
        "The saved Microsoft sign-in has expired (a sign-in frequency policy asks for a new one).",
        f"Sign in again: {SIGN_IN_COMMAND}",
    ),
    "50173": (
        "ms.sign_in_revoked",
        "The saved Microsoft sign-in was revoked (a password change or an administrator).",
        f"Sign in again: {SIGN_IN_COMMAND}",
    ),
    "50076": (
        "ms.mfa_required",
        "Microsoft asks this account for multi-factor authentication.",
        f"Sign in again and complete the extra step: {SIGN_IN_COMMAND}",
    ),
    "70019": (
        "ms.device_code_expired",
        "The sign-in code has expired (it works for 15 minutes).",
        f"Start the sign-in again: {SIGN_IN_COMMAND}",
    ),
}

_GRAPH = {
    "InvalidAuthenticationToken": (
        "ms.token_invalid",
        "Microsoft rejected the access token (expired or for another service).",
        "Retry; if it persists check the tenant, client id and secret.",
    ),
    "Authorization_RequestDenied": (
        "ms.consent_missing",
        "The app lacks a permission for this call.",
        "Add the permission in the app registration and grant admin consent.",
    ),
    "ErrorAccessDenied": (
        "ms.rbac_scope",
        "The app may not use this mailbox.",
        "Check the Exchange application access scope of the app for this mailbox.",
    ),
    "ErrorInvalidUser": (
        "ms.mailbox_unknown",
        "The mailbox does not exist.",
        "Check MS_MAILBOX (the full email address).",
    ),
    "MailboxNotEnabledForRESTAPI": (
        "ms.mailbox_unknown",
        "The mailbox is not available through Graph.",
        "Use a licensed Exchange Online mailbox.",
    ),
    "ResourceNotFound": (
        "ms.not_found",
        "The item does not exist or is not visible to the app.",
        "Check the id or path.",
    ),
    "itemNotFound": (
        "ms.not_found",
        "The item does not exist or is not visible to the app.",
        "Check the id or path.",
    ),
    "accessDenied": (
        "ms.site_not_granted",
        "The app has no access to this site or folder.",
        "Grant the app access to the site (Sites.Selected) or use a site it may read.",
    ),
    "ErrorMailboxConcurrency": (
        "ms.throttled",
        "Microsoft is limiting requests to this mailbox.",
        "Wait a moment; awa slows down by itself.",
    ),
    "activityLimitReached": (
        "ms.throttled",
        "Microsoft is limiting requests.",
        "Wait a moment; awa slows down by itself.",
    ),
    "invalid_grant": (
        "ms.sign_in_expired",
        "The saved Microsoft sign-in is no longer valid.",
        f"Sign in again: {SIGN_IN_COMMAND}",
    ),
    "interaction_required": (
        "ms.interaction_required",
        "Microsoft needs this account to sign in again in a browser.",
        f"Sign in again: {SIGN_IN_COMMAND}",
    ),
    "authorization_declined": (
        "ms.sign_in_declined",
        "The sign-in was declined in the browser.",
        f"Start again and approve it: {SIGN_IN_COMMAND}",
    ),
    "expired_token": (
        "ms.device_code_expired",
        "The sign-in code has expired (it works for 15 minutes).",
        f"Start the sign-in again: {SIGN_IN_COMMAND}",
    ),
    "bad_verification_code": (
        "ms.device_code_unknown",
        "Microsoft does not know this sign-in code.",
        f"Start the sign-in again: {SIGN_IN_COMMAND}",
    ),
}

# Teams 403s are all `Forbidden`: they are told apart by their wording.
_TEAMS = (
    (
        "import purposes",
        (
            "ms.teams_app_only",
            "Microsoft does not let an app post Teams messages as itself (only to import history).",
            "Post through a Workflows webhook (post_card), or sign in as a Teams user: "
            + SIGN_IN_COMMAND,
        ),
    ),
    (
        "protected api",
        (
            "ms.teams_protected_api",
            "Reading Teams messages as an app may need Microsoft's approval first (a protected API).",
            "Read as a signed-in user instead (that needs no approval from Microsoft).",
        ),
    ),
    (
        "not a member",
        (
            "ms.not_a_member",
            "The signed-in Teams user is not a member of this team or channel.",
            "Add that user to the team or channel in Teams.",
        ),
    ),
)

_WEBHOOK = {
    400: (
        "ms.card_rejected",
        "The Workflows webhook did not accept the card.",
        "Check that the flow is turned on and starts with 'When a Teams webhook request is "
        "received'.",
    ),
    401: (
        "ms.webhook_rejected",
        "The Workflows webhook refused the request (wrong signature or the flow is off).",
        "Copy the webhook URL again from the flow; the sig= part must be complete.",
    ),
    403: (
        "ms.webhook_rejected",
        "The Workflows webhook refused the request (the flow limits who may call it).",
        "Set the trigger's authentication to 'Anyone' (awa sends no sign-in with the card).",
    ),
    404: (
        "ms.webhook_gone",
        "The Workflows webhook does not exist any more.",
        "Create the webhook again in Teams (channel menu, Workflows) and update TEAMS_WEBHOOK_URL.",
    ),
    413: (
        "ms.card_too_large",
        "The card is larger than Teams allows (28 KB).",
        "Send less text or fewer facts.",
    ),
}

_TEAMS_BY_STATUS = {
    401: (
        "ms.sign_in_rejected",
        "Microsoft rejected the signed-in user's access token.",
        f"Sign in again: {SIGN_IN_COMMAND}",
    ),
    404: (
        "ms.not_found",
        "The team, channel or message does not exist, or the signed-in user cannot see it.",
        "Check the ids, and that the user is a member of the team.",
    ),
}

_BY_STATUS = {
    401: (
        "ms.token_invalid",
        "Microsoft rejected the sign-in.",
        "Check AZURE_TENANT_ID, AZURE_CLIENT_ID and the secret.",
    ),
    404: (
        "ms.not_found",
        "The item does not exist or is not visible to the app.",
        "Check the id or path.",
    ),
    429: (
        "ms.throttled",
        "Microsoft is limiting requests.",
        "Wait a moment; awa slows down by itself.",
    ),
}


def explain(status: int, body: Any, *, service: str = "graph", operation: str = "") -> Advice:
    """The advice for a failed request: HTTP `status`, the response `body` (text, bytes or JSON)."""

    document = _document(body)
    message = _text(document)
    sign_in = re.search(r"AADSTS(\d+)", message)
    if sign_in and sign_in.group(1) == "53003" and operation == "device_code":
        return Advice(
            "ms.device_code_blocked",
            "Your tenant blocks the device code sign-in (Conditional Access).",
            "Ask an administrator to allow it for the Teams user (Conditional Access, "
            "Authentication flows), or post through a Workflows webhook instead.",
        )
    if sign_in and sign_in.group(1) in _SIGN_IN:
        return Advice(*_SIGN_IN[sign_in.group(1)])
    if service == "teams_webhook" and status in _WEBHOOK:
        return Advice(*_WEBHOOK[status])
    if service == "teams" and status in _TEAMS_BY_STATUS:
        return Advice(*_TEAMS_BY_STATUS[status])
    if status == 403:
        lowered = message.lower()
        for marker, found in _TEAMS:
            if marker in lowered:
                return Advice(*found)
        scopes = re.search(r"requires one of '([^']+)'", message)
        if scopes:
            return Advice(
                "ms.scope_missing",
                f"The sign-in lacks the permission {scopes.group(1)}.",
                "Sign in again and approve it (an administrator may have to consent): "
                + SIGN_IN_COMMAND,
            )
        if service == "teams":
            return Advice(
                "ms.forbidden",
                "The signed-in Teams user may not do this.",
                "Check that the user is a member of the team and channel, then sign in again: "
                + SIGN_IN_COMMAND,
            )
    code = _code(document)
    if code in _GRAPH:
        return Advice(*_GRAPH[code])
    if status in _BY_STATUS:
        return Advice(*_BY_STATUS[status])
    if status == 403:
        return Advice(
            "ms.forbidden",
            f"The app may not do this ({service} {operation}).".replace(" )", ")"),
            "Check the permissions of the app registration and the admin consent.",
        )
    if status >= 500:
        return Advice(
            "ms.service_unavailable",
            "Microsoft had a problem answering.",
            "Try again later; a write may or may not have happened, check before repeating it.",
        )
    return Advice(
        "ms.error", message or f"Microsoft answered with status {status}.", "See the details above."
    )


def _document(body: Any) -> Mapping[str, Any]:
    if isinstance(body, Mapping):
        return body
    try:
        value = json.loads(
            body if isinstance(body, str) else bytes(body).decode("utf-8", "replace")
        )
    except (TypeError, ValueError):
        return {"error_description": str(body)[:500]} if body else {}
    return value if isinstance(value, Mapping) else {}


def _code(document: Mapping[str, Any]) -> str:
    error = document.get("error")
    return str(error.get("code", "")) if isinstance(error, Mapping) else str(error or "")


def _text(document: Mapping[str, Any]) -> str:
    error = document.get("error")
    parts = [
        document.get("error_description"),
        error.get("message") if isinstance(error, Mapping) else None,
    ]
    return " ".join(str(part) for part in parts if part)[:500]
