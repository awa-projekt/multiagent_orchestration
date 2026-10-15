"""The door for extension authors: the small building blocks an extension reuses.

Preview: it works and is tested, but its API may still change in a minor release.

An extension imports these names instead of reaching into awa's private modules: the secret and
PII redactors, `new_id`, `get_logger` and the sync-over-async runner `run_sync` (with its
`LoopLocal` companion).

Example:
    ```python
    from awa.ext import redact_secrets, run_sync

    run_sync(send(redact_secrets("token=abc123")))
    ```
"""

from __future__ import annotations

from awa._internal.aio import LoopLocal
from awa._internal.aio import run as run_sync
from awa._internal.ids import new_id
from awa._internal.logging import get_logger
from awa.kernel.redaction import (
    PII_PATTERNS,
    SECRET_PATTERNS,
    RedactionPattern,
    RegexRedactor,
    redact_mapping,
    redact_secrets,
    redact_values,
)

__stability__ = "preview"

__all__ = [
    "PII_PATTERNS",
    "SECRET_PATTERNS",
    "LoopLocal",
    "RedactionPattern",
    "RegexRedactor",
    "get_logger",
    "new_id",
    "redact_mapping",
    "redact_secrets",
    "redact_values",
    "run_sync",
]
