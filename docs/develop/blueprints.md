# Blueprints

A **blueprint** is one YAML (or JSON) file that describes your whole process: the agents that do
the work, their tools and skills, how they work together, and which actions wait for a person.
awa checks the file without running anything and builds it into a running system with one call.

Use a blueprint when you want the process as a file that people who know the work can read and
review. This page is for Python developers who know the [Quickstart](quickstart.md). To write
blueprints without code, see [Describe a process](../guide/describe.md); every command is in the
[CLI reference](../reference/cli.md).

## Write and run a blueprint

Four agents check an invoice and book it. Every booking waits for a person:

```yaml
# invoice_desk.yaml
name: invoice-desk
description: Checks incoming invoices against purchase orders and books them.
version: 1.0.0

defaults:
  model: default          # a profile name or provider:model
  max_steps: 12

agents:
  - name: intake
    description: Receives invoices, coordinates the checks and reports the outcome.
    instructions: |
      1. Delegate field extraction to extractor.
      2. Delegate the purchase-order match to matcher.
      3. If the invoice matches, delegate booking to bookkeeper; otherwise explain the mismatch.
    skills: [orchestration-planning, delegation-briefs]

  - name: extractor
    description: Extracts vendor, amount, currency and due date from an invoice.
    instructions: Extract the fields exactly as written on the invoice.
    tools: [read_invoice]
    output_schema:
      type: object
      properties:
        vendor: {type: string}
        amount: {type: number, minimum: 0}
        currency: {enum: [EUR, USD]}
        due_date: {type: string}
      required: [vendor, amount, currency]

  - name: matcher
    description: Compares an invoice with its purchase order.
    instructions: Report every difference in vendor, amount or quantity.
    tools: [find_purchase_order]
    capabilities: [procurement]

  - name: bookkeeper
    description: Books matched invoices for payment.
    instructions: Book the payment with book_payment and return the booking reference.
    tools: [book_payment]
    skills: [safe-tool-execution]

team:
  orchestrator: intake
  members: [extractor, matcher, bookkeeper]
  peers:
    bookkeeper: [matcher]
  limits:
    max_calls_per_member: 4
    delegation_timeout: 120

approvals:
  - tool: book_payment          # every booking waits for a human
```

Load it, run it, and answer the approval:

```python
from awa import load
from awa.persistence import StateInMemorySaver

desk = load("invoice_desk.yaml",
            tools=[read_invoice, find_purchase_order, book_payment],
            checkpointer=StateInMemorySaver())       # required because of the approval
result = await desk.run("Process invoice INV-1042.")
if result.is_interrupted:
    print(result.interrupts[0]["value"])        # the pending book_payment call
    result = await desk.resume(result.thread_id, {"action": "approve"})
print(result.output)
```

`load` checks the file, matches the tool names against `tools=` and builds the team. A run that
waits for a person must be saved somewhere, so approvals need a checkpointer. Given a project folder
or its `blueprint.yaml`, `load` also picks up the `tools.py` and the `skills/` folder next to the
blueprint, as every command does ([The project folder](project-folder.md)).

## Validation

`Blueprint.check(tools=None, skills=None)` finds every problem without building anything. It
checks names and references, the team, workflow references, approvals, output schemas and skills.
It checks tool names too when you pass the tools. Each finding is a `BlueprintIssue` (severity,
path, message). `build` refuses a blueprint with errors and raises `BlueprintError` listing them.

```python
from awa import Blueprint

blueprint = Blueprint.from_yaml("invoice_desk.yaml")
for issue in blueprint.check(tools=[read_invoice, find_purchase_order, book_payment]):
    print(issue)
print(blueprint.to_mermaid())
```

On the command line, `awa validate` does the same and `awa show` explains the blueprint in words.

Blueprints round-trip: `Blueprint.from_dict`, `from_yaml`, `to_dict`, `to_yaml`.
`Blueprint.explain()` returns the auto-matching report (the same as
`awa.blueprints.explain(system)` for a built system).

## Loading tools

