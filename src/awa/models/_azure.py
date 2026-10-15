"""Azure OpenAI and Microsoft Foundry: which API route (v1 or dated), which sign-in, the Entra token."""

from __future__ import annotations

import asyncio
import functools
import json
import re
import threading
import time
import urllib.parse
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from awa.core.settings import Settings
from awa.errors import ModelProviderError, OptionalDependencyError
from awa.kernel.redaction import redact_values

AUTHORITY_HOST = "https://login.microsoftonline.com"
"""Where Entra tokens are requested when `AZURE_AUTHORITY_HOST` is not set."""

TOKEN_SCOPES = ("https://ai.azure.com/.default", "https://cognitiveservices.azure.com/.default")
"""The token scope of the current Azure AI documentation, then the older one that every
Azure OpenAI resource also accepts; the second is tried when the first is refused."""

_REFRESH_MARGIN = 300.0
_SCOPE_ERRORS = frozenset({"invalid_scope", "invalid_resource"})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_SIGN_IN_HINTS = {
    "AADSTS7000215": "AZURE_CLIENT_SECRET is wrong: use the secret VALUE, not its id",
    "AADSTS7000222": "AZURE_CLIENT_SECRET has expired: create a new one in the app registration",
    "AADSTS700016": "no app with this AZURE_CLIENT_ID in this tenant: check it and AZURE_TENANT_ID",
    "AADSTS90002": "no tenant with this AZURE_TENANT_ID: check the tenant id",
    "AADSTS900023": "AZURE_TENANT_ID is not a valid tenant id or domain",
}


def v1_base_url(endpoint: str) -> str:
    """The v1 API base URL (`<endpoint>/openai/v1/`) of a resource endpoint, in any common spelling."""

    base = endpoint.strip().rstrip("/")
    if base.endswith("/openai/v1"):
        return base + "/"
    if base.endswith("/openai"):
        return base + "/v1/"
    return base + "/openai/v1/"


