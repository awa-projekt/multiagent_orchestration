"""Teams plug-in (minimal): channel messages and the Workflows webhook.

Routes: `GET /me/joinedTeams`, `GET /teams/{id}/channels`, `GET|POST
/teams/{id}/channels/{id}/messages`, `GET|POST .../messages/{id}/replies` and, outside Graph,
`POST /workflows/{id}` (the Teams Workflows "webhook request" trigger).

State is `graph.store("teams")`: `add_team`, `add_channel`, `add_message`, `approve_protected_api`.
`apply_limits(graph)` switches the documented Teams rate limits on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from _helpers.fakegraph.core import Ctx, GraphError, Perm, Plugin, Reply, denied, new_guid
from _helpers.fakegraph.identity import User, resolve_user
from _helpers.fakegraph.odata import collection

plugin = Plugin("teams")

ADAPTIVE = "application/vnd.microsoft.card.adaptive"
WORKFLOW_LIMIT = 28 * 1024
IMPORT_MESSAGE = (
    "Message POST is allowed in application-only context only for import purposes. Refer to "
    "https://docs.microsoft.com/microsoftteams/platform/graph-api/import-messages/"
    "import-external-messages-to-teams for more details."
)

READ = Perm(
    app=("ChannelMessage.Read.All", "ChannelMessage.Read.Group"), user=("ChannelMessage.Read.All",)
)
SEND = Perm(user=("ChannelMessage.Send",))
JOINED = Perm(user=("Team.ReadBasic.All", "User.Read"))
CHANNELS = Perm(
    app=("Channel.ReadBasic.All", "ChannelSettings.Read.All"),
    user=("Channel.ReadBasic.All", "ChannelSettings.Read.All"),
)

R_APP_POST = plugin.rule(
    "teams.app_only_post_forbidden",
    "POST of a channel message with an application token is 403 Forbidden (only the import "
    "permission Teamwork.Migrate.All exists for applications, for migration only); a delegated "
    "token needs ChannelMessage.Send and membership of the channel.",
    source="learn.microsoft.com/graph/api/channel-post-messages",
    source_date="2026-05-16",
)
R_PROTECTED = plugin.rule(
    "teams.app_only_read_is_protected_api",
    "Reading channel messages with application permissions is a protected API: without Microsoft's "
    "approval for the app the answer is 403 even with ChannelMessage.Read.All.",
    source="learn.microsoft.com/graph/teams-protected-apis (docs-repo copy; the Learn URL 404s)",
    source_date="unverified",
)
R_TOP50 = plugin.rule(
    "teams.channel_messages_top_max_50",
    "$top above 50 is rejected with 400; $filter and $orderby are not supported on the channel "
    "message list; replies come from /replies.",
    source="learn.microsoft.com/graph/api/channel-list-messages",
    source_date="2026-04-10",
)
R_CARD = plugin.rule(
    "teams.adaptive_card_attachment_shape",
    "An adaptive-card attachment needs contentType application/vnd.microsoft.card.adaptive, a "
    "STRING `content` holding the card JSON and a matching <attachment id> in the body.",
    source="learn.microsoft.com/graph/api/chatmessage-post",
    source_date="2024-07-23",
)
R_WORKFLOW = plugin.rule(
    "teams.workflows_envelope",
    '/workflows/{id} wants {"type":"message","attachments":[{contentType adaptive, content '
    "{type AdaptiveCard, version, body}}]}; anything else is 400, a valid post 202 with an empty "
    "body, and a body over 28 KB 413.",
    source="learn.microsoft.com/connectors/teams (Workflows webhook trigger)",
    source_date="2026-04-07",
)
R_TEAMS_LIMITS = plugin.rule(
    "teams.channel_message_rate_limits",
    "POST to a channel: 1 request per second per channel and per user, 500/s per app, 50/s per "
    "app and tenant; GET: 200/s, 20/s, 1/s per channel (apply_limits(graph) enforces the "
    "per-channel and per-user ones on the fake clock).",
    source="learn.microsoft.com/graph/throttling-limits",
    source_date="2026-09-17",
)


@dataclass
class Channel:
    id: str
    team_id: str
    name: str
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Team:
    id: str
    name: str
    members: set[str] = field(default_factory=set)
    channels: dict[str, Channel] = field(default_factory=dict)


@dataclass
class TeamsStore:
    """Teams, channels, messages and what the Workflows endpoint received."""

    graph: Any
    teams: dict[str, Team] = field(default_factory=dict)
    protected_api_approved: bool = False
    workflow_posts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    workflow_sig: str | None = None
    counter: int = 0
    device_declined: bool = False  # the person cancels the next device code sign-in
    stale_refresh_tokens: int = 0  # refresh tokens presented after they were replaced

    def add_team(self, name: str, *members: User) -> Team:
        team = Team(new_guid(f"team:{name}"), name, {m.id for m in members})
        if team.id in self.teams:
            raise ValueError(f"a team named {name!r} exists already")
        self.teams[team.id] = team
        return team

    def add_channel(self, team: Team, name: str) -> Channel:
        channel = Channel(f"19:{new_guid(f'channel:{team.id}:{name}')}@thread.tacv2", team.id, name)
        if channel.id in team.channels:
            raise ValueError(f"team {team.name!r} has a channel named {name!r} already")
        team.channels[channel.id] = channel
        return channel

    def add_message(
        self,
        channel: Channel,
        text: str,
        *,
        sender: User | None = None,
        application: str | None = None,
        html: bool = False,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        """A message by `sender` (or by an app, e.g. a bot) in `channel`."""

        self.counter += 1
        now = self.graph.clock.iso()
        message: dict[str, Any] = {
            "id": str(1_760_000_000_000 + self.counter),
            "replyToId": in_reply_to,
            "etag": str(1_760_000_000_000 + self.counter),
            "messageType": "message",
            "createdDateTime": now,
            "lastModifiedDateTime": now,
            "lastEditedDateTime": None,
            "deletedDateTime": None,
            "subject": None,
            "summary": None,
            "importance": "normal",
            "locale": "en-us",
            "webUrl": f"https://teams.microsoft.com/l/message/{channel.id}/{self.counter}",
            "from": {
                "application": {
                    "id": new_guid(application),
                    "displayName": application,
                    "applicationIdentityType": "bot",
                }
                if application
                else None,
                "device": None,
                "user": {"id": sender.id, "displayName": sender.name, "userIdentityType": "aadUser"}
                if sender
                else None,
            },
            "body": {"contentType": "html" if html else "text", "content": text},
            "channelIdentity": {"teamId": channel.team_id, "channelId": channel.id},
            "attachments": [],
            "mentions": [],
            "reactions": [],
            "_replies": [],
        }
        if in_reply_to is None:
            channel.messages.insert(0, message)
        else:
            parent = next(m for m in channel.messages if m["id"] == in_reply_to)
            parent["_replies"].append(message)
            parent["lastModifiedDateTime"] = now
        return message

    def approve_protected_api(self, approved: bool = True) -> None:
        """Microsoft's approval of the app for protected Teams APIs."""

        self.protected_api_approved = approved


