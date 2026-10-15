"""Email files (`mail`): one message or a sampled `.mbox`, attachments profiled as children;
HTML becomes text, nothing is fetched or run, and hidden text is scanned for injection phrases."""

from __future__ import annotations

import mimetypes
import re
from collections import Counter
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Literal

from awa._internal.html import HtmlEvents
from awa.data._injection import guess_language, injection_flags, injection_signals
from awa.data._model import FileProfile, MailAttachment, MailInfo, MailMessage
from awa.data._parse import ParseContext

KINDS = ("mail",)

MAX_MESSAGES = 50
MAX_ATTACHMENTS = 20
EXCERPT_CHARS = 800
_MAX_BODY_CHARS = 2_000_000
_MAX_OPEN_TAGS = 500
_HEADERS = ("from", "to", "cc", "reply-to", "subject", "date", "message-id", "in-reply-to")
_SINGLE_HEADERS = ("from", "subject", "date", "message-id")
_FROM_LINE = re.compile(rb"From \S+ .*\d{4}")
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|pt|em|%)?\s*(?:;|$)"
    r"|opacity\s*:\s*0\s*(?:;|$)|mso-hide\s*:\s*all",
    re.IGNORECASE,
)
_BLOCKS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "table", "blockquote", "pre"})
_SKIPPED = frozenset({"script", "style", "head", "title"})
_DROPPED = frozenset({"script", "style"})
_VOID = frozenset({"br", "img", "hr", "meta", "link", "input", "area", "base", "col", "wbr"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BLANK_LINES = re.compile(r"\n\s*\n+")


def profile_mail(path: Path, ctx: ParseContext) -> FileProfile:
    """Profile a mail file: headers, bodies, attachments (recursively) and injection hints."""

    if ctx.profile.size == 0:
        return ctx.fail("DF-E-EMPTY", "the file is empty")
    total, raws = _raw_messages(path, ctx)
    info = ctx.profile.mail = MailInfo(messages_total=total, sampled=len(raws) < total)
    if ctx.embedded_budget is None:  # one budget for this mail and every mail attached to it
        ctx.embedded_budget = [MAX_ATTACHMENTS]
    budget = ctx.embedded_budget
    for index, raw in raws:
        ctx.check_deadline()
        message = message_from_bytes(raw, policy=policy.default)
        where = ctx.file_id if total == 1 else f"{ctx.file_id}.m{index}"
        info.messages.append(_message(message, index, where, ctx, budget))
    if info.sampled:
        ctx.profile.status = "partial"
        ctx.flag("DF-W-SAMPLED", f"{len(raws)} of {total} messages are profiled (an even sample)")
    if budget[0] < 0:
        ctx.profile.status = "partial"
        ctx.flag("DF-W-SAMPLED", f"only the first {MAX_ATTACHMENTS} attachments are profiled")
    return ctx.profile


def _message(
    message: EmailMessage, index: int, where: str, ctx: ParseContext, budget: list[int]
) -> MailMessage:
    limit = ctx.limits.max_chars_per_cell
    headers = {name: _clean(message[name], limit) for name in _HEADERS if message[name] is not None}
    plain: list[str] = []
    html: list[str] = []
    attachments: list[Any] = []
    for part in _leaves(message):
        is_text = part.get_content_type() in ("text/plain", "text/html")
        if is_text and not part.get_filename() and part.get_content_disposition() != "attachment":
            (html if part.get_content_type() == "text/html" else plain).append(_body_text(part))
        else:
            attachments.append(part)
    visible, hidden, comments = _html_text("\n".join(html)) if html else ("", "", "")
    readable = "\n".join(plain) if plain else visible
    readable = _BLANK_LINES.sub("\n\n", readable).strip()
    result = MailMessage(
        index=index,
        headers=headers,
        content_type=message.get_content_type(),
        body_kind=_body_kind(bool(plain), bool(html)),
        language=guess_language(readable),
        excerpt=readable[:EXCERPT_CHARS],
    )
    for part in attachments:
        result.attachments.append(_attachment(part, ctx, budget))
    defects = len(message.defects) + sum(
        len(message.get_all(name) or []) > 1 for name in _SINGLE_HEADERS
    )
    if defects:
        ctx.flag("DF-W-MAIL-HEADER-DEFECT", f"{defects} header or structure problems", where)
    names = " ".join(attachment.name for attachment in result.attachments)
    scanned = "\n".join([*headers.values(), readable, hidden, comments, names])
    flags = injection_flags(scanned, where)
    result.injection_suspected = any(flag.code == "DF-W-INJECTION-SUSPECTED" for flag in flags)
    ctx.profile.flags.extend(flags)
    if (
        hidden
        and injection_signals(hidden)
        and not any(f.code == "DF-W-HIDDEN-TEXT" for f in flags)
    ):
        ctx.flag("DF-W-HIDDEN-TEXT", "instructions are hidden with CSS in the HTML body", where)
    return result


def _body_kind(plain: bool, html: bool) -> Literal["plain", "html", "both", "none"]:
    if plain:
        return "both" if html else "plain"
    return "html" if html else "none"


def _attachment(part: Any, ctx: ParseContext, budget: list[int]) -> MailAttachment:
    limit = ctx.limits.max_chars_per_cell
    content_type = part.get_content_type()
    if content_type == "message/rfc822":
        try:
            data = part.get_payload(0).as_bytes(policy=policy.default)
        except (RecursionError, IndexError, ValueError, AttributeError):
            data = b""
    else:
        data = part.get_payload(decode=True) or b""
    suffix = ".eml" if content_type == "message/rfc822" else mimetypes.guess_extension(content_type)
    name = _clean(part.get_filename() or f"attachment{suffix or ''}", limit) or "attachment"
    attachment = MailAttachment(name=name, content_type=content_type, size=len(data))
    budget[0] -= 1
    if budget[0] >= 0 and data:
        child = ctx.child_profile(name, data)
        ctx.profile.children.append(child)
        attachment.file_id = child.id
    return attachment


class _Html(HtmlEvents):
    """Visible text, CSS-hidden text and comments of an HTML body; nothing is fetched or run."""

    def __init__(self) -> None:
        self.visible: list[str] = []
        self.hidden: list[str] = []
        self.comments: list[str] = []
        self._stack: list[tuple[str, bool]] = []
        self._open: Counter[str] = Counter()  # names on the stack: end tags cost O(1) to match
        self._hidden = 0  # entries on the stack that hide their content
        self._dropped = 0  # script and style entries: their text is not even kept as hidden

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCKS:
            self.visible.append("\n")
        if tag in _VOID:
            return
        values = dict(attrs)
        hidden = (
            tag in _SKIPPED
            or "hidden" in values
            or bool(_HIDDEN_STYLE.search(values.get("style") or ""))
        )
        if len(self._stack) < _MAX_OPEN_TAGS:
            self._stack.append((tag, hidden))
            self._open[tag] += 1
            self._hidden += hidden
            self._dropped += tag in _DROPPED

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCKS:
            self.visible.append("\n")
        if self._open[tag] > 0:
            while self._stack:
                name, hidden = self._stack.pop()
                self._open[name] -= 1
                self._hidden -= hidden
                self._dropped -= name in _DROPPED
                if name == tag:
                    break

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.visible.append(data)
        elif not self._dropped:
            self.hidden.append(data)

    def handle_comment(self, data: str) -> None:
        self.comments.append(f"<!--{data}-->")  # the injection rules know comments by their marks


def _html_text(html: str) -> tuple[str, str, str]:
    """`(visible text, hidden text, comments)` of an HTML body."""

    parser = _Html()
    parser.parse(html)
    return "".join(parser.visible), " ".join(parser.hidden), " ".join(parser.comments)


def _body_text(part: Any) -> str:
    payload = part.get_payload(decode=True) or b""
    try:
        return payload.decode(part.get_content_charset() or "utf-8", "replace")[:_MAX_BODY_CHARS]
    except LookupError:  # a charset name Python does not know
        return payload.decode("utf-8", "replace")[:_MAX_BODY_CHARS]


def _leaves(message: Any) -> list[Any]:
    """The parts that are not containers (a mail inside a mail counts as one part), in order."""

    found: list[Any] = []
    stack = [message]
    while stack:
        part = stack.pop()
        if part.is_multipart() and part.get_content_type() != "message/rfc822":
            stack.extend(reversed(list(part.iter_parts())))
        else:
            found.append(part)
    return found


def _message_starts(path: Path, ctx: ParseContext) -> list[int]:
    """Byte offsets where messages start: `From ` lines after a blank line, else the whole file."""

    starts: list[int] = []
    position, after_blank = 0, True
    with path.open("rb") as handle:
        for count, line in enumerate(handle):
            if after_blank and _FROM_LINE.match(line):
                starts.append(position)
            after_blank = not line.strip()
            position += len(line)
            if count % 100_000 == 0:
                ctx.check_deadline()
    return starts


def _raw_messages(path: Path, ctx: ParseContext) -> tuple[int, list[tuple[int, bytes]]]:
    """`(number of messages, [(1-based index, raw bytes)])` for an even sample of the file."""

    starts = _message_starts(path, ctx)
    size = path.stat().st_size
    if not starts or starts[0] > 0:  # no mailbox: the whole file is one message
        return 1, [(1, path.read_bytes())]
    stride = max(1, -(-len(starts) // MAX_MESSAGES))
    chosen: list[tuple[int, bytes]] = []
    with path.open("rb") as handle:
        for index in range(0, len(starts), stride):
            end = starts[index + 1] if index + 1 < len(starts) else size
            handle.seek(starts[index])
            raw = handle.read(end - starts[index])
            chosen.append((index + 1, raw.split(b"\n", 1)[1] if b"\n" in raw else b""))
    return len(starts), chosen


def _clean(value: object, limit: int) -> str:
    return " ".join(_CONTROL.sub(" ", str(value)).split())[:limit]
