# Quickstart

awa runs a business process with AI agents: a model does the work, and Python code sets the
limits it works in. This page takes you from zero to a running agent in Python, then adds a team
and a workflow.

Every example runs as written, without a model key: a scripted model plays the model. To use a
real model, you leave out one argument.

## Install for development

In the awa folder, with Python 3.11 or newer:

```console
uv sync --extra dev --extra docs
uv run awa doctor
```

`awa doctor` checks Python, the environment, the extras and the model credentials. For each
problem it prints the fix. It never calls a model unless you add `--check-models`.

Without uv: run `python -m venv .venv`, activate it, then `pip install -e ".[dev,docs]"`. To use
awa in your own project, see [Install in your own project](#install-in-your-own-project).

## One agent

An `Agent` is a model, instructions and tools. A tool is a typed Python function with a
docstring. Create `first_agent.py`:

```python title="first_agent.py"
from awa import Agent
from awa.testing import FakeChatModel, tool_call

ORDERS = {"A-1003": "delayed, new delivery date 9 October"}


def lookup_order(order_id: str) -> str:
    """Return the status of an order."""
    return ORDERS.get(order_id, "unknown order")


model = FakeChatModel(  # plays the model: first a tool call, then the answer
    [tool_call("lookup_order", {"order_id": "A-1003"}), "Order A-1003 arrives on 9 October."]
)
agent = Agent("support", instructions="Look orders up before you answer.", tools=[lookup_order], model=model)

result = agent.run_sync("Where is order A-1003?")
print(result.output)
print(result.usage.requests, "model calls,", result.usage.tool_calls, "tool call")
print(model.calls[1].last_message.content)
```

`python first_agent.py` prints:

```text title="first_agent.out"
Order A-1003 arrives on 9 October.
2 model calls, 1 tool call
delayed, new delivery date 9 October
```

What happened:

1. The model asked for the tool `lookup_order`.
2. awa ran the tool and sent its result back to the model.
3. The model answered.

The last line is what the model saw on its second call.

### Use a real model

Leave out `model=`. The agent then uses the `default` profile, an Anthropic model unless you set
another one. Put the key into the environment or into a `.env` file in the working directory:

```text
ANTHROPIC_API_KEY=...
```

`awa setup` writes that file for you and never prints the key. You need the key only to run an
agent, not to build one: the model is created on the first run. Other providers and the profiles
`fast` and `strong` are on [Models](models.md).

## Typed output

Set `output_type`, and `result.output` is a validated object instead of text:

```python title="typed_output.py"
from pydantic import BaseModel

from awa import Agent
from awa.testing import FakeChatModel, structured_output


class Invoice(BaseModel):
    vendor: str
    total: float
    currency: str


model = FakeChatModel([structured_output(Invoice, {"vendor": "ACME", "total": 120.5, "currency": "EUR"})])
extractor = Agent("extractor", instructions="Extract the invoice fields.", output_type=Invoice, model=model)

invoice = extractor.run_sync("ACME GmbH, total 120.50 EUR").output
print(type(invoice).__name__, invoice.total, invoice.currency)
```

```text title="typed_output.out"
Invoice 120.5 EUR
```

## A team

Use a team when a model should decide who does what. A `Team` gives one agent, the orchestrator,
a `delegate_task` tool for its members. The team code checks the limits before each call; the
model only chooses within them:

```python title="team.py"
from awa import Agent, Team, TeamLimits
from awa.testing import FakeChatModel, tool_call

researcher = Agent(
    "researcher",
    description="Finds facts and cites sources.",
    model=FakeChatModel(["Heat pumps cut heating emissions by about 60 % (example.org/study)."]),
)
lead = Agent(
    "lead",
    description="Plans the work, delegates it and writes the final answer.",
    model=FakeChatModel(
        [
            tool_call("delegate_task", {"agent": "researcher", "task": "Find the emission saving of heat pumps."}),
            "Heat pumps cut heating emissions by about 60 %.",
        ]
    ),
)
team = Team("desk", orchestrator=lead, members=[researcher], limits=TeamLimits(max_calls_per_member=5))

result = team.run_sync("One fact about heat pumps for the city council.")
print(result.output)
print({name: usage.requests for name, usage in sorted(result.usage.by_agent.items())})
```

```text title="team.out"
Heat pumps cut heating emissions by about 60 %.
{'lead': 2, 'researcher': 1}
```

`result.usage` counts every agent of the run, and `by_agent` shows who worked. Peers, handoffs,
background work and agent networks are on [Teams](teams.md).

## A workflow

Use a workflow when the order of the steps is fixed. Code runs the steps: `Sequential` in order,
`Parallel` at the same time, `Loop` until a condition holds.

```python title="workflow.py"
from awa import Agent, Parallel, Sequential
from awa.testing import FunctionChatModel


def scripted(text: str) -> FunctionChatModel:
    return FunctionChatModel(lambda call: text)


intake = Agent("intake", model=scripted("Obligations: pay within 30 days; keep data in the EU."))
legal = Agent("legal", model=scripted("Legal: no unusual risk."))
privacy = Agent("privacy", model=scripted("Privacy: EU storage is required."))
editor = Agent("editor", model=scripted("Two findings: payment terms, EU storage."))

review = Sequential("contract_review", [intake, Parallel("reviews", [legal, privacy]), editor])
print(review.run_sync("<contract text>").output)
```

```text title="workflow.out"
Two findings: payment terms, EU storage.
```

More: [Workflows](workflows.md).

## A team from names and tasks

`awa.quick` builds a team, workflow or network from one sentence per agent. It adds the built-in
skills and tools each task calls for. It matches plain words and never calls a model:

```python title="quick_team.py"
from awa import quick

desk = quick.team(
    "Invoice processing",
    lead="coordinator",
    members={
        "extractor": "Reads incoming invoices and extracts the fields.",
        "checker": "Validates the totals against the purchase order.",
    },
)
print(quick.explain(desk))
```

```text title="quick_team.out"
auto-matching for invoice_processing
  coordinator
    + skill orchestration-planning (role: orchestrator)
    + skill delegation-briefs (matched: coordinator, delegates, writes)
  extractor
    + skill document-extraction (matched: extractor, invoices, fields)
  checker
    + skill data-validation (matched: checker, validates)
    + skill invoice-reconciliation (matched: checker, purchase, order)
    + tool  builtin:calculator (matched: totals)
```

- `+` is granted, `?` suggested but not granted, `=` given by you.
- A tool that sends, writes or deletes is never granted automatically, only suggested.
- `quick.workflow(...)` and `quick.network(...)` work the same way. `quick.blueprint(...)` returns
  the blueprint instead of building it.

## A blueprint from Python

A [blueprint](../reference/glossary.md#blueprint) is the YAML file that the web app and the
command line build. `awa.load` builds it into the same objects you write by hand:

```python title="from_blueprint.py"
import awa
from awa.models import ModelRegistry
from awa.testing import FunctionChatModel

BLUEPRINT = """
format_version: awa/v1
name: support-desk
agents:
  - name: support
    instructions: Answer questions about orders in one sentence.
"""

models = ModelRegistry()
models.register("default", FunctionChatModel(lambda call: "Your order ships tomorrow."))

desk = awa.load(BLUEPRINT, model_registry=models)  # a file path works the same way
print(type(desk).__name__, desk.name)
print(desk.run_sync("When does my order ship?").output)
```

```text title="from_blueprint.out"
Agent support
Your order ships tomorrow.
```

The registry replaces the `default` profile for this blueprint only. Without `model_registry=`,
the blueprint uses the models of your settings. Every key of a blueprint is on
[Blueprints](blueprints.md).

## Watch a run

Every run is one stream of typed events:

```python title="stream.py"
import asyncio

from awa import Agent
from awa.contracts import RunCompletedEvent, ToolCallEvent, ToolResultEvent
from awa.testing import FakeChatModel, tool_call


def lookup_order(order_id: str) -> str:
    """Return the status of an order."""
    return f"{order_id}: shipped"


agent = Agent(
    "support",
    tools=[lookup_order],
    model=FakeChatModel([tool_call("lookup_order", {"order_id": "A-1001"}), "A-1001 has shipped."]),
)


async def main() -> None:
    async for event in agent.stream("Has order A-1001 shipped?"):
        if isinstance(event, ToolCallEvent):
            print("call  ", event.tool_name, event.arguments)
        elif isinstance(event, ToolResultEvent):
            print("result", event.content)
        elif isinstance(event, RunCompletedEvent):
            print("done  ", event.result.output)


asyncio.run(main())
```

```text title="stream.out"
call   lookup_order {'order_id': 'A-1001'}
result A-1001: shipped
done   A-1001 has shipped.
```

Every event type, hooks, traces and Langfuse are on [Observability](observability.md).

## What can go wrong

| You see | Cause and fix |
| --- | --- |
| `ModelProviderError: no API key for anthropic ...` | The key is not in the environment or in `.env`. Set it, then run `awa doctor`. |
| `ModelResolutionError: Cannot create chat model ...` | The provider's extra is missing: install it, for example `pip install ".[anthropic]"` in the awa folder. |
| `AssertionError` about an exhausted script | A `FakeChatModel` got more calls than its script has items. Add items, or pass `repeat_last=True`. |
| `StructuredOutputError` | The model never returned the `output_type`. Say in the instructions which fields to fill, or use a stronger model (`model="strong"`). |

## Next

- [Agents](agents.md): every option of an agent, output checks, deps and conversations.
- [Testing](testing.md): offline tests and evaluations with the scripted models used here.
- [Architecture](architecture.md): how the packages fit together.
- Runnable scripts with offline tests: `examples/01_first_agent.py` to
  `examples/11_build_from_description.py` (see `examples/README.md`), in the awa folder or the
  source archive; the wheel does not contain them.

## Install in your own project

Install awa from its folder or a wheel, with the extras you need, for example
`pip install "<awa folder>[anthropic]"`. The distribution is `awa-agents`; the import and the
command are `awa`.

Quote the requirement (`".[dev]"`): zsh and PowerShell treat `[...]` specially. The double-click
installers and the install for people without Python are on [Install](../start/install.md).

### Extras

| Extra | Adds | Needed for |
| --- | --- | --- |
| `anthropic` | `langchain-anthropic`, `anthropic` | `anthropic:` models (the default provider) |
| `openai` | `langchain-openai` | `openai:` and `azure_openai:` models |
| `postgres` | `langgraph-checkpoint-postgres`, `psycopg`, `psycopg-pool` | PostgreSQL for checkpoints, stores, runs and artifacts |
| `mcp` | `langchain-mcp-adapters`, `mcp` | tools of MCP servers and `awa mcp serve` |
| `server` | `fastapi`, `uvicorn`, `python-multipart` | `awa serve`, `awa ui` and file uploads |
| `otel` | `opentelemetry-api` | OpenTelemetry spans |
| `langfuse` | `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` | sending runs to Langfuse; includes `otel` |
| `files` | `openpyxl`, `pypdf`, `python-docx`, `pillow` | Excel, PDF and Word files in the file tools and in `awa data` |
| `m365` | `openpyxl`, `pypdf`, `python-docx`, `pillow`, `cryptography` | the connectors for Microsoft 365 with certificate sign-in, and the files they read; `files` with `cryptography` |
| `sql` | `PyMySQL`, `pymssql` | MySQL, MariaDB and SQL Server (SQLite needs no extra) |
| `connectors` | `PyMySQL`, `pymssql`, `langchain-mcp-adapters`, `mcp` | SQL databases and MCP servers as connectors; `sql` and `mcp` together |
| `knowledge` | `openpyxl`, `pypdf`, `python-docx`, `pillow` | knowledge bases over Excel, PDF and Word documents; the same set as `files` |
| `automation` | `fastapi`, `uvicorn`, `python-multipart` | webhook triggers served over HTTP; the same set as `server` |
| `ui` | `fastapi`, `uvicorn`, `python-multipart` | the web app; the same set as `server` |
| `testing` | `pytest`, `pytest-asyncio` | the contract suites of `awa.testing.contracts` |
| `studio` | `langgraph-cli[inmem]` | LangGraph Studio |
| `docs` | `mkdocs-material`, `mkdocstrings` | building this documentation |
| `all` | `anthropic`, `openai`, `postgres`, `mcp`, `server`, `otel`, `langfuse`, `sql`, `files`, `connectors`, `m365`, `knowledge`, `automation`, `ui` | every runtime extra: everything except `testing`, `studio` and `docs` |
| `dev` | `all` and `testing`, plus the test and lint tools | working on awa itself |
