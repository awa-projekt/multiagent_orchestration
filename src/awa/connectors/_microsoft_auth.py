"""Signing in to Microsoft 365 as a person: device code flow, refresh tokens and the token cache.

The cache is written under a cross-process lock, since Microsoft rotates the refresh token on use.
"""

from __future__ import annotations

import base64
import contextlib
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awa.connectors._engine import ConnectorContext, send
from awa.connectors._microsoft_errors import SIGN_IN_COMMAND, MicrosoftAuthError, explain
from awa.connectors._spec import ConnectorSpec, EnvVar
from awa.connectors.ports import Identity
from awa.core.dotenv import Environment, restrict_to_owner

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

CACHE_VERSION = 1
"""Format version of the token cache file; a file of another version is refused, not rewritten."""
DEFAULT_CACHE = ".awa/ms_token_cache.json"
DEFAULT_AUTHORITY = "https://login.microsoftonline.com"
DEFAULT_SCOPES = ("offline_access", "User.Read")
"""Permissions every sign-in asks for: a renewable sign-in and the user's own name."""

REFRESH_MARGIN = 300.0
"""Seconds before an access token ends at which it is renewed."""
_MAX_LIFETIME = 86_400.0  # no access token lives longer: a later `expires_at` is a clock error
_LOCK_WAIT = 60.0
_MAX_FILE = 1_000_000
_MAX_TOKEN = 16_384

_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_CLIENT = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_SCOPE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_USER_CODE = re.compile(r"^[A-Za-z0-9-]{4,32}$")
_HTTP_OPTIONS = frozenset(
    {"allow_private_networks", "allow_http", "timeout", "retries", "use_env_proxies"}
)
_SIGN_IN_HOSTS = (
    "microsoft.com",
    "*.microsoft.com",
    "*.microsoftonline.com",
    "*.microsoftonline.us",
    "*.microsoftonline.cn",
)
_PLAIN = {
    "offline_access": "stay signed in without asking again",
    "User.Read": "read the name of the signed-in user",
    "ChannelMessage.Send": "post messages in Teams channels",
    "ChannelMessage.Read.All": "read messages in Teams channels",
    "User.ReadBasic.All": "find colleagues by name and email address",
}

_now = time.time
_sleep = time.sleep


@dataclass(frozen=True)
class TokenClaims:
    """What an access token says about itself, read WITHOUT checking its signature.

    For display only (who is signed in, which permissions were granted): never base a decision on
    it. Missing or malformed claims are empty; `scopes` are delegated (`scp`), `roles` application
    permissions; `expires_at` is seconds since the epoch.
    """

    username: str | None = None
    tenant: str | None = None
    scopes: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    expires_at: float | None = None


def decode_claims(token: str) -> TokenClaims:
    """The claims of `token`, offline and unverified; never raises."""

    parts = token.split(".")
    if len(parts) != 3 or len(token) > _MAX_TOKEN:
        return TokenClaims()
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return TokenClaims()
    if not isinstance(payload, dict):
        return TokenClaims()

    def text(*keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value and len(value) <= 320 and value.isprintable():
                return value
        return None

    scopes = payload.get("scp")
    roles = payload.get("roles")
    expires = payload.get("exp")
    return TokenClaims(
        username=text("upn", "preferred_username", "unique_name", "email"),
        tenant=text("tid"),
        scopes=tuple(scopes.split()) if isinstance(scopes, str) else (),
        roles=tuple(r for r in roles if isinstance(r, str)) if isinstance(roles, list) else (),
        expires_at=_timestamp(expires),
    )


def _timestamp(value: object) -> float | None:
    """A plausible epoch time in seconds, or `None` (an int too large for a float is no time)."""

    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) and 0 < seconds < 1e12 else None


