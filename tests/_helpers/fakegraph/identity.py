"""The Entra ID side of `FakeGraph`: app registrations, users and the OAuth token endpoints.

Endpoints (`{tenant}` is the tenant id, its domain, or `common`/`organizations` where Entra allows):

- `POST /{tenant}/oauth2/v2.0/token` with `grant_type` `client_credentials`, `refresh_token` or
  `urn:ietf:params:oauth:grant-type:device_code`
- `POST /{tenant}/oauth2/v2.0/devicecode`

Errors use Entra's body (`error`, `error_description` with an `AADSTS` code, `error_codes`).
Access tokens are JWTs with the claims a real v1 Graph token carries (`aud`, `iss`, `tid`, `appid`,
`roles` for app tokens, `scp` and `upn` for user tokens, `iat`, `nbf`, `exp`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

from _helpers.fakegraph.core import (
    GRAPH_AUDIENCE,
    Ctx,
    GraphError,
    Plugin,
    Reply,
    TokenError,
    new_guid,
    unb64url,
)

plugin = Plugin("identity")

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
OIDC_SCOPES = frozenset({"openid", "profile", "offline_access", "email"})
REFRESH_IDLE_DAYS = 90

R_SECRET_WRONG = plugin.rule(
    "entra.client_secret_wrong",
    "A wrong client secret is refused with 401 invalid_client AADSTS7000215.",
    source="learn.microsoft.com/entra/identity-platform/reference-error-codes (AADSTS7000215)",
    source_date="2026-10-05",
)
R_SECRET_EXPIRED = plugin.rule(
    "entra.client_secret_expired",
    "An expired client secret is refused with 401 invalid_client AADSTS7000222.",
    source="learn.microsoft.com/entra/identity-platform/reference-error-codes (AADSTS7000222)",
    source_date="2026-10-05",
)
R_DEFAULT_SCOPE = plugin.rule(
    "entra.client_credentials_need_default_scope",
    "The client-credentials grant needs scope `<resource>/.default`; anything else is "
    "400 invalid_scope AADSTS1002012.",
    source="learn.microsoft.com/entra/identity-platform/v2-oauth2-client-creds-grant-flow",
    source_date="2026-10-05",
)
R_ROTATION = plugin.rule(
    "entra.refresh_token_rotates",
    "Every refresh returns a new refresh token; with strict rotation the old one is refused "
    "(Entra itself does not revoke it; strictness proves a client stores the new one).",
    source="learn.microsoft.com/entra/identity-platform/refresh-tokens",
    source_date="2026-06-15",
)
R_REVOKED = plugin.rule(
    "entra.refresh_token_revoked",
    "A revoked refresh token is refused with invalid_grant AADSTS50173.",
    source="learn.microsoft.com/entra/identity-platform/refresh-tokens",
    source_date="2026-06-15",
)
R_IDLE = plugin.rule(
    "entra.refresh_token_idle_90_days",
    "A refresh token unused for 90 days is refused with invalid_grant AADSTS70008.",
    source="learn.microsoft.com/entra/identity-platform/refresh-tokens",
    source_date="2026-06-15",
)
R_DEVICE_PENDING = plugin.rule(
    "entra.device_code_pending",
    "Polling a device code before sign-in answers authorization_pending (AADSTS70016); an expired "
    "code answers expired_token (AADSTS70019).",
    source="learn.microsoft.com/entra/identity-platform/v2-oauth2-device-code",
    source_date="2026-10-05",
)
R_PUBLIC_CLIENT = plugin.rule(
    "entra.device_code_needs_public_client",
    "Device code and secret-less refresh need 'Allow public client flows'; a confidential app "
    "without a secret gets 400 invalid_client AADSTS7000218.",
    source="learn.microsoft.com/entra/identity-platform/reference-error-codes (AADSTS7000218)",
    source_date="2026-10-05",
)
R_CA_BLOCK = plugin.rule(
    "entra.device_code_blocked_by_conditional_access",
    "A tenant that blocks the device code flow lets the poll fail with interaction_required "
    "AADSTS53003.",
    source="learn.microsoft.com/entra/identity/conditional-access/policy-block-authentication-flows",
    source_date="2026-04-07",
)
R_CONSENT = plugin.rule(
    "entra.delegated_scope_needs_consent",
    "A delegated scope the app was not consented for is refused with AADSTS65001.",
    source="learn.microsoft.com/entra/identity-platform/reference-error-codes (AADSTS65001)",
    source_date="2026-10-05",
)
R_FORM = plugin.rule(
    "entra.token_request_is_a_form",
    "The token endpoint reads form fields; a missing grant_type is AADSTS900144 and a GET is "
    "AADSTS900561.",
    source="learn.microsoft.com/entra/identity-platform/reference-error-codes",
    source_date="2026-10-05",
)
R_TENANT = plugin.rule(
    "entra.unknown_tenant",
    "An unknown tenant in the token URL is 400 invalid_request AADSTS90002.",
    source="learn.microsoft.com/entra/identity-platform/reference-error-codes (AADSTS90002)",
    source_date="2026-10-05",
)
R_ASSERTION = plugin.rule(
    "entra.client_assertion_certificate",
    "A certificate credential is a JWT client assertion (client_assertion_type "
    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer) with header alg PS256 and x5t#S256 "
    "(base64url SHA-256 of the certificate's DER) and claims aud (the token endpoint), iss = sub "
    "= client id, jti, nbf and exp. An unknown thumbprint, a bad signature or an expired "
    "certificate is 401 invalid_client AADSTS700027; an assertion outside its time is "
    "AADSTS700024; a reused jti is refused.",
    source="learn.microsoft.com/entra/identity-platform/certificate-credentials",
    source_date="2026-06-15",
)
R_RESOURCE = plugin.rule(
    "entra.unknown_resource",
    "A scope for an unknown resource is 400 invalid_resource AADSTS500011.",
    source="learn.microsoft.com/entra/identity-platform/reference-error-codes (AADSTS500011)",
    source_date="2026-10-05",
)


@dataclass
class App:
    """An app registration.

    Attributes:
        client_id: Application (client) id.
        secret: The client secret value (`None`: a public client).
        roles: Application permissions with admin consent (the `roles` claim). Exchange RBAC
            grants are NOT roles: see `rbac`.
        scopes: Delegated permissions the app may be consented for (the `scp` claim).
        public_client: "Allow public client flows" (device code, secret-less refresh).
        name: Display name.
        secret_expires: When the secret stops working.
        rbac: Exchange "RBAC for Applications" assignments: role name (`Application Mail.Read`)
            to the mailboxes it is scoped to. Exchange keeps them; they are not part of the token.
        certificates: The DER bytes of the certificates uploaded to the app (Certificates &
            secrets, Certificates); their thumbprints identify a client assertion.
    """

    client_id: str
    secret: str | None = None
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    public_client: bool = False
    name: str = "awa"
    secret_expires: datetime | None = None
    rbac: dict[str, set[str]] = field(default_factory=dict)
    object_id: str = ""
    certificates: list[bytes] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.object_id = self.object_id or new_guid(f"sp:{self.client_id}")


@dataclass
class User:
    """A user (and, for a licensed one, a mailbox)."""

    upn: str
    name: str
    id: str = ""

    def __post_init__(self) -> None:
        self.id = self.id or new_guid(f"user:{self.upn}")


@dataclass
class _Refresh:
    client_id: str
    user_id: str
    scopes: frozenset[str]
    last_used: datetime
    valid: bool = True
    revoked: bool = False


@dataclass
class _Device:
    client_id: str
    scopes: frozenset[str]
    user_code: str
    expires: datetime
    interval: int
    pending: int
    last_poll: datetime | None = None


@dataclass
class IdentityStore:
    """Mutable behavior switches and issued grants of the token endpoint."""

    refresh_tokens: dict[str, _Refresh] = field(default_factory=dict)
    devices: dict[str, _Device] = field(default_factory=dict)
    strict_rotation: bool = True
    device_pending_polls: int = 2
    enforce_device_interval: bool = False
    block_device_code: bool = False
    device_user: str | None = None
    extra_resources: set[str] = field(default_factory=lambda: {"https://vault.azure.net"})
    tenant_aliases: set[str] = field(default_factory=lambda: {"common", "organizations"})
    used_assertions: set[str] = field(default_factory=set)
    device_interval: int = 5


@plugin.setup
def _setup(graph: Any) -> IdentityStore:
    return IdentityStore()


# --------------------------------------------------------------------------- helpers


def resolve_user(ctx: Ctx, reference: str, *, family: str = "outlook") -> User:
    """The user a `/users/{id}` or `/me` path names.

    `/me` needs a delegated token (an app token has no user): 400 as Graph answers. A delegated
    token reaches another user only with a `.Shared` scope, an app token any user.
    """

    graph, principal = ctx.graph, ctx.principal
    if reference.lower() == "me":
        if principal is None or principal.kind != "user":
            raise GraphError(
                400, "BadRequest", "/me request is only valid with delegated authentication flow."
            )
        return graph.user(principal.user_id or "")
    found = graph.find_user(reference)
    if found is None:
        if family == "outlook":
            raise GraphError(
                404, "ErrorInvalidUser", f"The requested user '{reference}' is invalid."
            )
        raise GraphError(
            404,
            "Request_ResourceNotFound",
            f"Resource '{reference}' does not exist or one of its queried reference-property "
            "objects are not present.",
        )
    return found


# --------------------------------------------------------------------------- token endpoint


def _form(ctx: Ctx) -> dict[str, str]:
    parsed = parse_qs(ctx.request.body.decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


def _need(form: dict[str, str], *names: str) -> None:
    for name in names:
        if not form.get(name):
            raise TokenError(
                400,
                "invalid_request",
                900144,
                f"The request body must contain the following parameter: '{name}'.",
            )


def _check_tenant(ctx: Ctx) -> str:
    graph = ctx.graph
    tenant = ctx.params["tenant"]
    store: IdentityStore = ctx.store
    if tenant.lower() in {graph.tenant_id.lower(), graph.tenant_domain.lower()}:
        return graph.tenant_id
    if tenant.lower() in store.tenant_aliases:
        return graph.tenant_id
    raise TokenError(
        400,
        "invalid_request",
        90002,
        f"Tenant '{tenant}' not found. Check to make sure you have the correct tenant ID and "
        "are signing into the correct cloud. Check with your subscription administrator, this "
        "may happen if there are no active subscriptions for the tenant.",
    )


def _resource(ctx: Ctx, scopes: list[str], tenant: str) -> tuple[str, list[str]]:
    """The audience of the requested scopes and the bare permission names."""

    store: IdentityStore = ctx.store
    audience, names = GRAPH_AUDIENCE, []
    for scope in scopes:
        if "://" in scope:
            parts = urlsplit(scope)
            resource, _, permission = scope.rpartition("/")
            if resource != GRAPH_AUDIENCE and resource not in store.extra_resources:
                raise TokenError(
                    400,
                    "invalid_resource",
                    500011,
                    f"The resource principal named {resource} was not found in the tenant named "
                    f"{tenant}. This can happen if the application has not been installed by the "
                    "administrator of the tenant or consented to by any user in the tenant.",
                )
            audience = (
                f"{parts.scheme}://{parts.netloc}" if resource != GRAPH_AUDIENCE else resource
            )
            names.append(permission)
        else:
            names.append(scope)
    return audience, names


def _lookup_app(ctx: Ctx, client_id: str, tenant: str) -> App:
    app = ctx.graph.apps.get(client_id)
    if app is None:
        raise TokenError(
            401,
            "unauthorized_client",
            700016,
            f"Application with identifier '{client_id}' was not found in the directory "
            f"'{tenant}'. This can happen if the application has not been installed by the "
            "administrator of the tenant or consented to by any user in the tenant. You may have "
            "sent your authentication request to the wrong tenant.",
        )
    return app


def _check_secret(ctx: Ctx, app: App, secret: str | None) -> None:
    if secret is None:
        raise TokenError(
            400,
            "invalid_client",
            7000218,
            "The request body must contain the following parameter: 'client_assertion' or "
            "'client_secret'.",
        )
    if app.secret != secret:
        raise TokenError(
            401,
            "invalid_client",
            7000215,
            "Invalid client secret provided. Ensure the secret being sent in the request is the "
            f"client secret value, not the client secret ID, for a secret added to app "
            f"'{app.client_id}'.",
        )
    if app.secret_expires is not None and ctx.clock.now() >= app.secret_expires:
        raise TokenError(
            401,
            "invalid_client",
            7000222,
            f"The provided client secret keys for app '{app.client_id}' are expired. Visit the "
            "Azure portal to create new keys for your app: https://aka.ms/NewClientSecret, or "
            "consider using certificate credentials for added security: https://aka.ms/certCreds.",
        )


_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


def _rejected(text: str, code: int = 700027) -> TokenError:
    return TokenError(401, "invalid_client", code, text)


def _check_assertion(ctx: Ctx, app: App, form: dict[str, str]) -> None:
    """Verify a certificate client assertion as Entra does (see `verify_assertion`)."""

    if form.get("client_assertion_type") != _ASSERTION_TYPE:
        raise TokenError(
            400,
            "invalid_request",
            50027,
            "Invalid client_assertion_type: expected "
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer.",
        )
    store: IdentityStore = ctx.store
    audiences = {
        f"{ctx.base}/{name}/oauth2/v2.0/token"
        for name in (ctx.params["tenant"], ctx.graph.tenant_id, ctx.graph.tenant_domain)
    }
    verify_assertion(
        form["client_assertion"],
        client_id=app.client_id,
        certificates=app.certificates,
        audiences=audiences,
        now=ctx.clock.now(),
        used=store.used_assertions,
    )


def verify_assertion(
    assertion: str,
    *,
    client_id: str,
    certificates: list[bytes],
    audiences: set[str],
    now: datetime,
    used: set[str],
) -> dict[str, Any]:
    """Check a client assertion the way the Entra token endpoint does; returns its claims.

    Needs the `cryptography` package (awa's `m365` extra installs it). A `jti` is accepted
    once; it is added to `used`.

    Raises:
        TokenError: 400 for a malformed token; 401 AADSTS700027 for an unknown thumbprint, a bad
            signature, an expired certificate or wrong claims; 401 AADSTS700024 for an assertion
            outside its time or a reused `jti`.
    """

    parts = assertion.split(".")
    try:
        header, claims = (json.loads(unb64url(part)) for part in parts[:2])
        signature = unb64url(parts[2])
    except (ValueError, IndexError):
        raise TokenError(
            400, "invalid_request", 50027, "Invalid JWT token. The token is malformed."
        ) from None
    if header.get("alg") != "PS256" or not isinstance(header.get("x5t#S256"), str):
        raise _rejected("The client assertion must be signed with PS256 and carry x5t#S256.")
    certificate = next(
        (
            der
            for der in certificates
            if base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()
            == header["x5t#S256"].rstrip("=")
        ),
        None,
    )
    if certificate is None:
        raise _rejected(
            f"Client assertion failed signature validation: no certificate with thumbprint "
            f"{header['x5t#S256']} is registered on application '{client_id}'."
        )
    try:
        from cryptography import x509
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:  # pragma: no cover - the m365 extra installs it
        raise _rejected("FakeGraph needs the cryptography package to check certificates.") from None
    loaded = x509.load_der_x509_certificate(certificate)
    if now >= loaded.not_valid_after_utc:
        raise _rejected(
            f"The certificate with identifier used to sign the client assertion is expired on "
            f"application '{client_id}'."
        )
    key = loaded.public_key()
    if not isinstance(key, rsa.RSAPublicKey):
        raise _rejected("Only RSA certificates are supported.")
    try:
        key.verify(
            signature,
            f"{parts[0]}.{parts[1]}".encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )
    except InvalidSignature:
        raise _rejected("Client assertion failed signature validation.") from None
    if claims.get("aud") not in audiences:
        raise _rejected("The audience claim of the client assertion is not this token endpoint.")
    if claims.get("iss") != client_id or claims.get("sub") != client_id:
        raise _rejected("The iss and sub claims of the client assertion must be the client id.")
    expires, begins = claims.get("exp"), claims.get("nbf", claims.get("iat"))
    if not isinstance(expires, int | float) or not isinstance(begins, int | float):
        raise _rejected("The client assertion needs exp and nbf (or iat) claims.", 700024)
    if not begins - 300 <= now.timestamp() < expires:
        raise _rejected("Client assertion is not within its valid time range.", 700024)
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti or jti in used:
        raise _rejected("The client assertion's jti is missing or was already used.", 700024)
    used.add(jti)
    return dict(claims)


def _consent_error(app: App, missing: set[str]) -> TokenError:
    return TokenError(
        400,
        "invalid_grant",
        65001,
        f"The user or administrator has not consented to use the application with ID "
        f"'{app.client_id}' named '{app.name}' for {', '.join(sorted(missing))}. Send an "
        "interactive authorization request for this user and resource.",
    )


def _token_reply(
    ctx: Ctx, token: str, *, scopes: frozenset[str] | None = None, refresh: str | None = None
) -> Reply:
    lifetime = ctx.graph.token_lifetime
    body: dict[str, Any] = {
        "token_type": "Bearer",
        "expires_in": lifetime,
        "ext_expires_in": lifetime,
        "access_token": token,
    }
    if scopes is not None:
        body["scope"] = " ".join(
            f"{GRAPH_AUDIENCE}/{s}" if s not in OIDC_SCOPES else s for s in sorted(scopes)
        )
    if refresh is not None:
        body["refresh_token"] = refresh
    return Reply(
        200, body, {"Cache-Control": "no-store, no-cache"}, "application/json; charset=utf-8"
    )


@plugin.route("POST", "/{tenant}/oauth2/v2.0/token", auth="none")
def token_endpoint(ctx: Ctx) -> Reply:
    try:
        return _token(ctx)
    except TokenError as error:
        return error.to_reply(ctx.clock)


@plugin.route("GET", "/{tenant}/oauth2/v2.0/token", auth="none")
def token_endpoint_get(ctx: Ctx) -> Reply:
    return TokenError(
        405,
        "invalid_request",
        900561,
        "The endpoint only accepts POST requests. Received a GET request.",
    ).to_reply(ctx.clock)


def _token(ctx: Ctx) -> Reply:
    tenant = _check_tenant(ctx)
    form = _form(ctx)
    _need(form, "grant_type")
    grant = form["grant_type"]
    if grant == "client_credentials":
        return _client_credentials(ctx, form, tenant)
    if grant == "refresh_token":
        return _refresh(ctx, form, tenant)
    if grant == DEVICE_GRANT:
        return _device_poll(ctx, form, tenant)
    raise TokenError(
        400,
        "unsupported_grant_type",
        70003,
        f"The app is configured with a grant type that is not supported: '{grant}'.",
    )


def _client_credentials(ctx: Ctx, form: dict[str, str], tenant: str) -> Reply:
    _need(form, "client_id", "scope")
    app = _lookup_app(ctx, form["client_id"], tenant)
    if "client_assertion" in form:
        _check_assertion(ctx, app, form)
    else:
        _check_secret(ctx, app, form.get("client_secret"))
    scopes = form["scope"].split()
    if len(scopes) != 1 or not scopes[0].endswith("/.default"):
        raise TokenError(
            400,
            "invalid_scope",
            1002012,
            f"The provided value for scope {form['scope']} is not valid. Client credential flows "
            "must have a scope value with /.default suffixed to the resource identifier "
            "(application ID URI).",
        )
    audience, _ = _resource(ctx, scopes, tenant)
    token = ctx.graph.issue_token(app, audience=audience, tenant=tenant)
    return _token_reply(ctx, token)


def _grant_scopes(app: App, requested: set[str]) -> frozenset[str]:
    """The delegated permissions of a token: what was asked for, if the app is consented."""

    permissions = {s for s in requested if s not in OIDC_SCOPES}
    if ".default" in permissions:
        permissions = set(app.scopes)
    missing = permissions - set(app.scopes)
    if missing:
        raise _consent_error(app, missing)
    return frozenset(permissions | (requested & OIDC_SCOPES))


def _issue_user_tokens(
    ctx: Ctx, app: App, user: User, scopes: frozenset[str], tenant: str
) -> Reply:
    store: IdentityStore = ctx.store
    token = ctx.graph.issue_token(
        app,
        kind="user",
        scopes=sorted(s for s in scopes if s not in OIDC_SCOPES),
        user=user,
        tenant=tenant,
    )
    refresh = None
    if "offline_access" in scopes:
        refresh = "1." + secrets.token_urlsafe(48)
        store.refresh_tokens[refresh] = _Refresh(app.client_id, user.id, scopes, ctx.clock.now())
    return _token_reply(ctx, token, scopes=scopes, refresh=refresh)


def _refresh(ctx: Ctx, form: dict[str, str], tenant: str) -> Reply:
    _need(form, "client_id", "refresh_token")
    store: IdentityStore = ctx.store
    app = _lookup_app(ctx, form["client_id"], tenant)
    if not app.public_client or form.get("client_secret") is not None:
        _check_secret(ctx, app, form.get("client_secret"))
    record = store.refresh_tokens.get(form["refresh_token"])
    if record is None or not record.valid:
        raise TokenError(
            400,
            "invalid_grant",
            70008,
            "The provided authorization code or refresh token has expired due to inactivity. "
            "Send a new interactive authorization request for this user and resource.",
        )
    if record.revoked:
        raise TokenError(
            400,
            "invalid_grant",
            50173,
            "The provided grant has expired due to it being revoked, a fresh auth token is "
            "needed. The user might have changed or reset their password.",
        )
    if record.client_id != app.client_id:
        raise TokenError(
            400,
            "invalid_grant",
            70000,
            "The provided value for the 'refresh_token' was issued to a different client.",
        )
    if ctx.clock.now() - record.last_used > timedelta(days=REFRESH_IDLE_DAYS):
        raise TokenError(
            400,
            "invalid_grant",
            70008,
            "The provided authorization code or refresh token has expired due to inactivity. "
            "Send a new interactive authorization request for this user and resource.",
        )
    requested = set(form["scope"].split()) if form.get("scope") else set(record.scopes)
    granted = _grant_scopes(app, requested)
    if not {s for s in granted if s not in OIDC_SCOPES} <= {
        s for s in record.scopes if s not in OIDC_SCOPES
    }:
        raise _consent_error(app, set(granted) - set(record.scopes))
    if store.strict_rotation:
        record.valid = False
    record.last_used = ctx.clock.now()
    return _issue_user_tokens(
        ctx, app, ctx.graph.user(record.user_id), granted | {"offline_access"}, tenant
    )


# --------------------------------------------------------------------------- device code


@plugin.route("POST", "/{tenant}/oauth2/v2.0/devicecode", auth="none")
def device_code_endpoint(ctx: Ctx) -> Reply:
    try:
        tenant = _check_tenant(ctx)
        form = _form(ctx)
        _need(form, "client_id")
        app = _lookup_app(ctx, form["client_id"], tenant)
        if not app.public_client:
            raise TokenError(
                400,
                "invalid_client",
                7000218,
                "The request body must contain the following parameter: 'client_assertion' or "
                "'client_secret'.",
            )
        store: IdentityStore = ctx.store
        requested = set(form.get("scope", "openid").split())
        _resource(ctx, [s for s in requested if "://" in s], tenant)
        scopes = _grant_scopes(app, requested)
        code = secrets.token_urlsafe(96)
        user_code = secrets.token_hex(5).upper()[:9]
        device = _Device(
            app.client_id,
            scopes,
            user_code,
            ctx.clock.now() + timedelta(seconds=900),
            store.device_interval,
            store.device_pending_polls,
        )
        store.devices[code] = device
        return Reply(
            200,
            {
                "user_code": user_code,
                "device_code": code,
                "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 900,
                "interval": device.interval,
                "message": f"To sign in, use a web browser to open the page "
                f"https://microsoft.com/devicelogin and enter the code {user_code} to authenticate.",
            },
            content_type="application/json; charset=utf-8",
        )
    except TokenError as error:
        return error.to_reply(ctx.clock)


def _device_poll(ctx: Ctx, form: dict[str, str], tenant: str) -> Reply:
    _need(form, "client_id", "device_code")
    store: IdentityStore = ctx.store
    app = _lookup_app(ctx, form["client_id"], tenant)
    device = store.devices.get(form["device_code"])
    if device is None or device.client_id != app.client_id:
        raise TokenError(
            400,
            "bad_verification_code",
            70000,
            "The device code is not valid for this application.",
        )
    now = ctx.clock.now()
    if now >= device.expires:
        raise TokenError(
            400, "expired_token", 70019, "Verification code expired. Restart the device flow."
        )
    polled_too_soon = (
        store.enforce_device_interval
        and device.last_poll is not None
        and (now - device.last_poll).total_seconds() < device.interval
    )
    device.last_poll = now
    if polled_too_soon:
        raise TokenError(
            400, "slow_down", 70016, "The app is polling too quickly. Increase the interval."
        )
    if device.pending > 0:
        device.pending -= 1
        raise TokenError(
            400,
            "authorization_pending",
            70016,
            "OAuth 2.0 device flow error. Authorization is pending. Continue polling.",
        )
    if store.block_device_code:
        raise TokenError(
            400,
            "interaction_required",
            53003,
            "Access has been blocked by Conditional Access policies. The access policy does not "
            "allow token issuance.",
        )
    if store.device_user:
        user = ctx.graph.find_user(store.device_user)
    else:
        user = next(iter(ctx.graph.users.values()), None)
    if user is None:
        raise TokenError(400, "access_denied", 50034, "The signed-in user does not exist.")
    del store.devices[form["device_code"]]
    return _issue_user_tokens(ctx, app, user, device.scopes, tenant)


def revoke_refresh_tokens(store: IdentityStore) -> None:
    """Revoke every refresh token (an admin's "revoke sessions" or a password reset)."""

    for record in store.refresh_tokens.values():
        record.revoked = True
