"""Plain-language advice for Microsoft errors, and nothing built on a retired Microsoft interface."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from awa.connectors._microsoft_errors import SIGN_IN_COMMAND, explain


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (
            400,
            {
                "error": "invalid_client",
                "error_description": "AADSTS7000215: Invalid client secret",
            },
            "ms.secret_wrong",
        ),
        (
            400,
            {"error_description": "AADSTS7000222: The provided client secret keys are expired."},
            "ms.secret_expired",
        ),
        (400, {"error_description": "AADSTS700016: Application not found"}, "ms.app_unknown"),
        (400, {"error_description": "AADSTS90002: Tenant not found"}, "ms.tenant_unknown"),
        (400, {"error_description": "AADSTS65001: consent required"}, "ms.consent_missing"),
        (
            401,
            {"error": {"code": "InvalidAuthenticationToken", "message": "expired"}},
            "ms.token_invalid",
        ),
        (403, {"error": {"code": "Authorization_RequestDenied"}}, "ms.consent_missing"),
        (403, {"error": {"code": "ErrorAccessDenied"}}, "ms.rbac_scope"),
        (403, {"error": {"code": "accessDenied"}}, "ms.site_not_granted"),
        (404, {"error": {"code": "ErrorInvalidUser"}}, "ms.mailbox_unknown"),
        (404, {"error": {"code": "itemNotFound"}}, "ms.not_found"),
        (429, {}, "ms.throttled"),
        (503, "", "ms.service_unavailable"),
        (418, "teapot", "ms.error"),
    ],
)
def test_each_error_has_a_stable_code_and_a_fix(status: int, body: object, code: str) -> None:
    advice = explain(status, body)
    assert advice.code == code and advice.text and advice.fix


def test_the_body_may_be_json_text_or_bytes() -> None:
    document = {"error": {"code": "ErrorAccessDenied"}}
    assert explain(403, json.dumps(document)).code == "ms.rbac_scope"
    assert explain(403, json.dumps(document).encode()).code == "ms.rbac_scope"


def test_an_unknown_forbidden_names_the_operation() -> None:
    assert "graph send" in explain(403, {}, operation="send").text


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (
            400,
            {"error_description": "AADSTS7000218: client_assertion or client_secret"},
            "ms.public_client_off",
        ),
        (
            400,
            {"error": "invalid_grant", "error_description": "AADSTS70008: inactivity"},
            "ms.sign_in_expired",
        ),
        (
            400,
            {"error": "invalid_grant", "error_description": "AADSTS70043: frequency"},
            "ms.sign_in_expired",
        ),
        (
            400,
            {"error": "invalid_grant", "error_description": "AADSTS50173: revoked"},
            "ms.sign_in_revoked",
        ),
        (
            400,
            {"error": "interaction_required", "error_description": "AADSTS50076: MFA"},
            "ms.mfa_required",
        ),
        (
            400,
            {"error": "expired_token", "error_description": "AADSTS70019: expired"},
            "ms.device_code_expired",
        ),
        (400, {"error": "invalid_grant"}, "ms.sign_in_expired"),
        (400, {"error": "interaction_required"}, "ms.interaction_required"),
        (400, {"error": "authorization_declined"}, "ms.sign_in_declined"),
        (400, {"error": "expired_token"}, "ms.device_code_expired"),
        (400, {"error": "bad_verification_code"}, "ms.device_code_unknown"),
    ],
)
def test_sign_in_errors_have_their_own_codes_and_name_the_next_step(
    status: int, body: object, code: str
) -> None:
    advice = explain(status, body, operation="refresh")
    assert advice.code == code and advice.text and advice.fix
    if code in {"ms.sign_in_expired", "ms.sign_in_revoked", "ms.mfa_required"}:
        assert SIGN_IN_COMMAND in advice.fix


def test_a_conditional_access_block_of_device_code_says_so() -> None:
    body = {"error": "interaction_required", "error_description": "AADSTS53003: blocked"}
    assert explain(400, body, operation="device_code").code == "ms.device_code_blocked"
    assert explain(400, body).code == "ms.blocked_by_policy"  # elsewhere the general advice stays


@pytest.mark.parametrize(
    ("message", "code"),
    [
        (
            "Message POST is allowed in application-only context only for import purposes.",
            "ms.teams_app_only",
        ),
        ("This API is a protected API: the app has not been approved.", "ms.teams_protected_api"),
        ("The signed-in user is not a member of the team or channel.", "ms.not_a_member"),
        (
            "Missing scope permissions on the request. API requires one of "
            "'ChannelMessage.Send'. Scopes on the request 'User.Read'",
            "ms.scope_missing",
        ),
    ],
)
def test_teams_forbidden_answers_are_told_apart_by_their_wording(message: str, code: str) -> None:
    advice = explain(403, {"error": {"code": "Forbidden", "message": message}}, service="teams")
    assert advice.code == code
    assert "ChannelMessage.Send" in advice.text or code != "ms.scope_missing"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "ms.card_rejected"),
        (401, "ms.webhook_rejected"),
        (403, "ms.webhook_rejected"),
        (404, "ms.webhook_gone"),
        (413, "ms.card_too_large"),
    ],
)
def test_a_workflows_webhook_answer_is_explained_by_its_status(status: int, code: str) -> None:
    assert explain(status, {}, service="teams_webhook").code == code
    assert explain(status, {}, service="graph").code != code  # only for the webhook


def test_a_user_whose_token_is_rejected_is_asked_to_sign_in_again() -> None:
    advice = explain(401, {"error": {"message": "x"}}, service="teams")
    assert advice.code == "ms.sign_in_rejected" and SIGN_IN_COMMAND in advice.fix
    assert explain(401, {}).code == "ms.token_invalid"  # the app-only wording is unchanged


def test_teams_user_answers_name_the_user_not_the_app() -> None:
    gone = explain(404, {}, service="teams")
    assert gone.code == "ms.not_found" and "signed-in user" in gone.text
    denied = explain(403, {"error": {"message": "Nope."}}, service="teams")
    assert denied.code == "ms.forbidden" and "Teams user" in denied.text
    assert "app may not" in explain(403, {}, service="graph").text  # other services unchanged


ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src", "docs", "examples", "scripts")
SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".cfg",
    ".ini",
    ".json",
    ".sh",
    ".ps1",
    ".cmd",
    ".command",
    ".example",
    ".html",
}
SKIPPED_DIRS = {"__pycache__", ".venv", "node_modules", "site", ".git"}
CONTEXT_LINES = 3  # how far the explanation may be from the mention

RETIRED: dict[str, re.Pattern[str]] = {
    "Office 365 connector webhook (replaced by Workflows webhooks)": re.compile(
        r"webhook\.office(?:365)?\.com|outlook\.office(?:365)?\.com/webhook"
        r"|\b(?-i:(?:Office|Microsoft) 365 Connectors?)\b|incoming webhook connector",
        re.IGNORECASE,
    ),
    "Azure AD Graph (replaced by Microsoft Graph)": re.compile(
        r"graph\.windows\.net|\bAzure AD Graph\b", re.IGNORECASE
    ),
    "ADAL (replaced by MSAL or a direct Entra v2.0 flow)": re.compile(
        r"\badal\b|\badal-python\b|\bAuthenticationContext\b"
        r"|Microsoft\.IdentityModel\.Clients\.ActiveDirectory",
        re.IGNORECASE,
    ),
    "login.windows.net, sts.windows.net or a v1 token endpoint (use .../oauth2/v2.0/...)": re.compile(
        r"login\.windows\.net|sts\.windows\.net"
        r"|(?:microsoftonline\.[a-z.]+|AZURE_AUTHORITY_HOST\}?|authority[a-z_]*)[^\s\"']*/oauth2/(?:token|authorize)\b",
        re.IGNORECASE,
    ),
    "legacy Outlook REST API (use Microsoft Graph mail)": re.compile(
        r"outlook\.office(?:365)?\.com/api\b|/api/v2\.0/(?:me|users)\b", re.IGNORECASE
    ),
    "Exchange Web Services (use Microsoft Graph)": re.compile(
        r"/EWS/Exchange\.asmx|\bExchange Web Services\b", re.IGNORECASE
    ),
    "SMTP/IMAP hosts of Exchange Online (use Microsoft Graph mail)": re.compile(
        r"smtp\.office365\.com|outlook\.office365\.com|imap-mail\.outlook\.com"
        r"|smtp-mail\.outlook\.com",
        re.IGNORECASE,
    ),
}
EXPLAINED = re.compile(
    r"retired|retire\b|retirement|switched off|turned off|no longer|deprecated|discontinued"
    r"|end of life|does not work|do not work|stopped working|removed",
    re.IGNORECASE,
)


def problems(text: str) -> list[tuple[int, str, str]]:
    """The mentions of retired interfaces in `text` that nothing nearby explains."""

    lines = text.splitlines()
    found: list[tuple[int, str, str]] = []
    for number, line in enumerate(lines):
        for name, pattern in RETIRED.items():
            if not pattern.search(line):
                continue
            around = "\n".join(lines[max(0, number - CONTEXT_LINES) : number + CONTEXT_LINES + 1])
            if not EXPLAINED.search(around):
                found.append((number + 1, name, line.strip()[:120]))
    return found


def files() -> Iterator[Path]:
    for name in SCANNED:
        folder = ROOT / name
        if folder.is_dir():
            for path in sorted(folder.rglob("*")):
                parts = set(path.relative_to(ROOT).parts)
                if path.is_file() and path.suffix in SUFFIXES and not parts & SKIPPED_DIRS:
                    yield path
    yield from sorted(ROOT.glob("*.md"))
    if (ROOT / ".env.example").is_file():
        yield ROOT / ".env.example"


def test_the_scan_really_covers_the_product() -> None:
    scanned = list(files())
    assert len(scanned) > 300, "the scan found too few files: is the layout still as expected?"
    names = {p.relative_to(ROOT).as_posix() for p in scanned}
    assert "src/awa/connectors/_saas_microsoft.py" in names
    assert "src/awa/connectors/_saas_microsoft_teams.py" in names
    assert "README.md" in names and any(n.startswith("docs/") for n in names)
    assert any(n.startswith("examples/") for n in names)
    assert any(n.startswith("scripts/") for n in names)


@pytest.mark.slow
def test_no_retired_microsoft_interface_is_used_or_recommended() -> None:
    report = [
        f"{path.relative_to(ROOT).as_posix()}:{number}: {name}: {line}"
        for path in files()
        for number, name, line in problems(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not report, "retired Microsoft interfaces without an explanation:\n" + "\n".join(report)


@pytest.mark.parametrize(
    "line",
    [
        "TEAMS_WEBHOOK_URL=https://contoso.webhook.office.com/webhookb2/a@b/IncomingWebhook/c/d",
        "post to https://outlook.office.com/webhook/abc/IncomingWebhook/x/y",
        "Use an Office 365 Connector in the channel settings.",
        "GET https://graph.windows.net/myorganization/users",
        "import adal; context = adal.AuthenticationContext(authority)",
        "POST https://login.windows.net/{tenant}/oauth2/token",
        "token_url = 'https://login.microsoftonline.com/{tenant}/oauth2/token'",
        '"{AZURE_AUTHORITY_HOST}/{AZURE_TENANT_ID}/oauth2/authorize"',
        "iss = https://sts.windows.net/{tenant}/",
        "GET https://outlook.office.com/api/v2.0/me/messages",
        "https://outlook.office365.com/EWS/Exchange.asmx",
        "SMTP_HOST=smtp.office365.com",
        "IMAP_HOST=outlook.office365.com",
    ],
)
def test_the_scanner_catches_each_retired_interface(line: str) -> None:
    assert problems(line), line


@pytest.mark.parametrize(
    "line",
    [
        "token_url = 'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token'",
        '"{AZURE_AUTHORITY_HOST}/{AZURE_TENANT_ID}/oauth2/v2.0/token"',
        "https://graph.microsoft.com/v1.0/me",
        "https://login.microsoftonline.us/common/oauth2/deviceauth",
        "TEAMS_WEBHOOK_URL=https://prod-00.westeurope.logic.azure.com:443/workflows/abc?sig=x",
        "https://abc.environment.api.powerplatform.com:443/powerautomate/automations/direct/",
        "https://outlook.office.com/owa/?ItemID=1",
        "SALESFORCE_URL/services/oauth2/token",
        "Use Microsoft Graph for mail.",
        "The Microsoft 365 connectors read mail and calendars.",
    ],
)
def test_the_scanner_leaves_current_interfaces_alone(line: str) -> None:
    assert problems(line) == [], line


def test_a_mention_is_fine_where_the_text_says_why_it_is_retired() -> None:
    explained = (
        "Microsoft switched the Office 365 Connectors off in May 2026.\n"
        "\n"
        "Old webhook addresses (webhook.office.com) no longer work; create a Workflows webhook.\n"
    )
    assert problems(explained) == []
    far = "webhook.office.com\n" + "\n" * 10 + "this was retired\n"
    assert problems(far), "an explanation ten lines away does not count"
    assert problems("The address was fine.\nwebhook.office.com\n")
