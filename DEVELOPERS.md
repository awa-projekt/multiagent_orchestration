# awa for developers

This page is for people who write Python with awa, extend it or work on it. To automate a process in the web
app you do not need it: start with the [README](README.md).

## Start here

1. **[Quickstart](docs/develop/quickstart.md):** install for development, then one agent, a team and a workflow in
   a few lines, all runnable without a model key.
2. **[How awa works](docs/start/how-awa-works.md):** the overview in diagrams: a lead that delegates, members that
   consult each other and share a task board, the limits in code.
3. **[Cheat sheet](docs/reference/cheatsheet.md):** every command and the main Python calls on one page.

## A first example, offline

An agent with one tool that needs approval. The model is scripted, so this runs without a key or a network:

<!-- guide:python -->
```python
import asyncio

from awa import Agent, tool
from awa.persistence import StateInMemorySaver
from awa.testing import FakeChatModel, tool_call


@tool(approval=True)  # a person says yes or no before this tool runs
def pay_invoice(invoice: str, amount: float) -> str:
    """Release an invoice for payment."""
    return f"{invoice} released: {amount} EUR"


model = FakeChatModel([  # a scripted model: no key, no network
    tool_call("pay_invoice", {"invoice": "INV-2042", "amount": 7450}),
    "INV-2042 is released for payment.",
])
clerk = Agent("clerk", tools=[pay_invoice], model=model, checkpointer=StateInMemorySaver())


async def main() -> None:
    paused = await clerk.run("Pay invoice INV-2042.")
    print("waiting for a person:", paused.interrupts[0]["value"]["tool"])
    done = await clerk.resume(paused.thread_id, {"action": "approve"})
    print(done.output)


asyncio.run(main())
```

It prints:

<!-- guide:expect -->
```text
waiting for a person: pay_invoice
INV-2042 is released for payment.
```

The run stops before `pay_invoice`, keeps its state in the checkpointer and continues when the decision arrives.
Most examples in these docs are written this way so that they run offline in the test suite: `FakeChatModel` plays
the model and the prints show what happens inside. In your own code you drop `model=` (or name a real model) and
set your provider's key.

## The blueprint: one file for the whole process

A blueprint names the agents, their tools, the order they work in and the tool calls that need
approval. Save this as `blueprint.yaml` with a folder `data/` beside it:

```yaml
name: invoice-desk
description: Checks supplier invoices against their purchase orders and releases them for payment.
defaults:
  model: default

toolkits:
  files: {root: ./data}
  email: {sender: accounting@example.com, allowed_recipients: ["@example.com"]}

agents:
  - name: invoice_checker
    description: Compares an invoice with its purchase order.
    instructions: Read invoices.csv and orders.csv with read_csv. Report every difference in amount.
    tools: [builtin:read_csv]
  - name: payment_clerk
    description: Releases checked invoices for payment.
    instructions: For a clean invoice, email finance the payment with send_email. Never pay twice.
    tools: [builtin:send_email]

workflow:
  sequential: [invoice_checker, payment_clerk]

approvals:
  - tool: builtin:send_email    # a person says yes before the email goes out
```

Checking and explaining it needs no key:

```console
$ awa validate blueprint.yaml
invoice-desk: valid (tool names not checked; no tools.py beside it)
$ awa show blueprint.yaml
invoice-desk 1.0.0
Checks supplier invoices against their purchase orders and releases them for payment.
entrypoint: workflow

agent            model    tools       skills  description
invoice_checker  default  read_csv    -       Compares an invoice with its purchase order.
payment_clerk    default  send_email  -       Releases checked invoices for payment.

workflow: invoice_checker -> payment_clerk

approvals: send_email
```

`send_email` sends through the mail server in `SMTP_HOST` (environment or `.env`); without it the call returns an error.

The web app writes this file for you; you read it and change it where it does not fit.

## What awa gives you in code

| Area | What it is | Read |
| --- | --- | --- |
| Agents | One model with instructions, tools, structured output and output checks | [Agents](docs/develop/agents.md), [Models](docs/develop/models.md) |
| Tools and skills | Typed functions, tool policies, approvals on any tool, reusable skills | [Tools](docs/develop/tools.md), [Skills](docs/develop/skills.md) |
| Working together | Teams with an orchestrator, fixed workflows, networks on a shared task board | [Teams and networks](docs/develop/teams.md), [Workflows](docs/develop/workflows.md) |
| Limits and safety | Default ceilings per run tree, usage and money limits, guardrails, redaction | [Safety](docs/develop/safety.md) |
| Data and memory | Files, scans, databases, checkpointers and stores | [Data](docs/develop/data.md), [Memory and persistence](docs/develop/memory.md) |
| Connections | REST, OpenAPI, GraphQL, OData, SQL, email, Microsoft 365, Slack, Jira, MCP | [Connectors](docs/develop/connectors.md), [MCP](docs/develop/mcp.md) |
| Testing | `FakeChatModel`, eval cases, pass^k, contract suites | [Testing](docs/develop/testing.md) |
| Running it | HTTP server, worker, triggers, tracing with Langfuse and OpenTelemetry | [Serving](docs/develop/serving.md), [Production](docs/develop/production.md), [Observability](docs/develop/observability.md) |
| Leaving awa | A one-agent blueprint becomes plain LangChain and LangGraph code | [Export](docs/develop/export.md), [Extend](docs/develop/extend.md) |

## Blueprints and settings

| Topic | Read |
| --- | --- |
| The blueprint file and how Python loads it with `awa.load` | [Blueprints](docs/develop/blueprints.md) |
| The project folder and every file in it | [The project folder](docs/develop/project-folder.md) |
| One agent's fields | [Agent spec](docs/develop/agent-spec.md) |
| The process section | [The process section](docs/develop/process-section.md) |
| Environment and settings | [Configuration](docs/develop/configuration.md), [Settings](docs/reference/settings.md) |

## Reference

| You look for | Read |
| --- | --- |
| A command and its options | [Command line](docs/reference/cli.md) |
| A built-in tool or skill | [Tools](docs/reference/tools.md), [Skills](docs/reference/skills.md) |
| A class or function | [Python API](docs/reference/index.md) |
| An error | [Exceptions](docs/reference/exceptions.md) |
| A word | [Glossary](docs/reference/glossary.md) |

## Examples

Numbered scripts, nine recipes and 13 use cases, all runnable offline: [examples/README.md](examples/README.md) and
[Examples](docs/guide/examples.md). The [benchmark](docs/develop/benchmarks.md) of eight scenarios can be rerun.

## Architecture, status and versions

- [Architecture](docs/develop/architecture.md): the layers and the one engine port on LangChain and LangGraph.
- [Stability](docs/about/stability.md): what is stable, what is preview, and the supported Python, LangChain and
  LangGraph versions.
- [Why awa](docs/about/why-awa.md), [Roadmap](docs/about/roadmap.md), [CHANGELOG.md](CHANGELOG.md).

## Read the docs as a site

The pages under `docs/` read best as the documentation site. In the awa folder:

```console
python -m pip install ".[docs]"
python -m mkdocs serve
```

Then open `http://127.0.0.1:8000`. The first start usually takes one to three minutes, because the API reference is
generated from the code. Do not open `site/index.html` by double-click: its links and its search need a web server.

## Work on awa itself

[CONTRIBUTING.md](CONTRIBUTING.md) is the contributor guide: setup with `uv`, the checks, the layers and the
conventions. [AGENTS.md](AGENTS.md) is its short version for coding assistants.
