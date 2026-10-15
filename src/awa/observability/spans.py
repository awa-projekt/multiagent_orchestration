"""Short OpenTelemetry spans for awa's safety decisions and outbound requests.

Without the `otel` extra they do nothing.
Attribute values are ids, counts, modes and flags only, never message text, addresses or secrets.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

AttributeValue = str | bool | int | float


def _tracer() -> Any:
    """The global tracer `awa`, or `None` without OpenTelemetry."""

    try:
        from opentelemetry import trace
    except ImportError:  # the otel extra is optional
        return None
    return trace.get_tracer("awa")


def record_span(name: str, attributes: Mapping[str, AttributeValue | None] | None = None) -> None:
    """A zero-length span `name` with `attributes` (`None` values left out), for an instant decision."""

    tracer = _tracer()
    if tracer is None:
        return
    with tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            if value is not None:
                span.set_attribute(key, value)


@contextmanager
def timed_span(
    name: str, attributes: Mapping[str, AttributeValue | None] | None = None
) -> Iterator[dict[str, AttributeValue | None]]:
    """A span `name` around the block, with `attributes` plus those added to the yielded dict.

    A raising block sets `error.type` to the exception class; its message is never recorded.
    """

    added: dict[str, AttributeValue | None] = {}
    tracer = _tracer()
    if tracer is None:
        yield added
        return
    with tracer.start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield added
        except BaseException as exc:
            from opentelemetry.trace import Status, StatusCode

            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            for key, value in {**(attributes or {}), **added}.items():
                if value is not None:
                    span.set_attribute(key, value)
