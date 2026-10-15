"""The connections of docs/develop/observability.md that need no account: they run against local servers.

* An OpenAI-compatible endpoint (a local model server) is reached through `OPENAI_BASE_URL`.
* The spans of awa reach an OTLP/HTTP backend that is configured only with the standard
  `OTEL_EXPORTER_OTLP_*` variables (the way any OTLP backend is connected).
* The page exists, is in the navigation and names files and functions that exist.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from awa import Agent
from awa.models import allow_model_requests

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "develop" / "observability.md"


class Server:
    """A local HTTP server that records POSTs and answers with `reply(path, body)`."""

    def __init__(self, reply: Any) -> None:
        self.seen: list[tuple[str, dict[str, str], bytes]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.seen.append(
                    (self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                )
                answer = reply(self.path, body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def chat_completion(path: str, body: bytes) -> bytes:
    model = json.loads(body)["model"]
    return json.dumps(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "pong from the local server"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        }
    ).encode()


@pytest.fixture
def openai_compatible_server() -> Iterator[Server]:
    server = Server(chat_completion)
    yield server
    server.close()


def test_an_openai_compatible_server_is_reached_through_the_base_url(
    openai_compatible_server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", openai_compatible_server.url + "/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "local-not-a-key")

    with allow_model_requests(True):  # the server is on this computer; the guard blocks real ones
        result = asyncio.run(Agent("local", model="openai:qwen3-local").run("ping"))

    assert result.output == "pong from the local server"
    assert result.usage.input_tokens == 5 and result.usage.output_tokens == 4
    path, headers, body = openai_compatible_server.seen[0]
    assert path == "/v1/chat/completions"
    assert headers["authorization"] == "Bearer local-not-a-key"
    assert json.loads(body)["model"] == "qwen3-local"


def test_spans_reach_a_backend_configured_with_the_standard_otlp_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from _helpers import traced_team
    from _helpers.otlp_collector import decode_protobuf
    from awa.observability import OpenTelemetrySubscriber

    backend = Server(lambda path, body: b"")
    try:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", backend.url + "/otel")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=otlp-test,x-project=awa-demo")
        provider = TracerProvider(resource=Resource({"service.name": "awa"}))
        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
        tracing = OpenTelemetrySubscriber(provider.get_tracer("awa"))
        asyncio.run(traced_team.run_scenario(traced_team.build_team(hooks=[tracing])))
        tracing.close()
        provider.shutdown()
    finally:
        backend.close()

    assert backend.seen, "nothing reached the backend"
    assert {path for path, _, _ in backend.seen} == {"/otel/v1/traces"}
    assert all(h["x-api-key"] == "otlp-test" for _, h, _ in backend.seen)
    assert all(h["x-project"] == "awa-demo" for _, h, _ in backend.seen)
    names = {s.name for _, _, body in backend.seen for s in decode_protobuf(body)}
    assert {"invoke_workflow desk", "invoke_agent payments", "execute_tool issue_refund"} <= names


# ------------------------------------------------------------------ the page


def page_text() -> str:
    if not PAGE.is_file():
        pytest.skip("docs/ is in the repository only")
    return PAGE.read_text("utf-8").replace("\r\n", "\n")


def test_the_page_is_in_the_navigation() -> None:
    page_text()
    nav = (ROOT / "mkdocs.yml").read_text("utf-8")
    assert "develop/observability.md" in nav


def test_every_file_and_screenshot_the_page_names_exists() -> None:
    text = page_text()
    for path in re.findall(r"`((?:examples|scripts|tests)/[\w./-]+\.\w+)`", text):
        assert (ROOT / path).is_file(), f"the page names {path}, which does not exist"
    for image in re.findall(r"\]\((\.\./assets/[\w./-]+\.png)\)", text):
        assert (PAGE.parent / image).resolve().is_file(), f"missing image {image}"


def test_every_setting_the_page_names_is_what_the_code_reads() -> None:
    text = page_text()
    source = (ROOT / "src" / "awa" / "observability" / "langfuse.py").read_text("utf-8")
    for name in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_HOST",
    ):
        assert name in text and name in source
    assert "connect_langfuse" in text
