"""A small, defensive HTTP client for the web tools (stdlib `urllib` only).

Requests and redirects are checked against a host allowlist; private addresses, plain remote
`http://` and odd URLs are refused unless allowed; bodies are capped; secret headers are dropped
on cross-host redirects; a resolved address is pinned, so DNS rebinding cannot reach a private
address.
"""

from __future__ import annotations

import contextlib
import functools
import http.client
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from email.message import Message
from http.client import HTTPMessage
from typing import Any

from awa._internal.upstream.tools import ToolException
from awa.errors import ConfigurationError

_UNSAFE_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")
_HOST_PATTERN = re.compile(
    r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$"
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_MAX_REDIRECTS = 5


class Refused(ToolException):
    """The policy refused the request before anything was sent, so it is never worth retrying."""


def normalize_hosts(hosts: Sequence[str] | None) -> tuple[str, ...] | None:
    """Validate an allowlist: exact hosts (`api.example.com`, `10.0.0.5`) or `*.example.com`.

    Raises:
        ConfigurationError: If an entry is not a host name, IP address or wildcard.
    """

    if hosts is None:
        return None
    if isinstance(hosts, str):
        hosts = [hosts]
    result: list[str] = []
    for host in hosts:
        entry = str(host).strip().lower().rstrip(".")
        if entry.startswith("[") and entry.endswith("]"):
            entry = entry[1:-1]
        if not (_is_ip(entry) or _HOST_PATTERN.fullmatch(entry)):
            raise ConfigurationError(
                f"allowed host {host!r} is not a host name, an IP address or '*.domain'"
            )
        result.append(entry)
    return tuple(result)


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def host_allowed(host: str, allowed: Sequence[str] | None) -> bool:
    """Return whether `host` matches the allowlist (`None` allows every host)."""

    if allowed is None:
        return True
    host = host.lower().rstrip(".")
    for entry in allowed:
        if entry.startswith("*."):
            if host.endswith(entry[1:]) and len(host) > len(entry) - 1:
                return True
        elif host == entry:
            return True
    return False


@dataclass(frozen=True)
class HttpPolicy:
    """Where requests may go and how much they may return."""

    allowed_hosts: tuple[str, ...] | None
    allow_private_networks: bool = False
    allow_http: bool = False
    timeout: float = 20.0
    max_response_bytes: int = 2_000_000
    user_agent: str = "awa-toolkits/1"
    use_env_proxies: bool = False
    max_seconds: float | None = None  # wall-clock limit of one request; None: 3 x `timeout`

    @property
    def deadline(self) -> float:
        """Seconds one whole request may take: `timeout` bounds each network read only."""

        return self.max_seconds if self.max_seconds is not None else self.timeout * 3

    def check_url(self, url: str) -> urllib.parse.SplitResult:
        """Parse `url` and refuse it unless the policy allows it.

        Raises:
            ToolException: If the URL is malformed or not allowed.
        """

        if _UNSAFE_URL_CHARS.search(url):
            raise Refused("the URL contains spaces, control characters or backslashes")
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise Refused(f"invalid URL: {exc}") from None
        if parts.scheme not in ("http", "https"):
            raise Refused("only http:// and https:// URLs are allowed")
        if "@" in parts.netloc or parts.username is not None:
            raise Refused("URLs with user names or passwords are not allowed")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise Refused("the URL has no host")
        if not host_allowed(host, self.allowed_hosts):
            raise Refused(f"host {host!r} is not in the allowed hosts")
        if parts.scheme == "http" and not self.allow_http and host not in _LOCAL_HOSTS:
            raise Refused("plain http:// is only allowed for the local machine; use https://")
        if not self.allow_private_networks:
            self._check_addresses(host, port or (443 if parts.scheme == "https" else 80))
        return parts

    @staticmethod
    def _check_addresses(host: str, port: int) -> list[str]:
        """The addresses of `host`, all of which must be public."""

        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError) as exc:
            raise Refused(f"cannot resolve host {host!r}: {exc}") from None
        found: list[str] = []
        for info in infos:
            address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
            address = _embedded_ipv4(address)
            if (
                not address.is_global
                or address.is_multicast
                or getattr(address, "is_site_local", False)
            ):
                raise Refused(
                    f"host {host!r} resolves to a private or reserved address; requests to "
                    "internal networks are blocked (allow_private_networks)"
                )
            found.append(str(info[4][0]))
        return found


