# Cheat sheet

Every `awa` command and the Python calls you use most, on one page. Each section links the page
with the details; [Command line](cli.md) lists every option of every command.

- A `BLUEPRINT` argument takes `blueprint.yaml` or the project folder that holds it.
- Commands that run a model need a key: `awa setup` writes it to the project `.env`, `awa doctor`
  checks it. `validate`, `show`, `init`, `new` and `export` need no key.
- In Python, the names used most import from `awa` directly: `from awa import Agent, Team, load`.

## The shortest path

From a description of your process to runs that start by themselves:

```console
awa setup --provider anthropic --key-stdin              # the model key, written to .env
awa build "Check supplier invoices against their purchase orders" --out invoice-desk --data invoices
awa validate invoice-desk                               # errors and warnings, no model call
awa chat invoice-desk                                   # try it; approvals are asked inline
awa eval invoice-desk invoice-desk/evals/cases.yaml     # the pass rate of the eval cases
awa add trigger invoice-desk/blueprint.yaml             # asks what starts a run
awa worker invoice-desk                                 # runs the triggers until Ctrl+C
awa runs decide RUN_ID --approve                        # answer an approval of a process run
```

The same path in the web app: `awa ui --project invoice-desk`. With Python instead of a
description: [Quickstart](../develop/quickstart.md).

## Command line

Global options go before the command: `--project DIR` (the project folder), `--verbose`
(tracebacks), `--version`. A command marked *preview* has no stability promise yet
([Stability](../about/stability.md)).

### Start

```console
awa ui                                       # preview: the web app on this computer
awa ui --project invoice-desk                # ... for one project folder
awa ui --example invoice-processing          # ... starting from a shipped recipe or use case
awa build "Triage the support inbox" --out support-desk --data samples   # preview: a tested draft
awa init my-desk --template support          # a starter project: minimal, team, workflow, network, support
awa setup                                    # model key, database and tracing, asked step by step
awa setup --provider openai --key-stdin      # ... without questions
awa setup --database postgres-docker --tracing langfuse-docker
awa setup services up                        # start the project's PostgreSQL and Langfuse in Docker
awa setup services status                    # ... are they running
awa setup services down                      # ... stop them; data is kept
```