@plugin.setup
def _setup(graph: Any) -> TeamsStore:
    store = TeamsStore(graph)
    _wrap_token_route(graph, store)
    return store


def apply_limits(graph: Any) -> None:
    """Enforce the per-channel and per-user rates of the Teams throttling table."""

    graph.rate_limit("POST", "/teams/*/channels/*/messages", 1, key="path")
    graph.rate_limit("POST", "/teams/*/channels/*/messages", 1, key="user")
    graph.rate_limit("GET", "/teams/*/channels/*/messages", 1, key="path")


# --------------------------------------------------------------------------- helpers


def _channel(ctx: Ctx) -> tuple[Team, Channel]:
    store: TeamsStore = ctx.store
    team = store.teams.get(ctx.params["team_id"])
    channel = team.channels.get(ctx.params["channel_id"]) if team else None
    if team is None or channel is None:
        raise GraphError(404, "NotFound", "The team or channel was not found.")
    return team, channel


def _member(ctx: Ctx, team: Team) -> None:
    principal = ctx.principal
    if principal is not None and principal.kind == "user" and principal.user_id not in team.members:
        raise GraphError(
            403, "Forbidden", "The signed-in user is not a member of the team or channel."
        )


def _public(message: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in message.items() if not k.startswith("_")}


@plugin.route("GET", "/me/joinedTeams", perm=JOINED)
def joined_teams(ctx: Ctx) -> Any:
    store: TeamsStore = ctx.store
    user = resolve_user(ctx, "me")
    teams = [
        {"id": t.id, "displayName": t.name} for t in store.teams.values() if user.id in t.members
    ]
    return collection(ctx, teams, default_top=100, max_top=100, context="teams")


