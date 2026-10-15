# Workflows

Use a workflow when you know the order of the steps. Code runs the steps, so no model chooses what
comes next. For work where a model should decide who does what, use a [team](teams.md).

Every workflow is `AgentLike`: run it directly, make it a team member, or nest it in another
workflow. Its members share usage, limits and events with the rest of the run.

## Review in parallel, then refine until done

This workflow reads a contract, runs two reviews at the same time, then lets an editor refine the
result until it reports `done`:

```python title="review_loop.py"
from pydantic import BaseModel

from awa import Agent, Loop, Parallel, Sequential
from awa.testing import FakeChatModel, FunctionChatModel, structured_output


class Review(BaseModel):
    text: str
    done: bool  # true when nothing high-severity remains


def scripted(text: str) -> FunctionChatModel:
    return FunctionChatModel(lambda call: text)


intake = Agent("intake", model=scripted("Obligations: pay within 30 days; keep data in the EU."))
legal = Agent("legal", model=scripted("Legal: the liability cap is missing."))
privacy = Agent("privacy", model=scripted("Privacy: no finding."))
editor = Agent(
    "editor",
    output_type=Review,
    model=FakeChatModel(
        [
            structured_output(Review, {"text": "Add a liability cap.", "done": False}),
            structured_output(Review, {"text": "No high-severity finding left.", "done": True}),
        ]
    ),
)

reviews = Parallel("reviews", [legal, privacy], fail_fast=False)
refine = Loop("refine", editor, until="done", max_iterations=3)
contract_review = Sequential("contract_review", [intake, reviews, refine], carry="all")

result = contract_review.run_sync("<contract text>")
print(result.output)
```

```text title="review_loop.out"
text='No high-severity finding left.' done=True
```

The loop stopped in its second round because the editor's typed field `done` was true. No text is
searched for a marker.

## The building blocks