`load`, `aload`, `Blueprint.build` and `abuild` take `tools=`: a list or a mapping of names to
tools. You rarely need to pass them yourself: the command line (`validate`, `run`, `chat`, `eval`,
`serve`, `worker`, `trigger run`), `awa.load`, `awa.server.build_blueprint_app` and the web app
load the `tools.py` and the `skills/` folder next to the blueprint by themselves. `--tools` and
`--skills` only add more.

Some tools need no code of yours. The blueprint resolves them itself:

| Written as | Is |
| --- | --- |
| `builtin:read_csv` | a built-in toolkit tool ([Tools](tools.md#builtin-toolkits)) |
| `connector:jira.search_issues` | an operation of a connected system ([Connectors](connectors.md)) |
| `mcp:crm` | the tools of an MCP server ([MCP](mcp.md#in-a-blueprint)) |
| `plugin:<dist>/<name>` | a tool of an installed plugin ([Extend](extend.md#plugins)) |

More ways to load tools:

- `awa.blueprints.load_tools("tools.py")` collects tools the way `--tools` does. It takes dotted
  module names or `.py` files, and returns every LangChain tool they define plus the functions in
  their `__all__` (a `Toolbox` is flattened into its tools). Each file is imported under a module
  name of its own, so two `tools.py` in different folders never collide.
- A tool may be written as `"package.module:attribute"`. Such references are only imported with
  `allow_imports=True`, because importing runs module code. Enable it only for blueprints you trust.
- Code in a tools file (`when:` predicates, loop `until:` conditions, tools) is referenced by path,
  `"./tools.py:needs_approval"`, resolved against the blueprint's folder, or by file stem,
  `"tools:needs_approval"`.

## How the agents work together

A blueprint with one agent runs that agent. With more agents, pick one structure; the
[Teams](teams.md) and [Workflows](workflows.md) pages explain each in depth.

| Section | The agents | Use it when |
| --- | --- | --- |
| `team` | An orchestrator delegates to members | The model should decide who does what. |
| `workflow` | Steps in a fixed order, in parallel or in a loop | The order is known in advance. |
| `process` | Steps in order, each checked before the next starts | Each result must pass checks or a person ([The process section](process-section.md)). |
| `network` | A lead creates tasks that teammates claim | The work splits into tasks that teammates pick up ([Agent networks](teams.md#agent-networks)). |

- `team` takes `name`, `description`, `orchestrator`, `members`, `delegates`, `peers`,
  `symmetric_peers`, `handoffs`, `handoff_history`, `messaging`, `blackboard`, `task_board` and
  `limits`. Members are agents, or the workflow or network by name.
- A `workflow` step is an agent, team or network name, or `sequential:`, `parallel:`, `loop:` or
  `map:` with nested steps.
- `network` takes `name`, `description`, `lead`, `teammates`, `peers` (a mapping or `all`),
  `teammates_create_tasks`, `blackboard` and `limits` (`max_tasks`, `max_concurrent_teammates`,
  `max_task_attempts`, `max_lead_turns`, `max_message_turns`, `idle_timeout`, `run_timeout`,
  `max_result_chars`).

Short forms keep simple blueprints short. `agents` may map names to tasks
(`checker: Validates the totals.`) or to agent fields. `team: <orchestrator>` makes every other
agent a member, `network: <lead>` every other agent a teammate, and `workflow: [a, b]` is a
sequence:

```yaml
name: invoice_processing
auto: true
agents:
  coordinator: Coordinates the processing of incoming invoices.
  extractor: Reads incoming invoices and extracts the fields.
  checker: Validates the totals against the purchase order.
team: coordinator
```

A workflow with a parallel review and a loop:

```yaml
workflow:
  sequential:
    - intake
    - parallel: [legal_reviewer, compliance_reviewer, style_reviewer]
      fail_fast: false
      name: reviews
    - loop: editor
      until_contains: APPROVED
      max_iterations: 3
  carry: all
```

A `map` step runs one step once per part of a list, concurrently, and checks that every item has
exactly one result ([Workflows](workflows.md#a-list-of-items)):

```yaml
workflow:
  sequential:
    - map: invoice_reader
      part_size: 5        # items per part; more than one is passed as a JSON array
      max_concurrency: 4
    - writer
```

A network (teammates claim the ready tasks matching their `capabilities`):

```yaml
network:
  lead: research_lead
  teammates: [market_researcher, analyst, writer]
  peers: {}
  limits: {max_tasks: 8, max_lead_turns: 10, run_timeout: 1200}
```

Agents may also carry a role (`role`, `sub_agents`, `rubric`, `judges`, `reviews`): an
orchestrator over workers, a judge that gates a worker's output with a typed verdict, and
reviewers that read it once. A role tree needs no `team:` section. See
[Agent roles](teams.md#roles-in-a-blueprint) and `examples/blueprints/market_brief.yaml`.

### Nesting

The team, workflow and network nest by name. A team member or workflow step may name the workflow
or the network (give them a `name`), and a workflow step may name the team.

- Only the entrypoint gets the checkpointer. Nested workflows and teams persist on its thread, so
  an approval anywhere in them pauses the whole run.
- A network pauses for approvals only as the entrypoint. `check` reports approvals inside a nested
  network.
- `build(task_board=..., bus=..., blackboard=...)` hands shared objects of your own (a
  `BaseTaskBoard`, a `MessageBus`) to the network when the blueprint uses one, else to the team.

`Blueprint.build_agent(name, tools=...)` builds one role on its own, with its model, tools
(approval-gated as declared), skills, memory and output schema. Use it to compose roles in code or
to test one role.

## The schema

`name` and `agents` are required. The sections are listed from common to rare.

| Section | Purpose |
| --- | --- |
| `name`, `description`, `version` | The system. `name` is also the default name of the team, workflow and network. `version` (default `1.0.0`) is yours to bump. |
| `defaults` | Settings every agent inherits: `model`, `model_settings`, `max_steps` (default 25), `skill_mode` (`inline` or `on_demand`). `tool_timeout`: seconds a call of the blueprint's own tools (`tools.py`, import references) may take (default 120, `null`: no limit) unless a tool sets `@tool(timeout=...)`. `cache`: the cache settings ([Models](models.md#prompt-caching-and-result-caches)); `AWA_CACHE_*` variables win. |
| `agents[]` | One entry per agent; `name` is required. The common fields: `description`, `instructions`, `model`, `model_settings`, `tools`, `skills`, `skill_mode`, `output_schema`, `max_steps`. Below the table: the rare ones. |
| `approvals[]` | `tool` and an optional `when` (below). |
| `team`, `workflow`, `process`, `network` | How the agents work together ([above](#how-the-agents-work-together)). Set `process` or `workflow`, not both. |
| `entrypoint` | `team`, `workflow`, `network` or an agent name. Default: the team if declared, else the workflow, else the network, else the only agent. |
| `limits`, `prices` | What one run may use at most (below). |
| `auto` | `true` gives every agent the built-in skills and tools its task calls for (agents override it with their own `auto`). Tools that act outside the agent are only suggested. `Blueprint.explain()` and `awa show` report what was matched and why; see [`awa.quick`](quickstart.md#a-team-from-names-and-tasks). |
| `data[]` | The real files a process works on ([Data from your files](#data-from-your-files)). |
| `connectors`, `toolkits`, `mcp_servers[]`, `tool_risks` | Connected systems ([Connectors](connectors.md)), built-in toolkit settings ([Toolkits](tools.md#builtin-toolkits)), MCP servers ([MCP](mcp.md#in-a-blueprint)) and corrections of the risk check ([Tools](tools.md#untrusted-output-and-risky-combinations)). |
| `triggers[]`, `automation` | What starts runs by itself and how those runs are processed (below). |
| `policies[]` | Run-time tool policies for all agents or those in `agents` (below). |
| `special_cases` | Rule tables that decide on the result of a `process:` step ([Special cases](#special-cases)). |
| `components` | Built-in parts switched on or off by kind and name, written by `awa plugins switch` ([Extend](extend.md#plugins)). |
| `format_version` | The file format, `awa/v1` (the default). An unknown format is refused with a message that names it. Unrelated to `version`. |

`output_schema` is a safe subset of JSON Schema (objects, arrays, scalars, enums, defaults and the
usual constraints), converted into a pydantic model. Anything unsupported is an error, never
silently ignored.

### More agent fields

- **Memory and long conversations:** `memory` (`scope`, `auto_recall`) and `compaction`
  (`max_tokens`, `keep_last_messages`); see [Memory](memory.md).
- **Documents:** `documents` names `data:` entries the agent always has
  ([The process section](process-section.md)).
- **Directory data:** `capabilities`, `tags`.
- **Auto-matching:** `auto` overrides the blueprint's `auto`.
- **Roles:** `role`, `rubric`, `judges`, `reviews`, `max_iterations` (how often a judge may send a
  draft back, default 3), `sub_agents` and `delegation` (`max_depth`, `max_parallel` of the
  sub-agents) place the agent in a role tree ([Roles in a blueprint](teams.md#roles-in-a-blueprint)).
- **The system message from sections:** `goal`, `system`, `escalation` and `acknowledge_ungated`
  ([Agent spec](agent-spec.md)).
- **Texts and model settings:** `describe` (instructions from a file, tool descriptions), `refine`
  (provider fields and middleware) and `refinements` (lines a build added, with their evidence);
  see [Test and improve](../guide/test.md#texts-and-model-settings-of-an-agent).

### Approvals

An entry is a `tool` and an optional `when`. `when` is either:

- a condition over the call arguments, such as `"amount >= 5000"`; `check()` checks it against the
  tool's arguments ([Conditional approvals](safety.md#conditions));
- or a predicate that takes the arguments: `"package.module:function"`, a file path such as
  `"./tools.py:needs_approval"` (relative to the blueprint's folder), or the stem of a loaded tools
  file (`"tools:needs_approval"`).

### Limits and prices

`limits` caps one run: `max_requests`, `max_total_tokens`, `max_tool_calls`, `max_cost`,
`max_duration`. `max_cost` needs `prices` (`<model>: {input_per_mtok, output_per_mtok,
cache_read_per_mtok}`, the shape of `PriceTable.from_mapping`). What each limit stops, and where
a stopped run shows, is in [Keep it running](../guide/operate.md#limits-and-costs).

- `awa run`, `chat`, `eval`, `serve`, `worker` and the web app apply the limits to every run tree.
  Their `--max-*` options can only tighten them (`merge_limits`; in `eval` the larger value
  holds), and a served request asking for more gets the blueprint's limits.
- An automation stores the `usage` and `cost` of each process run. A breach ends the run `failed`
  with `awa.usage_limit_exceeded`, without a retry.
- `check` errors when `max_cost` is set and an agent's model, written as `provider:model`, has no
  price. It warns when the blueprint has triggers but no limits.
- In Python, `Blueprint.usage_limits()` and `price_table()` give the objects for
  `run(usage_limits=...)`. `build()` itself applies nothing.

### Triggers and automation

`triggers[]` starts runs by itself: schedules, webhooks, file drops, mailboxes, queues.
`automation` says how triggered runs are processed: templates, dedup, concurrency, retries, SLAs,
notifications. The guide is [Run automatically](../guide/run.md).

- Webhooks check a signature or token (`scheme`: `timestamped`, `body`, `token`).
- File drops and mail attachments arrive with their text.
- An attempt may take `run_timeout` (default `30m`).
- `check` warns about templates that do not parse and unset secret variables.
- Without a database, `awa serve` (or `awa worker`) runs every trigger and keeps runs and paused
  runs in `.awa/` next to the blueprint. With the same `--dsn`, the server receives webhooks and
  events and `awa worker` processes on many machines run the rest.

In Python, `awa.blueprints.triggers` writes `triggers:` entries (`trigger_entry`, `add_trigger`)
and lists the commands that run them (`trigger_steps`).

### Policies

A policy has `deny` (tool names refused), `ask` (risk flags whose tools pause for a human),
`reason`, and `use` (a `ToolPolicy` function, imported with `allow_imports`). Policies become the
agents' `tool_policy`. `check` counts denied and asked tools as gated.

## Special cases

`special_cases` holds rule tables that decide what happens with the result of a `process:` step.
Each table is named after its step. Someone who knows the process can fill it in like a
worksheet: a list of rows, an `else` and a `when_unclear`.

```yaml
special_cases:
  decide:                                  # the step these cases are for
    cases:                                 # first match wins
      - {if: "supplier in ['Blocked Supplies Ltd']", then: reject, message: Blocked supplier.}
      - {if: [{field: amount_eur, op: ">=", value: 20000}], then: ask, by: board}
      - {if: [{field: amount_eur, op: ">=", value: 5000}], then: ask, by: finance}
      - {if: [{field: status, op: "=", value: paid}], then: go, to: archive}
      - {if: [{field: amount_eur, op: "<", value: 5000}], then: approve}
    else: ask                              # when no row holds; default ask
    when_unclear: ask                      # ask or reject, never approve
    decimal: "."                           # how numbers written as text are read; "." or ","
```

A bare list is the short form of `cases` (`decide: [{if: ..., then: ask}]`); `else` and
`when_unclear` then default to `ask`.

| Part | What it takes |
| --- | --- |
| `if` | A list of `{field, op, value}` conditions that must all hold, or an expression in the language of approval conditions (`"amount_eur >= 20000 or len(lines) > 50"`). |
| `op` | `=`, `!=`, `<`, `<=`, `>`, `>=`, `one of`, `not one of` (`==`, `in` and `not in` are read as `=`, `one of` and `not one of`). `<` to `>=` need a number, `one of` a list. |
| `then` | `approve` (no person needed for this step), `ask` (a person decides; `by` names the role), `reject`, or `go` with `to: <step>` (also written `go: <step>`). `message` is one sentence for the person or the record. |
| `else` | What happens when every row is false. Required in the model and `ask` by default. `approve` is allowed and gives a warning in `check` and `awa show cases`. |
| `when_unclear` | What happens when a row cannot be decided: `ask` (default) or `reject`. `approve` and `go` are refused when the blueprint loads. |

### How a table decides

1. The rows are tried in order. The first true row decides.
2. A row is true, false or unclear. The first unclear row stops the table, and `when_unclear`
   decides. An unclear row never falls through to a later row or to `else`.
3. `else` decides only when every row is false.

A row is **unclear** when a value it reads:

- is missing or empty;
- is not a finite number (`NaN`, infinity);
- is a list or an object where a `{field, op, value}` row expects one value;
- or is not one clear number where the row compares it with numbers only (`"n/a"`, `true`, and
  `5.000` or `5,000`, which read as 5 or as 5000).

More rules:

- Every row compiles to one expression and is evaluated in strict mode, by the same evaluator as
  approval conditions.
- A row is read from left to right. Once a part is false the row is false, and a value it did not
  reach does not matter: `amount_eur < 5000 and supplier one of [...]` is false for 6,000 even
  without a supplier.
- Text compares exactly, including capital letters: `ACME` is not `Acme`. Numbers written as text
  (`"1,250.50"`) are read as numbers with the table's `decimal`.

### In a run

The table of a step decides on the step's output after its `schema` and `rule` checks passed and
before its `approval` checks:

- `reject` ends the run with status `rejected`.
- `ask` pauses for a person, together with any approval check that applies, in one question.
- `go` continues at a later step.
- `approve` adds no question of its own; an approval check of the step still asks.

`check` refuses a table for a step the process does not have, a `go` to a step that is not later,
and a `go` that jumps over a step with a rule, an approval or special cases. How a run moves
through the steps, with retries and `when`, is in
[The process section](process-section.md#ifelse-retries-and-special-cases).

### Test a table

`awa show cases blueprint.yaml` prints each table in words, its warnings and notes, and the
boundary table (row, probe, input, expected outcome), without a model. `check` reports the
warnings and the rows that never apply.

In Python the table is `awa.workflows.SpecialCases`:

```python
from awa import Blueprint
from awa.workflows import boundary_cases, case_notes

table = Blueprint.from_yaml("blueprint.yaml").special_cases["decide"]
outcome = table.decide({"amount_eur": 6200, "supplier": "Acme Tools"})
outcome.then, outcome.source, outcome.row  # ("ask", "row", 3); source is "row", "else" or "unclear"
table.describe()                           # each row in words: "If amount is 5,000 EUR or more: a person decides (finance)"
boundary_cases(table)                      # test inputs at every edge, each with the outcome the table gives
case_notes(table)                          # rows that never apply, numbers no row decides
```

`boundary_cases` probes every row and records what the table decides, so you can keep each probe
as an eval case:

| The row tests | Probes |
| --- | --- |
| a number limit `c` | just below, exactly, just above `c`: steps of 1 when the field is compared with whole numbers only, else 0.01 or the finest decimal place used |
| `one of`, `not one of` | a value in the list, one outside it, and text in the list in other case |
| a text | exactly that text, another text, the same text in other case |
| `true` or `false` | both |
| every field | missing, and a number that reads two ways (`5.000` with a decimal point, `5,000` with a decimal comma) |

`case_notes` reports a row that never applies because earlier rows already decide every value it
covers, and a row whose conditions contradict each other. For a table that tests one number field
only, it also names the numbers no row decides. Only rows that test a single number field are
analyzed, so no note does not prove that every row can apply.

## Data from your files

A process often starts from files you already have: a bank statement, a folder of invoices. When
you build from your own files (`awa build "<process>" --data <files>`, or
`build_project(..., data=...)`), awa:

1. copies them into the project's `data/files/` folder (the originals are never touched);
2. profiles them without a model ([Data profile](data.md));
3. writes a `data:` section that records which file plays which role and what the process relies
   on in it.

```yaml
connectors:
  files:                       # the agents read the files only through this
    use: workspace
    root: data/files
    include: [read_csv, query_csv]
data:
  - name: bank_lines
    path: data/files/bank.csv  # relative to the blueprint; may be a glob
    role: input                # input | reference | template | output
    description: Bank statement lines to be matched to invoices.
    expect:                    # what the process relies on; every item is optional
      columns: [Buchungstag, Betrag_EUR]
      decimal: ","
      date_format: DD.MM.YYYY
    handling: Match each line to an open invoice by amount and reference.
```

`expect` may also name a key, a sheet, the header row and a minimum number of rows. `mask` and
`digest_tokens` set what the model sees of a document in a process step
([What the mask covers](process-section.md#what-the-mask-covers)).

Agents never open files themselves. A build gives them:

- one read-only `files` connector, with only the read operations that fit the kinds of file found;
- an `output` connector for `data/output/`. Each write waits for a person's approval in the
  generated configuration. You can remove the approval; `check` only warns about it.

### Check the files

`expect` is checked offline against the files as they are now. So a renamed column, a moved header
row or a changed number format is found before a run. `awa validate` and `awa data check` run the
check; from Python, pass a profile to `Blueprint.check`:

```python
from awa import Blueprint
from awa.data import profile

blueprint = Blueprint.from_yaml("blueprint.yaml")
for issue in blueprint.check(data=profile(["data/files/bank.csv"])):
    print(issue)      # prints nothing while the files match
```

With `Betrag` written where the file has `Betrag_EUR`:

```console
$ awa data check blueprint.yaml
error: data[0].expect.columns[1]: column 'Betrag' does not exist in 'data/files/bank.csv' (its columns: 'Buchungstag', 'Betrag_EUR', 'Verwendungszweck'); did you mean 'Betrag_EUR'? [DF-V-003]
blueprint.yaml: 1 error, 0 warnings
```

The exit code is 1 on errors. `awa data run PROJECT --file NAME` (or `--all`) runs the built
process on input files; results go to `data/output/`. Uploads, profiles and builds over HTTP are
on [Data API](data.md#the-data-endpoints).

## Designing and building from a description

`design` asks an architect agent to draft a blueprint from a plain-language description of the
process. It then checks the draft and feeds errors back, for up to `max_repairs` repair rounds.
The result has no error-severity issues; review it before you run it.

```python
from pathlib import Path

from awa.blueprints import design

blueprint = await design(
    "Incoming invoices are checked against purchase orders. Mismatches go to a clerk; "
    "matches above 10,000 EUR need a manager's approval before payment.",
    model="strong",
    tools=[read_invoice, find_purchase_order, book_payment],   # the architect may only use these
)
Path("invoice_desk.yaml").write_text(blueprint.to_yaml())
```

- Without `tools`, the architect names the tools it needs; implement them before building.
- It always sees the built-in tools (`builtin:<name>`, with what each one needs in `toolkits:`) and
  the skills, so it uses them instead of inventing equivalents.
- From synchronous code, call `asyncio.run(design(...))`.

### Build a tested project

`design_project` goes further. It writes the acceptance cases first, then a blueprint wired to a
local workspace of sample data, the process rules as tested tools, the approvals and one
`ConnectionStep` per outside system. `build_project` evaluates that design and repairs it in
bounded rounds:

```python
from awa import UsageLimits
from awa.blueprints import build_project

report = await build_project(
    "Incoming supplier invoices are checked against their purchase order ...",
    out_dir="invoice_desk",
    model="strong",
    usage_limits=UsageLimits(max_requests=150),
    on_progress=lambda step: print(f"[{step.stage}] {step.message}"),
)
print(report.summary())    # what passes, what is connected, what is left to do
```

- `BuildReport` has `passed`, `pass_rate`, `rounds`, `stopped`, `usage`, `eval_report`, `flaky`,
  `design` and `directory`.
- Pass a `ProjectDesign` to `build` to evaluate a design you already have. `environment=` chooses
  the reachable systems (`"auto"` discovers them from the environment and `.env`, names only).
- The test designer (`TEST_DESIGNER_INSTRUCTIONS`) also designs `triggers` (`TriggerDesign`) with a
  `sample_payload`, `assumptions`, and cases that send a `payload` to a `trigger` instead of an
  `input`. The build writes the triggers
  into the blueprint and names them in the README.
- The design briefs include the closest shipped examples, chosen by word overlap without a model
  call.

What this looks like to a user, and the project folder it writes, is in
[Describe a process](../guide/describe.md).

## Projects

`awa init <dir> --template minimal|team|workflow|network|support` writes a runnable starter
project: `blueprint.yaml`, a typed `tools.py`, sample data, `evals/cases.yaml`, `.env.example`,
`README.md`, `pyproject.toml` and offline tests as `test_blueprint.py` in the `tests` folder of the
project (every agent gets a scripted `FakeChatModel` through a `ModelRegistry`).
For real use you need a model key and your own tools. From Python:
`awa.blueprints.scaffold.create_project("my-desk", template="support")`. What each file is for:
[The project folder](project-folder.md).

## What can go wrong

| You see | Cause and fix |
| --- | --- |
| `unknown tool 'read_invoice' (known: none)` | The blueprint names a tool you did not pass. Give `tools=[...]` to `load` or `build`; on the command line put a `tools.py` beside the blueprint or add `--tools tools.py`. |
| `declares approvals (used by bookkeeper) ... pass a checkpointer` | A paused run must be saved somewhere. Pass `checkpointer=StateInMemorySaver()` (from `awa.persistence`), or a database-backed one in production. |
| `Step 'x' uses the agent 'y', which does not exist` | A step of `process` names an agent that is not in `agents`; the message suggests the closest name. |
| `team.orchestrator: unknown agent 'c' (known: a, b)` | A name is misspelled. Names in `team`, `workflow` and `network` must match an agent. |
| `Blueprint file not found: '...'` | A one-line string is read as a path. For YAML text use `Blueprint.from_yaml_text`. |
| `column '...' does not exist in '...'` (data) | A file changed under the `data:` section. Fix the name in `expect`, or put the file back; the message offers the closest real name. |

## Related

- [Describe a process](../guide/describe.md): the same YAML explained for non-programmers.
- [CLI](../reference/cli.md): `awa validate`, `show`, `run`, `chat`, `eval`, `serve`, `build`.
- [Testing](testing.md): offline tests and evaluation of blueprints.
- [Data profile](data.md): what `awa data inspect` reports about your files.
- [The process section](process-section.md): steps with documents and checks that run before the next step starts.
- Examples: `examples/blueprints/support_triage.yaml` (a team with peers, handoffs, a task board and an approval), `examples/blueprints/document_review.yaml` (a workflow with a parallel review), `examples/processes/invoice-check/`. Reference: [Blueprints](../reference/api/core.md#awa.blueprints).