@plugin.route("GET", "/teams/{team-id}/channels", perm=CHANNELS)
def list_channels(ctx: Ctx) -> Any:
    store: TeamsStore = ctx.store
    team = store.teams.get(ctx.params["team_id"])
    if team is None:
        raise GraphError(404, "NotFound", "The team was not found.")
    _member(ctx, team)
    channels = [{"id": c.id, "displayName": c.name} for c in team.channels.values()]
    return collection(ctx, channels, default_top=100, max_top=100, context="channels")


def _check_list(ctx: Ctx, *, expand: bool) -> bool:
    principal = ctx.principal
    assert principal is not None
    store: TeamsStore = ctx.store
    if principal.kind == "app" and not store.protected_api_approved:
        raise GraphError(
            403,
            "Forbidden",
            "This API is a protected API: the application has not been approved for protected "
            "Teams APIs (https://aka.ms/teams-graph-protected-apis).",
        )
    for name in ("$filter", "$orderby"):
        if ctx.request.q(name) is not None:
            raise GraphError(
                400, "BadRequest", f"Query option '{name}' is not supported on this resource."
            )
    expand_value = ctx.request.q("$expand")
    if expand_value not in (None, "replies") or (expand_value and not expand):
        raise GraphError(
            400, "BadRequest", f"Query option '$expand={expand_value}' is not supported."
        )
    return expand_value == "replies"


@plugin.route(
    "GET", "/teams/{team-id}/channels/{channel-id}/messages/{message-id}/replies", perm=READ
)
def list_replies(ctx: Ctx) -> Any:
    team, channel = _channel(ctx)
    _member(ctx, team)
    _check_list(ctx, expand=False)
    parent = next((m for m in channel.messages if m["id"] == ctx.params["message_id"]), None)
    if parent is None:
        raise GraphError(404, "NotFound", "The message was not found.")
    return collection(
        ctx,
        [_public(m) for m in parent["_replies"]],
        default_top=20,
        max_top=50,
        reject_top=True,
        context="replies",
    )


@plugin.route("GET", "/teams/{team-id}/channels/{channel-id}/messages", perm=READ)
def list_messages(ctx: Ctx) -> Any:
    team, channel = _channel(ctx)
    _member(ctx, team)
    expand = _check_list(ctx, expand=True)
    ordered = sorted(channel.messages, key=lambda m: m["lastModifiedDateTime"], reverse=True)
    return collection(
        ctx,
        ordered,
        default_top=20,
        max_top=50,
        reject_top=True,
        context="messages",
        shape=lambda m: {
            **_public(m),
            **({"replies": [_public(r) for r in m["_replies"]]} if expand else {}),
        },
    )


def _validate_message(body: Any) -> dict[str, Any]:
    """The documented shape of a posted chatMessage (card attachments, mentions)."""

    if not isinstance(body, dict):
        raise GraphError(400, "BadRequest", "The request body must be a JSON object.")
    content = body.get("body")
    if not isinstance(content, dict) or not str(content.get("content") or "").strip():
        raise GraphError(400, "BadRequest", "The message body content must not be empty.")
    if content.get("contentType", "text") not in ("text", "html"):
        raise GraphError(400, "BadRequest", "body.contentType must be 'text' or 'html'.")
    text = str(content["content"])
    attachments = body.get("attachments") or []
    ids = set()
    for attachment in attachments:
        if (
            not isinstance(attachment, dict)
            or not attachment.get("id")
            or not attachment.get("contentType")
        ):
            raise GraphError(400, "BadRequest", "Every attachment needs an id and a contentType.")
        ids.add(str(attachment["id"]))
        if attachment["contentType"] == ADAPTIVE:
            card = attachment.get("content")
            if not isinstance(card, str):
                raise GraphError(
                    400, "BadRequest", "An adaptive card's `content` must be a JSON string."
                )
            try:
                parsed = json.loads(card)
            except ValueError:
                raise GraphError(
                    400, "BadRequest", "The adaptive card content is not valid JSON."
                ) from None
            if not isinstance(parsed, dict) or parsed.get("type") != "AdaptiveCard":
                raise GraphError(
                    400, "BadRequest", "The adaptive card must have type AdaptiveCard."
                )
    referenced = set(re.findall(r'<attachment id="([^"]+)"', text))
    if referenced != ids:
        raise GraphError(
            400,
            "BadRequest",
            'Every attachment needs a matching <attachment id="..."> in the body '
            f"(attachments: {sorted(ids)}, referenced: {sorted(referenced)}).",
        )
    mention_ids = {str(m.get("id")) for m in body.get("mentions") or [] if isinstance(m, dict)}
    if set(re.findall(r'<at id="(\d+)"', text)) != mention_ids:
        raise GraphError(400, "BadRequest", "Every <at id> in the body needs a matching mention.")
    return body


