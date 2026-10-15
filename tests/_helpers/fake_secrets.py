"""Fake credentials for the redaction tests, joined at runtime so the source holds no token."""

from __future__ import annotations


def fake_secret(prefix: str, body: str) -> str:
    """Return `prefix + body`: a string shaped like a real credential, never a real one."""
    return prefix + body