| Combinator | Runs | Output |
| --- | --- | --- |
| `Sequential(name, steps, carry="previous")` | steps in order | the last step's output |
| `Parallel(name, branches, aggregate=None, max_concurrency=None, fail_fast=True)` | branches at the same time on the same input | `{branch: output}`, or what `aggregate` returns |
| `Loop(name, body, until=..., max_iterations=..., next_input=None)` | `body` repeatedly | the last round's output |
| `Map(name, member, part_size=1, max_concurrency=None, split=None)` | `member` once per part of a list, in parallel | one result per item, in input order |
| `Checked(name, worker, judge=..., rubric=..., reviewers=..., max_iterations=3)` | a worker gated by a judge | the accepted draft ([Roles](teams.md#roles-in-a-blueprint)) |
| `ProcessWorkflow(name, steps)` | `ProcessStep`s in order, each with documents and checks that run before the next step | the last step's output; a blueprint's `process:` builds into it ([The process section](process-section.md)) |

**Sequential.** What each step receives depends on `carry`:

- `carry="previous"`: the previous step's text, or the JSON of its structured output.
- `carry="all"`: the request and every earlier step, each labeled `[<agent>]: ...`.

**Parallel.** Each branch runs as its own LangGraph `Send`, so it is checkpointed and can pause on
its own. With `fail_fast=True`, the first failure cancels the others. With `False`, failures are
collected in `result.metadata["errors"]`.

**Loop.** `until` is the name of a boolean field of the body's structured output (checked when
the loop is built), or a predicate on the round's result. The loop also ends:

- after `max_iterations`;
- with `stopped_reason="no_progress"` when the body returns the same result `max_repeats` times in
  a row (default `AWA_MAX_IDENTICAL_OUTPUTS`, 2);
- with `stopped_reason="step_limit"` before LangGraph's recursion limit, so it never ends with a
  graph error.

## A list of items

`Map` splits its input into items and runs the member on each item, in parallel. An input can be
a JSON array, a JSON object with one list field, or one item per line. For anything else, pass
`split=`:

```python title="map_items.py"
import json

from pydantic import BaseModel

from awa import Agent
from awa.testing import FunctionChatModel, ModelCall, structured_output
from awa.workflows import Map


class Invoice(BaseModel):
    number: str
    total: float


def read(call: ModelCall):
    item = json.loads(call.last_message.content)
    return structured_output(Invoice, {"number": item["id"], "total": item["amount"]})


reader = Agent("invoice_reader", output_type=Invoice, model=FunctionChatModel(read))
extract = Map("extract", reader, max_concurrency=4)

items = [{"id": "R-1", "amount": 12.5}, {"id": "R-2", "amount": 40.0}, {"id": "R-3", "amount": 7.2}]
result = extract.run_sync(json.dumps(items))
print([invoice.number for invoice in result.output])
print(result.metadata["missing"])
```

```text title="map_items.out"
['R-1', 'R-2', 'R-3']
[]
```

`Map` checks without a model that every item has exactly one result:

- Items whose part failed are tried once more.
- Items still without a result are listed in `metadata["missing"]`, with their errors in
  `metadata["errors"]`. They are never dropped silently.
- With `part_size` above 1, each part is a JSON array, and the member answers with a list of the
  same length.

In a blueprint:

```yaml
workflow:
  sequential:
    - map: invoice_reader
      part_size: 5
      max_concurrency: 4
    - writer
```

## Pauses and resume

A step can pause for a person, for example on a tool that needs an approval. Give the workflow a
checkpointer. The whole workflow then pauses, and `resume` continues exactly where it stopped:

```python title="pause.py"
import asyncio

from awa import Agent, Sequential, tool
from awa.persistence import StateInMemorySaver
from awa.testing import FakeChatModel, FunctionChatModel, tool_call


@tool(approval=True)
def publish_article(title: str) -> str:
    """Publish an article on the website."""
    return f"published {title}"


writer_model = FunctionChatModel(lambda call: "Launch post: the new release is out.")
writer = Agent("writer", model=writer_model)
publisher = Agent(
    "publisher",
    tools=[publish_article],
    model=FakeChatModel([tool_call("publish_article", {"title": "the new release is out"}), "Published."]),
)
pipeline = Sequential("content", [writer, publisher], checkpointer=StateInMemorySaver())


async def main() -> None:
    paused = await pipeline.run("Write and publish the launch post.")
    pending = paused.interrupts[0]
    print(pending["agent"], pending["value"]["tool"], pending["value"]["args"])
    done = await pipeline.resume(paused.thread_id, {"action": "approve"})
    print(done.output, "| writer calls:", len(writer_model.calls))


asyncio.run(main())
```

```text title="pause.out"
publisher publish_article {'title': 'the new release is out'}
Published. | writer calls: 1
```

The writer ran once: finished members are not run again.

- In a `Parallel`, finished branches keep their results. Answer several pending approvals with a
  mapping of interrupt id to decision.
- A `Loop` keeps its round count.
- A run that was cancelled, timed out or crashed resumes from its last finished member with
  `resume(thread_id, None)`.
- A workflow nested in a checkpointed run persists on that run's thread.
- Without any checkpointer, an approval raises `AgentRunError`.

Workflows have the run API of agents (`run`, `run_sync`, `stream`, `start`, `resume`). They keep
no conversation, so `history` raises `ValueError`.

## Agents inside your own graph

Sometimes you need routing on typed state, or an approval at an exact point. Then build a
LangGraph graph and call awa agents from its nodes. A model may recommend; Python decides the
route:

```python title="graph_node.py"
import asyncio
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from awa import Agent
from awa.testing import FakeChatModel, FunctionChatModel, structured_output


class Category(BaseModel):
    label: Literal["billing", "support"]


class State(TypedDict, total=False):
    ticket: str
    category: str
    reply: str


classifier = Agent("classifier", output_type=Category, model=FakeChatModel([structured_output(Category, {"label": "billing"})]))
billing = Agent("billing", model=FunctionChatModel(lambda call: "Invoice 5 is paid."))
support = Agent("support", model=FunctionChatModel(lambda call: "Please restart the device."))


async def classify(state: State) -> dict:
    return {"category": (await classifier.run(state["ticket"])).output.label}


async def answer_billing(state: State) -> dict:
    return {"reply": (await billing.run(state["ticket"])).output}


async def answer_support(state: State) -> dict:
    return {"reply": (await support.run(state["ticket"])).output}


builder = StateGraph(State)
builder.add_node("classify", classify)
builder.add_node("billing", answer_billing)
builder.add_node("support", answer_support)
builder.add_edge(START, "classify")
builder.add_conditional_edges("classify", lambda s: s["category"])  # Python decides the route
builder.add_edge("billing", END)
builder.add_edge("support", END)
graph = builder.compile()

print(asyncio.run(graph.ainvoke({"ticket": "Is invoice 5 paid?"})))
```

```text title="graph_node.out"
{'ticket': 'Is invoice 5 paid?', 'category': 'billing', 'reply': 'Invoice 5 is paid.'}
```

LangGraph's `interrupt()` pauses a graph at an exact point. `Command(resume=...)` continues it
with the person's decision:

```python title="gate.py"
import asyncio
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from awa.persistence import StateInMemorySaver


class InvoiceState(TypedDict, total=False):
    invoice_id: str
    amount: float
    status: str
    reason: str


def approval(state: InvoiceState) -> dict[str, Any]:
    if state["amount"] <= 10_000:
        return {"status": "approved"}
    decision = interrupt({"invoice": state["invoice_id"], "amount": state["amount"]})
    if decision.get("action") == "approve":
        return {"status": "approved"}
    return {"status": "rejected", "reason": decision.get("message", "")}


builder = StateGraph(InvoiceState)
builder.add_node("approval", approval)
builder.add_edge(START, "approval")
builder.add_edge("approval", END)
graph = builder.compile(checkpointer=StateInMemorySaver())


async def main() -> None:
    config = {"configurable": {"thread_id": "inv-1042"}}
    paused = await graph.ainvoke({"invoice_id": "INV-1042", "amount": 12_400}, config)
    print(paused["__interrupt__"][0].value)
    done = await graph.ainvoke(Command(resume={"action": "approve"}), config)
    print(done["status"])


asyncio.run(main())
```

```text title="gate.out"
{'invoice': 'INV-1042', 'amount': 12400}
approved
```

- LangGraph's `RetryPolicy` retries a node that fails on a transient error:
  `builder.add_node("fetch", fetch, retry_policy=RetryPolicy(max_attempts=3))`.
- `awa.testing.assert_terminates_via_own_budget(graph, state)` proves that a loop of your graph
  stops on its own budget, not on LangGraph's recursion limit.
- Runs started in a node are top-level runs with their own usage, unless the graph itself runs
  inside an awa run.

Example: `examples/04_workflows.py`.
