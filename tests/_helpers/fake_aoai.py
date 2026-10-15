"""A fake Azure OpenAI resource for offline tests: a real HTTP server on localhost.

`FakeAoai` speaks the shapes of the two Azure OpenAI routes, so the real model classes
(`ChatOpenAI`, `AzureChatOpenAI`) can talk to it without any patching of the transport:

- the v1 route `/openai/v1/chat/completions` and `/openai/v1/responses` (no `api-version`),
- the classic route `/openai/deployments/{name}/chat/completions?api-version=...`
  (a missing `api-version` is a 404 `Resource not found`, as on Azure),
- authentication by the `api-key` header or `Authorization: Bearer`, where the bearer is
  either an API key or an Entra token the fake itself issued (with roles),
- Entra: `POST /{tenant}/oauth2/v2.0/token` (client credentials) with the scopes and
  errors of the real service,
- the failures awa gives hints for: 401 wrong key, 403 key authentication disabled, 403
  missing role, 404 `DeploymentNotFound`, 400 `max_tokens` on a reasoning deployment,
  429 with `Retry-After`.

Every request is recorded in `requests`; nothing leaves the machine.

Example:
    ```python
    with FakeAoai(deployments=["chat"]) as aoai:
        registry = ModelRegistry(Settings(azure_openai_endpoint=aoai.url, ...))
    ```
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any

OPENAI_USER_ROLE = "Cognitive Services OpenAI User"
ACCEPTED_SCOPES = ("https://ai.azure.com/.default", "https://cognitiveservices.azure.com/.default")
DATED_MODEL = "model-snapshot-1"


@dataclass(frozen=True)
class RecordedRequest:
    """One request the fake received."""

    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: Any


@dataclass
class _Error(Exception):
    status: int
    code: str
    message: str
    headers: dict[str, str] = field(default_factory=dict)
    param: str | None = None


class FakeAoai:
    """A localhost Azure OpenAI resource.

    Args:
        deployments: Deployment names that exist; any other name is a 404.
        reasoning_deployments: Deployments that refuse `max_tokens`.
        rejected_params: Per deployment, request parameters it refuses with a 400
            `unsupported_parameter` (a name says nothing about what a deployment accepts).
        reported_models: Per deployment, the `model` its answers report (default
            `DATED_MODEL`).
        api_keys: Valid API keys.
        key_auth_enabled: `False` mimics a resource with key authentication disabled
            (`disableLocalAuth`): keys are refused with 403, only Entra tokens work.
        tenant: Tenant id of the fake Entra authority (the sign-in host is `url` itself).
        client_id: The one service principal that may sign in.
        client_secret: Its secret.
        client_roles: Roles of that principal on the resource.
        accepted_scopes: Token scopes the authority knows; others get `invalid_scope`.
        token_lifetime: `expires_in` of issued tokens, in seconds.
        reply: The assistant's answer; `{deployment}` is replaced by the deployment name.
    """

    def __init__(
        self,
        *,
        deployments: Iterable[str] = ("my-deployment",),
        reasoning_deployments: Iterable[str] = (),
        rejected_params: Mapping[str, Collection[str]] | None = None,
        reported_models: Mapping[str, str] | None = None,
        api_keys: Iterable[str] = ("fake-key",),
        key_auth_enabled: bool = True,
        tenant: str = "tenant-1",
        client_id: str = "client-1",
        client_secret: str = "fake-secret",
        client_roles: Collection[str] = (OPENAI_USER_ROLE,),
        accepted_scopes: Collection[str] = ACCEPTED_SCOPES,
        token_lifetime: int = 3600,
        reply: str = "hello from {deployment}",
    ) -> None:
        self.deployments = set(deployments)
        self.reasoning_deployments = set(reasoning_deployments)
        self.rejected_params = {k: set(v) for k, v in (rejected_params or {}).items()}
        self.reported_models = dict(reported_models or {})
        self.api_keys = set(api_keys)
        self.key_auth_enabled = key_auth_enabled
        self.tenant = tenant
        self.client_id = client_id
        self.client_secret = client_secret
        self.client_roles = set(client_roles)
        self.accepted_scopes = set(accepted_scopes)
        self.token_lifetime = token_lifetime
        self.reply = reply
        self.requests: list[RecordedRequest] = []
        self.tokens: dict[str, set[str]] = {}
        self._expired: set[str] = set()
        self._throttled = 0
        self._retry_after = 1
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    # ----------------------------------------------------------------- lifecycle

    def __enter__(self) -> FakeAoai:
        handler = type("_Handler", (_Handler,), {"fake": self})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self) -> str:
        """The endpoint (`AZURE_OPENAI_ENDPOINT`) and the Entra authority host."""

        assert self._server is not None, "use FakeAoai as a context manager"
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    # ------------------------------------------------------------- test controls

    def throttle(self, times: int, *, retry_after: int = 1) -> None:
        """Answer the next `times` model requests with 429 and a `Retry-After` header."""

        with self._lock:
            self._throttled = times
            self._retry_after = retry_after

    def expire_tokens(self) -> None:
        """Make every token issued so far invalid (an expired or revoked token)."""

        with self._lock:
            self._expired |= set(self.tokens)

    def model_requests(self) -> list[RecordedRequest]:
        """The requests to a model route (not the token endpoint)."""

        return [r for r in self.requests if "/oauth2/" not in r.path]

    def token_requests(self) -> list[RecordedRequest]:
        """The requests to the token endpoint, with the form fields in `body`."""

        return [r for r in self.requests if "/oauth2/" in r.path]

    # ------------------------------------------------------------------- serving

    def handle(self, request: RecordedRequest) -> tuple[int, dict[str, Any], dict[str, str]]:
        """Answer one request: status, JSON body and extra headers."""

        with self._lock:
            self.requests.append(request)
        try:
            if "/oauth2/" in request.path:
                return 200, self._token(request), {}
            return 200, self._model(request), {}
        except _Error as error:
            if "/oauth2/" in request.path:  # OAuth error shape of the token endpoint
                body = {"error": error.code, "error_description": error.message}
            else:
                body = {"error": {"code": error.code, "message": error.message}}
                if error.param:
                    body["error"]["param"] = error.param
            return error.status, body, error.headers

    def _token(self, request: RecordedRequest) -> dict[str, Any]:
        form = request.body
        if request.path != f"/{self.tenant}/oauth2/v2.0/token":
            raise _Error(400, "invalid_request", "AADSTS90002: tenant not found")
        if form.get("client_id") != self.client_id:
            raise _Error(400, "unauthorized_client", "AADSTS700016: application not found")
        if form.get("client_secret") != self.client_secret:
            raise _Error(401, "invalid_client", "AADSTS7000215: Invalid client secret provided.")
        if form.get("grant_type") != "client_credentials":
            raise _Error(400, "unsupported_grant_type", "only client_credentials is supported")
        if form.get("scope") not in self.accepted_scopes:
            raise _Error(
                400, "invalid_scope", f"AADSTS70011: The scope {form.get('scope')} is not valid."
            )
        with self._lock:
            token = f"entra-token-{len(self.tokens) + 1}"
            self.tokens[token] = set(self.client_roles)
        return {"token_type": "Bearer", "expires_in": self.token_lifetime, "access_token": token}

    def _authenticate(self, request: RecordedRequest) -> None:
        bearer = request.headers.get("authorization", "")
        credential = request.headers.get("api-key") or bearer.removeprefix("Bearer ").strip()
        if credential in self.api_keys:
            if not self.key_auth_enabled:
                raise _Error(
                    403,
                    "AuthenticationTypeDisabled",
                    "Key based authentication is disabled for this resource.",
                )
            return
        roles = self.tokens.get(credential)
        if roles is None:
            raise _Error(
                401,
                "401",
                "Access denied due to invalid subscription key or wrong API endpoint. Make sure "
                "to provide a valid key for an active subscription and use a correct regional "
                "API endpoint for your resource.",
            )
        if credential in self._expired:
            raise _Error(401, "401", "Access token has expired.")
        if OPENAI_USER_ROLE not in roles:
            raise _Error(
                403,
                "PermissionDenied",
                "The principal lacks the required data action "
                "Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action "
                "to perform this operation.",
            )

    def _model(self, request: RecordedRequest) -> dict[str, Any]:
        route = _route(request.path)
        if route is None:
            raise _Error(404, "404", "Resource not found")
        deployment, kind = route
        if deployment is not None and "api-version" not in request.query:
            # The classic route is versioned by the query parameter; without it Azure answers
            # as if the path did not exist.
            raise _Error(404, "404", "Resource not found")
        self._authenticate(request)
        with self._lock:
            throttled = self._throttled > 0
            self._throttled = max(0, self._throttled - 1)
            retry_after = self._retry_after
        if throttled:
            raise _Error(
                429,
                "429",
                f"Requests to the ChatCompletions_Create Operation have exceeded token rate "
                f"limit of your current OpenAI S0 pricing tier. Please retry after "
                f"{retry_after} seconds.",
                {"Retry-After": str(retry_after)},
            )
        body = request.body if isinstance(request.body, dict) else {}
        name = deployment or str(body.get("model") or "")
        if name not in self.deployments:
            raise _Error(
                404,
                "DeploymentNotFound",
                "The API deployment for this resource does not exist. If you created the "
                "deployment within the last 5 minutes, please wait a moment and try again.",
            )
        refused = set(self.rejected_params.get(name, ()))
        if name in self.reasoning_deployments:
            refused.add("max_tokens")
        for param in sorted(refused & set(body)):
            raise _Error(
                400,
                "unsupported_parameter",
                f"Unsupported parameter: '{param}' is not supported with this model.",
                param=param,
            )
        text = self.reply.format(deployment=name)
        model = self.reported_models.get(name, DATED_MODEL)
        if kind == "responses":
            return _response(text, model)
        return _chat_completion(text, model)


def _route(path: str) -> tuple[str | None, str] | None:
    """`(deployment from the path or None, "chat" or "responses")`, or `None` for other paths."""

    parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]
    if parts == ["openai", "v1", "chat", "completions"]:
        return None, "chat"
    if parts == ["openai", "v1", "responses"]:
        return None, "responses"
    if (
        len(parts) == 5
        and parts[:2] == ["openai", "deployments"]
        and parts[3:] == ["chat", "completions"]
    ):
        return parts[2], "chat"
    return None


def _chat_completion(text: str, model: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


def _response(text: str, model: str) -> dict[str, Any]:
    return {
        "id": "resp_fake",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": model,
        "output": [
            {
                "type": "message",
                "id": "msg_fake",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


_NO_RESPONSES_STREAM = "FakeAoai streams chat completions only, not the responses API."


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fake: FakeAoai

    def do_POST(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        headers = {name.lower(): value for name, value in self.headers.items()}
        content_type = headers.get("content-type", "")
        body: Any
        if "x-www-form-urlencoded" in content_type:
            body = dict(urllib.parse.parse_qsl(raw.decode()))
        else:
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
        request = RecordedRequest(
            "POST", parts.path, dict(urllib.parse.parse_qsl(parts.query)), headers, body
        )
        status, payload, extra = self.fake.handle(request)
        if status == 200 and isinstance(body, dict) and body.get("stream"):
            if "choices" in payload:
                self._stream(payload, body)
                return
            status, extra = 400, {}
            payload = {"error": {"code": "unsupported", "message": _NO_RESPONSES_STREAM}}
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, completion: dict[str, Any], body: dict[str, Any]) -> None:
        """Server-sent events of a chat completion: the text in two chunks, then usage."""

        text = completion["choices"][0]["message"]["content"]
        half = len(text) // 2
        base = {"id": completion["id"], "object": "chat.completion.chunk", "created": 0}
        base["model"] = completion["model"]
        chunks = [
            {
                **base,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": text[:half]}}],
            },
            {**base, "choices": [{"index": 0, "delta": {"content": text[half:]}}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        if (body.get("stream_options") or {}).get("include_usage"):
            chunks.append({**base, "choices": [], "usage": completion["usage"]})
        data = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        encoded = data.encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        pass
