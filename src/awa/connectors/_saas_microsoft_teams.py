"""Catalog entry for Microsoft Teams: cards through a Workflows webhook, messages as a user.

The webhook URL is a credential and is redacted like one; user messages go to ONE channel.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from awa._internal.upstream.tools import BaseTool, ToolException
from awa.connectors._engine import (
    ConnectorContext,
    RetryAfter,
    UncertainOutcome,
    build_connector_tool,
    call_operation,
)
from awa.connectors._microsoft_auth import UserTokenProvider, decode_claims
from awa.connectors._microsoft_errors import SIGN_IN_COMMAND, MicrosoftAuthError, explain
from awa.connectors._saas_microsoft import _AUTHORITY, _GRAPH_URL, microsoft_user_agent
from awa.connectors._spec import (
    ConnectorSpec,
    EnvVar,
    Operation,
    Pagination,
    Param,
    TokenAuth,
    body_param,
    path_param,
)

ADAPTIVE = "application/vnd.microsoft.card.adaptive"
CARD_VERSION = "1.2"
"""The Adaptive Card version posted; only elements and actions of this version are used."""
MESSAGE_LIMIT = 28_000
"""Teams' limit for one message, in bytes of the JSON sent (28 KB)."""
SPACING = 1.1
"""Seconds between two posts to the same webhook, user or channel (Teams: 1 per second)."""
_MAX_QUEUE = 30.0
_RETIRED_HOSTS = ("outlook.office.com", "outlook.office365.com")
_SECRET_QUERY = 8  # query values at least this long are treated as part of the secret

_clock = time.monotonic
_sleep = time.sleep


class _Spacing:
    """Queues posts so that two with the same key are at least `gap` seconds apart."""

    def __init__(self, gap: float) -> None:
        self.gap = gap
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, key: str) -> None:
        """Block until `key` may post again; refuse when its queue is longer than half a minute."""

        with self._lock:
            now = _clock()
            slot = max(now, self._next.get(key, 0.0))
            if slot - now > _MAX_QUEUE:
                raise ToolException(
                    "Too many Teams messages are queued for this destination (Teams allows one per "
                    "second and asks that people read what is sent). Send fewer, longer messages."
                )
            self._next[key] = slot + self.gap
            if len(self._next) > 1000:
                self._next = {k: v for k, v in self._next.items() if v > now}
        if slot > now:
            _sleep(slot - now)


_SPACE = _Spacing(SPACING)


def _key(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:20]


_TEXT = Param("text", "The message text.", required=True, location="local")