def cache_path(option: str | None = None, *, root: Path | None = None) -> Path:
    """Where the token cache lives: `option`, else `.awa/ms_token_cache.json` below `root`.

    A relative path must stay below `root`; any path must name a `.json` file that is not a link.

    Raises:
        MicrosoftAuthError: If the path is not acceptable.
    """

    text = option or DEFAULT_CACHE
    if "\x00" in text:
        raise MicrosoftAuthError("the token cache path contains a NUL character")
    path = Path(text)
    if ".." in path.parts:
        raise MicrosoftAuthError(f"the token cache path {text!r} must not contain '..'")
    if path.suffix.lower() != ".json":
        raise MicrosoftAuthError(f"the token cache path {text!r} must name a .json file")
    if not path.is_absolute():
        base = (root or Path.cwd()).resolve()
        path = base / path
        if not path.resolve(strict=False).is_relative_to(base):
            raise MicrosoftAuthError(f"the token cache path {text!r} leads out of {base}")
    if path.is_symlink() or path.is_dir():
        raise MicrosoftAuthError(f"the token cache path {text!r} is a link or a folder")
    return path


def _damaged(path: Path, reason: str) -> MicrosoftAuthError:
    return MicrosoftAuthError(
        f"The Microsoft sign-in file {path} cannot be used: {reason}. awa did not change it. "
        f"Move or delete it, then sign in again: {SIGN_IN_COMMAND}",
        "ms.cache_unusable",
    )


def _record_problem(record: Any) -> str | None:
    """Why a cache entry is damaged, or `None` when it is fine."""

    if not isinstance(record, dict):
        return "an entry is not an object"
    for key in ("client_id", "tenant", "username", "refresh_token", "access_token"):
        value = record.get(key)
        if not isinstance(value, str) or len(value) > _MAX_TOKEN:
            return f"entry field {key!r} is not text"
    if not record["client_id"] or not record["tenant"] or not record["refresh_token"]:
        return "an entry has no client, tenant or refresh token"
    scopes = record.get("scopes")
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        return "entry field 'scopes' is not a list of text"
    expires = record.get("expires_at")
    if not isinstance(expires, int | float) or isinstance(expires, bool):
        return "entry field 'expires_at' is not a number"
    return None


def _restrict(path: Path) -> None:
    """Make `path` readable by its owner only (best effort: on Windows it is an ACL)."""

    with contextlib.suppress(OSError):
        restrict_to_owner(path)


