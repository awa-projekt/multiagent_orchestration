"""A local OTLP/HTTP endpoint that keeps every request body (tests of `connect_langfuse`)."""

from __future__ import annotations

import http.server
import threading
from typing import Any

from _helpers.otlp_collector import decode_protobuf


class Receiver:
    """A local OTLP/HTTP endpoint that keeps every request body."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                outer.bodies.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def spans(self) -> list[Any]:
        return [s for body in self.bodies for s in decode_protobuf(body)]