Details: [`awa ui`](cli.md#awa-ui), [`awa build`](cli.md#awa-build), [`awa init`](cli.md#awa-init),
[`awa setup`](cli.md#awa-setup), [Install](../start/install.md),
[The guide](../guide/tour.md).

### Build

```console
awa new process invoice-desk --agent checker="Checks the totals" --structure workflow
awa new tool lookup_order --approval         # a typed tool with its test
awa new toolbox crm                          # several tools with one prefix and a config
awa new connector erp                        # a connector to one of your systems
awa new parser image                         # a reader for another file kind
awa new skill refund-policy                  # a skill folder with SKILL.md
awa add trigger blueprint.yaml               # preview: schedule, webhook, file drop, mailbox, queue
awa add trigger blueprint.yaml --kind schedule --name daily --cron "0 7 * * MON-FRI"
awa add tool langchain_tavily:TavilySearch --blueprint blueprint.yaml --agent researcher
awa add graph billing.graph:graph --name billing --blueprint blueprint.yaml
awa add skills ./my-skills                   # import skill folders
awa connect                                  # preview: ask for every missing setting, test, save to .env
awa connect microsoft                        # ... Microsoft 365 instead
awa brief --out BRIEF.md                     # preview: a brief for a coding assistant
awa people search "Ana"                      # preview: find a colleague in Entra ID
awa people pin ana@example.com --role approver
awa people list                              # the pinned people and their roles
awa people check                             # ... do they still exist in the directory
awa people unpin ana                         # ... remove one
awa export --out refund-desk-langgraph       # one-agent project to plain LangChain and LangGraph
awa export --check                           # ... only check whether it can be exported
```

Details: [`awa new`](cli.md#awa-new), [`awa add`](cli.md#awa-add),
[`awa connect`](cli.md#awa-connect), [`awa people`](cli.md#awa-people),
[`awa export`](cli.md#awa-export), [Describe a process](../guide/describe.md),
[Connect systems](../guide/connect.md), [Export](../develop/export.md).

### Check

```console
awa validate blueprint.yaml                  # schema, agents, tool names, triggers; exit 1 on errors
awa show blueprint.yaml                      # agents, models, tools, approvals, flow
awa show blueprint.yaml --mermaid            # ... as a diagram
awa show cases blueprint.yaml                # the special cases and their tests
awa show dry-run blueprint.yaml              # the path a run would take, without a model
awa eval blueprint.yaml evals/cases.yaml     # pass rate; exit 1 below --min-pass-rate
awa eval blueprint.yaml evals/cases.yaml --repetitions 3 --min-pass-hat-k 0.9
awa eval blueprint.yaml evals/cases.yaml --case refund-status --json report.json
awa data inspect invoices/orders.xlsx        # preview: what is in a file; changes nothing
awa data check invoice-desk                  # the data section against the files on disk
awa data run invoice-desk --all              # run the project on its input files
awa data add-system invoice-desk --type sql --name "Invoice ledger"   # test it read-only, save to .env
awa data remove-system invoice-desk invoice_ledger
awa doctor                                   # installation, extras, keys, settings and their source
awa doctor --check-models                    # ... with one small model call
awa check                                    # preview: the Microsoft 365 setup, read-only
```

Details: [`awa validate`](cli.md#awa-validate), [`awa show`](cli.md#awa-show),
[`awa eval`](cli.md#awa-eval), [`awa data`](cli.md#awa-data), [`awa doctor`](cli.md#awa-doctor),
[Test and improve](../guide/test.md), [Your files](../guide/files.md).

### Run

```console
awa chat blueprint.yaml                      # a conversation; approvals are asked inline
awa run blueprint.yaml "Check invoice INV-2042"            # one task, one answer; exit 2 while it waits
awa run blueprint.yaml "Check invoice INV-2042" --stream   # ... with every event
awa run blueprint.yaml "Check invoice INV-2042" --max-cost 0.5 --max-duration 5m
awa worker blueprint.yaml                    # preview: triggers and queued runs until Ctrl+C
awa serve blueprint.yaml --port 8000         # preview: HTTP API, webhooks and triggers
awa serve blueprint.yaml --docs --api-key-env AWA_API_KEY
awa mcp serve blueprint.yaml                 # preview: the system as an MCP server (stdio)
awa mcp lock blueprint.yaml                  # pin the tools of the blueprint's MCP servers
awa mcp list blueprint.yaml                  # ... and see whether one changed
```

`--max-requests`, `--max-total-tokens`, `--max-tool-calls`, `--max-cost` and `--max-duration`
work on `run`, `chat`, `eval`, `worker`, `serve` and `trigger run`; they can only tighten the
limits of the blueprint.

Details: [`awa chat`](cli.md#awa-chat), [`awa run`](cli.md#awa-run),
[`awa worker`](cli.md#awa-worker), [`awa serve`](cli.md#awa-serve), [`awa mcp`](cli.md#awa-mcp),
[Run automatically](../guide/run.md), [Serving](../develop/serving.md), [MCP](../develop/mcp.md).

### Operate

```console
awa runs list                                # preview: process runs, newest first
awa runs show RUN_ID --steps                 # status, input, output, waits and history
awa runs decide RUN_ID --approve             # answer an approval
awa runs decide RUN_ID --reject --note "Delivered 45 days ago"
awa runs retry RUN_ID                        # queue a failed run again
awa runs cancel RUN_ID --reason "duplicate"
awa runs trace RUN_ID                        # the run as a tree: calls, tokens, cost, time
awa runs export-cases RUN_ID --out evals/cases.yaml   # a finished run becomes an eval case
awa runs dlq                                 # runs that failed every retry
awa runs migrate blueprint.yaml --dry-run    # waiting runs to the current blueprint version
awa trigger list blueprint.yaml              # preview: the triggers of a blueprint
awa trigger run blueprint.yaml daily --payload @sample.json   # fire one once
awa connectors list                          # every connector and whether its settings are there
awa connectors show sql                      # its settings (secrets only as set) and tools
awa connectors test --all                    # one small real call each
awa skills list                              # the built-in skills and those of the project
awa skills show writing                      # print one SKILL.md
awa tools list builtin                       # the built-in tools with risk and approval rule
awa tools list tools.py                      # the tools of a file
awa tools call builtin:calculator --args "{\"expression\": \"2 * 21\"}"   # call one, no model
awa plugins list                             # plugins and switchable parts
awa plugins show parser.pdf
awa plugins check parser.pdf                 # run its contract test
awa plugins switch parser.pdf off --blueprint blueprint.yaml
```

Details: [`awa runs`](cli.md#awa-runs), [`awa trigger`](cli.md#awa-trigger),
[`awa connectors`](cli.md#awa-connectors), [`awa skills`](cli.md#awa-skills),
[`awa tools`](cli.md#awa-tools), [`awa plugins`](cli.md#awa-plugins),
[Approvals](../guide/approvals.md), [Keep it running](../guide/operate.md).

### Settings

```console
awa env list                                 # every setting, its value and source; secrets as set or not set
awa env get AWA_DEFAULT_MODEL                # one setting and where it comes from
awa env set ANTHROPIC_API_KEY                # asks for the value without showing it
awa env set AWA_RUN_MAX_REQUESTS=200
awa env unset AWA_RUN_MAX_REQUESTS
awa cache stats                              # what the caches hold
awa cache clear                              # ... delete it
awa cache salt                               # a random AWA_CACHE_SALT in .env
awa store status                             # preview: where runs are kept
awa store use postgres                       # move the runs to PostgreSQL (or back: sqlite)
awa store migrate --to postgres --dry-run    # the move of store use, with every option
awa store upgrade                            # apply pending schema migrations
awa store backup ./backup-2026-10            # a copy of the local SQLite data
awa store sweep --days 30                    # delete finished runs older than 30 days
awa store erase --automation invoice-desk --run RUN_ID
```

Details: [`awa env`](cli.md#awa-env), [`awa cache`](cli.md#awa-cache),
[`awa store`](cli.md#awa-store), [Settings](settings.md), [Configuration](../develop/configuration.md).

Exit codes: 0 done, 1 failed, 2 waits for an approval or an event, 3 needs a confirmation,
4 export refused, 78 configuration incomplete, 130 Ctrl+C ([the table](cli.md#exit-codes)).

## Python

### An agent and a tool

```python
from awa import Agent, tool


@tool(approval="amount > 50")                 # a person decides above 50; True: always
def issue_refund(order_id: str, amount: float) -> str:
    """Refund an order."""                    # the docstring is the tool description
    return f"refunded {amount:.2f} EUR for {order_id}"


clerk = Agent(
    "clerk",
    instructions="Answer refund questions. Refund only within 30 days.",
    tools=[issue_refund],                     # typed functions, @tool, LangChain tools
    model="default",                          # or "fast", "strong", "provider:model"
)
result = clerk.run_sync("Refund 30 EUR for order A-7.")
print(result.output)
```

| Call | Does |
| --- | --- |
| `agent.run_sync(input)` | Runs to the end and returns a `RunResult` (from synchronous code). |
| `await agent.run(input)` | The same, async. |
| `async for event in agent.stream(input)` | Typed events: `ToolCallEvent`, `ToolResultEvent`, ..., `RunCompletedEvent`. |
| `handle = agent.start(input)` | Runs in the background; `handle.cancel()`, `await handle.result()`. |
| `await agent.resume(result.thread_id, decision)` | Continues a run paused for an approval. |
| `agent.as_tool()`, `agent.clone(...)`, `agent.with_tools(...)` | The agent as a tool, a copy with changes, a variant with more tools. |
| `with agent.override(model=...)` | Swaps the model for a test. |

Every run call takes `deps=`, `history=`, `thread_id=`, `usage_limits=`, `timeout=` and
`metadata=`. A `RunResult` has `output`, `text`, `usage` (`usage.by_agent`), `thread_id`,
`interrupts`, `is_interrupted`, `duration_ms` and `metadata`.

Common `Agent` options: `description`, `output_type` (a pydantic model), `skills`, `memory`,
`input_guardrails`, `output_guardrails`, `output_validators`, `model_settings`, `max_steps`
(default 25), `checkpointer`, `capabilities`.

Details: [Agents](../develop/agents.md), [Tools](../develop/tools.md),
[Models](../develop/models.md), [Observability](../develop/observability.md#watch-a-run).

### Approvals

```python
from awa.persistence import StateInMemorySaver

clerk = Agent("clerk", tools=[issue_refund], checkpointer=StateInMemorySaver())  # a pause needs one


async def refund() -> None:
    paused = await clerk.run("Refund 80 EUR for order A-7.")
    if paused.is_interrupted:                 # waits for a person
        request = paused.interrupts[0]["value"]
        print(request["tool"], request["args"])
        done = await clerk.resume(paused.thread_id, {"action": "approve", "reviewer": "ana"})
        print(done.output)
```

| Decision | Effect |
| --- | --- |
| `{"action": "approve"}` | The call runs. |
| `{"action": "reject", "note": "..."}` | The tool returns the rejection with the note. |
| `{"action": "edit", "args": {"amount": 40.0}}` | The call runs with the changed arguments. |

Other ways to gate: `approvals:` in a blueprint (with `when:`), a tool policy returning `Ask(...)`,
`create_tool_approval_wrapper(tool)` from `awa.tools` around any tool. A durable checkpointer
(`SqliteCheckpointer("runs.db")`, PostgreSQL) lets the decision come later from another process.

Details: [Safety: approvals](../develop/safety.md#approvals), [Approvals](../guide/approvals.md),
[Memory and persistence](../develop/memory.md#checkpointers-and-threads).

### Typed output and checks

```python
from pydantic import BaseModel

from awa import Agent
from awa.guardrails import BlocklistGuardrail, PIIGuardrail


class Invoice(BaseModel):
    vendor: str
    total: float


extractor = Agent(
    "extractor",
    output_type=Invoice,                                   # result.output is an Invoice
    input_guardrails=[BlocklistGuardrail(["password"])],   # checked before the first model call
    output_guardrails=[PIIGuardrail(mode="redact")],       # checked on the final output
)
```

Guardrails: `LengthGuardrail`, `BlocklistGuardrail`, `PIIGuardrail`, `SchemaGuardrail`,
`NonEmptyFieldsGuardrail`, `MustIncludeGuardrail`; a blocking result raises
`GuardrailTripwireError`. Details: [Agents: structured output](../develop/agents.md#structured-output-and-output-checks),
[Safety: guardrails](../develop/safety.md#guardrails).

### Limits

```python
from awa import UsageLimits, UsageLimitExceededError
from awa.contracts import ModelPrice, PriceTable

limits = UsageLimits(max_requests=20, max_tool_calls=40, max_total_tokens=200_000, max_duration=300)
prices = PriceTable(prices={"my-model": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0)})
budget = UsageLimits(max_cost=2.0, prices=prices)          # a money limit

try:
    agent.run_sync("...", usage_limits=limits, timeout=120)
except UsageLimitExceededError as error:
    print(error.diagnosis.bound)                            # which limit stopped the run
```

Every run tree has default ceilings even without `UsageLimits`
([the run limits](settings.md#run-limits-and-loop-protection)). Teams add `TeamLimits`, networks
`NetworkLimits`, one agent run `Agent(max_steps=...)`. Details: [Safety: limits](../develop/safety.md#limits).

### Teams

```python
from awa import Agent, Team, TeamLimits

team = Team(
    "desk",
    orchestrator=lead,                        # gets delegate_task for its members
    members=[researcher, writer, billing],
    peers={"researcher": ["writer"]},         # consult_peer and send_message between them
    handoffs={"lead": ["billing"]},           # transfer_to_billing: billing takes over
    task_board=True,                          # shared tasks with owners and claims
    blackboard=True,                          # shared versioned notes
    limits=TeamLimits(max_calls_per_member=5, max_delegation_depth=2),
)
result = team.run_sync("One fact about heat pumps for the city council.")
print(result.usage.by_agent)
print(team.to_mermaid())                      # the edges as a diagram
```

Details: [Teams and networks](../develop/teams.md), [How awa works](../start/how-awa-works.md),
[Agents and teams](../guide/agents-and-teams.md).

### Workflows

```python
from awa import Loop, Parallel, Sequential
from awa.workflows import Checked, Map

reviews = Parallel("reviews", [legal, privacy], fail_fast=False)       # at the same time
refine = Loop("refine", editor, until="done", max_iterations=3)        # until a typed field is true
review = Sequential("contract_review", [intake, reviews, refine], carry="all")
extract = Map("extract", invoice_reader, max_concurrency=4)            # once per item of a list
gated = Checked("brief", writer, judge=judge, rubric=["Cites a source"])  # a judge gates the draft
```

Details: [Workflows](../develop/workflows.md), [The process section](../develop/process-section.md).

### Agent networks

```python
from awa import AgentNetwork, NetworkLimits

network = AgentNetwork(
    "market_scan",
    lead=lead,                                # plans the goal as tasks on a board
    teammates=[pricing, reviews],             # claim the tasks matching their capabilities
    limits=NetworkLimits(max_tasks=10, run_timeout=600),
)
```

Details: [Teams: agent networks](../develop/teams.md#agent-networks).

### From one sentence per agent

```python
from awa import quick

desk = quick.team("Invoice processing", lead="coordinator", members={
    "extractor": "Reads incoming invoices and extracts the fields.",
    "checker": "Validates the totals against the purchase order.",
})
print(quick.explain(desk))                    # which skills and tools were matched, and why
```

`quick.workflow(...)`, `quick.network(...)` and `quick.blueprint(...)` work the same way. Details:
[Quickstart: a team from names and tasks](../develop/quickstart.md#a-team-from-names-and-tasks).

### Blueprints

```python
import awa
from awa import Blueprint
from awa.persistence import StateInMemorySaver

system = awa.load("blueprint.yaml", checkpointer=StateInMemorySaver())   # or the project folder
result = system.run_sync("Check invoice INV-2042")

blueprint = Blueprint.from_yaml("blueprint.yaml")
print([str(issue) for issue in blueprint.check()])    # what awa validate reports
clerk = blueprint.build_agent("payment_clerk")        # one role on its own
limits = blueprint.usage_limits()                     # the limits: section as UsageLimits
```

`await awa.aload(...)` is the async form. Details: [Blueprints](../develop/blueprints.md),
[The project folder](../develop/project-folder.md).

### Tools, toolkits, connectors and MCP

```python
from awa import Agent
from awa.connectors import connector_tools
from awa.mcp import MCPServer, MCPToolset
from awa.toolkits import file_toolkit

tools = [
    *file_toolkit("./data", read_only=True),                       # files inside one folder
    *connector_tools("jira", config={"read_only": True}),          # a catalog connector
]
crm = MCPServer(name="crm", command="python", args=["crm_server.py"])


async def ask_crm() -> None:
    async with MCPToolset(crm) as crm_tools:                       # one MCP session for the block
        agent = Agent("account_manager", tools=[*tools, *crm_tools])
        print((await agent.run("Who is ACME?")).output)
```

Details: [Tools](../develop/tools.md), [Built-in toolkits](../develop/tools.md#builtin-toolkits),
[Tools reference](tools.md), [Connectors](../develop/connectors.md), [MCP](../develop/mcp.md).

### Skills and memory

```python
from awa import Agent, SkillRegistry
from awa.memory import AgentMemory

registry = SkillRegistry.from_directory("skills", include_builtin=True)
agent = Agent(
    "refunds",
    skills=["refund-policy", "writing"],
    skill_registry=registry,
    skill_mode="on_demand",                    # loaded with load_skill when needed
    memory=AgentMemory(scope="user", auto_recall=3),   # remember, recall, forget
)
agent.run_sync("Please invoice me in EUR.", deps={"user_id": "customer-17"})
```

Details: [Skills](../develop/skills.md), [Skills reference](skills.md),
[Memory and persistence](../develop/memory.md).

### Tests and evals, without a model key

```python
import awa
from awa import Agent
from awa.evals import Dataset, evaluate_sync
from awa.testing import FakeChatModel, tool_call

model = FakeChatModel([tool_call("lookup_order", {"order_id": "A-1"}), "A-1 has shipped."])
agent = Agent("support", tools=[lookup_order], model=model)
assert agent.run_sync("Where is A-1?").output == "A-1 has shipped."
print(model.calls[1].last_message.content)               # what the model saw

report = evaluate_sync(lambda: awa.load("blueprint.yaml"), Dataset.load("evals/cases.yaml"), repetitions=3)
print(report.pass_rate, report.pass_hat_k, report.gate(min_pass_rate=0.9).reasons)
```

`FunctionChatModel(lambda call: ...)` answers from a function; `structured_output(Model, {...})`
scripts a typed answer. Details: [Testing](../develop/testing.md),
[Test and improve](../guide/test.md).

### Serve, export, trace and set up

```python
import awa
from awa.exporter import export
from awa.observability import connect_langfuse
from awa.server import create_app, serve

app = create_app({"support": support}, api_key="...")    # a FastAPI app: uvicorn service:app
serve(support, port=8080, api_key="...")                  # or serve it directly
report = export("refund-desk", "refund-desk-langgraph")   # plain LangChain and LangGraph code
connect_langfuse()                                        # every run as a trace in Langfuse
plan = awa.setup(provider="openai", api_key="...")        # writes the key to .env
```

Details: [Serving](../develop/serving.md), [Export](../develop/export.md),
[Observability](../develop/observability.md), [Langfuse](../guide/langfuse.md),
[Production](../develop/production.md).

## A blueprint at a glance

The sections used most, in one file that `awa validate` accepts; only `name` and `agents` are required.

```yaml
format_version: awa/v1
name: invoice-desk
description: Checks supplier invoices and releases them for payment.
auto: false                       # true: add matching built-in skills and tools
defaults: {model: default, max_steps: 25}

agents:
  - name: invoice_checker
    description: Compares an invoice with its purchase order.
    instructions: Read invoices.csv and orders.csv. Report every difference in amount.
    tools: [builtin:read_csv, connector:ledger]
    skills: [invoice-reconciliation]
  - name: payment_clerk
    instructions: Release clean invoices for payment.
    tools: [builtin:send_email]

workflow:                         # or team:, network:, process: (one structure as entrypoint)
  sequential: [invoice_checker, payment_clerk]

toolkits:
  files: {root: ./data}
  email: {sender: accounting@example.com, allowed_recipients: ["@example.com"]}
connectors:
  ledger: {use: sql, read_only: true}
approvals:
  - tool: builtin:send_email      # optional when: "amount >= 5000" over the tool's arguments
limits: {max_requests: 50, max_tool_calls: 100, max_duration: 10m}
triggers:
  - name: daily
    schedule: {cron: 0 7 * * MON-FRI, timezone: Europe/Berlin}
    input: Check all open invoices.
```

More sections (`data`, `policies`, `special_cases`, `mcp_servers`, `prices`, `automation`,
`components`) and every key: [Blueprints: the schema](../develop/blueprints.md#the-schema).

## Settings you set most

| Setting | What it does |
| --- | --- |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | The model key; other providers in [Settings](settings.md). |
| `AWA_DEFAULT_MODEL`, `AWA_FAST_MODEL`, `AWA_STRONG_MODEL` | The models of the profiles `default`, `fast` and `strong`. |
| `AWA_RUN_MAX_REQUESTS`, `AWA_RUN_MAX_TOOL_CALLS`, `AWA_RUN_MAX_TOKENS`, `AWA_RUN_MAX_SECONDS` | The ceilings of every run tree ([run limits](settings.md#run-limits-and-loop-protection)). |
| `AWA_DATA_BACKEND`, `AWA_POSTGRES_DSN` | Where runs are kept: SQLite or PostgreSQL. |
| `AWA_LANGFUSE_TRACING` | Send every run to Langfuse. |
| `AWA_API_KEY` | The key callers of `awa serve` send. |

A value comes from the environment, else the project `.env`, else the default. Every setting:
[Settings](settings.md).

## When something does not work

| You see | Do |
| --- | --- |
| A missing key, extra or setting | `awa doctor` names it and prints the fix. |
| A blueprint that does not load | `awa validate blueprint.yaml` lists every error with its place. |
| A run that waits | `awa runs list`, then `awa runs decide RUN_ID --approve`, or Approvals in `awa ui`. |
| A run that went wrong | `awa runs trace RUN_ID`, or the trace in [Langfuse](../guide/langfuse.md). |
| An error class or code | [Exceptions](exceptions.md), [Troubleshooting](../guide/troubleshooting.md). |
