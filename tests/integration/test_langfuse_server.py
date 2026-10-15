"""Against a real Langfuse: the scripted demo team arrives with its structure (no model key).

Runs only when a Langfuse server answers: set AWA_TEST_LANGFUSE_URL (for example
http://localhost:3000) and the project's LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY. Start one with
docs/develop/observability.md#langfuse.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytest.importorskip("opentelemetry.exporter.otlp.proto.http")

URL = os.environ.get("AWA_TEST_LANGFUSE_URL", "").rstrip("/")
PUBLIC = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
SECRET = os.environ.get("LANGFUSE_SECRET_KEY", "")


def _reachable() -> bool:
    if not (URL and PUBLIC and SECRET):
        return False
    try:
        with urllib.request.urlopen(f"{URL}/api/public/health", timeout=5) as response:  # noqa: S310
            return response.status == 200
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason="no Langfuse: set AWA_TEST_LANGFUSE_URL and the LANGFUSE_* keys"
)


def _observations(since: datetime) -> list[dict[str, Any]]:
    token = base64.b64encode(f"{PUBLIC}:{SECRET}".encode()).decode()
    query = urllib.parse.urlencode(
        {
            "fromStartTime": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 1000,
            "fields": "core,basic,usage,metadata",
        }
    )
    request = urllib.request.Request(
        f"{URL}/api/public/v2/observations?{query}", headers={"Authorization": f"Basic {token}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - local server
        return list(json.load(response)["data"])


def test_the_demo_team_arrives_with_agents_tools_approval_tokens_and_metadata() -> None:
    from _helpers import traced_team
    from awa.observability import connect_langfuse

    since = datetime.now(UTC) - timedelta(seconds=5)
    request_id = f"req-it-{time.time_ns()}"
    connection = connect_langfuse(
        public_key=PUBLIC, secret_key=SECRET, base_url=URL, prices=traced_team.PRICES
    )
    team = traced_team.build_team(hooks=[connection.subscriber])
    metadata = {**traced_team.METADATA, "awa.request_id": request_id}

    async def scenario() -> None:
        paused = await team.run(traced_team.TASK, metadata=metadata)
        assert paused.thread_id is not None
        await team.resume(paused.thread_id, {"action": "approve"}, metadata=metadata)

    asyncio.run(scenario())
    connection.shutdown()

    rows: list[dict[str, Any]] = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        rows = [
            r
            for r in _observations(since)
            if (r.get("metadata") or {}).get("request_id") == request_id
        ]
        if len({r["traceId"] for r in rows}) >= 2 and len(rows) >= 24:
            break
        time.sleep(2)
    kinds = Counter(r["type"] for r in rows)
    names = {r["name"] for r in rows}
    assert len({r["traceId"] for r in rows}) == 2  # paused run and resumed run
    assert len({r.get("sessionId") for r in rows}) == 1  # one thread, one session
    assert {"CHAIN", "AGENT", "GENERATION", "TOOL", "EVENT"} <= set(kinds)
    assert {"invoke_agent lead", "invoke_agent researcher", "invoke_agent payments"} <= names
    assert {"awa.approval_requested", "awa.approval_decided"} <= names
    levels = {(r["name"], r["level"]) for r in rows}
    assert ("execute_tool lookup_order", "ERROR") in levels or ("lookup_order", "ERROR") in levels
    generations = [r for r in rows if r["type"] == "GENERATION"]
    assert all((r.get("usageDetails") or {}).get("input", 0) > 0 for r in generations)
    assert all(r.get("totalCost") for r in generations)
    assert all((r.get("metadata") or {}).get("tenant") == "demo-shop" for r in rows)
