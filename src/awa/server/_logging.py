"""Request ids and structured logs: `current_request_id`, `RequestContextFilter`, `JsonLogFormatter`."""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from awa.server._auth import current_principal

REQUEST_ID_HEADER = "X-Request-ID"
"""Header carrying the request id, in requests and responses."""

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
request_id_context: ContextVar[str | None] = ContextVar("awa_server_request_id", default=None)

# Attributes every log record has; anything else passed as `extra` is emitted too.
_STANDARD_ATTRIBUTES = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "request_id",
    "principal",
    "tenant",
    "taskName",
}


def new_request_id() -> str:
    """A fresh request id (`req_<32 hex>`)."""

    return f"req_{uuid4().hex}"


def accept_request_id(value: str | None) -> str:
    """The client's request id if it is well formed (at most 128 characters of
    letters, digits and `_.:-`), else a fresh one."""

    if value and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return new_request_id()


def current_request_id() -> str | None:
    """The id of the request being served (`X-Request-ID`), or `None` outside a request.

    Available in tools and deps factories of served runs.
    """

    return request_id_context.get()


class RequestContextFilter(logging.Filter):
    """Adds `request_id`, `principal` and `tenant` of the current request to log records.

    Attach it to a handler (not a logger), so child loggers' records get them too.

    Example:
        ```python
        handler = logging.StreamHandler()
        handler.addFilter(RequestContextFilter())
        handler.setFormatter(JsonLogFormatter())
        logging.getLogger().addHandler(handler)
        ```
    """

    def filter(self, record: logging.LogRecord) -> bool:
        principal = current_principal()
        if getattr(record, "request_id", None) is None:
            record.request_id = current_request_id()
        if getattr(record, "principal", None) is None:
            record.principal = principal.id if principal is not None else None
        if getattr(record, "tenant", None) is None:
            record.tenant = principal.tenant if principal is not None else None
        return True


class JsonLogFormatter(logging.Formatter):
    """Formats log records as one JSON object per line.

    Fields: `ts` (UTC), `level`, `logger`, `message`, the request context when set, `extra`
    values, and `exc_type` / `exc`.
    """

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in ("request_id", "principal", "tenant"):
            value = getattr(record, name, None)
            if value is not None:
                data[name] = value
        for name, value in vars(record).items():
            if name not in _STANDARD_ATTRIBUTES and not name.startswith("_"):
                data[name] = value
        if record.exc_info and record.exc_info[0] is not None:
            data["exc_type"] = record.exc_info[0].__name__
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


def json_log_config(level: str = "info") -> dict[str, Any]:
    """A `logging.config.dictConfig` for JSON logs of the server and uvicorn.

    Pass it as uvicorn's `log_config`; uvicorn's access log is replaced by the server's
    (`awa.server.access`), which carries the request id.
    """

    upper = level.upper() if level.lower() != "trace" else "DEBUG"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"request_context": {"()": RequestContextFilter}},
        "formatters": {"json": {"()": JsonLogFormatter}},
        "handlers": {
            "json": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "filters": ["request_context"],
                "stream": "ext://sys.stderr",
            }
        },
        "loggers": {
            "awa": {"handlers": ["json"], "level": upper, "propagate": False},
            "uvicorn": {"handlers": ["json"], "level": upper, "propagate": False},
            "uvicorn.error": {"level": upper},
            "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": False},
        },
    }
