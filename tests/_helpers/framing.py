"""Test helpers: the original text of a tool output that was framed as external data."""

from __future__ import annotations

import json


def unframe(text: str) -> str:
    """The original output inside a `<tool_output>` frame."""

    assert text.startswith("<tool_output ") and text.endswith("\n</tool_output>"), text
    return str(json.loads(text.split("\n", 1)[1].rsplit("\n", 1)[0])["content"])


def tool_content(text: str) -> str:
    """What a tool returned: the text inside its frame, or the text itself when it has none."""

    return unframe(text) if text.startswith("<tool_output ") else text