def _embedded_ipv4(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """The IPv4 address an IPv6 one carries (mapped, compatible, 6to4, Teredo, NAT64), else `address`."""

    if not isinstance(address, ipaddress.IPv6Address):
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    if int(address) >> 32 == 0 and int(address) > 1:  # IPv4-compatible (::a.b.c.d), not :: or ::1
        return ipaddress.IPv4Address(int(address))
    if address in _NAT64:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


@dataclass(frozen=True)
class HttpRequest:
    """One request a connector sends; header values and the body are hidden from `repr`."""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes | None = field(default=None, repr=False)

    def header(self, name: str) -> str | None:
        """The value of header `name` (any case), or `None`."""

        lowered = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == lowered), None)

    def json(self) -> Any:
        """The body decoded as JSON (`None` without a body)."""

        return json.loads(self.body) if self.body else None


@dataclass(frozen=True)
class HttpResponse:
    """A response read by `fetch`."""

    url: str
    status: int
    reason: str
    headers: Message
    body: bytes
    truncated: bool

    @property
    def content_type(self) -> str:
        """The media type of the body, without parameters (`text/html`)."""

        return self.headers.get_content_type()

    def text(self) -> str:
        """The body decoded with its declared charset (UTF-8 when none or unknown)."""

        charset = self.headers.get_content_charset() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """The body decoded as JSON."""

        return json.loads(self.text())


def _public_connection(address: tuple[str, int], *args: Any, **kwargs: Any) -> socket.socket:
    """Connect to a checked public address of `address[0]` (resolved here, once)."""

    host, port = address
    failure: OSError | None = None
    for ip in HttpPolicy._check_addresses(host, port):
        try:
            return socket.create_connection((ip, port), *args, **kwargs)
        except OSError as exc:
            failure = exc
    raise failure or OSError(f"no address for {host}")


