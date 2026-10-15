"""Communication tools: `send_email` (SMTP), `post_webhook` and `render_template`.

Sending waits for approval by default. Servers, credentials and webhook URLs come from
environment variables read at call time and are kept out of descriptions, results and logs; the
sender is configured, and recipients can be limited to an allowlist.
"""

from __future__ import annotations

import json
import re
import smtplib
import ssl
import string
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from typing import Any, Literal

from pydantic import Field, SecretStr

from awa._internal.http import HttpPolicy, fetch, normalize_hosts
from awa._internal.upstream.tools import BaseTool, ToolException
from awa.core.config import env_value as project_value
from awa.errors import ConfigurationError
from awa.toolkits._common import (
    ToolArgs,
    build_tool,
    env_value,
    header_values,
    positive,
    secret_value,
)
from awa.tools.risk import ToolRisk

SmtpSecurity = Literal["starttls", "ssl", "none"]
"""How `send_email` protects the SMTP connection."""

EmailTransport = Callable[[EmailMessage], None]
"""Delivers a finished message; replaces SMTP (for tests or an email API)."""

_ADDRESS = re.compile(
    r"^[^@\s<>()\[\],;:\"]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}


def _address(value: str, what: str) -> str:
    if "\n" in value or "\r" in value:
        raise ToolException(f"{what} contains a line break")
    _, address = parseaddr(value)
    if not address or not _ADDRESS.fullmatch(address):
        raise ToolException(f"{what} {value!r} is not a valid email address")
    return address


class SendEmailArgs(ToolArgs):
    to: list[str] = Field(min_length=1, description="Recipient addresses.")
    subject: str = Field(min_length=1, max_length=300, description="Subject line.")
    body: str = Field(description="Plain-text message body.")
    cc: list[str] | None = Field(default=None, description="Copy recipients.")
    reply_to: str | None = Field(default=None, description="Address replies should go to.")
    html_body: str | None = Field(default=None, description="Optional HTML version of the body.")


@dataclass(frozen=True)
class _Email:
    host: str | None
    port: int | None
    host_env: str
    port_env: str
    username_env: str
    password_env: str
    password: SecretStr | None
    username: str | None
    sender: str | None
    sender_env: str
    security: SmtpSecurity
    allowed: tuple[str, ...] | None
    max_recipients: int
    max_body_chars: int
    timeout: float
    transport: EmailTransport | None

    def _permitted(self, address: str) -> bool:
        if self.allowed is None:
            return True
        lowered = address.lower()
        domain = lowered.rpartition("@")[2]
        for entry in self.allowed:
            if entry.startswith("@"):
                if domain == entry[1:]:
                    return True
            elif "@" in entry:
                if lowered == entry:
                    return True
            elif domain == entry or domain.endswith(f".{entry}"):
                return True
        return False

    def message(self, args: SendEmailArgs) -> EmailMessage:
        recipients = [_address(value, "recipient") for value in args.to]
        copies = [_address(value, "cc recipient") for value in args.cc or ()]
        everyone = recipients + copies
        if len(everyone) > self.max_recipients:
            raise ToolException(f"at most {self.max_recipients} recipients are allowed")
        refused = [address for address in everyone if not self._permitted(address)]
        if refused:
            raise ToolException(f"recipient(s) not allowed: {', '.join(refused)}")
        if len(args.body) + len(args.html_body or "") > self.max_body_chars:
            raise ToolException(f"the message is longer than {self.max_body_chars} characters")
        if "\n" in args.subject or "\r" in args.subject:
            raise ToolException("the subject contains a line break")
        sender = self.sender or env_value(self.sender_env, what="the sender address")
        message = EmailMessage()
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        if copies:
            message["Cc"] = ", ".join(copies)
        if args.reply_to:
            message["Reply-To"] = _address(args.reply_to, "reply_to")
        message["Subject"] = args.subject
        domain = parseaddr(sender)[1].rpartition("@")[2] or None
        message["Message-ID"] = make_msgid(domain=domain)
        message.set_content(args.body)
        if args.html_body:
            message.add_alternative(args.html_body, subtype="html")
        return message

    def send(self, args: SendEmailArgs) -> str:
        message = self.message(args)
        (self.transport or self._smtp)(message)
        recipients = message["To"] + (f", {message['Cc']}" if message["Cc"] else "")
        return (
            f"Sent the email {args.subject!r} to {recipients} (Message-ID {message['Message-ID']})."
        )

    def _smtp(self, message: EmailMessage) -> None:
        host = self.host or env_value(self.host_env, what="the SMTP server")
        port_text = str(self.port) if self.port is not None else None
        if port_text is None:
            port_text = project_value(self.port_env) or str(_DEFAULT_PORTS[self.security])
        try:
            port = int(port_text)
        except ValueError:
            raise ToolException(
                f"environment variable {self.port_env} is not a port number"
            ) from None
        username = self.username or project_value(self.username_env) or None
        password = secret_value(self.password) or project_value(self.password_env) or None
        check_smtp_security(self.security, host, username)
        context = ssl.create_default_context()
        try:
            server: smtplib.SMTP = (
                smtplib.SMTP_SSL(host, port, timeout=self.timeout, context=context)
                if self.security == "ssl"
                else smtplib.SMTP(host, port, timeout=self.timeout)
            )
            with server:
                if self.security == "starttls":
                    server.starttls(context=context)
                if username:
                    server.login(username, password or "")
                server.send_message(message)
        except smtplib.SMTPAuthenticationError:
            raise ToolException("the SMTP server rejected the credentials") from None
        except smtplib.SMTPRecipientsRefused as exc:
            raise ToolException(
                f"the SMTP server refused the recipient(s) {', '.join(exc.recipients)}"
            ) from None
        except (smtplib.SMTPException, OSError) as exc:
            raise ToolException(f"sending failed: {type(exc).__name__}: {exc}") from None


def check_smtp_security(security: str, host: str, username: str | None) -> None:
    """Refuse an unknown `security` and a login without TLS to a host that is not local.

    Raises:
        ToolException: If either applies.
    """

    if security not in _DEFAULT_PORTS:
        raise ToolException(f"security must be starttls, ssl or none, not {security!r}")
    if username and security == "none" and host.lower() not in _LOCAL_HOSTS:
        raise ToolException("refusing to send SMTP credentials without TLS (security='none')")


def email_toolkit(
    *,
    sender: str | None = None,
    host: str | None = None,
    port: int | None = None,
    security: SmtpSecurity = "starttls",
    allowed_recipients: Sequence[str] | None = None,
    host_env: str = "SMTP_HOST",
    port_env: str = "SMTP_PORT",
    username_env: str = "SMTP_USERNAME",
    password_env: str = "SMTP_PASSWORD",
    sender_env: str = "SMTP_SENDER",
    password: SecretStr | None = None,
    username: str | None = None,
    max_recipients: int = 20,
    max_body_chars: int = 100_000,
    timeout: float = 30.0,
    requires_approval: bool = True,
    transport: EmailTransport | None = None,
) -> list[BaseTool]:
    """Build `send_email`, which sends a plain-text (optionally HTML) email over SMTP.

    Every connection setting is taken from its argument, else from the named environment variable
    at call time (`host_env`, `port_env`, `username_env`, `password_env`, `sender_env`); without a
    user name there is no login.

    Args:
        sender: The From address (`"Support <support@example.com>"`).
        port: SMTP port; default 587 (`starttls`), 465 (`ssl`) or 25 (`none`).
        security: `"starttls"` (default), `"ssl"` (implicit TLS) or `"none"` (only for a local
            relay; credentials are refused).
        allowed_recipients: Addresses (`a@example.com`), exact domains (`@example.com`) or
            domains with subdomains (`example.com`); `None` allows any recipient.
        password: The password as a `SecretStr`, instead of `password_env`.
        transport: Deliver messages with this function instead of SMTP.

    Raises:
        ConfigurationError: If `security` is unknown, `sender` is not an address, or a limit is
            not positive.

    Example:
        ```python
        [send] = email_toolkit(sender="Billing <billing@example.com>",
                               allowed_recipients=["example.com"])
        ```
    """

    if security not in _DEFAULT_PORTS:
        raise ConfigurationError(f"security must be starttls, ssl or none, not {security!r}")
    positive(max_recipients, "max_recipients")
    positive(max_body_chars, "max_body_chars")
    positive(timeout, "timeout")
    if sender is not None:
        try:
            _address(sender, "sender")
        except ToolException as exc:
            raise ConfigurationError(str(exc)) from None
    allowed = (
        tuple(entry.strip().lower() for entry in allowed_recipients)
        if allowed_recipients is not None
        else None
    )
    impl = _Email(
        host=host,
        port=port,
        host_env=host_env,
        port_env=port_env,
        username_env=username_env,
        password_env=password_env,
        password=password,
        username=username,
        sender=formataddr(parseaddr(sender)) if sender else None,
        sender_env=sender_env,
        security=security,
        allowed=allowed,
        max_recipients=max_recipients,
        max_body_chars=max_body_chars,
        timeout=timeout,
        transport=transport,
    )
    where = f" Recipients must be in: {', '.join(allowed)}." if allowed else ""
    description = (
        "Send an email (plain text, optionally with an HTML version) from the "
        f"configured sender address.{where}"
        + (" Needs human approval." if requires_approval else "")
    )
    return [
        build_tool(
            impl.send,
            name="send_email",
            description=description,
            args_schema=SendEmailArgs,
            toolkit="email",
            risk=ToolRisk(untrusted_input=False, private_data=False, external_action=True),
            requires_approval=requires_approval,
        )
    ]


class PostWebhookArgs(ToolArgs):
    text: str | None = Field(
        default=None, description='A message; sent as {"text": ...} when payload is omitted.'
    )
    payload: dict[str, Any] | None = Field(
        default=None, description="The complete JSON payload, for services that need one."
    )


@dataclass(frozen=True)
class _Webhook:
    url: str | None
    url_env: str | None
    headers: Mapping[str, str] | None
    headers_env: Mapping[str, str] | None
    timeout: float
    allow_private_networks: bool
    allow_http: bool
    max_payload_bytes: int

    def post(self, args: PostWebhookArgs) -> str:
        if (args.text is None) == (args.payload is None):
            raise ToolException("give either text or payload")
        payload = args.payload if args.payload is not None else {"text": args.text}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(data) > self.max_payload_bytes:
            raise ToolException(f"the payload is larger than {self.max_payload_bytes} bytes")
        url = self.url or env_value(str(self.url_env), what="the webhook URL")
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        policy = HttpPolicy(
            allowed_hosts=normalize_hosts([host]) if host else (),
            allow_private_networks=self.allow_private_networks,
            allow_http=self.allow_http,
            timeout=self.timeout,
            max_response_bytes=10_000,
        )
        headers, secret = header_values(self.headers, self.headers_env)
        headers["Content-Type"] = "application/json"
        try:
            response = fetch(
                policy, url, method="POST", headers=headers, secret_headers=secret, data=data
            )
        except ToolException as exc:
            # The URL can hold a token: never repeat it.
            raise ToolException(str(exc).replace(url, "<webhook URL>")) from None
        if response.status >= 400:
            detail = response.text()[:300]
            raise ToolException(f"the webhook answered HTTP {response.status}: {detail}")
        return f"Posted to the webhook (HTTP {response.status})."


def webhook_toolkit(
    *,
    url: str | None = None,
    url_env: str | None = None,
    headers: Mapping[str, str] | None = None,
    headers_env: Mapping[str, str] | None = None,
    name: str = "post_webhook",
    description: str | None = None,
    timeout: float = 15.0,
    max_payload_bytes: int = 100_000,
    requires_approval: bool = True,
    allow_private_networks: bool = False,
    allow_http: bool = False,
) -> list[BaseTool]:
    """Build a tool that posts JSON to one webhook (chat channels, automation services).

    Args:
        url: The webhook URL, when it holds no secret.
        url_env: Environment variable holding the URL, read at call time (preferred: chat
            webhook URLs usually contain a token).
        headers: Headers sent with every post, when they hold no secret.
        headers_env: Secret headers: header name -> environment variable name.
        description: Tool description; say where messages go.
        allow_private_networks: Allow a webhook on an internal network.

    Raises:
        ConfigurationError: If neither or both of `url` and `url_env` are given, or `url` is not
            an http(s) URL.

    Example:
        ```python
        [notify] = webhook_toolkit(url_env="OPS_WEBHOOK_URL", name="notify_ops",
                                   description="Post a short status message to the ops channel.")
        ```
    """

    if (url is None) == (url_env is None):
        raise ConfigurationError("webhook_toolkit needs either url or url_env")
    if url is not None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigurationError("the webhook url is not an http(s) URL")
    positive(timeout, "timeout")
    positive(max_payload_bytes, "max_payload_bytes")
    impl = _Webhook(
        url=url,
        url_env=url_env,
        headers=headers,
        headers_env=headers_env,
        timeout=timeout,
        allow_private_networks=allow_private_networks,
        allow_http=allow_http,
        max_payload_bytes=max_payload_bytes,
    )
    text = description or (
        "Post a message (text) or a JSON payload to the configured webhook, such as a chat "
        "channel or an automation service."
    )
    if requires_approval:
        text += " Needs human approval."
    return [
        build_tool(
            impl.post,
            name=name,
            description=text,
            args_schema=PostWebhookArgs,
            toolkit="webhook",
            risk=ToolRisk(untrusted_input=False, private_data=False, external_action=True),
            requires_approval=requires_approval,
        )
    ]


class RenderTemplateArgs(ToolArgs):
    template: str | None = Field(
        default=None, description="Template text with $name or ${name} placeholders."
    )
    name: str | None = Field(default=None, description="Name of a configured template.")
    values: dict[str, Any] = Field(default_factory=dict, description="Placeholder values.")
    strict: bool = Field(default=True, description="Fail when a placeholder has no value.")


class _Templates:
    def __init__(self, templates: Mapping[str, str]) -> None:
        self.templates = dict(templates)

    def render(self, args: RenderTemplateArgs) -> str:
        if (args.template is None) == (args.name is None):
            raise ToolException("give either template or name")
        if args.name is not None:
            if args.name not in self.templates:
                known = ", ".join(sorted(self.templates)) or "none"
                raise ToolException(f"unknown template {args.name!r} (known: {known})")
            text = self.templates[args.name]
        else:
            text = str(args.template)
        template = string.Template(text)
        values = {key: "" if value is None else str(value) for key, value in args.values.items()}
        if args.strict:
            missing = [name for name in template.get_identifiers() if name not in values]
            if missing:
                raise ToolException(f"no value for placeholder(s): {', '.join(missing)}")
            if not template.is_valid():
                raise ToolException("the template has an invalid placeholder; write $$ for '$'")
            return template.substitute(values)
        return template.safe_substitute(values)


def template_toolkit(templates: Mapping[str, str] | None = None) -> list[BaseTool]:
    """Build `render_template`, which fills `$name` placeholders in a text.

    Placeholders follow `string.Template` (`$name`, `${name}`, `$$`). Nothing in a template is
    evaluated, so templates written by anyone are safe to render. `templates` holds named texts
    the model can render by `name`.
    """

    impl = _Templates(templates or {})
    names = ", ".join(sorted(impl.templates))
    description = (
        "Fill the $name / ${name} placeholders of a template with values and return the text. "
        "Use it to produce messages and documents from fixed wording."
    )
    if names:
        description += f" Configured templates: {names}."
    return [
        build_tool(
            impl.render,
            name="render_template",
            description=description,
            args_schema=RenderTemplateArgs,
            toolkit="templates",
            risk=ToolRisk(untrusted_input=False, private_data=False, external_action=False),
        )
    ]


__all__ = ["EmailTransport", "SmtpSecurity", "email_toolkit", "template_toolkit", "webhook_toolkit"]
