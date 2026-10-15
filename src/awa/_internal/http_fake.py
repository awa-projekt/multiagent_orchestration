"""`MockTransport`: answer connector requests from scripted routes, offline."""

from __future__ import annotations

import dataclasses
import json as _json
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from email.message import Message
from typing import Any

from awa._internal.http import HttpRequest, HttpResponse

Reply = HttpResponse | Sequence[HttpResponse] | Callable[[HttpRequest], HttpResponse]
"""A route's answer: one response, responses in turn (the last repeats), or a function."""


def mock_response(
    status: int = 200,
    *,
    json: Any = None,
    text: str | None = None,
    headers: Mapping[str, str | Sequence[str]] | None = None,
    content_type: str | None = None,
    url: str = "",
) -> HttpResponse:
    """Build a response for `MockTransport`; a list header value repeats the header (`Set-Cookie`)."""

    message = Message()
    if json is not None:
        body = _json.dumps(json).encode("utf-8")
        message["Content-Type"] = content_type or "application/json"
    else:
        body = (text or "").encode("utf-8")
        message["Content-Type"] = content_type or "text/plain; charset=utf-8"
    for name, value in (headers or {}).items():
        for item in [value] if isinstance(value, str) else value:
            message[name] = item
    return HttpResponse(
        url=url, status=status, reason="", headers=message, body=body, truncated=False
    )


class MockTransport:
    """A transport that answers from routes and records every request in `requests`.

    Routes are keyed `"METHOD /path"` (any host) or `"METHOD https://host/path"`; a request without
    a route gets a 404. Pass it as `transport=` to `connector_tools` or `check_connector`.

    Example:
        ```python
        transport = MockTransport({
            "GET /rest/api/3/search/jql": mock_response(json={"issues": [{"key": "OPS-1"}]}),
        })
        ```
    """

    def __init__(self, routes: Mapping[str, Reply] | None = None) -> None:
        self.routes: dict[str, Reply] = dict(routes or {})
        self.requests: list[HttpRequest] = []
        self._served: dict[str, int] = {}

    def __repr__(self) -> str:
        return f"MockTransport(routes={sorted(self.routes)}, requests={len(self.requests)})"

    def add(self, route: str, reply: Reply) -> None:
        """Answer `route` (`"GET /path"`) with `reply`."""

        self.routes[route] = reply

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        parts = urllib.parse.urlsplit(request.url)
        absolute = f"{request.method} {parts.scheme}://{parts.netloc}{parts.path}"
        for key in (absolute, f"{request.method} {parts.path}"):
            if key in self.routes:
                return self._answer(key, request)
        return mock_response(404, json={"message": f"no route for {request.method} {parts.path}"})

    def _answer(self, key: str, request: HttpRequest) -> HttpResponse:
        reply = self.routes[key]
        if callable(reply):
            response = reply(request)
        elif isinstance(reply, HttpResponse):
            response = reply
        else:
            index = self._served.get(key, 0)
            self._served[key] = index + 1
            response = reply[min(index, len(reply) - 1)]
        return response if response.url else dataclasses.replace(response, url=request.url)