class _Deadline:
    """Shuts the request's sockets down once the policy's total time is over."""

    def __init__(self, seconds: float) -> None:
        self.expired = False
        self._sockets: list[socket.socket] = []
        self._lock = threading.Lock()
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def watch(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.append(sock)
            if self.expired:
                _shut(sock)

    def stop(self) -> None:
        self._timer.cancel()

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            for sock in self._sockets:
                _shut(sock)


def _shut(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


class _WatchedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args: Any, deadline: _Deadline, public: bool, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._deadline = deadline
        if public:
            self._create_connection = _public_connection

    def connect(self) -> None:
        super().connect()
        self._deadline.watch(self.sock)


class _WatchedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, deadline: _Deadline, public: bool, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._deadline = deadline
        if public:
            self._create_connection = _public_connection

    def connect(self) -> None:
        super().connect()
        self._deadline.watch(self.sock)


class _WatchedHTTP(urllib.request.HTTPHandler):
    def __init__(self, deadline: _Deadline, public: bool) -> None:
        super().__init__()
        self._connection = functools.partial(
            _WatchedHTTPConnection, deadline=deadline, public=public
        )

    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(self._connection, req)


class _WatchedHTTPS(urllib.request.HTTPSHandler):
    def __init__(self, deadline: _Deadline, public: bool) -> None:
        super().__init__()
        self._connection = functools.partial(
            _WatchedHTTPSConnection, deadline=deadline, public=public
        )

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(self._connection, req)


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects the policy allows; a hop to another scheme or host drops secrets."""

    max_redirections = _MAX_REDIRECTS

    def __init__(self, policy: HttpPolicy, secret_headers: frozenset[str]) -> None:
        super().__init__()
        self.policy = policy
        self.secret_headers = secret_headers

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        target = urllib.parse.urljoin(req.full_url, newurl)
        self.policy.check_url(target)
        new = super().redirect_request(req, fp, code, msg, headers, target)
        same_origin = urllib.parse.urlsplit(target)[:2] == urllib.parse.urlsplit(req.full_url)[:2]
        if new is not None and not same_origin:
            dropped = self.secret_headers | {"authorization", "cookie"}
            for name in [name for name in new.headers if name.lower() in dropped]:
                del new.headers[name]
        return new


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Hand a redirect back as a response, so the caller decides whether to follow it."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def fetch(
    policy: HttpPolicy,
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    secret_headers: frozenset[str] = frozenset(),
    data: bytes | None = None,
    follow_redirects: bool = True,
) -> HttpResponse:
    """Send one request under `policy`; HTTP error statuses are returned, not raised.

    With `follow_redirects=False` a 3xx answer is returned as it is, so a caller can drop
    credentials on the next hop.

    Raises:
        ToolException: If the URL is not allowed, the request fails or times out.
    """

    started = time.monotonic()
    policy.check_url(url)
    request = urllib.request.Request(_ascii_url(url), data=data, method=method)
    request.add_header("User-Agent", policy.user_agent)
    for name, value in (headers or {}).items():
        if "\n" in value or "\r" in value:
            raise Refused(f"header {name!r} contains a line break")
        if not _is_latin1(value):
            raise Refused(f"header {name!r} contains characters HTTP headers cannot carry")
        request.add_header(name, value)
    deadline = _Deadline(policy.deadline - (time.monotonic() - started))
    public = not policy.use_env_proxies and not policy.allow_private_networks
    handlers: list[urllib.request.BaseHandler] = [
        _Redirects(policy, secret_headers) if follow_redirects else _NoRedirects(),
        _WatchedHTTP(deadline, public),
        _WatchedHTTPS(deadline, public),
    ]
    if not policy.use_env_proxies:
        handlers.append(urllib.request.ProxyHandler({}))
    try:
        response = _open(urllib.request.build_opener(*handlers), request, policy)
        with response:
            body = _read_body(response, policy, started)
        if deadline.expired:  # the shut socket ended the body early
            raise ToolException(_too_long(policy))
    except ToolException:
        if deadline.expired:
            raise ToolException(_too_long(policy)) from None
        raise
    finally:
        deadline.stop()
    return HttpResponse(
        url=response.geturl(),
        status=int(response.status or 0),
        reason=str(response.reason or ""),
        headers=response.headers,
        body=body[: policy.max_response_bytes],
        truncated=len(body) > policy.max_response_bytes,
    )


def _too_long(policy: HttpPolicy) -> str:
    return f"the response took longer than {policy.deadline:g} seconds in all"


def _open(
    opener: urllib.request.OpenerDirector, request: urllib.request.Request, policy: HttpPolicy
) -> Any:
    """Open `request`; an HTTP error status is returned as the response, any other failure raises `ToolException`."""

    try:
        return opener.open(request, timeout=policy.timeout)
    except urllib.error.HTTPError as exc:
        return exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise ToolException(f"the request timed out after {policy.timeout:g} seconds") from None
        if isinstance(reason, ToolException):
            raise reason from None
        raise ToolException(f"the request failed: {reason}") from None
    except TimeoutError:
        raise ToolException(f"the request timed out after {policy.timeout:g} seconds") from None
    except http.client.HTTPException as exc:
        raise ToolException(f"the response was malformed: {type(exc).__name__}") from None
    except (OSError, UnicodeError) as exc:  # a reset or TLS failure after the request was sent
        raise ToolException(f"the request failed: {type(exc).__name__}") from None


def _is_latin1(value: str) -> bool:
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def _ascii_url(url: str) -> str:
    """`url` with an IDNA host and a percent-encoded path, query and fragment (existing `%XX` kept)."""

    if url.isascii():
        return url
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise Refused(f"invalid host name {host!r}") from None
    if ":" in host:
        host = f"[{host}]"
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    safe = "/%:@!$&'()*+,;=~-._?"
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            netloc,
            urllib.parse.quote(parts.path, safe=safe),
            urllib.parse.quote(parts.query, safe=safe),
            urllib.parse.quote(parts.fragment, safe=safe),
        )
    )


def _read_body(response: Any, policy: HttpPolicy, started: float) -> bytes:
    """Read up to `max_response_bytes` (plus one) within the policy's total time."""

    chunks: list[bytes] = []
    size = 0
    while size <= policy.max_response_bytes:
        if time.monotonic() - started > policy.deadline:
            raise ToolException(_too_long(policy))
        try:  # one network read at a time: a slow server cannot stretch a single call
            chunk = response.read1(min(65_536, policy.max_response_bytes + 1 - size))
        except TimeoutError:
            raise ToolException(
                f"reading the response timed out after {policy.timeout:g} seconds"
            ) from None
        except http.client.HTTPException as exc:
            raise ToolException(f"the response was malformed: {type(exc).__name__}") from None
        except OSError as exc:
            raise ToolException(f"reading the response failed: {type(exc).__name__}") from None
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)
