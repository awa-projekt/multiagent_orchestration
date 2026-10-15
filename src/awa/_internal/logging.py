"""Library loggers: every one lives below `awa` and redacts credentials in what it records."""

from __future__ import annotations

import logging

from awa.kernel.redaction import redact_secrets


class _RedactSecrets(logging.Filter):
    """Redacts credentials in awa log records, except those logged with `extra={"unredacted": True}`."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "unredacted", False):
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format is the handler's to report, not the caller's
            return True
        if (redacted := redact_secrets(message)) != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            trace = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact_secrets(trace)
        return True


_REDACT = _RedactSecrets()


def get_logger(name: str) -> logging.Logger:
    """The logger `awa.<name>` (a name starting with `awa.` is used as-is), with secrets redacted."""

    logger = logging.getLogger(name if name == "awa" or name.startswith("awa.") else f"awa.{name}")
    _protect(logger)
    return logger


def _protect(logger: logging.Logger) -> None:
    if _REDACT not in logger.filters:
        logger.addFilter(_REDACT)
