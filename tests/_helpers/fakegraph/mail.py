"""Outlook mail plug-in: the mailbox rules every Microsoft connector shares.

Routes, each under `/users/{user-id}` and under `/me`:

- reading: `GET /messages`, `/mailFolders/{id}`, `/mailFolders/{id}/messages`, `/messages/{id}`
  with `$select`, `$top`, paging, `$filter`, `$orderby` and `$search` (the grammar below);
  `/messages/{id}/attachments`, `.../attachments/{id}`, `.../attachments/{id}/$value`
- writing: `PATCH /messages/{id}`, `POST /messages` (draft), `/messages/{id}/createReply`,
  `.../createReplyAll`, `.../send` (a draft), `.../move`, and `POST /sendMail`

`$filter` understands `and` of `<property> <eq|ne|gt|ge|lt|le> <value>`, `contains`, `startswith`
and `categories/any(c:c eq 'x')` on the properties listed in `FILTER_PROPERTIES`; `or`, `not` and
parentheses answer 501 (the fake does not know Microsoft's rule for them, so no test may pass on
a guess). `$search` understands words and `from:`, `to:`, `cc:`, `subject:`, `body:`,
`participants:`, `hasAttachments:`; words are matched as case-insensitive substrings (Microsoft
matches tokens). Each behavior that is Microsoft's, not ours, is a `Rule` with its source.

State is `graph.store("mail")`: `add_message(user, ...)` fills a mailbox (`attachments` of any
size, `headers`, `categories`, `received`), `fail_after_send()` makes a send answer 5xx after it
was done.
"""

from __future__ import annotations

import base64
import operator as _operator
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from _helpers.fakegraph.core import Ctx, GraphError, Perm, Plugin, Reply, new_guid
from _helpers.fakegraph.identity import resolve_user
from _helpers.fakegraph.odata import apply_select, check_properties, collection, split_list

plugin = Plugin("mail")

WELL_KNOWN = ("inbox", "drafts", "sentitems", "deleteditems", "archive", "junkemail", "outbox")
MESSAGE_PROPERTIES = (
    "id",
    "createdDateTime",
    "lastModifiedDateTime",
    "receivedDateTime",
    "sentDateTime",
    "hasAttachments",
    "internetMessageId",
    "internetMessageHeaders",
    "subject",
    "bodyPreview",
    "importance",
    "parentFolderId",
    "conversationId",
    "isRead",
    "isDraft",
    "webLink",
    "body",
    "sender",
    "from",
    "toRecipients",
    "ccRecipients",
    "bccRecipients",
    "replyTo",
    "flag",
    "categories",
)
ATTACHMENT_PROPERTIES = (
    "id",
    "name",
    "contentType",
    "size",
    "isInline",
    "lastModifiedDateTime",
    "contentBytes",
    "contentId",
    "contentLocation",
)
_ATTACHMENT_ALWAYS = ("id", "@odata.etag", "@odata.type")  # a derived type always names itself
INLINE_ATTACHMENT_LIMIT = 3 * 1024 * 1024
SEARCH_LIMIT = 1000
_DRAFT_FIELDS = ("subject", "body", "toRecipients", "ccRecipients", "bccRecipients")
_RECEIVED_FIELDS = ("isRead", "categories", "importance", "flag", "inferenceClassification")

READ = Perm(app=("Mail.Read", "Mail.ReadWrite"), user=("Mail.Read", "Mail.ReadWrite"))
WRITE = Perm(app=("Mail.ReadWrite",), user=("Mail.ReadWrite",))
SEND = Perm(app=("Mail.Send",), user=("Mail.Send",))

