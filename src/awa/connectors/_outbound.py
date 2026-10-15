"""The outbound HTML filter: what a model writes into a mail or chat message cannot leak data.

`clean_html` is deterministic, so the approved and the sent body share one `sha256`.
"""

from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from awa.connectors._engine import ConnectorContext

_KEPT_TAGS = frozenset(
    [
        "at",  # a Teams @mention
        "p",
        "br",
        "b",
        "strong",
        "i",
        "em",
        "u",
        "s",
        "strike",
        "ul",
        "ol",
        "li",
        "a",
        "img",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "caption",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "code",
        "span",
        "div",
        "hr",
        "sub",
        "sup",
        "small",
        "font",
        "center",
    ]
)
_DROPPED_WITH_CONTENT = frozenset(
    [
        "script",
        "style",
        "iframe",
        "frame",
        "frameset",
        "object",
        "embed",
        "noscript",
        "template",
        "svg",
        "math",
        "head",
        "title",
    ]
)
_VOID = frozenset(["br", "img", "hr"])
_BLOCKS = frozenset(
    [
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "hr",
        "table",
    ]
)
_ATTRIBUTES: Mapping[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "width", "height", "title"}),
    "td": frozenset({"colspan", "rowspan", "align", "valign"}),
    "th": frozenset({"colspan", "rowspan", "align", "valign"}),
    "font": frozenset({"color", "size", "face"}),
    "at": frozenset({"id"}),
}
_COMMON_ATTRIBUTES = frozenset({"style", "align", "dir"})
_STYLE_PROPERTIES = frozenset(  # what a mail body needs to look right; nothing that loads
    """
    color background-color font-family font-size font-weight font-style font-variant
    text-align text-decoration text-transform text-indent vertical-align line-height
    letter-spacing word-spacing white-space direction
    margin margin-top margin-right margin-bottom margin-left
    padding padding-top padding-right padding-bottom padding-left
    border border-top border-right border-bottom border-left border-color border-style
    border-width border-collapse border-spacing border-radius
    width height min-width max-width min-height max-height display
    """.split()  # noqa: SIM905 - a word list is easier to read and extend
)
_COLOR_FUNCTION = re.compile(r"\b(?:rgba?|hsla?)\(\s*[\d.,%\s]*\)", re.I)
_STYLE_VALUE = re.compile(r"[\w\s#.,%+'\"-]*")  # no parentheses, backslashes, colons or `@`
_LINK_UNSAFE = re.compile(r"[\\\x00-\x20\x7f]")


@dataclass(frozen=True)
class CleanedHtml:
    """The result of `clean_html`.

    `preview` is the body as plain text (links show their target); `hidden` lists what was removed,
    one line each; `sha256` is the hex SHA-256 of `html` (UTF-8).
    """

    html: str
    preview: str
    hidden: tuple[str, ...]
    sha256: str


def clean_html(text: str, allowed_hosts: Iterable[str] = ()) -> CleanedHtml:
    """Remove from the HTML `text` everything that loads, runs or links outside `allowed_hosts`.

    `allowed_hosts` are hosts without scheme, subdomains included.
    """

    parser = _Cleaner(frozenset(h.strip().lower().lstrip(".") for h in allowed_hosts if h))
    parser.feed(text)
    parser.close()
    body = "".join(parser.out)
    preview = re.sub(r"\n{3,}", "\n\n", "".join(parser.text)).strip()
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return CleanedHtml(body, preview, tuple(parser.hidden), digest)


@dataclass
class _State:
    skipping: int = 0
    open_links: list[str | None] = field(default_factory=list)


