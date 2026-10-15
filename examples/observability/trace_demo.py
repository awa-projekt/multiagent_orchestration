"""See a team run as a trace in your local Langfuse, without a model key.

The models are scripted (`awa.testing.FakeChatModel`), so the run makes no model request. Start
Langfuse as docs/guide/langfuse.md describes, put the four lines of `awa-langfuse.env` into the
`.env` of the folder you run in (or into the environment), then:

    pip install -e ".[langfuse]"
    python examples/observability/trace_demo.py

Open Langfuse, choose **Tracing**, and the trace `desk` shows the lead, the delegated researcher,
every model call and the tool call. To trace your own agents, replace the scripted team with yours
and keep `connect_langfuse()` and its subscriber.
"""

from __future__ import annotations

import asyncio
import sys

from awa import Agent, Team
from awa.errors import ConfigurationError
from awa.observability import OpenTelemetrySubscriber, connect_langfuse
from awa.testing import FakeChatModel, tool_call


def lookup_price(product: str) -> str:
    """Return the list price of a product."""

    return f"{product}: 49.00 EUR"


def build_team(tracing: OpenTelemetrySubscriber) -> Team:
    """A lead that delegates a price lookup to a researcher; both models are scripted."""

    researcher = Agent(
        "researcher",
        description="Looks up prices.",
        model=FakeChatModel(
            [tool_call("lookup_price", {"product": "Widget"}), "The Widget costs 49.00 EUR."],
            model_name="demo-model",
        ),
        tools=[lookup_price],
    )
    lead = Agent(
        "lead",
        description="Answers price questions.",
        model=FakeChatModel(
            [
                tool_call(
                    "delegate_task", {"agent": "researcher", "task": "What does the Widget cost?"}
                ),
                "The Widget costs 49.00 EUR.",
            ],
            model_name="demo-model",
        ),
    )
    return Team("desk", orchestrator=lead, members=[researcher], hooks=[tracing])


async def main() -> None:
    try:
        connection = connect_langfuse(metadata={"example": "trace_demo"})  # reads LANGFUSE_*
    except ConfigurationError as exc:
        sys.exit(f"cannot connect to Langfuse: {exc}")
    try:
        result = await build_team(connection.subscriber).run("What does the Widget cost?")
        print(result.output)
    finally:
        connection.shutdown()  # sends the buffered spans before the process ends
    print(f"trace sent to {connection.endpoint}")


if __name__ == "__main__":
    asyncio.run(main())