R_RBAC = plugin.rule(
    "outlook.rbac_roles_not_in_token",
    "Exchange RBAC for Applications grants a mailbox-scoped role that never shows in the token's "
    "`roles` claim; the call works for the scoped mailbox only (403 ErrorAccessDenied elsewhere).",
    source="learn.microsoft.com/exchange/permissions-exo/application-rbac",
    source_date="2026-08-21",
)
R_ME = plugin.rule(
    "graph.me_needs_delegated_token",
    "/me with an app token is 400 BadRequest: '/me request is only valid with delegated "
    "authentication flow.'",
    source="learn.microsoft.com/graph/api/user-get (behavior widely documented)",
    source_date="unverified",
)
R_SHARED = plugin.rule(
    "outlook.other_mailbox_needs_shared_scope",
    "A delegated token reaches another user's mailbox only with a `.Shared` scope "
    "(Mail.Read.Shared, Mail.ReadWrite.Shared); otherwise 403 ErrorAccessDenied.",
    source="learn.microsoft.com/graph/permissions-reference",
    source_date="unverified",
)
R_SEND_SHAPE = plugin.rule(
    "outlook.sendmail_shape",
    "sendMail answers 202 with an empty body; without message.toRecipients[].emailAddress.address "
    "it is 400 ErrorInvalidRecipients; an inline attachment above 3 MB is refused.",
    source="learn.microsoft.com/graph/api/user-sendmail",
    source_date="2026-10-05",
)
R_MAIL_WRITE = plugin.rule(
    "outlook.mark_read_needs_readwrite",
    "PATCH of a message (isRead, categories) needs Mail.ReadWrite; Mail.Read is refused with 403 "
    "ErrorAccessDenied.",
    source="learn.microsoft.com/graph/api/message-update",
    source_date="2026-10-05",
)
R_MAIL_PAGING = plugin.rule(
    "outlook.messages_paging",
    "A message list answers 10 per page by default, $top between 1 and 1000, an absolute "
    "@odata.nextLink that carries the original query after that; an unknown $select property "
    "is 400.",
    source="learn.microsoft.com/graph/api/user-list-messages",
    source_date="2026-06-19",
)
R_PREFER_TEXT = plugin.rule(
    "outlook.prefer_body_content_type",
    'Prefer: outlook.body-content-type="text" returns body.contentType text and answers '
    "Preference-Applied.",
    source="learn.microsoft.com/graph/api/message-get",
    source_date="2026-10-05",
)
R_MAIL_UNKNOWN_USER = plugin.rule(
    "outlook.unknown_mailbox",
    "A mailbox that does not exist is 404 ErrorInvalidUser.",
    source="learn.microsoft.com/graph/api/message-get",
    source_date="unverified",
)
R_CONCURRENCY = plugin.rule(
    "outlook.four_concurrent_requests_per_mailbox",
    "More than four concurrent requests per app and mailbox answer 429 ApplicationThrottled "
    "(MailboxConcurrency) with Retry-After; enabled by FakeGraph.limit_concurrency().",
    source="learn.microsoft.com/graph/throttling-limits",
    source_date="2026-09-17",
)
R_ORDER_RULE = plugin.rule(
    "outlook.filter_orderby_order_rule",
    "With $filter and $orderby on messages, every $orderby property must appear in $filter, in "
    "the same order, and before any filter property that is not ordered; otherwise 400 "
    "InefficientFilter 'The restriction or sort order is too complex for this operation.' "
    "$orderby alone, or $filter alone, is fine.",
    source="learn.microsoft.com/graph/api/user-list-messages",
    source_date="2026-06-19",
)
R_SEARCH = plugin.rule(
    "outlook.search_results_by_sent_date_capped",
    "$search on messages returns results sorted by the time the message was sent, newest first, "
    "and at most 1,000 of them; without a property it searches from, subject and body.",
    source="learn.microsoft.com/graph/search-query-parameter",
    source_date="2026-03-07",
)
R_SEARCH_ALONE = plugin.rule(
    "outlook.search_excludes_filter_orderby",
    "$search on messages cannot be combined with $filter or $orderby (400). Reported by "
    "developers and by earlier Learn text; the 2026 Learn page does not state it.",
    source="learn.microsoft.com/answers (community reports; not on the 2026-03-07 Learn page)",
    source_date="unverified",
)
R_FILTER_PROPERTY = plugin.rule(
    "outlook.filter_unknown_property",
    "$filter or $orderby on a property messages do not have is 400 BadRequest 'Could not find a "
    "property named ...'; property names are case-sensitive.",
    source="learn.microsoft.com/graph/query-parameters (error text from the service)",
    source_date="unverified",
)
R_REPLY_DRAFT = plugin.rule(
    "outlook.create_reply_draft",
    "createReply (no body) answers 201 with a draft Message in Drafts (isDraft, webLink) whose "
    "recipient is the original's replyTo, else its sender; it needs Mail.ReadWrite, not "
    "Mail.Send. createReplyAll adds the other recipients except the mailbox itself.",
    source="learn.microsoft.com/graph/api/message-createreply",
    source_date="2026-06-19",
)
R_DRAFT_WRITE = plugin.rule(
    "outlook.draft_writes_need_readwrite",
    "Creating a draft (POST /messages), changing one (PATCH) and moving a message need "
    "Mail.ReadWrite; Mail.Send alone is 403 ErrorAccessDenied. A draft's subject, body and "
    "recipients can be changed; a received message's cannot.",
    source="learn.microsoft.com/graph/api/user-post-messages",
    source_date="unverified",
)
R_SEND_DRAFT = plugin.rule(
    "outlook.send_draft",
    "POST /messages/{id}/send needs no body and Mail.Send (Mail.ReadWrite alone is 403), answers "
    "202 with an empty body and saves the message in Sent Items.",
    source="learn.microsoft.com/graph/api/message-send",
    source_date="2025-07-23",
)
R_SENT_DRAFT_GONE = plugin.rule(
    "outlook.sent_draft_leaves_drafts",
    "After a draft is sent its id answers 404 ErrorItemNotFound (the message lives on in Sent "
    "Items under another id), so reading the draft id tells whether the send happened; a draft "
    "without recipients is 400 ErrorInvalidRecipients.",
    source="learn.microsoft.com/graph/api/message-send (behavior; the page is silent)",
    source_date="unverified",
)
R_MOVE = plugin.rule(
    "outlook.move_returns_new_id",
    "POST /messages/{id}/move {destinationId} answers 201 with the message under a NEW id in the "
    "destination folder; the old id is then 404; an unknown folder is 404 ErrorFolderNotFound.",
    source="learn.microsoft.com/graph/api/message-move",
    source_date="unverified",
)
R_ATTACHMENTS = plugin.rule(
    "outlook.attachment_listing_and_value",
    "GET .../attachments lists file attachments with id, name, contentType, size, isInline and "
    "contentBytes; .../attachments/{id}/$value is the raw bytes; $value of a reference "
    "attachment is 405.",
    source="learn.microsoft.com/graph/api/attachment-get",
    source_date="2026-05-19",
)
R_HEADERS = plugin.rule(
    "outlook.internet_headers_only_on_select",
    "internetMessageHeaders is returned only when $select names it.",
    source="learn.microsoft.com/graph/api/resources/message",
    source_date="unverified",
)
R_CATEGORIES = plugin.rule(
    "outlook.categories_replaced_whole",
    "PATCH of categories replaces the whole list; there is no add or remove.",
    source="learn.microsoft.com/graph/api/message-update",
    source_date="unverified",
)


