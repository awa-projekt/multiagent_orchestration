"""A local OTLP/HTTP trace collector for tests: a real receiver on a free localhost port.

`Collector` accepts `POST /v1/traces` over HTTP in both OTLP/HTTP encodings, protobuf (decoded with
`opentelemetry-proto`) and JSON, keeps the raw request bodies and decodes every span into a plain
`WireSpan`, so a test sees exactly what a backend would receive and never the SDK objects.

`exporter(wire, endpoint)` returns the exporter that sends to it: the real `OTLPSpanExporter` of
`opentelemetry-exporter-otlp-proto-http` for `"protobuf"` (skipped when that package or
`opentelemetry-proto` is not installed), and `OtlpJsonExporter` for `"json"`, a small exporter
that needs only `opentelemetry-sdk` and writes the OTLP/JSON encoding of the spec.
"""

from __future__ import annotations

import gzip
import http.server
import json
import threading
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

import pytest

__all__ = [
    "WIRES",
    "Collector",
    "OtlpJsonExporter",
    "Received",
    "WireEvent",
    "WireLink",
    "WireSpan",
    "decode_json",
    "decode_protobuf",
    "exporter",
]

WIRES = ("json", "protobuf")

KINDS = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
STATUS_CODES = {0: "UNSET", 1: "OK", 2: "ERROR"}
PROTOBUF = "application/x-protobuf"
JSON = "application/json"


@dataclass(frozen=True)
class WireEvent:
    name: str
    time_ns: int
    attributes: Mapping[str, Any]


@dataclass(frozen=True)
class WireLink:
    trace_id: str
    span_id: str
    attributes: Mapping[str, Any]


@dataclass(frozen=True)
class WireSpan:
    """A span as it arrived at the collector."""

    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    kind: str
    start_ns: int
    end_ns: int
    attributes: Mapping[str, Any]
    events: tuple[WireEvent, ...] = ()
    links: tuple[WireLink, ...] = ()
    status_code: str = "UNSET"
    status_message: str = ""
    scope_name: str = ""
    scope_version: str = ""
    resource: Mapping[str, Any] = field(default_factory=dict)

    @property
    def operation(self) -> str | None:
        value = self.attributes.get("gen_ai.operation.name")
        return value if isinstance(value, str) else None


# ------------------------------------------------------------------------ decoding


def _protobuf_value(value: Any) -> Any:
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    inner = getattr(value, kind)
    if kind == "array_value":
        return [_protobuf_value(item) for item in inner.values]
    if kind == "kvlist_value":
        return {item.key: _protobuf_value(item.value) for item in inner.values}
    return inner


def _protobuf_attributes(items: Any) -> dict[str, Any]:
    return {item.key: _protobuf_value(item.value) for item in items}


def decode_protobuf(body: bytes) -> list[WireSpan]:
    """The spans of an `ExportTraceServiceRequest` in protobuf encoding."""

    try:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
    except ImportError as exc:
        raise RuntimeError(
            "decoding protobuf needs opentelemetry-proto: pip install opentelemetry-exporter-otlp-proto-http"
        ) from exc
    request = ExportTraceServiceRequest()
    request.ParseFromString(body)
    spans: list[WireSpan] = []
    for resource_spans in request.resource_spans:
        resource = _protobuf_attributes(resource_spans.resource.attributes)
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                events = tuple(
                    WireEvent(e.name, e.time_unix_nano, _protobuf_attributes(e.attributes))
                    for e in span.events
                )
                links = tuple(
                    WireLink(
                        link.trace_id.hex(),
                        link.span_id.hex(),
                        _protobuf_attributes(link.attributes),
                    )
                    for link in span.links
                )
                spans.append(
                    WireSpan(
                        trace_id=span.trace_id.hex(),
                        span_id=span.span_id.hex(),
                        parent_id=span.parent_span_id.hex() or None,
                        name=span.name,
                        kind=KINDS[span.kind],
                        start_ns=span.start_time_unix_nano,
                        end_ns=span.end_time_unix_nano,
                        attributes=_protobuf_attributes(span.attributes),
                        events=events,
                        links=links,
                        status_code=STATUS_CODES[span.status.code],
                        status_message=span.status.message,
                        scope_name=scope_spans.scope.name,
                        scope_version=scope_spans.scope.version,
                        resource=resource,
                    )
                )
    return spans


