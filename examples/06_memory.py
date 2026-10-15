"""Agent memory: an assistant that remembers each customer across threads.

The account assistant saves durable facts with its `remember` tool, looks them
up with `recall`, and deletes outdated ones with `forget`. Memory is scoped per
user (`deps={"user_id": ...}`), so one customer never sees another's
memories, and `auto_recall` adds the memories relevant to each new request
as a context message before the user's input. `Compaction` keeps a long
support thread within its token budget by summarizing older turns.

Run:
    pip install -e ".[anthropic]"
    export ANTHROPIC_API_KEY=...
    python examples/06_memory.py
    # No model key? Run its offline tests instead:
    #   python -m pytest tests/examples/test_06_memory.py
"""

from __future__ import annotations

import asyncio

from langgraph.store.base import BaseStore

from _support import require_model_credentials
from awa import Agent
from awa.memory import AgentMemory, Compaction, MemoryScope
from awa.persistence import StateInMemorySaver


def build_account_assistant(store: BaseStore | None = None) -> Agent:
    """Build the account assistant with per-user long-term memory.

    Args:
        store: Where memories are kept; an in-memory store by default. Pass
            the store from `awa.persistence.open_store` to keep them in Postgres.
    """

    return Agent(
        "account_assistant",
        description="Helps customers with billing and account questions.",
        instructions=(
            "You help customers with billing and account questions. When a customer "
            "states a lasting preference or fact about their account, remember it. When "
            "a remembered fact changes, forget the old memory and remember the new one."
        ),
        memory=AgentMemory(store, scope="user", auto_recall=3),
        compaction=Compaction(max_tokens=6000, keep_last_messages=12),
        checkpointer=StateInMemorySaver(),
    )


async def main() -> None:
    require_model_credentials()
    assistant = build_account_assistant()

    first = await assistant.run(
        "Please send my invoices in EUR and address them to our finance team.",
        deps={"user_id": "customer-17"},
    )
    print(f"customer-17, first thread:\n  {first.text}\n")

    later = await assistant.run(
        "Which currency do you use for my invoices?", deps={"user_id": "customer-17"}
    )
    print(f"customer-17, a new thread:\n  {later.text}\n")

    other = await assistant.run(
        "Which currency do you use for my invoices?", deps={"user_id": "customer-99"}
    )
    print(f"customer-99 (no memories):\n  {other.text}\n")

    assert assistant.memory is not None
    owner = MemoryScope(agent=assistant.name, user_id="customer-17")
    print("Stored for customer-17:")
    for item in await assistant.memory.list_items(owner=owner):
        print(f"  {item.id}: {item.content}")


if __name__ == "__main__":
    asyncio.run(main())
