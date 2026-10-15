# Agents and tools

One agent and what it works with: tools, built-in toolkits, skills, memory, guardrails and the tools
of MCP servers.

## Example

An agent calls a tool and answers from its result:

```python title="agents_example.py"
import asyncio

from awa import Agent, tool
from awa.testing import FakeChatModel, tool_call


@tool
def stock(item: str) -> int:
    """How many units of an item are in stock."""
    return {"widget": 12}.get(item, 0)


model = FakeChatModel([tool_call("stock", {"item": "widget"}), "12 widgets are in stock."])
clerk = Agent("clerk", tools=[stock], model=model)
result = asyncio.run(clerk.run("How many widgets are left?"))
print(result.output)
print("the tool returned", model.calls[1].last_message.content)
```

It prints:

```text title="agents_example.out"
12 widgets are in stock.
the tool returned 12
```

::: awa.agents
    options:
      show_root_full_path: true
      inherited_members: [run_sync, start, stream]

::: awa.agents.graph_agent
    options:
      show_root_full_path: true
      inherited_members: [run_sync, start, stream]

::: awa.tools
    options:
      show_root_full_path: true

::: awa.tools.importing
    options:
      show_root_full_path: true

::: awa.toolkits
    options:
      show_root_full_path: true

::: awa.skills
    options:
      show_root_full_path: true

::: awa.skills.importing
    options:
      show_root_full_path: true

::: awa.memory
    options:
      show_root_full_path: true

::: awa.guardrails
    options:
      show_root_full_path: true

::: awa.mcp
    options:
      show_root_full_path: true
