"""`examples/observability/trace_demo.py`: a scripted team run sent to Langfuse.

A local OTLP/HTTP endpoint stands in for Langfuse; the documented command runs literally as a
subprocess with the `LANGFUSE_*` variables pointing at it, in a folder without a `.env`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http")

from _helpers.langfuse_receiver import Receiver  # noqa: E402
from _helpers.polling import SUBPROCESS_TIMEOUT  # noqa: E402
from awa.observability import connect_langfuse  # noqa: E402

DEMO = Path(__file__).resolve().parents[2] / "examples" / "observability" / "trace_demo.py"
sys.path.insert(0, str(DEMO.parent))

import trace_demo  # noqa: E402

EXPECTED_TREE = [
    ("invoke_workflow desk", "chain"),
    ("  invoke_agent lead", "agent"),
    ("    chat demo-model", "generation"),
    ("    execute_tool delegate_task", "tool"),
    ("    awa.delegation", "event"),
    ("    invoke_agent researcher", "agent"),
    ("      chat demo-model", "generation"),
    ("      execute_tool lookup_price", "tool"),
    ("      chat demo-model", "generation"),
    ("    awa.delegation_result", "event"),
    ("    chat demo-model", "generation"),
]


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    rec = Receiver()
    rec.thread.start()
    yield rec
    rec.server.shutdown()
    rec.server.server_close()


def tree(spans: list[Any]) -> list[tuple[str, str]]:
    """The spans as indented names in start order, each with its Langfuse observation type."""

    ids = {span.span_id for span in spans}
    lines: list[tuple[str, str]] = []

    def walk(parent: str | None, depth: int) -> None:
        children = [s for s in spans if (s.parent_id if s.parent_id in ids else None) == parent]
        for span in sorted(children, key=lambda s: s.start_ns):
            kind = span.attributes.get("langfuse.observation.type", "")
            lines.append(("  " * depth + span.name, kind))
            walk(span.span_id, depth + 1)

    walk(None, 0)
    return lines


def langfuse_environment(url: str) -> dict[str, str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("LANGFUSE_")}
    environment.update(
        LANGFUSE_PUBLIC_KEY="pk-lf-demo", LANGFUSE_SECRET_KEY="sk-lf-demo", LANGFUSE_BASE_URL=url
    )
    return environment


def test_the_team_run_reaches_langfuse_as_one_typed_tree(receiver: Receiver) -> None:
    connection = connect_langfuse(public_key="pk", secret_key="sk", base_url=receiver.url)
    team = trace_demo.build_team(connection.subscriber)
    result = asyncio.run(team.run("What does the Widget cost?"))
    connection.shutdown()

    assert result.output == "The Widget costs 49.00 EUR."
    spans = receiver.spans()
    assert tree(spans) == EXPECTED_TREE
    assert len({span.trace_id for span in spans}) == 1


def test_the_documented_command_sends_the_trace(receiver: Receiver, tmp_path: Path) -> None:
    demo = subprocess.run(
        [sys.executable, str(DEMO)],
        cwd=tmp_path,
        env=langfuse_environment(receiver.url),
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )

    assert demo.returncode == 0, demo.stderr
    assert "The Widget costs 49.00 EUR." in demo.stdout
    assert f"trace sent to {receiver.url}/api/public/otel/v1/traces" in demo.stdout
    spans = receiver.spans()
    assert tree(spans) == EXPECTED_TREE
    assert all(s.attributes.get("langfuse.trace.metadata.example") == "trace_demo" for s in spans)
    assert b"sk-lf-demo" not in b"".join(receiver.bodies)


def test_without_keys_the_command_says_what_is_missing(tmp_path: Path) -> None:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("LANGFUSE_")}
    demo = subprocess.run(
        [sys.executable, str(DEMO)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )

    assert demo.returncode == 1
    assert "cannot connect to Langfuse" in demo.stderr and "LANGFUSE_PUBLIC_KEY" in demo.stderr