def _address(address: str, name: str | None = None) -> dict[str, Any]:
    return {"emailAddress": {"name": name or address, "address": address}}


def _plain(body: str, html: bool) -> str:
    return " ".join((re.sub(r"<[^>]+>", " ", body) if html else body).split())


@dataclass
class MailStore:
    """Mailboxes by user id: well-known folder name to messages."""

    graph: Any
    boxes: dict[str, dict[str, list[dict[str, Any]]]] = field(default_factory=dict)
    counter: int = 0
    lost_sends: list[int] = field(default_factory=list)

    def box(self, user_id: str) -> dict[str, list[dict[str, Any]]]:
        return self.boxes.setdefault(user_id, {name: [] for name in WELL_KNOWN})

    def folder_id(self, user_id: str, name: str) -> str:
        return "AAMkAD" + new_guid(f"folder:{user_id}:{name}").replace("-", "")[:24] + "AAA="

    def _new_id(self) -> str:
        self.counter += 1
        return "AAMkAD" + new_guid(f"msg:{self.counter}").replace("-", "")[:30] + "AAA="

    def fail_after_send(self, status: int = 504, times: int = 1) -> None:
        """The next `times` sends of a draft are done, but the answer is `status`."""

        self.lost_sends.extend([status] * times)

    def add_message(
        self,
        user: Any,
        folder: str = "inbox",
        *,
        subject: str = "Hello",
        body: str = "Hi",
        html: bool = False,
        sender: str = "ext@fabrikam.test",
        recipients: tuple[str, ...] | None = None,
        is_read: bool = False,
        attachments: tuple[dict[str, Any], ...] = (),
        headers: dict[str, str] | None = None,
        categories: tuple[str, ...] = (),
        received: str | None = None,
        reply_to: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Put a message into `user`'s `folder` and return it.

        `attachments` are dicts `{"name", "content": bytes, "content_type", "inline", "kind"}`
        (`kind` is `file`, `item` or `reference`); any size, since the fake has no upload session.
        """

        message_id = self._new_id()
        now = received or self.graph.clock.iso()
        message: dict[str, Any] = {
            "@odata.etag": f'W/"CQAAABYAAAB{self.counter:06d}"',
            "id": message_id,
            "createdDateTime": now,
            "lastModifiedDateTime": now,
            "receivedDateTime": now,
            "sentDateTime": now,
            "hasAttachments": any(not a.get("inline") for a in attachments),
            "internetMessageId": f"<{new_guid(f'imid:{self.counter}')}@fabrikam.test>",
            "subject": subject,
            "bodyPreview": _plain(body, html)[:255],
            "importance": "normal",
            "parentFolderId": self.folder_id(user.id, folder),
            "conversationId": "AAQkAD" + new_guid(f"conv:{self.counter}").replace("-", "")[:20],
            "isRead": is_read,
            "isDraft": folder == "drafts",
            # The web link Graph returns; nothing calls this host (SMTP and IMAP for apps are retired).
            "webLink": f"https://outlook.office365.com/owa/?ItemID={quote(message_id)}"
            "&exvsurl=1&viewmodel=ReadMessageItem",
            "body": {"contentType": "html" if html else "text", "content": body},
            "sender": _address(sender),
            "from": _address(sender),
            "toRecipients": [_address(a) for a in (recipients or (user.upn,))],
            "ccRecipients": [],
            "bccRecipients": [],
            "replyTo": [_address(a) for a in reply_to],
            "categories": list(categories),
            "_attachments": [self._attachment(n, a) for n, a in enumerate(attachments)],
            "_headers": [{"name": k, "value": v} for k, v in (headers or {}).items()],
            "_folder": folder,
            "_user": user.id,
        }
        self.box(user.id)[folder].insert(0, message)
        return message

    def _attachment(self, index: int, spec: dict[str, Any]) -> dict[str, Any]:
        content: bytes = spec.get("content", b"")
        kind = spec.get("kind", "file")
        item = {
            "file": "fileAttachment",
            "item": "itemAttachment",
            "reference": "referenceAttachment",
        }[kind]
        seed = f"att:{self.counter}:{index}:{spec['name']}"
        found: dict[str, Any] = {
            "@odata.type": f"#microsoft.graph.{item}",
            "id": "AAMkAD" + new_guid(seed).replace("-", "")[:20],
            "lastModifiedDateTime": self.graph.clock.iso(),
            "name": spec["name"],
            "contentType": spec.get("content_type", "application/octet-stream"),
            "size": len(content),
            "isInline": bool(spec.get("inline", False)),
            "_bytes": content,
            "_kind": kind,
        }
        if kind == "file":
            found.update(
                contentBytes=base64.b64encode(content).decode("ascii"),
                contentId=None,
                contentLocation=None,
            )
        return found

    def find(self, user_id: str, message_id: str) -> tuple[str, dict[str, Any]] | None:
        for folder, messages in self.box(user_id).items():
            for message in messages:
                if message["id"] == message_id:
                    return folder, message
        return None

    def deliver(self, upn: str, message: dict[str, Any]) -> None:
        """Drop a copy of a sent message into the inbox of `upn` if that mailbox exists."""

        user = self.graph.find_user(upn)
        if user is None:
            return
        self.add_message(
            user,
            subject=message["subject"],
            body=message["body"]["content"],
            html=message["body"]["contentType"] == "html",
            sender=message["from"]["emailAddress"]["address"],
            recipients=tuple(r["emailAddress"]["address"] for r in message["toRecipients"]),
            attachments=tuple(
                {
                    "name": item["name"],
                    "content": item["_bytes"],
                    "content_type": item["contentType"],
                    "inline": item["isInline"],
                    "kind": item["_kind"],
                }
                for item in message["_attachments"]
            ),
        )


@plugin.setup
def _setup(graph: Any) -> MailStore:
    return MailStore(graph)


def _rbac(*roles: str) -> Callable[[Ctx], bool]:
    """`also_if` hook: the app holds an Exchange RBAC assignment of one of `roles` for this
    mailbox (`Application Mail.ReadWrite` includes reading, as Mail.ReadWrite does)."""

    def allowed(ctx: Ctx) -> bool:
        principal = ctx.principal
        if principal is None or principal.kind != "app":
            return False
        app = ctx.graph.apps.get(principal.client_id)
        target = ctx.graph.find_user(ctx.params.get("user_id"))
        if app is None or target is None:
            return False
        scoped = {m.lower() for role in roles for m in app.rbac.get(role, set())}
        return target.upn.lower() in scoped or target.id.lower() in scoped

    return allowed


def _folder(ctx: Ctx, user: Any, key: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    store: MailStore = ctx.store
    key = key or ctx.params["folder_id"]
    for name in WELL_KNOWN:
        if key.lower() == name or key == store.folder_id(user.id, name):
            return name, store.box(user.id)[name]
    raise GraphError(
        404, "ErrorInvalidIdMalformed", "The folder id is not valid or the folder does not exist."
    )


def _mailbox(ctx: Ctx) -> Any:
    user = resolve_user(ctx, ctx.params.get("user_id", "me"), family="outlook")
    principal = ctx.principal
    if (
        principal is not None
        and principal.kind == "user"
        and user.id != principal.user_id
        and not principal.grants & {"Mail.Read.Shared", "Mail.ReadWrite.Shared"}
    ):
        raise GraphError(
            403, "ErrorAccessDenied", "Access is denied. Check credentials and try again."
        )
    return user


def _not_found() -> GraphError:
    return GraphError(404, "ErrorItemNotFound", "The specified object was not found in the store.")


def _message(ctx: Ctx) -> tuple[Any, str, dict[str, Any]]:
    user = _mailbox(ctx)
    found = ctx.store.find(user.id, ctx.params["message_id"])
    if found is None:
        raise _not_found()
    return user, found[0], found[1]


def _public(message: dict[str, Any], select: list[str] | None = None) -> dict[str, Any]:
    """The message as the service shows it: no private fields; headers only when selected."""

    shown = {k: v for k, v in message.items() if not k.startswith("_")}
    if select and "internetMessageHeaders" in select:
        shown["internetMessageHeaders"] = message["_headers"]
    return shown


def _body_object(ctx: Ctx) -> dict[str, Any]:
    body = ctx.json()
    if not isinstance(body, dict):
        raise GraphError(400, "ErrorInvalidRequest", "The request body must be a JSON object.")
    return body


# --------------------------------------------------------------------------- $filter, $orderby


def _sort_key(value: Any) -> Any:
    return value.casefold() if isinstance(value, str) else value


FILTER_PROPERTIES: dict[str, tuple[str, Callable[[dict[str, Any]], Any]]] = {
    "receivedDateTime": ("date", lambda m: m["receivedDateTime"]),
    "sentDateTime": ("date", lambda m: m["sentDateTime"]),
    "createdDateTime": ("date", lambda m: m["createdDateTime"]),
    "lastModifiedDateTime": ("date", lambda m: m["lastModifiedDateTime"]),
    "isRead": ("bool", lambda m: m["isRead"]),
    "isDraft": ("bool", lambda m: m["isDraft"]),
    "hasAttachments": ("bool", lambda m: m["hasAttachments"]),
    "importance": ("text", lambda m: m["importance"]),
    "subject": ("text", lambda m: m["subject"]),
    "conversationId": ("text", lambda m: m["conversationId"]),
    "internetMessageId": ("text", lambda m: m["internetMessageId"]),
    "from/emailAddress/address": ("text", lambda m: m["from"]["emailAddress"]["address"]),
    "sender/emailAddress/address": ("text", lambda m: m["sender"]["emailAddress"]["address"]),
}
"""The properties `$filter` and `$orderby` know: name -> (kind, getter)."""

_COMPARE = re.compile(
    r"\s*(?P<prop>[\w/]+)\s+(?P<op>eq|ne|ge|gt|le|lt)\s+(?P<value>'(?:[^']|'')*'|[^\s')]+)\s*"
)
_FUNCTION = re.compile(
    r"\s*(?P<fn>contains|startswith|endswith)\(\s*(?P<prop>[\w/]+)\s*,\s*"
    r"'(?P<value>(?:[^']|'')*)'\s*\)\s*"
)
_CATEGORY = re.compile(
    r"\s*categories/any\(\s*(?P<v>\w+)\s*:\s*(?P=v)\s+eq\s+'(?P<value>(?:[^']|'')*)'\s*\)\s*"
)


def _unknown_property(name: str) -> GraphError:
    return GraphError(
        400,
        "BadRequest",
        f"Could not find a property named '{name}' on type 'Microsoft.OutlookServices.Message'.",
    )


def _moment(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        raise GraphError(400, "BadRequest", f"Invalid date and time value '{text}'.") from None


def _literal(kind: str, raw: str, prop: str) -> Any:
    if kind == "bool":
        if raw not in ("true", "false"):
            raise GraphError(400, "BadRequest", f"Invalid boolean value '{raw}' for '{prop}'.")
        return raw == "true"
    if kind == "date":
        return _moment(raw)
    if not (raw.startswith("'") and raw.endswith("'")):
        raise GraphError(400, "BadRequest", f"A string value for '{prop}' must be in quotes.")
    return raw[1:-1].replace("''", "'").casefold()


def _clause(text: str) -> tuple[str, Any, str]:
    """One `$filter` clause as `(property, predicate, rest-of-text)`."""

    if match := _CATEGORY.match(text):
        wanted = match["value"].replace("''", "'").casefold()

        def has_category(m: dict[str, Any]) -> bool:
            return wanted in [c.casefold() for c in m["categories"]]

        return "categories", has_category, text[match.end() :]
    if match := _FUNCTION.match(text):
        name = match["prop"]
        if name not in FILTER_PROPERTIES or FILTER_PROPERTIES[name][0] != "text":
            raise _unknown_property(name)
        needle, getter = match["value"].replace("''", "'").casefold(), FILTER_PROPERTIES[name][1]
        tests: dict[str, Callable[[str], bool]] = {
            "contains": lambda hay: needle in hay,
            "startswith": lambda hay: hay.startswith(needle),
            "endswith": lambda hay: hay.endswith(needle),
        }
        test = tests[match["fn"]]
        return name, (lambda m: test(str(getter(m)).casefold())), text[match.end() :]
    if match := _COMPARE.match(text):
        name = match["prop"]
        if name not in FILTER_PROPERTIES:
            raise _unknown_property(name)
        kind, getter = FILTER_PROPERTIES[name]
        wanted = _literal(kind, match["value"], name)
        operators: dict[str, Callable[[Any, Any], Any]] = {
            "eq": _operator.eq,
            "ne": _operator.ne,
            "ge": _operator.ge,
            "gt": _operator.gt,
            "le": _operator.le,
            "lt": _operator.lt,
        }
        operator = operators[match["op"]]
        if kind in ("bool", "text") and match["op"] not in ("eq", "ne"):
            raise GraphError(400, "BadRequest", f"Operator '{match['op']}' is not valid here.")

        def compare(m: dict[str, Any]) -> bool:
            have = getter(m)
            have = _moment(have) if kind == "date" else _sort_key(have)
            return bool(operator(have, wanted))

        return name, compare, text[match.end() :]
    raise GraphError(400, "ErrorInvalidRequest", f"Invalid filter clause: {text.strip()!r}")


def parse_filter(text: str | None) -> list[tuple[str, Any]]:
    """`$filter` as `[(property, predicate)]`, all of which must hold."""

    clauses: list[tuple[str, Any]] = []
    rest = text or ""
    while rest.strip():
        if re.match(r"\s*(not\b|\()", rest, re.I):
            raise _not_implemented("$filter with not or parentheses")
        name, predicate, rest = _clause(rest)
        clauses.append((name, predicate))
        if rest.strip():
            word = re.match(r"\s*(and|or)\s+", rest, re.I)
            if word is None or word[1].lower() == "or":
                raise _not_implemented("$filter with or")
            rest = rest[word.end() :]
            if not rest.strip():
                raise GraphError(400, "ErrorInvalidRequest", "Invalid filter clause: trailing and")
    return clauses


def _not_implemented(what: str) -> GraphError:
    return GraphError(
        501, "FakeGraphNotImplemented", f"The mail plug-in of FakeGraph does not implement {what}."
    )


def parse_orderby(text: str | None) -> list[tuple[str, bool]]:
    """`$orderby` as `[(property, descending)]`."""

    order: list[tuple[str, bool]] = []
    for part in split_list(text):
        name, _, direction = part.partition(" ")
        if name not in FILTER_PROPERTIES or direction.strip().lower() not in ("", "asc", "desc"):
            raise _unknown_property(name)
        order.append((name, direction.strip().lower() == "desc"))
    return order


# --------------------------------------------------------------------------- $search

_SEARCH_PROPS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "from": lambda m: m["from"]["emailAddress"],
    "to": lambda m: [r["emailAddress"] for r in m["toRecipients"]],
    "cc": lambda m: [r["emailAddress"] for r in m["ccRecipients"]],
    "subject": lambda m: m["subject"],
    "body": lambda m: _plain(m["body"]["content"], m["body"]["contentType"] == "html"),
}


def _text_of(value: Any) -> str:
    if isinstance(value, dict):
        return f"{value.get('name', '')} {value.get('address', '')}"
    if isinstance(value, list):
        return " ".join(_text_of(v) for v in value)
    return str(value)


def _search_terms(text: str) -> list[tuple[str | None, str]]:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    terms: list[tuple[str | None, str]] = []
    for token in re.findall(r'(?:[\w]+:)?(?:"(?:[^"\\]|\\.)*"|\S+)', text):
        if token in ("OR", "NOT") or token.startswith(("(", "-")):
            raise _not_implemented("$search with OR, NOT or parentheses")
        prop, colon, value = token.partition(":")
        if not colon or not prop.isalpha():
            prop, value = None, token
        terms.append((prop, value.strip('"').replace('\\"', '"').casefold()))
    return terms


def _search_matches(message: dict[str, Any], terms: list[tuple[str | None, str]]) -> bool:
    for prop, value in terms:
        if prop is None:
            hay = " ".join(_text_of(_SEARCH_PROPS[p](message)) for p in ("from", "subject", "body"))
        elif prop == "hasAttachments":
            if value not in ("true", "false"):
                raise GraphError(400, "BadRequest", f"Invalid value '{value}' for hasAttachments.")
            if message["hasAttachments"] != (value == "true"):
                return False
            continue
        elif prop == "participants":
            hay = " ".join(_text_of(_SEARCH_PROPS[p](message)) for p in ("from", "to", "cc"))
        elif prop in _SEARCH_PROPS:
            hay = _text_of(_SEARCH_PROPS[prop](message))
        else:
            raise _not_implemented(f"the search property '{prop}'")
        if value not in hay.casefold():
            return False
    return True


def _select(ctx: Ctx, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages after `$filter`, `$search` and `$orderby`, in the order they are listed."""

    request = ctx.request
    flt, order, search = request.q("$filter"), request.q("$orderby"), request.q("$search")
    if search is not None:
        if flt is not None or order is not None:
            raise GraphError(
                400,
                "BadRequest",
                "The $search parameter cannot be combined with $filter or $orderby.",
            )
        terms = _search_terms(search)
        found = [m for m in messages if _search_matches(m, terms)]
        found.sort(key=lambda m: _moment(m["sentDateTime"]), reverse=True)
        return found[:SEARCH_LIMIT]
    clauses = parse_filter(flt)
    keys = parse_orderby(order)
    if clauses and keys:
        filtered = list(dict.fromkeys(name for name, _ in clauses))
        ordered = [name for name, _ in keys]
        if filtered[: len(ordered)] != ordered:
            raise GraphError(
                400,
                "InefficientFilter",
                "The restriction or sort order is too complex for this operation.",
            )
    found = [m for m in messages if all(test(m) for _, test in clauses)]
    for name, descending in reversed(keys or [("receivedDateTime", True)]):
        found.sort(key=_ordering(name), reverse=descending)
    return found


def _ordering(name: str) -> Callable[[dict[str, Any]], Any]:
    kind, getter = FILTER_PROPERTIES[name]
    if kind == "date":
        return lambda m: _moment(getter(m))
    return lambda m: _sort_key(getter(m))


# --------------------------------------------------------------------------- reading


def _shape(ctx: Ctx) -> tuple[Callable[[dict[str, Any]], dict[str, Any]], bool]:
    text = (ctx.request.header("prefer") or "").replace(" ", "").lower()
    as_text = 'outlook.body-content-type="text"' in text
    select = split_list(ctx.request.q("$select"))

    def shape(message: dict[str, Any]) -> dict[str, Any]:
        shaped = _public(message, select)
        if as_text and message["body"]["contentType"] == "html":
            shaped["body"] = {
                "contentType": "text",
                "content": _plain(message["body"]["content"], True),
            }
        return shaped

    return shape, as_text


def _with_preference(reply: Reply, as_text: bool) -> Reply:
    if as_text:
        reply.headers["Preference-Applied"] = 'outlook.body-content-type="text"'
    return reply


def folder_info(ctx: Ctx) -> Any:
    user = _mailbox(ctx)
    name, messages = _folder(ctx, user)
    store: MailStore = ctx.store
    body = {
        "id": store.folder_id(user.id, name),
        "displayName": name,
        "parentFolderId": store.folder_id(user.id, "msgfolderroot"),
        "childFolderCount": 0,
        "unreadItemCount": sum(1 for m in messages if not m["isRead"]),
        "totalItemCount": len(messages),
    }
    select = split_list(ctx.request.q("$select"))
    return {k: v for k, v in body.items() if not select or k in select or k == "id"}


def _list(ctx: Ctx, messages: list[dict[str, Any]], context: str) -> Reply:
    shape, as_text = _shape(ctx)
    body = collection(
        ctx,
        _select(ctx, messages),
        default_top=10,
        max_top=1000,
        known=MESSAGE_PROPERTIES,
        type_name="microsoft.graph.message",
        context=context,
        shape=shape,
    )
    return _with_preference(Reply(200, body), as_text)


def list_folder_messages(ctx: Ctx) -> Any:
    user = _mailbox(ctx)
    _, messages = _folder(ctx, user)
    return _list(ctx, messages, "users('x')/mailFolders('inbox')/messages")


def list_all_messages(ctx: Ctx) -> Any:
    user = _mailbox(ctx)
    everything = [m for folder in ctx.store.box(user.id).values() for m in folder]
    return _list(ctx, everything, "users('x')/messages")


def get_message(ctx: Ctx) -> Any:
    _, _, message = _message(ctx)
    shape, as_text = _shape(ctx)
    select = split_list(ctx.request.q("$select"))
    check_properties(select, MESSAGE_PROPERTIES, "microsoft.graph.message")
    return _with_preference(Reply(200, apply_select(shape(message), select)), as_text)


def _attachment(ctx: Ctx, message: dict[str, Any]) -> dict[str, Any]:
    for attachment in message["_attachments"]:
        if attachment["id"] == ctx.params["attachment_id"]:
            return dict(attachment)
    raise _not_found()


def list_attachments(ctx: Ctx) -> Any:
    _, _, message = _message(ctx)
    return collection(
        ctx,
        message["_attachments"],
        default_top=10,
        max_top=1000,
        known=ATTACHMENT_PROPERTIES,
        type_name="microsoft.graph.attachment",
        context="users('x')/messages('m')/attachments",
        always=_ATTACHMENT_ALWAYS,
    )


def get_attachment(ctx: Ctx) -> Any:
    _, _, message = _message(ctx)
    select = split_list(ctx.request.q("$select"))
    check_properties(select, ATTACHMENT_PROPERTIES, "microsoft.graph.attachment")
    return apply_select(_attachment(ctx, message), select, always=_ATTACHMENT_ALWAYS)


def attachment_value(ctx: Ctx) -> Reply:
    _, _, message = _message(ctx)
    attachment = _attachment(ctx, message)
    if attachment["_kind"] == "reference":
        raise GraphError(405, "ErrorInvalidRequest", "The operation is not allowed on this item.")
    return Reply(200, attachment["_bytes"], content_type=attachment["contentType"])


# --------------------------------------------------------------------------- writing


def update_message(ctx: Ctx) -> Any:
    _, folder, message = _message(ctx)
    body = _body_object(ctx)
    allowed = (*_RECEIVED_FIELDS, *(_DRAFT_FIELDS if folder == "drafts" else ()))
    refused = next((key for key in body if key not in allowed), None)
    if refused is not None:
        raise GraphError(
            400,
            "ErrorInvalidProperty",
            f"Property '{refused}' cannot be changed on a message in {folder} by this plug-in.",
        )
    message.update(body)
    if "body" in body:
        content = body["body"]
        message["bodyPreview"] = _plain(
            content.get("content", ""), str(content.get("contentType", "")).lower() == "html"
        )[:255]
    message["lastModifiedDateTime"] = ctx.clock.iso()
    return _public(message)


def _recipients(message: dict[str, Any]) -> list[str]:
    found = []
    for name in ("toRecipients", "ccRecipients", "bccRecipients"):
        for item in message[name]:
            address = (
                item.get("emailAddress", {}).get("address") if isinstance(item, dict) else None
            )
            if not isinstance(address, str) or "@" not in address:
                raise GraphError(
                    400,
                    "ErrorInvalidRecipients",
                    "At least one recipient is not valid., Recipient "
                    "'toRecipients[].emailAddress.address' is required.",
                )
            found.append(address)
    return found


def create_draft(ctx: Ctx) -> Reply:
    user = _mailbox(ctx)
    body = _body_object(ctx)
    content = body.get("body") or {"contentType": "text", "content": ""}
    draft = ctx.store.add_message(
        user,
        "drafts",
        subject=body.get("subject") or "",
        body=content.get("content") or "",
        html=str(content.get("contentType", "text")).lower() == "html",
        sender=user.upn,
        recipients=(),
        is_read=True,
    )
    for name in ("toRecipients", "ccRecipients", "bccRecipients"):
        draft[name] = body.get(name) or []
    return Reply(201, _public(draft))


def _reply(ctx: Ctx, everyone: bool) -> Reply:
    user, _, original = _message(ctx)
    store: MailStore = ctx.store
    target = original["replyTo"] or [original["from"]]
    if everyone:
        target = [*target, *original["toRecipients"], *original["ccRecipients"]]
    addresses = list(
        dict.fromkeys(
            r["emailAddress"]["address"]
            for r in target
            if r["emailAddress"]["address"].lower() != user.upn.lower()
        )
    )
    quoted = (
        f"<html><body><br><hr><b>From:</b> {original['from']['emailAddress']['address']}<br>"
        f"<b>Subject:</b> {original['subject']}<br><br>{original['body']['content']}</body></html>"
    )
    subject = original["subject"]
    draft = store.add_message(
        user,
        "drafts",
        subject=subject if subject.upper().startswith("RE:") else f"RE: {subject}",
        body=quoted,
        html=True,
        sender=user.upn,
        recipients=tuple(addresses),
        is_read=True,
    )
    draft["conversationId"] = original["conversationId"]
    return Reply(201, _public(draft))


def create_reply(ctx: Ctx) -> Reply:
    return _reply(ctx, False)


def create_reply_all(ctx: Ctx) -> Reply:
    return _reply(ctx, True)


def send_draft(ctx: Ctx) -> Reply:
    user, folder, draft = _message(ctx)
    store: MailStore = ctx.store
    if folder != "drafts":
        raise GraphError(400, "ErrorInvalidRequest", "The message is not a draft.")
    addresses = _recipients(draft)
    if not addresses:
        raise GraphError(400, "ErrorInvalidRecipients", "At least one recipient is not specified.")
    store.box(user.id)["drafts"].remove(draft)
    sent = store.add_message(
        user,
        "sentitems",
        subject=draft["subject"],
        body=draft["body"]["content"],
        html=draft["body"]["contentType"] == "html",
        sender=user.upn,
        recipients=(),
        is_read=True,
    )
    for name in ("toRecipients", "ccRecipients", "bccRecipients"):
        sent[name] = draft[name]
    for address in addresses:
        store.deliver(address, sent)
    if store.lost_sends:
        status = store.lost_sends.pop(0)
        raise GraphError(status, "ErrorInternalServerTransientError", "The server is busy.")
    return Reply(202)


def move_message(ctx: Ctx) -> Reply:
    user, folder, message = _message(ctx)
    store: MailStore = ctx.store
    body = _body_object(ctx)
    destination = body.get("destinationId")
    if not isinstance(destination, str):
        raise GraphError(400, "ErrorInvalidRequest", "destinationId is required.")
    try:
        name, target = _folder(ctx, user, destination)
    except GraphError:
        raise GraphError(
            404, "ErrorFolderNotFound", "The specified folder could not be found in the store."
        ) from None
    store.box(user.id)[folder].remove(message)
    moved = {
        **message,
        "id": store._new_id(),
        "parentFolderId": store.folder_id(user.id, name),
        "_folder": name,
    }
    target.insert(0, moved)
    return Reply(201, _public(moved))


def _attachment_spec(attachment: Any) -> dict[str, Any]:
    """The `add_message` form of a `fileAttachment` in a sendMail body."""

    encoded = attachment.get("contentBytes") if isinstance(attachment, dict) else None
    if not isinstance(encoded, str) or not attachment.get("name"):
        raise GraphError(400, "ErrorInvalidRequest", "An attachment needs name and contentBytes.")
    if len(encoded) * 3 // 4 > INLINE_ATTACHMENT_LIMIT:
        raise GraphError(
            413,
            "ErrorMessageSizeExceeded",
            "An inline attachment is limited to about 3 MB; use an upload session on a draft.",
        )
    return {
        "name": attachment["name"],
        "content": base64.b64decode(encoded),
        "content_type": attachment.get("contentType", "application/octet-stream"),
        "inline": bool(attachment.get("isInline", False)),
    }


def send_mail(ctx: Ctx) -> Reply:
    store: MailStore = ctx.store
    user = _mailbox(ctx)
    body = ctx.json()
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, dict):
        raise GraphError(400, "ErrorInvalidRequest", "The request needs a `message` object.")
    recipients = message.get("toRecipients")
    found = [
        r.get("emailAddress", {}).get("address") if isinstance(r, dict) else None
        for r in recipients or []
    ]
    addresses = [a for a in found if isinstance(a, str) and "@" in a]
    if not found or len(addresses) != len(found):
        raise GraphError(
            400,
            "ErrorInvalidRecipients",
            "At least one recipient is not valid., Recipient 'toRecipients[].emailAddress.address' "
            "is required.",
        )
    attachments = tuple(_attachment_spec(item) for item in message.get("attachments", []))
    content = message.get("body") or {"contentType": "text", "content": ""}
    sent = store.add_message(
        user,
        "sentitems",
        subject=message.get("subject") or "",
        body=content.get("content") or "",
        html=str(content.get("contentType", "text")).lower() == "html",
        sender=user.upn,
        recipients=tuple(addresses),
        is_read=True,
        attachments=attachments,
    )
    if body.get("saveToSentItems") is False:
        store.box(user.id)["sentitems"].remove(sent)
    for address in addresses:
        store.deliver(address, sent)
    return Reply(202)