def _post(ctx: Ctx, in_reply_to: str | None) -> Reply:
    store: TeamsStore = ctx.store
    principal = ctx.principal
    assert principal is not None
    team, channel = _channel(ctx)
    if principal.kind == "app":
        raise GraphError(403, "Forbidden", IMPORT_MESSAGE)
    if "ChannelMessage.Send" not in principal.grants:
        raise denied("graph", SEND, principal)
    _member(ctx, team)
    body = _validate_message(ctx.json())
    user = resolve_user(ctx, "me")
    message = store.add_message(
        channel,
        str(body["body"]["content"]),
        sender=user,
        html=body["body"].get("contentType") == "html",
        in_reply_to=in_reply_to,
    )
    message["attachments"] = [
        {**a, "contentUrl": a.get("contentUrl")} for a in body.get("attachments") or []
    ]
    message["mentions"] = body.get("mentions") or []
    return Reply(201, _public(message))


@plugin.route("POST", "/teams/{team-id}/channels/{channel-id}/messages")
def post_message(ctx: Ctx) -> Reply:
    return _post(ctx, None)


@plugin.route("POST", "/teams/{team-id}/channels/{channel-id}/messages/{message-id}/replies")
def post_reply(ctx: Ctx) -> Reply:
    _, channel = _channel(ctx)
    if not any(m["id"] == ctx.params["message_id"] for m in channel.messages):
        raise GraphError(404, "NotFound", "The message was not found.")
    return _post(ctx, ctx.params["message_id"])


# --------------------------------------------------------------------------- Workflows webhook


@plugin.route("POST", "/workflows/{workflow-id}", auth="none")
def workflows(ctx: Ctx) -> Reply:
    store: TeamsStore = ctx.store
    if store.workflow_sig is not None and ctx.request.q("sig") != store.workflow_sig:
        raise GraphError(
            401, "DirectApiAuthorizationRequired", "The request is missing a valid signature."
        )
    if len(ctx.request.body) > WORKFLOW_LIMIT:
        raise GraphError(413, "RequestEntityTooLarge", "The message is larger than 28 KB.")
    body = ctx.json()
    if not isinstance(body, dict) or body.get("type") != "message":
        raise GraphError(400, "InvalidRequest", 'The envelope must be {"type": "message", ...}.')
    attachments = body.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        raise GraphError(
            400, "InvalidRequest", "The envelope needs a non-empty `attachments` list."
        )
    for attachment in attachments:
        card = attachment.get("content") if isinstance(attachment, dict) else None
        if (
            not isinstance(attachment, dict)
            or attachment.get("contentType") != ADAPTIVE
            or not isinstance(card, dict)
            or card.get("type") != "AdaptiveCard"
            or not isinstance(card.get("body"), list)
            or not card.get("version")
        ):
            raise GraphError(
                400,
                "InvalidRequest",
                "Each attachment needs contentType 'application/vnd.microsoft.card.adaptive' and "
                "a `content` object with type AdaptiveCard, version and body.",
            )
    store.workflow_posts.setdefault(ctx.params["workflow_id"], []).append(body)
    return Reply(202)


# --- sign-in and webhook additions
# For the delegated sign-in (device code, refresh) and the Workflows rate limit. The token endpoint
# itself is `_helpers.fakegraph.identity`'s; the wrapper below only adds what a test script needs on top.

