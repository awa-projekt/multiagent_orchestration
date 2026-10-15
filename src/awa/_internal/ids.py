"""Short, prefixed identifiers."""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    """A new random identifier `"<prefix>_<12 hex chars>"` (48 random bits, enough for records)."""

    return f"{prefix}_{uuid.uuid4().hex[:12]}"