def _json_value(value: Mapping[str, Any]) -> Any:
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "arrayValue" in value:
        return [_json_value(item) for item in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {item["key"]: _json_value(item["value"]) for item in value["kvlistValue"]["values"]}
    return None


def _json_attributes(items: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    return {item["key"]: _json_value(item["value"]) for item in items or ()}


def decode_json(body: bytes) -> list[WireSpan]:
    """The spans of an `ExportTraceServiceRequest` in OTLP/JSON encoding."""

    spans: list[WireSpan] = []
    for resource_spans in json.loads(body).get("resourceSpans", []):
        resource = _json_attributes(resource_spans.get("resource", {}).get("attributes"))
        for scope_spans in resource_spans.get("scopeSpans", []):
            scope = scope_spans.get("scope", {})
            for span in scope_spans.get("spans", []):
                status = span.get("status", {})
                events = tuple(
                    WireEvent(
                        e["name"], int(e["timeUnixNano"]), _json_attributes(e.get("attributes"))
                    )
                    for e in span.get("events", [])
                )
                links = tuple(
                    WireLink(
                        link["traceId"], link["spanId"], _json_attributes(link.get("attributes"))
                    )
                    for link in span.get("links", [])
                )
                spans.append(
                    WireSpan(
                        trace_id=span["traceId"],
                        span_id=span["spanId"],
                        parent_id=span.get("parentSpanId") or None,
                        name=span["name"],
                        kind=KINDS[int(span.get("kind", 0))],
                        start_ns=int(span["startTimeUnixNano"]),
                        end_ns=int(span["endTimeUnixNano"]),
                        attributes=_json_attributes(span.get("attributes")),
                        events=events,
                        links=links,
                        status_code=STATUS_CODES[int(status.get("code", 0))],
                        status_message=status.get("message", ""),
                        scope_name=scope.get("name", ""),
                        scope_version=scope.get("version", ""),
                        resource=resource,
                    )
                )
    return spans


def decode(content_type: str, body: bytes) -> list[WireSpan]:
    """The spans of a request body of the given OTLP/HTTP content type."""

    return decode_json(body) if content_type == JSON else decode_protobuf(body)


# ------------------------------------------------------------------------ the receiver


@dataclass(frozen=True)
class Received:
    path: str
    content_type: str
    body: bytes


class Collector:
    """An OTLP/HTTP receiver on a free port of `127.0.0.1`, used as a context manager."""

    def __init__(self) -> None:
        self.requests: list[Received] = []
        self._lock = threading.Lock()
        collector = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the name http.server calls
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
                if self.path != "/v1/traces" or content_type not in (PROTOBUF, JSON):
                    self.send_response(404 if self.path != "/v1/traces" else 415)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    decode(content_type, body)
                except Exception as exc:  # noqa: BLE001 - answer the exporter, keep serving
                    message = f"cannot decode the request: {type(exc).__name__}: {exc}".encode()
                    self.send_response(400)
                    self.send_header("Content-Length", str(len(message)))
                    self.end_headers()
                    self.wfile.write(message)
                    return
                with collector._lock:
                    collector.requests.append(Received(self.path, content_type, body))
                answer = b"{}" if content_type == JSON else b""
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        class Server(http.server.ThreadingHTTPServer):
            allow_reuse_address = False  # on Windows, reuse would let two collectors share a port

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def endpoint(self) -> str:
        """The URL traces are posted to."""

        return f"http://127.0.0.1:{self.port}/v1/traces"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> Collector:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
        self._thread.join(timeout=5)

    @property
    def raw(self) -> bytes:
        """Every request body received, concatenated."""

        with self._lock:
            return b"".join(request.body for request in self.requests)

    @property
    def spans(self) -> list[WireSpan]:
        """Every span received so far, decoded from the wire."""

        with self._lock:
            received = list(self.requests)
        return [span for r in received for span in decode(r.content_type, r.body)]


# ------------------------------------------------------------------------ exporters


def _json_any(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    return {"arrayValue": {"values": [_json_any(item) for item in value]}}


def _json_attrs(attributes: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [{"key": key, "value": _json_any(value)} for key, value in (attributes or {}).items()]


def _json_span(span: Any) -> dict[str, Any]:
    context = span.get_span_context()
    encoded: dict[str, Any] = {
        "traceId": f"{context.trace_id:032x}",
        "spanId": f"{context.span_id:016x}",
        "name": span.name,
        "kind": int(span.kind.value) + 1,  # SpanKind: INTERNAL=0 -> OTLP 1
        "startTimeUnixNano": str(span.start_time),
        "endTimeUnixNano": str(span.end_time),
        "attributes": _json_attrs(span.attributes),
        "events": [
            {
                "timeUnixNano": str(event.timestamp),
                "name": event.name,
                "attributes": _json_attrs(event.attributes),
            }
            for event in span.events
        ],
        "links": [
            {
                "traceId": f"{link.context.trace_id:032x}",
                "spanId": f"{link.context.span_id:016x}",
                "attributes": _json_attrs(link.attributes),
            }
            for link in span.links
        ],
        "status": {
            "code": int(span.status.status_code.value),
            "message": span.status.description or "",
        },
    }
    if span.parent is not None:
        encoded["parentSpanId"] = f"{span.parent.span_id:016x}"
    return encoded


class OtlpJsonExporter:
    """A span exporter that POSTs the OTLP/JSON encoding to a collector (needs only the SDK)."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def export(self, spans: Sequence[Any]) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        grouped: dict[tuple[int, str, str], list[Any]] = {}
        for span in spans:
            scope = span.instrumentation_scope
            key = (
                id(span.resource),
                scope.name if scope else "",
                (scope.version or "") if scope else "",
            )
            grouped.setdefault(key, []).append(span)
        resource_spans = [
            {
                "resource": {"attributes": _json_attrs(group[0].resource.attributes)},
                "scopeSpans": [
                    {
                        "scope": {"name": scope_name, "version": scope_version},
                        "spans": [_json_span(span) for span in group],
                    }
                ],
            }
            for (_, scope_name, scope_version), group in grouped.items()
        ]
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps({"resourceSpans": resource_spans}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=10) as response:
                ok = response.status == 200
        except OSError:
            ok = False
        return SpanExportResult.SUCCESS if ok else SpanExportResult.FAILURE

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def exporter(wire: str, endpoint: str) -> Any:
    """The exporter for `wire` (`"json"` or `"protobuf"`) sending to `endpoint`.

    `"protobuf"` is the real OTLP exporter and needs `opentelemetry-exporter-otlp-proto-http`
    and `opentelemetry-proto`; the test is skipped when they are not installed.
    """

    if wire == "json":
        return OtlpJsonExporter(endpoint)
    pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    module = pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    return module.OTLPSpanExporter(endpoint=endpoint, timeout=10)
