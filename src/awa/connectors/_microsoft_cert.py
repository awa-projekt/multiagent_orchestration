"""Certificate sign-in for the Microsoft connectors: a PS256 client assertion instead of a secret.

Password, key and assertion never reach a message or a log.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awa.connectors._engine import ConnectorContext
from awa.connectors._microsoft_auth import post_form, token_error
from awa.connectors._microsoft_errors import MicrosoftAuthError
from awa.connectors._saas_microsoft import CERTIFICATE_PASSWORD, CERTIFICATE_PATH
from awa.connectors.ports import Identity

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ASSERTION_LIFETIME = 600
"""Seconds an assertion is valid after `nbf` (Microsoft: keep it short, 5 to 10 minutes)."""
INSTALL = 'pip install "awa-agents[m365]"'
"""The command that installs what certificate sign-in needs."""
_MAX_FILE = 100_000
_REFRESH_MARGIN = 300.0

_now = time.time


@dataclass(frozen=True)
class LoadedCertificate:
    """A certificate with its private key, read from a file.

    `thumbprint` is the `x5t#S256` (base64url SHA-256 of the DER bytes); `sha1` the hex thumbprint
    the Entra admin center lists; `key` is never printed.
    """

    thumbprint: str
    sha1: str
    not_after: datetime
    subject: str
    key: RSAPrivateKey

    def __repr__(self) -> str:
        return f"LoadedCertificate(subject={self.subject!r}, sha1={self.sha1!r})"


def _crypto() -> Any:
    try:
        import cryptography.x509  # noqa: F401
    except ImportError:
        raise MicrosoftAuthError(
            f"Certificate sign-in needs the cryptography package: {INSTALL}",
            "ms.certificate_dependency",
        ) from None
    import cryptography

    return cryptography


def load_certificate(path: str | Path, password: str | None = None) -> LoadedCertificate:
    """Read the certificate and its RSA private key from a PEM or PKCS#12 file.

    Raises:
        MicrosoftAuthError: If `cryptography` is missing, the file cannot be read, the password is
            wrong, the key is not RSA, or the file holds no certificate (codes
            `ms.certificate_dependency`, `ms.certificate_unreadable`).
    """

    _crypto()
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12

    file = Path(path)
    try:
        if file.stat().st_size > _MAX_FILE:
            raise MicrosoftAuthError(
                f"The certificate file {file} is too large to be a certificate.",
                "ms.certificate_unreadable",
            )
        data = file.read_bytes()
    except OSError as error:
        raise MicrosoftAuthError(
            f"The certificate file {file} cannot be read ({type(error).__name__}). Check "
            "AZURE_CLIENT_CERTIFICATE_PATH.",
            "ms.certificate_unreadable",
        ) from None
    secret = password.encode() if password else None
    key: Any = None
    certificate: Any = None
    try:
        if file.suffix.lower() in (".pfx", ".p12"):
            key, certificate, _ = pkcs12.load_key_and_certificates(data, secret)
        else:
            key = serialization.load_pem_private_key(data, secret)
            certificate = x509.load_pem_x509_certificate(data)
    except (ValueError, TypeError):
        raise MicrosoftAuthError(
            f"The certificate file {file} could not be opened: it needs the private key and the "
            "certificate, and AZURE_CLIENT_CERTIFICATE_PASSWORD must match a protected key.",
            "ms.certificate_unreadable",
        ) from None
    if certificate is None or not isinstance(key, rsa.RSAPrivateKey):
        raise MicrosoftAuthError(
            f"The certificate file {file} must hold an RSA private key and its certificate.",
            "ms.certificate_unreadable",
        )
    der = certificate.public_bytes(serialization.Encoding.DER)
    return LoadedCertificate(
        thumbprint=_b64(hashlib.sha256(der).digest()),
        sha1=hashlib.sha1(der, usedforsecurity=False).hexdigest().upper(),
        not_after=certificate.not_valid_after_utc,
        subject=certificate.subject.rfc4514_string(),
        key=key,
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def client_assertion(
    certificate: LoadedCertificate,
    *,
    client_id: str,
    token_url: str,
    now: float | None = None,
    jti: str | None = None,
) -> str:
    """The signed client assertion for one token request (PS256, `x5t#S256`).

    Args:
        token_url: The token endpoint the assertion is sent to (its `aud`).
        now: Seconds since the epoch (default: the clock).
        jti: The assertion's unique id (default: a new UUID).
    """

    _crypto()
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    issued = int(now if now is not None else _now())
    header = {"alg": "PS256", "typ": "JWT", "x5t#S256": certificate.thumbprint}
    claims = {
        "aud": token_url,
        "iss": client_id,
        "sub": client_id,
        "jti": jti or str(uuid.uuid4()),
        "nbf": issued,
        "iat": issued,
        "exp": issued + ASSERTION_LIFETIME,
    }
    signing_input = ".".join(
        _b64(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
    )
    signature = certificate.key.sign(
        signing_input.encode("ascii"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256.digest_size),
        hashes.SHA256(),
    )
    return f"{signing_input}.{_b64(signature)}"


class CertificateTokenProvider:
    """The app's access token from a certificate: one client assertion per token request, the
    token kept until five minutes before it expires (one connector instance asks once).

    Args:
        scope: The resource scope (`https://graph.microsoft.com/.default`).
    """

    kind: Identity = "app"

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self._lock = threading.Lock()
        self._token: tuple[str, float] | None = None
        self._certificate: LoadedCertificate | None = None

    def __repr__(self) -> str:
        return f"CertificateTokenProvider(scope={self.scope!r})"

    def certificate(self, ctx: ConnectorContext) -> LoadedCertificate:
        """The certificate of `ctx` (read once).

        Raises:
            MicrosoftAuthError: If it cannot be read or has expired (`ms.certificate_expired`).
        """

        if self._certificate is None:
            self._certificate = load_certificate(
                ctx.require(CERTIFICATE_PATH.name), ctx.value(CERTIFICATE_PASSWORD.name)
            )
        if self._certificate.not_after.timestamp() <= _now():
            raise MicrosoftAuthError(
                f"The certificate {self._certificate.subject} expired on "
                f"{self._certificate.not_after:%Y-%m-%d}. Create a new one, upload its public "
                "part to the app registration and update AZURE_CLIENT_CERTIFICATE_PATH.",
                "ms.certificate_expired",
            )
        return self._certificate

    def token(self, ctx: ConnectorContext) -> str:
        with self._lock:
            if self._token is not None and self._token[1] - _REFRESH_MARGIN > _now():
                return self._token[0]
            authority = (ctx.value("AZURE_AUTHORITY_HOST") or "").rstrip("/")
            tenant = urllib.parse.quote(ctx.require("AZURE_TENANT_ID"), safe="")
            client_id = ctx.require("AZURE_CLIENT_ID")
            token_url = f"{authority}/{tenant}/oauth2/v2.0/token"
            assertion = client_assertion(
                self.certificate(ctx), client_id=client_id, token_url=token_url
            )
            status, data = post_form(
                ctx,
                token_url,
                {
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "scope": self.scope,
                    "client_assertion_type": ASSERTION_TYPE,
                    "client_assertion": assertion,
                },
            )
            token = data.get("access_token")
            if status != 200 or not isinstance(token, str) or not token:
                raise token_error(status, data, "client_credentials")
            try:
                lifetime = float(data.get("expires_in", 3600))
            except (TypeError, ValueError):
                lifetime = 3600.0
            lifetime = lifetime if math.isfinite(lifetime) else 3600.0
            self._token = (token, _now() + min(max(lifetime, 60.0), 86_400.0))
            return token

    def invalidate(self, ctx: ConnectorContext) -> None:
        with self._lock:
            self._token = None


def expires_in_days(certificate: LoadedCertificate, now: float | None = None) -> float:
    """Days until `certificate` expires (negative when it has)."""

    moment = now if now is not None else _now()
    return (certificate.not_after - datetime.fromtimestamp(moment, UTC)).total_seconds() / 86_400