@contextlib.contextmanager
def _locked(path: Path, wait: float) -> Iterator[None]:
    """An exclusive lock on `path` across processes and threads; released when the process dies."""

    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                if sys.platform == "win32":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise MicrosoftAuthError(
                        "Another awa process is renewing the Microsoft sign-in and did not "
                        "finish. Try again in a minute.",
                        "ms.cache_busy",
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            if sys.platform == "win32":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


class TokenCache:
    """The sign-ins of this machine: `{"v": 1, "accounts": [...]}` in one JSON file.

    Reading never writes. `edit` changes the file under a lock shared by every awa process (waiting
    up to `wait` seconds), replaces it atomically and keeps it owner-readable only. A damaged, too
    large or foreign-version file is refused and never overwritten.
    """

    def __init__(self, path: Path, *, wait: float = _LOCK_WAIT) -> None:
        self.path = path
        self._wait = wait

    def __repr__(self) -> str:
        return f"TokenCache({str(self.path)!r})"

    def read(self) -> list[dict[str, Any]]:
        """The accounts (empty without a file).

        Raises:
            MicrosoftAuthError: If the file exists but cannot be used.
        """

        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise _damaged(self.path, f"it cannot be read ({type(exc).__name__})") from None
        if size > _MAX_FILE:
            raise _damaged(self.path, "it is unexpectedly large")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise _damaged(self.path, "it is not valid JSON") from None
        if not isinstance(document, dict) or "v" not in document:
            raise _damaged(self.path, "it is not a sign-in cache")
        if document["v"] != CACHE_VERSION:
            raise _damaged(self.path, f"it has format version {document['v']!r}, expected 1")
        accounts = document.get("accounts")
        if not isinstance(accounts, list):
            raise _damaged(self.path, "it has no list of accounts")
        for record in accounts:
            if (problem := _record_problem(record)) is not None:
                raise _damaged(self.path, problem)
        return accounts

    @contextlib.contextmanager
    def edit(self) -> Iterator[list[dict[str, Any]]]:
        """Lock the cache, yield its accounts for changes and save them when the block ends
        without an error.

        Raises:
            MicrosoftAuthError: If the file cannot be used or the lock is not granted.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(self.path.with_name(self.path.name + ".lock"), self._wait):
            accounts = self.read()
            yield accounts
            self._write(accounts)

    def _write(self, accounts: list[dict[str, Any]]) -> None:
        text = json.dumps({"v": CACHE_VERSION, "accounts": accounts}, indent=1)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            _restrict(temporary)
            for attempt in range(40):  # Windows refuses to replace a file someone is reading
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    if attempt == 39:
                        raise
                    time.sleep(0.05)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _find(
    accounts: Sequence[dict[str, Any]], tenant: str, client_id: str, username: str | None
) -> dict[str, Any]:
    """The one account of this app and tenant (`username` picks among several)."""

    found = [
        a
        for a in accounts
        if a["client_id"] == client_id
        and a["tenant"].lower() == tenant.lower()
        and (username is None or a["username"].lower() == username.lower())
    ]
    if not found:
        raise MicrosoftAuthError(
            f"Not signed in to Microsoft 365 (app {client_id}, tenant {tenant}). "
            f"Sign in first: {SIGN_IN_COMMAND}",
            "ms.not_signed_in",
        )
    if len(found) > 1:
        names = ", ".join(sorted(a["username"] or "?" for a in found))
        raise MicrosoftAuthError(
            f"Several Microsoft accounts are signed in ({names}). Set the connector option "
            "`account` to the one to use.",
            "ms.account_ambiguous",
        )
    return found[0]


def _authority(value: str) -> str:
    parts = urllib.parse.urlsplit(value.strip())
    if (
        parts.scheme not in ("https", "http")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path.strip("/")
    ):
        raise MicrosoftAuthError(
            "AZURE_AUTHORITY_HOST must be a plain host address such as "
            f"{DEFAULT_AUTHORITY} (no path, user name or query)."
        )
    return f"{parts.scheme}://{parts.netloc}"


def _identity(tenant: str | None, client_id: str | None) -> tuple[str, str]:
    if not tenant or not client_id:
        raise MicrosoftAuthError(
            "Microsoft sign-in needs AZURE_TENANT_ID and AZURE_CLIENT_ID (the app registration "
            f"that allows public client flows), then: {SIGN_IN_COMMAND}",
            "ms.not_configured",
        )
    if not _TENANT.fullmatch(tenant):
        raise MicrosoftAuthError("AZURE_TENANT_ID must be a tenant id or domain name.")
    if not _CLIENT.fullmatch(client_id):
        raise MicrosoftAuthError("AZURE_CLIENT_ID must be the application (client) id.")
    return tenant, client_id


def post_form(
    ctx: ConnectorContext, url: str, form: Mapping[str, str]
) -> tuple[int, dict[str, Any]]:
    """POST `form` to a sign-in endpoint without credentials; the status and the JSON object."""

    response = send(
        ctx,
        "POST",
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        body=urllib.parse.urlencode(form).encode(),
        authenticate=False,
    )
    try:
        data = json.loads(response.text())
    except ValueError:
        data = {}
    return response.status, data if isinstance(data, dict) else {}


def token_error(status: int, data: Mapping[str, Any], operation: str) -> MicrosoftAuthError:
    """The refused token request of `operation` in the words of `explain`."""

    advice = explain(status, data, service="entra", operation=operation)
    return MicrosoftAuthError(f"{advice.text} {advice.fix}", advice.code)


def _lifetime(data: Mapping[str, Any], token: str) -> float:
    given = data.get("expires_in")
    try:
        seconds = float(given) if given is not None else None
    except (TypeError, ValueError):
        seconds = None
    if seconds is None or not math.isfinite(seconds):
        claim = decode_claims(token).expires_at
        seconds = claim - _now() if claim is not None else 3600.0
    return min(max(seconds, 1.0), _MAX_LIFETIME)


def _record(
    data: Mapping[str, Any], base: Mapping[str, Any], tenant: str, client_id: str
) -> dict[str, Any]:
    """The cache entry after a successful token answer (`base`: the entry it replaces)."""

    access = data["access_token"]
    claims = decode_claims(access)
    refresh = data.get("refresh_token")
    scope = data.get("scope")
    return {
        **base,
        "client_id": client_id,
        "tenant": tenant,
        "username": claims.username or base.get("username") or "",
        "scopes": [s.rpartition("/")[2] for s in scope.split()]
        if isinstance(scope, str)
        else base["scopes"],
        "refresh_token": refresh if isinstance(refresh, str) and refresh else base["refresh_token"],
        "access_token": access,
        "expires_at": _now() + _lifetime(data, access),
        "updated_at": _now(),
    }


def _fresh(record: Mapping[str, Any]) -> bool:
    """Whether the entry's access token is usable now (not near its end, no impossible lifetime)."""

    expires, now = record["expires_at"], _now()
    return bool(record["access_token"]) and now + REFRESH_MARGIN < expires <= now + _MAX_LIFETIME


class UserTokenProvider:
    """The `TokenProvider` of a signed-in user: a cached access token, renewed before it ends.

    A renewal runs under the cache's cross-process lock and re-reads the file first, so the rotated
    refresh token is never lost. Settings come from the connector: `AZURE_TENANT_ID`,
    `AZURE_CLIENT_ID`, `AZURE_AUTHORITY_HOST` and the options `token_cache` and `account`.
    """

    kind: Identity = "user"

    def __init__(self, ctx: ConnectorContext) -> None:
        self._lock = threading.Lock()
        self._memory: tuple[str, float] | None = None

    def __repr__(self) -> str:
        return "UserTokenProvider()"

    def _where(self, ctx: ConnectorContext) -> tuple[str, str, str, TokenCache, str | None]:
        tenant, client_id = _identity(ctx.value("AZURE_TENANT_ID"), ctx.value("AZURE_CLIENT_ID"))
        authority = _authority(ctx.value("AZURE_AUTHORITY_HOST") or DEFAULT_AUTHORITY)
        cache = TokenCache(cache_path(ctx.option("token_cache")))
        account = ctx.option("account")
        return tenant, client_id, authority, cache, str(account) if account else None

    def token(self, ctx: ConnectorContext) -> str:
        """A valid access token.

        Raises:
            MicrosoftAuthError: Naming the next step when there is no sign-in, it has expired
                or was revoked, or the cache file cannot be used.
        """

        with self._lock:
            if self._memory is not None and self._memory[1] - REFRESH_MARGIN > _now():
                return self._memory[0]
            tenant, client_id, authority, cache, account = self._where(ctx)
            if _fresh(entry := _find(cache.read(), tenant, client_id, account)):
                return self._keep(entry)
            with cache.edit() as accounts:
                entry = _find(accounts, tenant, client_id, account)
                if not _fresh(entry):  # another process may have renewed it meanwhile
                    status, data = post_form(
                        ctx,
                        f"{authority}/{urllib.parse.quote(tenant, safe='')}/oauth2/v2.0/token",
                        {
                            "grant_type": "refresh_token",
                            "client_id": client_id,
                            "refresh_token": entry["refresh_token"],
                        },
                    )
                    if status != 200 or not isinstance(data.get("access_token"), str):
                        raise token_error(status, data, "refresh")
                    entry.update(_record(data, entry, tenant, client_id))
                return self._keep(entry)

    def _keep(self, entry: Mapping[str, Any]) -> str:
        self._memory = (entry["access_token"], entry["expires_at"])
        return str(entry["access_token"])

    def invalidate(self, ctx: ConnectorContext) -> None:
        """The service rejected the token: the next call renews it (unless another process
        already did)."""

        with self._lock:
            rejected = self._memory[0] if self._memory else None
            self._memory = None
            with contextlib.suppress(MicrosoftAuthError):
                tenant, client_id, _, cache, account = self._where(ctx)
                with cache.edit() as accounts:
                    entry = _find(accounts, tenant, client_id, account)
                    if entry["access_token"] == rejected:
                        entry["expires_at"] = 0


@dataclass(frozen=True)
class DeviceCodePrompt:
    """What the person must do to finish a sign-in, as data (a CLI prints `text()`).

    `verification_uri` is checked to be a Microsoft sign-in host; `cache` is the file the sign-in
    will be saved in.
    """

    user_code: str
    verification_uri: str
    expires_in: int
    scopes: tuple[str, ...]
    cache: str

    def text(self) -> str:
        """The instructions in plain words, including the warning against code phishing."""

        wanted = "\n".join(f"   - {_PLAIN.get(s, s)}" for s in self.scopes)
        return (
            f"Sign in to Microsoft 365 for awa\n"
            f"1. On any device open {self.verification_uri}\n"
            f"2. Enter the code {self.user_code}\n"
            f"3. Sign in with the account awa should act as (for Teams: the awa-bot user, not "
            f"your own admin account) and approve:\n{wanted}\n"
            f"The code works for {self.expires_in // 60} minutes. Only enter it if you started "
            f"this sign-in yourself just now. Never enter a code that someone sent you: it would "
            f"give that person access to the account.\n"
            f"awa keeps the sign-in in {self.cache}, readable by you only."
        )


@dataclass(frozen=True)
class SignedIn:
    """The result of `sign_in` (no token in it); `expires_at` is when the access token ends."""

    username: str
    tenant: str
    scopes: tuple[str, ...]
    expires_at: float
    cache: str


def _verification_uri(value: Any, authority: str) -> str:
    parts = urllib.parse.urlsplit(value) if isinstance(value, str) else None
    host = (parts.hostname or "").lower() if parts else ""
    ours = (urllib.parse.urlsplit(authority).hostname or "").lower()
    allowed = host == ours or any(
        host == h or (h.startswith("*.") and host.endswith(h[1:])) for h in _SIGN_IN_HOSTS
    )
    if parts is None or parts.username or parts.scheme not in ("https", "http") or not allowed:
        raise MicrosoftAuthError(
            "Microsoft answered with a sign-in address that is not a Microsoft page; nothing "
            "was shown. Check AZURE_AUTHORITY_HOST."
        )
    return str(value)


def _whole(value: Any, default: int, low: int, high: int) -> int:
    return min(max(value, low), high) if isinstance(value, int) and value > 0 else default


def sign_in(
    *,
    tenant: str,
    client_id: str,
    show: Callable[[DeviceCodePrompt], None],
    scopes: Sequence[str] = DEFAULT_SCOPES,
    authority: str = DEFAULT_AUTHORITY,
    token_cache: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> SignedIn:
    """Sign a user in with the device code flow and save the sign-in in the token cache.

    `show` is called once with the `DeviceCodePrompt`; the call then waits for the person to finish
    in the browser, polling at the pace Microsoft asks for.

    Args:
        client_id: Application (client) id; the app must allow public client flows.
        scopes: Permissions to ask for; `offline_access` is always added.
        authority: Sign-in host (`AZURE_AUTHORITY_HOST`).
        token_cache: Cache file; `.awa/ms_token_cache.json` when `None`.
        options: Connector HTTP options (`allow_private_networks`, `allow_http`, ...).

    Raises:
        MicrosoftAuthError: If Microsoft refuses (device code blocked, code expired, declined,
            public client flows off), the cache file is damaged, or the input is bad.
    """

    tenant, client_id = _identity(tenant, client_id)
    authority = _authority(authority)
    asked = tuple(dict.fromkeys((*scopes, "offline_access")))
    if not all(_SCOPE.fullmatch(s) for s in asked):
        raise MicrosoftAuthError("A permission name contains characters Microsoft does not use.")
    if unknown := set(options or {}) - _HTTP_OPTIONS:
        raise MicrosoftAuthError(
            f"sign_in takes only the HTTP options {', '.join(sorted(_HTTP_OPTIONS))}, "
            f"not {', '.join(sorted(unknown))}."
        )
    cache = TokenCache(cache_path(token_cache))
    cache.read()  # a damaged cache is found now, not after the person has signed in
    ctx = ConnectorContext(
        _SIGN_IN_SPEC,
        "microsoft_sign_in",
        Environment({"AZURE_AUTHORITY_HOST": authority}, dotenv=None),
        dict(options or {}),
    )
    base = f"{authority}/{urllib.parse.quote(tenant, safe='')}/oauth2/v2.0"
    begun = _now()
    status, started = post_form(
        ctx, f"{base}/devicecode", {"client_id": client_id, "scope": " ".join(asked)}
    )
    code = started.get("device_code")
    user_code = started.get("user_code")
    if status != 200 or not isinstance(code, str) or not code:
        raise token_error(status, started, "device_code")
    if not isinstance(user_code, str) or not _USER_CODE.fullmatch(user_code):
        raise MicrosoftAuthError("Microsoft answered without a usable sign-in code.")
    lifetime = _whole(started.get("expires_in"), 900, 30, 3600)
    interval = _whole(started.get("interval"), 5, 1, 60)
    show(
        DeviceCodePrompt(
            user_code,
            _verification_uri(started.get("verification_uri"), authority),
            lifetime,
            asked,
            str(cache.path),
        )
    )
    form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": client_id,
        "device_code": code,
    }
    data = _poll_token(ctx, f"{base}/token", form, interval, deadline=begun + lifetime)
    if not isinstance(data.get("refresh_token"), str) or not data["refresh_token"]:
        raise MicrosoftAuthError(
            "Microsoft gave no renewable sign-in; the app registration may not allow offline_access."
        )
    entry = _record(data, {"scopes": [], "refresh_token": "", "username": ""}, tenant, client_id)
    claims = decode_claims(entry["access_token"])
    with cache.edit() as accounts:
        accounts[:] = [a for a in accounts if not _same_account(a, entry)]
        accounts.append(entry)
    return SignedIn(
        entry["username"],
        tenant,
        claims.scopes or tuple(entry["scopes"]),
        entry["expires_at"],
        str(cache.path),
    )


def _poll_token(
    ctx: ConnectorContext, url: str, form: dict[str, str], interval: int, *, deadline: float
) -> dict[str, Any]:
    """Ask for the token at the pace Microsoft sets until the person finishes signing in."""

    while True:
        _sleep(interval)
        status, data = post_form(ctx, url, form)
        if status == 200 and isinstance(data.get("access_token"), str):
            return data
        problem = data.get("error")
        if problem == "slow_down":  # RFC 8628: wait 5 seconds longer from now on
            interval += 5
        elif problem != "authorization_pending":
            raise token_error(status, data, "device_code")
        if _now() >= deadline:
            raise token_error(400, {"error": "expired_token"}, "device_code")


def _same_account(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether two cache entries are the same person in the same tenant for the same app."""

    return bool(
        a["client_id"] == b["client_id"]
        and a["username"].lower() == b["username"].lower()
        and a["tenant"].lower() == b["tenant"].lower()
    )


_SIGN_IN_SPEC = ConnectorSpec(
    name="microsoft_sign_in",
    title="Microsoft sign-in",
    category="collaboration",
    summary="The sign-in host of Microsoft Entra ID.",
    base_url="{AZURE_AUTHORITY_HOST}",
    env=(
        EnvVar(
            "AZURE_AUTHORITY_HOST",
            "Sign-in host of Microsoft Entra ID.",
            secret=False,
            default=DEFAULT_AUTHORITY,
        ),
    ),
)