def _card(
    body: list[dict[str, Any]], actions: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The Workflows envelope around one Adaptive Card (version 1.2 elements only)."""

    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": CARD_VERSION,
        "body": body,
    }
    if actions:
        card["actions"] = actions
    envelope = {
        "type": "message",
        "attachments": [{"contentType": ADAPTIVE, "contentUrl": None, "content": card}],
    }
    if len(json.dumps(envelope, ensure_ascii=False).encode()) > MESSAGE_LIMIT:
        raise ToolException("The card is larger than Teams allows (28 KB): send less text.")
    return envelope


def _text(value: Any, name: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ToolException(f"{name} must not be empty")
    if len(text) > limit:
        raise ToolException(f"{name} is longer than {limit} characters")
    return text


def _link(arguments: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    url, title = arguments.get("link_url"), arguments.get("link_title")
    if not url and not title:
        return None
    if not url or not title:
        raise ToolException("link_url and link_title go together")
    if urllib.parse.urlsplit(str(url)).scheme != "https":
        raise ToolException("link_url must be an https:// address")
    return [{"type": "Action.OpenUrl", "title": _text(title, "link_title", 80), "url": str(url)}]


def _post_card_body(arguments: Mapping[str, Any]) -> dict[str, Any]:
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": _text(arguments.get("title"), "title", 200),
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
            **({"color": arguments["style"]} if arguments.get("style") else {}),
        },
        {"type": "TextBlock", "text": _text(arguments.get("text"), "text", 8000), "wrap": True},
    ]
    facts = arguments.get("facts") or {}
    if len(facts) > 20:
        raise ToolException("facts holds more than 20 entries")
    if facts:
        body.append(
            {
                "type": "FactSet",
                "facts": [{"title": str(k)[:80], "value": str(v)[:500]} for k, v in facts.items()],
            }
        )
    return _card(body, _link(arguments))


def _notify_body(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return _card(
        [{"type": "TextBlock", "text": _text(arguments.get("text"), "text", 8000), "wrap": True}]
    )


def _accepted(result: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "accepted": True,
        "note": "Workflows accepted the card; Teams shows it when the flow has run. awa cannot "
        "see a failure inside the flow.",
    }


_WEBHOOK_OPERATIONS = (
    Operation(
        "post_card",
        "Post a card (title, text, optional facts and one link button) to the Teams channel of "
        "the Workflows webhook. People read these: do not use Teams as a log.",
        method="POST",
        params=(
            Param("title", "Card title.", required=True, location="local"),
            _TEXT,
            Param(
                "facts",
                "Name and value pairs shown as a small table, for example {'Order': 'A-17'}.",
                type="object",
                location="local",
            ),
            Param("link_title", "Text of the link button.", location="local"),
            Param("link_url", "https:// address the button opens.", location="local"),
            Param(
                "style",
                "Title color.",
                enum=("good", "warning", "attention", "accent"),
                location="local",
            ),
        ),
        body_builder=_post_card_body,
        transform=_accepted,
        write=True,
    ),
    Operation(
        "notify_webhook",
        "Send a short plain-text notification to the Teams channel of the Workflows webhook.",
        method="POST",
        params=(_TEXT,),
        body_builder=_notify_body,
        transform=_accepted,
        write=True,
    ),
)


def _webhook_url(ctx: ConnectorContext) -> str:
    """`TEAMS_WEBHOOK_URL`, refused when it is an address Microsoft switched off."""

    url = ctx.require("TEAMS_WEBHOOK_URL").strip()
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.endswith(".webhook.office.com") or (
        host in _RETIRED_HOSTS and parts.path.lower().startswith("/webhook")
    ):
        raise ToolException(
            "TEAMS_WEBHOOK_URL is an old Office 365 connector address, which Microsoft switched "
            "off in May 2026. Create a Workflows webhook instead: in Teams open the channel menu "
            "(...), Workflows, the template 'Send webhook alerts to a channel', and copy its link."
        )
    return url


def _remember(hook: ConnectorContext, url: str) -> None:
    """Redact the parts of the webhook URL that carry the secret, wherever they show up."""

    parts = urllib.parse.urlsplit(url)
    pieces = [
        value
        for _, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if len(value) >= _SECRET_QUERY
    ]
    pieces += [s for s in parts.path.split("/") if len(s) >= _SECRET_QUERY and s.count("-") >= 3]
    hook._ephemeral[:] = [url, *pieces, *([parts.query] if parts.query else [])]


def _webhook_tool(ctx: ConnectorContext, operation: Operation) -> BaseTool:
    """A webhook operation on a context without credentials: no token reaches the webhook host."""

    hook = ctx.derive(replace(ctx.spec, base_url="{TEAMS_WEBHOOK_URL}", auth=()))
    hook.config = {k: v for k, v in ctx.config.items() if k not in ("base_url", "base_url_env")}

    def prepare(arguments: Mapping[str, Any]) -> None:
        url = _webhook_url(hook)
        _remember(hook, url)
        _SPACE.wait(_key("webhook", url))

    return build_connector_tool(
        hook, replace(operation, check=prepare, fallback=_translate(hook, "teams_webhook"))
    )


_HTTP = re.compile(r"\(HTTP (\d{3})\)(.*)", re.DOTALL)


def _translate(ctx: ConnectorContext, service: str) -> Callable[[Exception], Any]:
    """A fallback that rewords a failed call with the advice of `explain`."""

    def fallback(error: Exception) -> Any:
        if isinstance(error, RetryAfter | UncertainOutcome) or not isinstance(error, ToolException):
            raise error
        found = _HTTP.search(str(error))
        if found is None:
            raise error
        detail = found.group(2).split("; ")[0].lstrip(": ").strip()
        advice = explain(int(found.group(1)), {"error": {"message": detail}}, service=service)
        if advice.code == "ms.error":  # nothing better to say than what Microsoft said
            raise error
        raise ToolException(ctx.redact(f"{advice.text} {advice.fix}")) from None

    return fallback


def _spaced(key: Callable[[Mapping[str, Any]], str]) -> Callable[[Mapping[str, Any]], None]:
    return lambda arguments: _SPACE.wait(key(arguments))


def _check_message(arguments: Mapping[str, Any]) -> None:
    text = _text(arguments.get("text"), "text", MESSAGE_LIMIT)
    sent = {"body": {"contentType": "html", "content": text}}
    if len(json.dumps(sent, ensure_ascii=False).encode()) > MESSAGE_LIMIT:
        raise ToolException(f"text is larger than Teams' limit of {MESSAGE_LIMIT} bytes")


def _who(person: Mapping[str, Any]) -> dict[str, Any]:
    for kind in ("user", "application"):
        found = person.get(kind)
        if isinstance(found, Mapping):
            return {"name": found.get("displayName"), "kind": kind}
    return {"name": None, "kind": None}


def _shape(message: Mapping[str, Any]) -> dict[str, Any]:
    body = message.get("body") or {}
    shaped = {
        "id": message.get("id"),
        "reply_to_id": message.get("replyToId"),
        "created": message.get("createdDateTime"),
        "modified": message.get("lastModifiedDateTime"),
        "from": _who(message.get("from") or {}),
        "type": message.get("messageType"),
        "format": body.get("contentType"),
        "text": body.get("content"),
        "web_url": message.get("webUrl"),
    }
    if isinstance(message.get("replies"), list):
        shaped["replies"] = [_shape(r) for r in message["replies"]]
    return shaped


def _listed(result: Any, arguments: Mapping[str, Any]) -> Any:
    return {**result, "items": [_shape(m) for m in result["items"]]}


TEAM_ID = EnvVar(
    "TEAMS_TEAM_ID",
    "Id of the ONE team the signed-in user operations work in (a GUID: Teams, the team's menu, "
    "Get link to team, the groupId= part).",
    secret=False,
    required=False,
)
CHANNEL_ID = EnvVar(
    "TEAMS_CHANNEL_ID",
    "Id of the ONE channel of that team (19:...@thread.tacv2: the channel's menu, Get link to "
    "channel, the part after /channel/, decoded).",
    secret=False,
    required=False,
)
TEAM_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_PINNED = "/teams/<TEAMS_TEAM_ID>/channels/<TEAMS_CHANNEL_ID>"
"""Where the operations' paths name the pinned channel; `_graph_tool` fills it from .env."""
CHANNEL_PATTERN = re.compile(r"^19:[A-Za-z0-9_.=+-]{1,200}@thread\.(tacv2|skype)$")
_FORMAT = Param(
    "format",
    "text (default) or html.",
    location="body",
    key="body.contentType",
    enum=("text", "html"),
    default="text",
)
_CONTENT = body_param("text", "The message.", "body.content", required=True)
_MENTION = Param(
    "mention",
    "Roles whose pinned people are @mentioned, as role:<name> (role:approvers); only roles of "
    "the recipients option when it is set.",
    type="array",
    items="string",
    location="local",
)


def _mentions(ctx: ConnectorContext) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """The body builder that @mentions the people of `mention` roles, resolved at send time."""

    def build(arguments: Mapping[str, Any]) -> dict[str, Any]:
        values = list(arguments.get("mention") or ())
        if not values:
            return {}
        from awa.people import PeopleError, role_name
        from awa.people._connect import connector_recipients

        try:
            allowed = allowed_mentions(ctx)
            resolver = connector_recipients(ctx)
            people: dict[str, Any] = {}
            for value in values:
                role = role_name(str(value))
                if role is None:
                    raise PeopleError(f"mention takes roles such as role:approvers, not {value!r}.")
                if allowed is not None and role not in allowed:
                    raise PeopleError(
                        f"not posted: the role {role!r} is not among the allowed recipients "
                        f"({', '.join('role:' + r for r in allowed)})."
                    )
                for person in resolver.people(role):
                    if not person.object_id:
                        raise PeopleError(
                            f"{person.name} is a local person; Teams can only @mention people "
                            "from Entra ID."
                        )
                    people.setdefault(person.object_id, person)
        except PeopleError as exc:
            raise ToolException(str(exc)) from None
        text = str(arguments.get("text") or "")
        plain = arguments.get("format") != "html"
        shown = html.escape(text).replace("\n", "<br>") if plain else text
        tags = " ".join(
            f'<at id="{n}">{html.escape(p.name)}</at>' for n, p in enumerate(people.values())
        )
        return {
            "body": {"contentType": "html", "content": f"{shown}<p>{tags}</p>"},
            "mentions": [
                {
                    "id": n,
                    "mentionText": p.name,
                    "mentioned": {
                        "user": {
                            "id": p.object_id,
                            "displayName": p.name,
                            "userIdentityType": "aadUser",
                        }
                    },
                }
                for n, p in enumerate(people.values())
            ],
        }

    return build


def allowed_mentions(ctx: ConnectorContext) -> tuple[str, ...] | None:
    """The roles of the `recipients` option, or `None` when it is unset."""

    from awa.people import allowed_roles

    return allowed_roles(ctx.option("recipients"))


_GRAPH_OPERATIONS = (
    Operation(
        "list_channel_messages",
        "Read the newest messages of the Teams channel of this connection as the signed-in user "
        "(needs ChannelMessage.Read.All, which an administrator consents to). Replies come with "
        "include_replies. Microsoft allows one read per second per channel.",
        path=_PINNED + "/messages",
        params=(
            Param(
                "include_replies",
                "Also return the replies of each message.",
                type="boolean",
                key="$expand",
                convert=lambda value: "replies" if value else None,
            ),
        ),
        items="value",
        pages=Pagination("next_url", next="@odata.nextLink", size_param="$top", size_max=50),
        default_limit=20,
        max_limit=50,
        transform=_listed,
        needs="user",
    ),
    Operation(
        "post_message",
        "Post a message to the Teams channel of this connection as the signed-in user, who must "
        "be a member (needs ChannelMessage.Send). People read these: do not use Teams as a log.",
        method="POST",
        path=_PINNED + "/messages",
        params=(_CONTENT, _FORMAT, _MENTION),
        write=True,
        needs="user",
    ),
    Operation(
        "reply_message",
        "Reply in the thread of a message of the Teams channel of this connection as the "
        "signed-in user (needs ChannelMessage.Send).",
        method="POST",
        path=_PINNED + "/messages/{message_id}/replies",
        params=(
            path_param("message_id", "Id of the message to reply to."),
            _CONTENT,
            _FORMAT,
            _MENTION,
        ),
        write=True,
        needs="user",
    ),
)


def pinned_channel(ctx: ConnectorContext) -> tuple[str, str]:
    """`TEAMS_TEAM_ID` and `TEAMS_CHANNEL_ID`: the one channel the user operations work in."""

    team, channel = ctx.value(TEAM_ID.name), ctx.value(CHANNEL_ID.name)
    if not team or not channel:
        raise ToolException(
            "TEAMS_TEAM_ID and TEAMS_CHANNEL_ID are not set: set the one team and channel the "
            "agents may use in .env (Teams: the channel's menu, Get link to channel)."
        )
    team, channel = team.strip(), urllib.parse.unquote(channel.strip())
    if not TEAM_PATTERN.fullmatch(team):
        raise ToolException("TEAMS_TEAM_ID must be the team's id, a GUID (the groupId= part).")
    if not CHANNEL_PATTERN.fullmatch(channel):
        raise ToolException("TEAMS_CHANNEL_ID must look like 19:...@thread.tacv2.")
    return team, channel


def _graph_tool(ctx: ConnectorContext, operation: Operation) -> BaseTool:
    def where() -> str:
        team, channel = pinned_channel(ctx)
        return f"/teams/{urllib.parse.quote(team, safe='')}/channels/{urllib.parse.quote(channel, safe='')}"

    if operation.write:
        # Teams: one post per second per user, whatever the channel.
        user = _key("user", ctx.value("AZURE_TENANT_ID") or "", ctx.value("AZURE_CLIENT_ID") or "")
        checks: list[Callable[[Mapping[str, Any]], None]] = [
            _check_message,
            _spaced(lambda arguments: user),
        ]
    else:
        checks = [_spaced(lambda arguments: _key("read", where()))]

    def check(arguments: Mapping[str, Any]) -> None:
        where()  # an unset or malformed channel is refused before anything else
        for step in checks:
            step(arguments)

    try:
        prefix = where()
    except ToolException:
        prefix = _PINNED  # never sent: `check` refuses the call first
    builder = _mentions(ctx) if any(p.name == "mention" for p in operation.params) else None
    pinned = replace(
        operation,
        path=operation.path.replace(_PINNED, prefix),
        check=check,
        fallback=_translate(ctx, "teams"),
        body_builder=builder,
        html_body=_html_text if any(p.name == "format" for p in operation.params) else None,
    )
    return build_connector_tool(ctx, pinned)


def _html_text(arguments: Mapping[str, Any]) -> str | None:
    """`text` holds HTML when the format is html (the outbound filter cleans it)."""

    return "text" if arguments.get("format") == "html" else None


def _configure(spec: ConnectorSpec, instance: str, options: Mapping[str, Any]) -> ConnectorSpec:
    """Refuse a malformed `recipients` or `people_file` option up front."""

    from awa.people._connect import check_people_options

    check_people_options(instance, options)
    return spec


def _tools(ctx: ConnectorContext) -> list[BaseTool]:
    return [
        _graph_tool(ctx, operation) if operation.needs == "user" else _webhook_tool(ctx, operation)
        for operation in ctx.operations()
    ]


def _health(ctx: ConnectorContext) -> str:
    """One line per way in that is set up; the webhook is checked without sending a card."""

    lines: list[str] = []
    if ctx.value("TEAMS_WEBHOOK_URL"):
        parts = urllib.parse.urlsplit(_webhook_url(ctx))
        sig = "sig=" in parts.query
        lines.append(
            f"webhook {parts.hostname}: address accepted"
            + ("" if sig else " (no sig= part: only flows set to 'Anyone' can be called)")
            + "; no test card is sent"
        )
    if ctx.value("AZURE_TENANT_ID") and ctx.value("AZURE_CLIENT_ID"):
        me = Operation(
            "whoami",
            "Who is signed in.",
            path="/me",
            query={"$select": "userPrincipalName"},
            needs="user",
            fallback=_translate(ctx, "teams"),
        )
        try:
            who = call_operation(ctx, me, {})
            granted = (
                ", ".join(decode_claims(UserTokenProvider(ctx).token(ctx)).scopes) or "unknown"
            )
            lines.append(
                f"signed in as {who.get('userPrincipalName', '?')} (permissions: {granted})"
            )
        except MicrosoftAuthError as error:
            # The app variables are shared with `outlook` and `sharepoint`: without a sign-in
            # ever made, they say nothing about Teams. A sign-in that existed must work.
            if error.code != "ms.not_signed_in" or not lines:
                raise
            lines.append(
                "no user is signed in (only post_message, reply_message and "
                "list_channel_messages need one)"
            )
    if not lines:
        raise ToolException(
            "nothing is set up: set TEAMS_WEBHOOK_URL (a Workflows webhook), or sign in with "
            + SIGN_IN_COMMAND
        )
    return "; ".join(lines)


TEAMS = ConnectorSpec(
    name="teams",
    title="Microsoft Teams",
    category="communication",
    summary="Post cards to a channel through a Workflows webhook, or messages as a signed-in user.",
    base_url="{MS_GRAPH_URL}/v1.0",
    env=(
        EnvVar(
            "TEAMS_WEBHOOK_URL",
            "Address of a Teams Workflows webhook (the whole link, including sig=).",
        ),
        _GRAPH_URL,
        _AUTHORITY,
        TEAM_ID,
        CHANNEL_ID,
        EnvVar(
            "AZURE_TENANT_ID",
            "Directory (tenant) id, for the signed-in user operations.",
            secret=False,
            required=False,
        ),
        EnvVar(
            "AZURE_CLIENT_ID",
            "Application (client) id that allows public client flows.",
            secret=False,
            required=False,
        ),
    ),
    auth=(TokenAuth(UserTokenProvider, kind="user", token_urls=("{AZURE_AUTHORITY_HOST}",)),),
    headers={"User-Agent": microsoft_user_agent()},
    setup_steps={"user": f"sign in the Teams user with `{SIGN_IN_COMMAND}`"},
    options=("token_cache", "account", "recipients", "people_file"),
    configure=_configure,
    operations=(*_WEBHOOK_OPERATIONS, *_GRAPH_OPERATIONS),
    factory=_tools,
    check=_health,
    docs="https://learn.microsoft.com/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook",
    setup=(
        "In Teams open the channel menu (...), Workflows, the template 'Send webhook alerts to a "
        "channel', and copy the webhook link into TEAMS_WEBHOOK_URL. To post or read as a user "
        "(the awa-bot account), also set AZURE_TENANT_ID and AZURE_CLIENT_ID of an app "
        f"registration that allows public client flows and run {SIGN_IN_COMMAND}."
    ),
    notes=(
        "an app cannot post Teams messages as itself: use the webhook or a signed-in user",
        "Workflows accepts a card before the flow has run; a failure inside the flow is not visible",
    ),
)
