"""Shared projects and runners for the export tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

DESK_BLUEPRINT = """\
name: desk
description: Looks orders up and refunds them.
version: 0.2.0
defaults:
  model: anthropic:claude-sonnet-5-5
  model_settings: {temperature: 0.2}
  max_steps: 6
agents:
  - name: assistant
    description: Handles order questions.
    instructions: |
      You help with orders. Look the order up before you answer.
    tools: [lookup_order, refund, ship, delete_order]
    skills: [refund-policy]
    skill_mode: on_demand
approvals:
  - tool: refund
    when: "amount >= 100"
  - tool: ship
policies:
  - deny: [delete_order]
    reason: never delete
limits:
  max_requests: 5
  max_tool_calls: 4
  max_total_tokens: 100000
  max_duration: 2m
"""

DESK_TOOLS = '''\
"""Order tools."""

from __future__ import annotations

CALLS: list[str] = []


def lookup_order(order_id: str) -> str:
    """Return the status of an order."""

    CALLS.append(f"lookup:{order_id}")
    return f"order {order_id}: paid"


def refund(order_id: str, amount: float) -> str:
    """Refund an amount of an order."""

    CALLS.append(f"refund:{order_id}:{amount}")
    return f"refunded {amount}"


def ship(order_id: str) -> str:
    """Ship an order."""

    CALLS.append(f"ship:{order_id}")
    return "shipped"


def delete_order(order_id: str) -> str:
    """Delete an order."""

    return "deleted"


__all__ = ["delete_order", "lookup_order", "refund", "ship"]
'''

SKILL = """\
---
name: refund-policy
description: How refunds are decided. Use before refunding.
---
# Refund policy

Refund only paid orders.
"""

STRUCTURED_BLUEPRINT = """\
name: classifier
description: Classifies a message.
defaults:
  model: openai:gpt-4o-mini
agents:
  - name: sorter
    instructions: Classify the message.
    output_schema:
      type: object
      properties:
        label: {type: string, enum: [spam, ham]}
        score: {type: number, minimum: 0, maximum: 1}
        tags: {type: array, items: {type: string}}
      required: [label, score]
"""


def write_project(root: Path, blueprint: str, tools: str | None = None) -> Path:
    """Write a project folder with the blueprint and, when given, a `tools.py`."""

    root.mkdir(parents=True, exist_ok=True)
    (root / "blueprint.yaml").write_text(blueprint, encoding="utf-8")
    if tools is not None:
        (root / "tools.py").write_text(tools, encoding="utf-8")
    return root


def run_exported_tests(folder: Path) -> subprocess.CompletedProcess[str]:
    """Run the pytest suite of an export in a separate interpreter, with no awa source on the
    path (the repository's own `src` is removed from `PYTHONPATH`)."""

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