class EntraTokenProvider:
    """A cached, thread-safe Entra ID token (client credentials); errors never carry the secret."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        *,
        authority: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._authority = (authority or AUTHORITY_HOST).rstrip("/")
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires = 0.0
        self._scope = 0

    def __call__(self) -> str:
        with self._lock:
            if self._token is None or self._clock() >= self._expires:
                self._token, lifetime = self._request()
                # Renew shortly before the end; a short-lived token is renewed at half time.
                self._expires = self._clock() + max(
                    lifetime - _REFRESH_MARGIN, min(lifetime / 2, _REFRESH_MARGIN)
                )
            return self._token

    async def aget(self) -> str:
        """The token, fetched off the event loop when it must be renewed."""

        return await asyncio.get_running_loop().run_in_executor(None, self)

    def _request(self) -> tuple[str, float]:
        refused: ModelProviderError | None = None
        for index in range(self._scope, len(TOKEN_SCOPES)):
            try:
                token, lifetime = self._request_scope(TOKEN_SCOPES[index])
            except _ScopeRefused as exc:
                refused = exc.error
                continue
            self._scope = index
            return token, lifetime
        raise refused or self._failure("no token scope is configured", "authentication", None)

    def _request_scope(self, scope: str) -> tuple[str, float]:
        from awa._internal.http import HttpPolicy, fetch

        url = f"{self._authority}/{urllib.parse.quote(self._tenant_id, safe='')}/oauth2/v2.0/token"
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        policy = HttpPolicy(
            allowed_hosts=(host,),
            allow_private_networks=host in _LOCAL_HOSTS,
            max_response_bytes=200_000,
            user_agent="awa-models/1",
        )
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": scope,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        try:
            data = urllib.parse.urlencode(form).encode()
            response = fetch(policy, url, method="POST", headers=headers, data=data)
        except Exception as exc:
            raise self._failure(f"cannot reach {host}: {exc}", "connection", None) from None
        body = _json_object(response.body)
        token = body.get("access_token")
        if response.status < 400 and isinstance(token, str) and token:
            return token, _seconds(body.get("expires_in"))
        code = str(body.get("error") or "")
        description = str(body.get("error_description") or "").strip()
        detail = description.splitlines()[0][:300] if description else ""
        hint = next((text for key, text in _SIGN_IN_HINTS.items() if key in detail), None)
        message = f"Entra ID sign-in failed (HTTP {response.status}, {code or 'no error code'})"
        message += f": {detail}" if detail else ""
        message += (
            f". Hint: {hint}"
            if hint
            else ". Check AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET"
        )
        transient = response.status >= 500 or response.status == 429
        error = self._failure(
            message, "server_error" if transient else "authentication", response.status
        )
        if code in _SCOPE_ERRORS:
            raise _ScopeRefused(error)
        raise error

    def _failure(self, message: str, kind: str, status: int | None) -> ModelProviderError:
        return ModelProviderError(
            redact_values(message, [self._client_secret]),
            kind=kind,
            provider="azure_openai",
            status=status,
            retryable=kind in ("connection", "server_error"),
        )


class _ScopeRefused(Exception):
    """Entra does not know the requested scope: the next one is tried."""

    def __init__(self, error: ModelProviderError) -> None:
        super().__init__(str(error))
        self.error = error


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _seconds(value: object) -> float:
    try:
        return max(float(str(value)), 60.0)
    except ValueError:
        return 3600.0


def entra_provider(settings: Settings) -> EntraTokenProvider:
    """The shared token provider of `settings`; raises if tenant, client id or secret is missing."""

    secret = settings.azure_client_secret.get_secret_value() if settings.azure_client_secret else ""
    values = {
        "AZURE_TENANT_ID": settings.azure_tenant_id,
        "AZURE_CLIENT_ID": settings.azure_client_id,
        "AZURE_CLIENT_SECRET": secret,
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ModelProviderError(
            f"Entra ID sign-in for Azure OpenAI needs {', '.join(missing)} "
            "(set them in the environment or .env; see awa doctor)",
            kind="authentication",
            provider="azure_openai",
        )
    return _shared_provider(
        str(settings.azure_tenant_id),
        str(settings.azure_client_id),
        secret,
        settings.azure_authority_host,
    )


@functools.cache
def _shared_provider(
    tenant_id: str, client_id: str, client_secret: str, authority: str | None
) -> EntraTokenProvider:
    # Models are rebuilt often (every call with its own settings builds one): they share
    # the token of one service principal instead of each signing in.
    return EntraTokenProvider(tenant_id, client_id, client_secret, authority=authority)


_RENAMED_PARAMETERS = {"max_tokens": "max_completion_tokens", "max_completion_tokens": "max_tokens"}
_MAX_ADJUSTMENTS = 4
_QUOTED_NAME = re.compile(r"'([A-Za-z_][A-Za-z0-9_.]*)'")


def unsupported_parameter(exc: BaseException) -> str | None:
    """The request parameter a 400 answer says the deployment does not support, if any."""

    if getattr(exc, "status_code", None) != 400:
        return None
    body = getattr(exc, "body", None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    fields = body if isinstance(body, dict) else {}
    code = str(getattr(exc, "code", None) or fields.get("code") or "").lower()
    if code not in ("unsupported_parameter", "unsupported_value"):
        return None
    param = getattr(exc, "param", None) or fields.get("param")
    if isinstance(param, str) and param:
        return param
    found = _QUOTED_NAME.search(str(fields.get("message") or exc))
    return found.group(1) if found else None


@functools.cache
def _v1_chat_class() -> type[Any]:
    from langchain_openai import ChatOpenAI
    from pydantic import PrivateAttr

    class AzureV1ChatOpenAI(ChatOpenAI):
        """`ChatOpenAI` on Azure's v1 API that drops (or renames) parameters a 400 refuses."""

        _awa_adjustments: dict[str, str | None] = PrivateAttr(default_factory=dict)

        @property
        def _llm_type(self) -> str:
            return "azure-openai-chat"

        def _get_request_payload(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            payload: dict[str, Any] = super()._get_request_payload(*args, **kwargs)
            for name, replacement in self._awa_adjustments.items():
                if name in payload:
                    value = payload.pop(name)
                    if replacement is not None:
                        payload.setdefault(replacement, value)
            return payload

        def _learn(self, exc: Exception) -> bool:
            """Remember the adjustment `exc` asks for; `False` when there is none (or known)."""

            name = unsupported_parameter(exc)
            if name is None or name in self._awa_adjustments:
                return False
            other = _RENAMED_PARAMETERS.get(name)
            # Refused under both names: the deployment takes no token limit at all.
            self._awa_adjustments[name] = None if other in self._awa_adjustments else other
            return True

        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            for _ in range(_MAX_ADJUSTMENTS):
                try:
                    return super()._generate(*args, **kwargs)
                except Exception as exc:
                    if not self._learn(exc):
                        raise
            return super()._generate(*args, **kwargs)

        async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
            for _ in range(_MAX_ADJUSTMENTS):
                try:
                    return await super()._agenerate(*args, **kwargs)
                except Exception as exc:
                    if not self._learn(exc):
                        raise
            return await super()._agenerate(*args, **kwargs)

        def _stream(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
            for _ in range(_MAX_ADJUSTMENTS):
                started = False
                try:
                    for chunk in super()._stream(*args, **kwargs):
                        started = True
                        yield chunk
                    return
                except Exception as exc:
                    if started or not self._learn(exc):
                        raise
            yield from super()._stream(*args, **kwargs)

        async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            for _ in range(_MAX_ADJUSTMENTS):
                started = False
                try:
                    async for chunk in super()._astream(*args, **kwargs):
                        started = True
                        yield chunk
                    return
                except Exception as exc:
                    if started or not self._learn(exc):
                        raise
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk

    return AzureV1ChatOpenAI


def init_azure_v1(**kwargs: Any) -> Any:
    """Build the chat model of the Azure v1 route from `ChatOpenAI` keyword arguments."""

    try:
        chat_class = _v1_chat_class()
    except ImportError as exc:
        from awa._internal.install import install_hint

        raise OptionalDependencyError(
            f"Azure OpenAI models need langchain-openai; install it with {install_hint('openai')}"
        ) from exc
    return chat_class(**kwargs)