def _register(prefix: str) -> None:
    read, write, send = (
        _rbac("Application Mail.Read", "Application Mail.ReadWrite"),
        _rbac("Application Mail.ReadWrite"),
        _rbac("Application Mail.Send"),
    )
    options = {"family": "outlook"}
    message = f"{prefix}/messages/{{message-id}}"
    for template, handler in (
        ("/messages", list_all_messages),
        ("/mailFolders/{folder-id}/messages", list_folder_messages),
        ("/mailFolders/{folder-id}", folder_info),
        ("/messages/{message-id}", get_message),
        ("/messages/{message-id}/attachments", list_attachments),
        ("/messages/{message-id}/attachments/{attachment-id}/$value", attachment_value),
        ("/messages/{message-id}/attachments/{attachment-id}", get_attachment),
    ):
        plugin.route("GET", prefix + template, perm=READ, also_if=read, **options)(handler)
    plugin.route("PATCH", message, perm=WRITE, also_if=write, **options)(update_message)
    plugin.route("POST", f"{prefix}/messages", perm=WRITE, also_if=write, **options)(create_draft)
    plugin.route("POST", f"{message}/createReply", perm=WRITE, also_if=write, **options)(
        create_reply
    )
    plugin.route("POST", f"{message}/createReplyAll", perm=WRITE, also_if=write, **options)(
        create_reply_all
    )
    plugin.route("POST", f"{message}/move", perm=WRITE, also_if=write, **options)(move_message)
    plugin.route("POST", f"{message}/send", perm=SEND, also_if=send, **options)(send_draft)
    plugin.route("POST", f"{prefix}/sendMail", perm=SEND, also_if=send, **options)(send_mail)


_register("/users/{user-id}")
_register("/me")
