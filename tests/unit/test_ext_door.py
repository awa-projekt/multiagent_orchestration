"""`awa.ext`: every name is the implementation object, and importing it loads no LangChain."""

from __future__ import annotations

import subprocess
import sys

import pytest

import awa.ext
from awa._internal import aio, ids
from awa._internal import logging as internal_logging
from awa.kernel import redaction

OWNERS = {
    "run_sync": (aio, "run"),
    "LoopLocal": (aio, "LoopLocal"),
    "new_id": (ids, "new_id"),
    "get_logger": (internal_logging, "get_logger"),
    "RegexRedactor": (redaction, "RegexRedactor"),
    "RedactionPattern": (redaction, "RedactionPattern"),
    "SECRET_PATTERNS": (redaction, "SECRET_PATTERNS"),
    "PII_PATTERNS": (redaction, "PII_PATTERNS"),
    "redact_secrets": (redaction, "redact_secrets"),
    "redact_values": (redaction, "redact_values"),
    "redact_mapping": (redaction, "redact_mapping"),
}


def test_the_door_exports_exactly_the_documented_names() -> None:
    assert sorted(awa.ext.__all__) == sorted(OWNERS)


@pytest.mark.parametrize("name", sorted(OWNERS))
def test_a_name_is_the_implementation_object(name: str) -> None:
    owner, attribute = OWNERS[name]
    assert getattr(awa.ext, name) is getattr(owner, attribute)


def test_importing_the_door_loads_no_langchain() -> None:
    code = (
        "import sys, awa.ext; "
        "bad = [m for m in sys.modules if m.split('.')[0] in ('langchain', 'langchain_core', 'langgraph')]; "
        "sys.exit(1 if bad else 0)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