class _Cleaner(HTMLParser):
    def __init__(self, allowed: frozenset[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.allowed = allowed
        self.out: list[str] = []
        self.text: list[str] = []
        self.hidden: list[str] = []
        self.state = _State()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closed=True)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED_WITH_CONTENT:
            self.state.skipping = max(0, self.state.skipping - 1)
            return
        if self.state.skipping or tag not in _KEPT_TAGS or tag in _VOID:
            return
        if tag == "a" and self.state.open_links:
            target = self.state.open_links.pop()
            if target:
                self.text.append(f" ({target})")
        self.out.append(f"</{tag}>")
        if tag in _BLOCKS:
            self.text.append("\n")

    def handle_data(self, data: str) -> None:
        if self.state.skipping:
            return
        self.out.append(html.escape(data, quote=False))
        self.text.append(data)

    def handle_comment(self, data: str) -> None:
        if data.strip():
            self.hidden.append("comment")

    def handle_decl(self, decl: str) -> None:
        return None

    def unknown_decl(self, data: str) -> None:
        return None

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, closed: bool) -> None:
        if tag in _DROPPED_WITH_CONTENT:
            self.hidden.append(f"<{tag}> element")
            if not closed:
                self.state.skipping += 1
            return
        if self.state.skipping:
            return
        if tag not in _KEPT_TAGS:
            if tag in {"link", "meta", "base", "form", "input", "button", "video", "audio"}:
                self.hidden.append(f"<{tag}> element")
            return
        kept: list[tuple[str, str]] = []
        for name, value in attrs:
            value = value or ""
            if (
                name.startswith("on")
                or name == "srcset"
                or name not in (_ATTRIBUTES.get(tag, frozenset()) | _COMMON_ATTRIBUTES)
            ):
                if name.startswith("on") or name in {"srcset", "background", "formaction"}:
                    self.hidden.append(f"{name} attribute on <{tag}>")
                continue
            if name == "style":
                value, dropped = _clean_style(value)
                if dropped:
                    self.hidden.append(f"style {_shown(dropped)} on <{tag}>")
                if not value:
                    continue
            kept.append((name, value))
        if tag == "img":
            source = dict(kept).get("src", "")
            if not _inline_image(source):
                self.hidden.append(f"remote image {_shown(source)}")
                return
        if tag == "a":
            target = dict(kept).get("href")
            if target is not None and not self._link_allowed(target):
                self.hidden.append(f"link to {_shown(target)}")
                kept = [(n, v) for n, v in kept if n != "href"]
                target = None
            self.state.open_links.append(target)
        rendered = "".join(f' {n}="{html.escape(v, quote=True)}"' for n, v in kept)
        self.out.append(f"<{tag}{rendered}>")
        if tag in _BLOCKS:
            self.text.append("\n")

    def _link_allowed(self, target: str) -> bool:
        target = target.strip()
        parts = urllib.parse.urlsplit(target)
        scheme = parts.scheme.lower()
        if scheme == "mailto":
            return True
        if scheme not in {"http", "https"}:
            return False
        before_query = target.partition("?")[0].partition("#")[0]
        if _LINK_UNSAFE.search(target) or "@" in before_query or "%" in parts.netloc:
            return False  # a backslash, control character or user part reads differently elsewhere
        host = (parts.hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.allowed)


def _clean_style(style: str) -> tuple[str, str]:
    """The declarations of `style` that are on the allow-list, and the first one removed."""

    kept: list[str] = []
    dropped = ""
    for declaration in style.split(";"):
        if not declaration.strip():
            continue
        name, colon, value = declaration.partition(":")
        name, value = name.strip().lower(), value.strip()
        plain = _COLOR_FUNCTION.sub("0", value)
        if colon and name in _STYLE_PROPERTIES and _STYLE_VALUE.fullmatch(plain):
            kept.append(f"{name}: {value}")
        elif not dropped:
            dropped = declaration.strip()
    return "; ".join(kept), dropped


def _inline_image(source: str) -> bool:
    lowered = source.strip().lower()
    return lowered.startswith("cid:") or lowered.startswith("data:image/")


def _shown(url: str) -> str:
    """A removed address as the approver sees it: scheme, host and path, no query."""

    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme in {"http", "https"} and parts.hostname:
        return f"{parts.scheme}://{parts.hostname}{parts.path}"[:200]
    return (url.strip()[:60] or "(empty)") + ("..." if len(url.strip()) > 60 else "")


HtmlBody = Callable[[Mapping[str, Any]], str | None]
"""`(arguments) -> name of the argument holding an HTML body`, or `None` for a plain body."""


def link_hosts(ctx: ConnectorContext) -> tuple[str, ...]:
    """The hosts links may point to: `allowed_link_hosts`, and the mail `allowed_domains`."""

    hosts: list[str] = []
    for option in ("allowed_link_hosts", "allowed_domains"):
        raw = ctx.option(option) or ()
        values = raw.split(",") if isinstance(raw, str) else list(raw)
        hosts.extend(str(v).strip().lstrip("@") for v in values if str(v).strip())
    return tuple(hosts)


def cleaned_arguments(
    ctx: ConnectorContext, html_body: HtmlBody | None, args: Mapping[str, Any]
) -> tuple[Mapping[str, Any], CleanedHtml | None]:
    """`args` with their HTML body cleaned (unchanged without one), and the cleaning result."""

    name = html_body(args) if html_body is not None else None
    if name is None or not isinstance(args.get(name), str):
        return args, None
    cleaned = clean_html(str(args[name]), link_hosts(ctx))
    return {**args, name: cleaned.html}, cleaned


def review_payload(
    ctx: ConnectorContext, html_body: HtmlBody | None
) -> Callable[[str, dict[str, Any]], dict[str, Any]] | None:
    """An approval payload builder that adds the outbound review of an HTML body, or `None`."""

    if html_body is None:
        return None

    def build(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        from awa.tools.approval import default_approval_payload

        payload = default_approval_payload(tool_name, args)
        _, cleaned = cleaned_arguments(ctx, html_body, args)
        if cleaned is not None:
            payload["outbound"] = {
                "preview": cleaned.preview,
                "hidden": list(cleaned.hidden),
                "sha256": cleaned.sha256,
            }
        return payload

    return build