R_USER_READ = plugin.rule(
    "graph.me_profile_needs_user_read",
    "GET /me needs a delegated token with User.Read (an application token is 400 BadRequest, the "
    "rule graph.me_needs_delegated_token); $select limits the returned properties.",
    source="learn.microsoft.com/graph/api/user-get",
    source_date="2025-07-23",
)
R_DECLINED = plugin.rule(
    "entra.device_code_declined",
    "When the person cancels the device code sign-in, the next poll answers authorization_declined "
    "and the code is dead; the AADSTS number is unverified.",
    source="learn.microsoft.com/entra/identity-platform/v2-oauth2-device-code (expected errors)",
    source_date="2026-06-15",
)
R_SLOW_DOWN = plugin.rule(
    "entra.device_code_slow_down",
    "Polling a device code faster than its interval answers slow_down (RFC 8628 3.5); the client "
    "adds 5 seconds. The Learn page does not list slow_down.",
    source="RFC 8628 section 3.5; learn.microsoft.com/entra/identity-platform/v2-oauth2-device-code",
    source_date="unverified",
)
R_WORKFLOW_RATE = plugin.rule(
    "teams.workflows_four_requests_per_second",
    "More than four requests per second to one Workflows webhook are throttled (429 with "
    "Retry-After); apply_workflow_limit(graph) enforces it on the fake clock.",
    source="learn.microsoft.com/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook",
    source_date="2026-08-03",
)
R_SYSTEM_MESSAGE = plugin.rule(
    "teams.channel_lists_include_system_messages",
    "A channel message list also holds system event messages: messageType systemEventMessage, "
    "`from` null, body `<systemEventMessage/>`.",
    source="learn.microsoft.com/graph/api/channel-list-messages",
    source_date="2026-04-10",
)


def apply_workflow_limit(graph: Any, per_second: float = 4) -> None:
    """Throttle each Workflows webhook to `per_second` requests per second of the fake clock."""

    graph.rate_limit("POST", "/workflows/*", per_second, key="path")


def add_system_message(
    store: TeamsStore, channel: Channel, text: str = "<systemEventMessage/>"
) -> dict[str, Any]:
    """A system event message (a member joined, a channel was renamed): no sender."""

    message = store.add_message(channel, text, html=True)
    message["from"] = None
    message["messageType"] = "systemEventMessage"
    return message


@plugin.route("GET", "/me")
def my_profile(ctx: Ctx) -> Any:
    user = resolve_user(ctx, "me")  # an application token is 400 here, as in Graph
    principal = ctx.principal
    assert principal is not None
    if "User.Read" not in principal.grants:
        raise denied("graph", Perm(user=("User.Read",)), principal)
    profile = {
        "id": user.id,
        "userPrincipalName": user.upn,
        "displayName": user.name,
        "mail": user.upn,
    }
    selected = ctx.request.q("$select")
    if selected is None:
        return profile
    wanted = {name.strip() for name in selected.split(",")} | {"id"}
    return {k: v for k, v in profile.items() if k in wanted}


def _wrap_token_route(graph: Any, store: TeamsStore) -> None:
    """Put a wrapper around the token endpoint: a declined device code (`store.device_declined`)
    and a count of refresh tokens presented after they were replaced (`stale_refresh_tokens`)."""

    from _helpers.fakegraph.core import TokenError
    from _helpers.fakegraph.identity import DEVICE_GRANT, _form

    token_routes = [
        index
        for index, route in enumerate(graph.routes)
        if route.plugin == "identity"
        and route.method == "POST"
        and route.template.endswith("/token")
    ]
    if not token_routes:
        raise RuntimeError("serve the identity plug-in before the teams plug-in")
    for index in token_routes:
        route = graph.routes[index]
        original = route.handler

        def handler(ctx: Ctx, original: Any = original) -> Reply:
            form = _form(ctx)
            identity = ctx.graph.identity
            code = form.get("device_code", "")
            declined = form.get("grant_type") == DEVICE_GRANT and store.device_declined
            if declined and code in identity.devices:
                del identity.devices[code]
                return TokenError(
                    400,
                    "authorization_declined",
                    70000,
                    "The end user denied the authorization request.",
                ).to_reply(ctx.clock)
            record = identity.refresh_tokens.get(form.get("refresh_token", ""))
            if (
                form.get("grant_type") == "refresh_token"
                and record is not None
                and not record.valid
            ):
                store.stale_refresh_tokens += 1
            return original(ctx)  # type: ignore[no-any-return]

        graph.routes[index] = replace(route, handler=handler)
