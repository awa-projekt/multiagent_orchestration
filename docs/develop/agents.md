# Agents

An `Agent` is one model with instructions, tools and limits. It works in a loop: call the model,
run the tools the model asks for, call the model again, until it can answer. This page shows how
to run an agent, every option it takes, and how to check its answer.

New to awa? Start with the [Quickstart](quickstart.md): it builds and runs a first agent. An
agent runs on LangChain's `create_agent` through the
[engine port](architecture.md#the-engine-port). awa adds typed results, a run context, events,
limits and composition into teams.

## Run an agent

| Method | Use |
| --- | --- |
| `await agent.run(input, ...)` | Run to completion and return a `RunResult`. |
| `agent.run_sync(input, ...)` | The same from synchronous code (not inside a running event loop). |
| `agent.stream(input, ...)` | An async iterator of `RunEvent`s that ends with `RunCompletedEvent`. |
| `agent.start(input, ...)` | Start in the background; returns a `RunHandle` you can cancel ([Safety](safety.md#stop-a-run)). |
| `await agent.resume(thread_id, decision)` | Continue a run paused for an approval. |

`input` is a prompt or a list of messages. Every method also takes:

- `deps`: your own objects for the run ([below](#pass-your-own-objects-deps)).
- `history`: earlier messages.
- `thread_id`: the conversation to continue (needs a checkpointer).
- `usage_limits`: caps for a top-level run.
- `timeout`: seconds for the whole run tree.
- `metadata`: request values such as `awa.request_id`, readable from the run context.

The *run tree* is this run and every run it starts, such as a delegation to another agent.

`RunResult` holds:

- `output`: the answer, typed when you set `output_type`.
- `text`: the final assistant text.
- `messages`: the thread; `new_messages()`: the messages of this run only.
- `usage`: tokens, requests and tool calls of the whole run tree; `usage.by_agent` per agent.
- `duration_ms`, `thread_id`, `interrupts`, `handoff_path` and `metadata`.

Building an agent needs no credentials: the model and the graph are created on the first run.

## Options

| Option | Meaning |
| --- | --- |
| `name` | Starts with a letter; letters, digits, `_` and `-`; at most 48 characters. It becomes part of tool names such as `transfer_to_<name>`. |
| `description` | When to use the agent. Other agents read it when they decide whom to delegate to. |
| `instructions` | A string, or a function of the `RunContext` (sync or async). |
| `tools` | Plain functions, `@tool` functions, LangChain tools, a `Toolbox`. Names must be unique per agent ([Tools](tools.md)). |
| `skills`, `skill_mode`, `skill_registry` | Know-how loaded inline or on demand ([Skills](skills.md)). |
| `model`, `model_settings`, `model_registry` | A profile, `provider:model`, or a chat model instance ([Models](models.md)). |
| `output_type`, `output_validators`, `output_retries` | A typed, checked answer (below). |
| `max_steps` | Model calls per run, default 25. |
| `input_guardrails`, `output_guardrails` | Deterministic checks before and after the run ([Safety](safety.md#guardrails)). |
| `tool_policy` | Allow, ask, rewrite or deny each tool call at run time ([Tools](tools.md#tool-policies-at-run-time)). |
| `memory`, `compaction`, `checkpointer`, `store` | Long-term memory, long conversations and threads ([Memory](memory.md)). |
| `middleware`, `hooks` | LangChain agent middleware; event subscribers ([Observability](observability.md)). |
| `capabilities`, `tags`, `metadata` | Data for the agent card, directories and traces. |

## Structured output and output checks

Set `output_type` to a pydantic model, a dataclass or a `TypedDict`. `result.output` is then a
validated instance. On Python 3.11, define a `TypedDict` with `typing_extensions.TypedDict`.

You can add your own checks. A validator is `fn(output)` or `fn(output, ctx)`, sync or async:

- To reject, it raises `ModelRetry("reason")`.
- To accept, it returns nothing, or a replacement.

A rejected answer goes back to the model with the reasons, and the model answers again in the
same run:

```python title="output_check.py"
import asyncio

from pydantic import BaseModel

from awa.agents import Agent, ModelRetry
from awa.testing import FakeChatModel, structured_output


class Invoice(BaseModel):
    number: str
    total: float


def total_is_positive(invoice: Invoice) -> None:
    if invoice.total <= 0:
        raise ModelRetry("total must be greater than zero")


model = FakeChatModel(
    [
        structured_output(Invoice, {"number": "R-1", "total": 0}),
        structured_output(Invoice, {"number": "R-1", "total": 12.5}),
    ]
)
agent = Agent("reader", model=model, output_type=Invoice, output_validators=[total_is_positive])


async def main() -> None:
    result = await agent.run("Read the invoice.")
    print(result.output)
    print("model calls:", len(model.calls))
    print(model.calls[1].messages[-1].text)


asyncio.run(main())
```

```text title="output_check.out"
number='R-1' total=12.5
model calls: 2
Your answer failed 1 check(s) (retry 1 of 2):
- total must be greater than zero
Fix exactly these points and answer again in the same format; keep everything else.
```

- The model is told the reasons, never the rejected output.
- All validators run, so one retry names every problem.
- Without `output_type`, a validator checks the final text.
- One budget, `output_retries` (default 2), covers three cases: a schema mismatch, a rejecting
  validator, and an output guardrail with `on_fail="retry"`. Each retry is a model call. It counts
  against `max_steps` and the usage limits.
- When the budget is used up, the run raises `OutputRejectedError` (`problems`, `attempts`),
  `StructuredOutputError` for a schema that never matched, or `GuardrailTripwireError`.
- `ModelRetry` raised inside a tool becomes the tool's error result.
- The JSON schema of the output type is on the agent card (`agent.card.output_schema`).

## Conversations

To continue a conversation, give the agent a checkpointer and pass the same `thread_id` again:

```python title="conversation.py"
from awa import Agent
from awa.persistence import StateInMemorySaver
from awa.testing import FunctionChatModel


def count_questions(call):
    asked = [m for m in call.messages if m.type == "human"]
    return f"You have asked {len(asked)} question(s) in this conversation."


assistant = Agent("assistant", model=FunctionChatModel(count_questions), checkpointer=StateInMemorySaver())
first = assistant.run_sync("My name is Ada.")
second = assistant.run_sync("What is my name?", thread_id=first.thread_id)
print(first.output)
print(second.output)
print(len(second.messages), len(second.new_messages()))
```

```text title="conversation.out"
You have asked 1 question(s) in this conversation.
You have asked 2 question(s) in this conversation.
4 2
```

- With a checkpointer and no `thread_id`, awa creates a new thread and returns it in
  `result.thread_id`.
- Without a checkpointer, pass the earlier messages as `history=`.
- Durable checkpointers for production are on [Memory](memory.md).

## Pass your own objects: deps

Your application objects, such as a database client or the current user, travel as `deps`.
Dynamic instructions and tools read them from the run context:

```python title="run_context.py"
from dataclasses import dataclass

from awa import Agent, RunContext, current_run_context
from awa.testing import FakeChatModel, tool_call


@dataclass
class SupportDeps:
    customer_id: str
    orders: dict[str, str]


def instructions(ctx: RunContext[SupportDeps]) -> str:
    return f"You help customer {ctx.deps.customer_id}. Be concise."


def open_orders() -> str:
    """List the current customer's open orders."""
    deps: SupportDeps = current_run_context().deps
    return ", ".join(f"{order}: {status}" for order, status in deps.orders.items())


model = FakeChatModel([tool_call("open_orders"), "A-1 has shipped."])
support = Agent("support", instructions=instructions, tools=[open_orders], model=model)

result = support.run_sync("What is still open?", deps=SupportDeps("C-42", {"A-1": "shipped"}))
print(model.calls[0].messages[0].content)
print(model.calls[1].last_message.content)
print(result.output)
```

```text title="run_context.out"
You help customer C-42. Be concise.
A-1: shipped
A-1 has shipped.
```

The first line is the system prompt the model received. The second is the tool result.

- Dynamic instructions may be sync or async. They are evaluated at the start of every run.
- Nested runs (delegations, agents used as tools) inherit the deps.
- `model=` takes a scripted model here. Leave it out for the `default` profile, or name a profile
  or a `provider:model` ([Models](models.md)).

## Agents as tools, variants and overrides

- `as_tool()` exposes an agent as a tool that takes a self-contained `task`. The nested run shares
  the caller's usage, limits and events. For briefs, parallel work, peers and handoffs, use a
  [Team](teams.md).
- `agent.clone(max_steps=6, ...)` returns an independent copy with changed options.
- `agent.with_tools(extra_tool, extra_instructions="...")` returns a variant that shares the
  agent's identity.
- `with agent.override(model=..., deps=...):` swaps the model or the deps for a while, also where
  the agent runs nested inside a team. Tests use it ([Testing](testing.md)).

## The agent card

`agent.card` is an `AgentCard`: name, description, capabilities, skills, tool names, model, role,
tags, output schema and version. Directories and teams work from cards.

```python title="card.py"
from awa import Agent

agent = Agent(
    "billing",
    description="Explains invoices and refunds.",
    capabilities=["billing.refunds"],
    tags=["finance"],
)
print(agent.card.summary())
```

```text title="card.out"
billing: Explains invoices and refunds. | capabilities: billing.refunds
```

## Middleware and hooks

- `middleware=[...]` passes LangChain agent middleware to `create_agent`, for example
  `TodoListMiddleware` for a per-run checklist.
- `hooks=[...]` takes `AgentHooks` subclasses or plain callables. They receive every event of the
  agent's runs.

The order of awa's own middleware is on [Architecture](architecture.md#inside-an-agent).

## What can go wrong

| You see | Cause and fix |
| --- | --- |
| `ValueError: Invalid agent name ...` | Start with a letter; use letters, digits, `_` and `-`; at most 48 characters. |
| `ValueError: Agent '...' has duplicate tool names: ...` | Two tools share a name. Rename one (`@tool("name")`) or drop it. |
| `MaxStepsExceededError` | The agent kept calling tools without answering. Tighten the instructions or tools; raise `max_steps` only when the task needs it. |
| `StructuredOutputError` | The model never produced the `output_type`. Describe the fields in the instructions or use a stronger model. |
| `OutputRejectedError` | A validator rejected every retry. `error.problems` lists the reasons. |
| `ModelProviderError: no API key for ...` | Set the provider key; `awa doctor` shows which one is missing. |

Reference: [`Agent`](../reference/api/agents.md#awa.agents).
