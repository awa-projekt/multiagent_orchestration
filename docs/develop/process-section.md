# The process section

The `process:` section of a blueprint is a line of steps. Each step has one agent and optional
checks. The checks run after the agent has answered and before the next step starts, so a result
that fails a check stops the run, and the next agent is never called.

Use it when each result must pass rules, or a person, before the process goes on. A step can also
be tried again, be skipped by a condition, or have a rule table of special cases. This page is for
developers who write or review a blueprint and know [Blueprints](blueprints.md). The section is
optional; `format_version` stays `awa/v1`.

## An example

Two steps: read an invoice, then compare it with a price list. Bad values stop the run; a large
deviation asks a person.

```yaml
agents:
  - name: extractor
    description: Reads one supplier invoice and returns its fields.
    tools: [builtin:read_file]
    output_schema:
      type: object
      properties: {supplier: {type: string}, total: {type: number}}
      required: [supplier, total]
  - name: price_checker
    description: Compares the invoice lines with the price list.
    documents: [price_list]            # the agent always has this document
    tools: [builtin:read_file]
    output_schema:
      type: object
      properties: {ok: {type: boolean}, max_deviation_pct: {type: number}}
      required: [ok, max_deviation_pct]
data:
  - name: price_list
    path: data/files/price_list.csv
    role: reference
    expect: {columns: [item, unit_price], decimal: "."}
process:
  carry: previous
  steps:
    - name: extract
      title: Read the invoice
      agent: extractor
      checks:
        - kind: schema                  # the output has the fields of the agent's output_schema
        - kind: rule
          fail_when: "empty(supplier) or total <= 0"
          message: The invoice has no supplier, or a total of zero or less.
    - name: validate
      agent: price_checker
      checks:
        - kind: rule
          fail_when: "max_deviation_pct > 25"
          message: A line is more than 25 percent away from the list price.
        - kind: approval
          when: "max_deviation_pct > 2"
          reason: Invoices that deviate from the price list are decided by a person.
          by: accounts_manager
```

The complete, runnable version is `examples/processes/invoice-check/` (three steps: read the
invoice, compare with a price list document, decide with a person).

## What a step does, in order

1. **`when`.** A step whose `when` is false is `skipped`. If the condition cannot be decided, the
   run stops.
2. **Documents.** The step's documents (and its agent's) must exist and match their `expect:`.
   If not, the step fails before any model is called, with a message that says what to do.
3. **The agent runs.** Its input is the carried text, then a block that lists each document with
   a digest of the file. The block is marked as data: the model is told to treat it as data. That
   lowers the risk of instructions hidden in a document; it does not remove it. Documents come in
   the step input, not in the instructions, so one agent can sit on two steps with different
   documents.
4. **The answer is not empty.** An empty answer fails the step.
5. **`schema` and `rule` checks**, in the order written. A failed check (or an error of the agent)
   is tried again when the step has a `retry`. After the last try, `after_last_try` decides.
6. **Special cases** of the step (`special_cases.<step>`), on the output that passed the checks.
7. **`approval` checks, last.** A person is never asked about a result that already failed a
   check. When several approvals apply, or an approval and a special case that asks, the person is
   asked once, with every reason.

How a run ends when a step does not pass:

- A failed check, or an error of the agent, after the last try (with `after_last_try: stop`)
  stops the run with `StepCheckFailedError` (code `awa.step_check_failed`) or with the agent's
  error. The message names the step and says what to do. Its `steps` attribute lists the status of
  every step.
- A rejection, by a special case or by a person, stops the run with `StepRejectedError` (code
  `awa.step_rejected`, a `StepCheckFailedError`). Automation runs, served runs and jobs then have
  the status `rejected`, and an automation never retries it.
- There is no "accept anyway".

## The checks

- **`schema`**: the output must match a JSON schema. Without `schema:`, the agent's own
  `output_schema` is used. A JSON object in the answer text counts as the output.
- **`rule`**: `fail_when` is an expression over the output fields and `text` (the answer text).
  When it is true, the step fails with `message` and the values the rule read.
- **`approval`**: pauses the run until a person decides. `when` limits it (no `when`: always).
  `reason` is shown to the person; `by` names the role that should decide.

**Rules and approval conditions fail closed.** A rule that cannot be evaluated on the output (a
missing field, text where a number is expected) fails the step instead of passing it. An approval
condition that cannot be evaluated asks.

The expression language is the one of `approvals[].when`: comparisons, `and`, `or`, `not`, `in`,
arithmetic and `contains`, `startswith`, `endswith`, `lower`, `upper`, `len`, `number`, `empty`,
`abs`, `round`, `min`, `max`. There are no loops over list items and no regular expressions:
"every line has a price" needs a `schema` check, another step or a tool.

"Checked" means the checks passed. It does not mean the answer is correct: review results that
matter. A step with no checks of its own still gets the automatic ones (documents, non-empty
answer); `check` warns about it.

## The fields

| Field | Meaning |
| --- | --- |
| `process` | A mapping with `steps` and `carry`, or a bare list of steps. A step may be written as the agent's name. `process` and `workflow` cannot both be set. |
| `process.carry` | `previous` (default): a step receives the previous step's result (the JSON of its structured output, else its text). `all`: it receives the request and every earlier step's result. |
| `steps[].name` | Letters, digits, `_` and `-`, up to 48 characters, unique. Default: the agent's name (`_2`, `_3` for a repeated agent). |
| `steps[].title` | A label for people, up to 80 characters. |
| `steps[].agent` | An entry of `agents` (or the name of a team or network of the blueprint). |
| `steps[].documents` | Names of `data:` entries the agent gets in this step. |
| `steps[].checks` | `schema`, `rule` and `approval` checks ([above](#the-checks)). |
| `steps[].retry` | How often the step is tried: `{max_attempts, initial, backoff, max_delay, jitter, on}` (the shape of `automation.retry`; `attempts` or `tries` and `wait` are accepted too, `retry: 3` is the short form). At most 5 tries; `on` is `[error, check]` by default. Not allowed for an agent that writes or sends. |
| `steps[].after_last_try` | `stop` (default), `ask`, or `go: <later step>` (also `{go: <step>}`). `go` is not allowed for an agent that writes or sends, nor into a step whose agent does. |
| `steps[].when` | The step runs only when this condition is true; otherwise it is `skipped`. |
| `special_cases.<step>` | The rule table of the step (top level of the blueprint, named after the step). See [Blueprints: special cases](blueprints.md#special-cases). |
| `agents[].documents` | Names of `data:` entries the agent always has, in every step it does. They are listed in its instructions and checked and delivered like step documents. |
| `data[].role` | `input`, `reference`, `template` (the agent copies its layout) or `output`. |
| `data[].mask`, `data[].digest_tokens` | How personal data is hidden in the digest the model sees (`none`, `placeholders`, `drop`; default: none, unless the project says otherwise), and its size (200 to 12000 tokens). See [What the mask covers](#what-the-mask-covers). |

## If/else, retries and special cases

```yaml
process:
  - name: extract
    agent: invoice_reader                # reads only: a step that writes or sends may not retry
    retry: {max_attempts: 3, initial: 10s, on: [error, check]}
    after_last_try: ask                  # stop (default) | ask | go: <later step>
    checks:
      - {kind: rule, fail_when: "amount_eur <= 0", message: No amount found.}
  - name: board_review
    agent: board_secretary
    when: "amount_eur >= 50000"          # skipped below 50,000
  - name: decide
    agent: payment_clerk
    checks:
      - {kind: approval, when: "amount_eur >= 20000", reason: Large payments need a person.}
  - name: archive
    agent: archivist
special_cases:
  decide:                                # first match wins
    cases:
      - {if: "supplier in ['Blocked Supplies Ltd']", then: reject, message: Blocked supplier.}
      - {if: [{field: amount_eur, op: ">=", value: 5000}], then: ask, by: finance}
      - {if: [{field: status, op: "=", value: paid}], then: go, to: archive}
      - {if: [{field: amount_eur, op: "<", value: 5000}], then: approve}
    else: ask
    when_unclear: ask                    # ask | reject, never approve
```

### Retry

- Each try is its own step of the run graph. The try number is kept in the run's checkpointed
  state: a run that pauses for a tool approval in try 2 continues in try 2 when the decision
  comes, and try 1 is not run again.
- With `on: check`, the next try gets the message of the failed check in its input.
- The wait before try n+1 is `initial * backoff**(n-1)`, at most `max_delay` (default ten times
  `initial`), spread by `jitter`.
- Never tried again: a reached limit, a cancellation, a run timeout, a guardrail block, a
  permission or configuration error, and programming errors (`ValueError`, `TypeError` ...).
- Every try costs a model run. `limits` bound the whole run, retries included.

### After the last try

- `stop` fails the run.
- `ask` pauses for a person: approve continues with the last result, reject ends the run as
  `rejected`.
- `go: <step>` marks the step `failed` and the steps in between `skipped`. It continues at that
  step with what the step before the failed one handed on; the failed result is never passed on.

### `when`

The condition reads the request's JSON fields, the output fields of every earlier step that passed
(a later step wins), and `text`. It is evaluated like a special case: a value it needs that is
missing or unclear stops the run with a message, instead of guessing. A step skipped by `when`
skips its checks too; `check` warns when the step has a rule, an approval or special cases.

### Special cases

How a table decides is in [Blueprints: special cases](blueprints.md#special-cases). In a run:

- `approve` adds no question of its own. An approval check of the step that applies still asks: in
  the example, 25,000 EUR asks a person although no row asks.
- `ask` pauses for a person. `by` is recorded and shown, not enforced.
- `reject` ends the run with status `rejected`.
- `go` continues at a later step after this step's approval checks; the steps in between are
  `skipped`.

There are no loops back: a `go` must lead to a later step. What else `check` refuses is listed in
[Checks of the blueprint](#checks-of-the-blueprint).

### Try it without a model

The dry run walks the process with stand-in agents that answer the values you give:

1. Put the values in a file, for example `{"supplier": "Acme Tools", "amount_eur": 6200}` in
   `values.json` (a file avoids the quoting rules of each shell). Fields of an agent's output
   schema that you leave out get sample values.
2. Run `awa show dry-run blueprint.yaml --input values.json`.
3. Read the path it prints: each step, each try with the wait a real run would make, each pause
   with the question and how the dry run answered it, and the result.

`--fail extract=2` lets the first two tries fail; `--decide reject` says no at each pause. The dry
run asks no model, calls no tool and reads no document. So it is free and gives the same path every
time; it proves the routing, not what a model would answer. In Python:
`await awa.blueprints.dry_run(blueprint, values, checkpointer=StateInMemorySaver(),
fail={"extract": 2})`.

## Approvals: what is and is not protected

- A step approval needs a checkpointer (`StateInMemorySaver()` in tests, a database-backed one in
  production). `Blueprint.needs_checkpointer()` says whether a blueprint needs one: it covers
  tool approvals, `ask` policies and step approvals. `build`, `awa run`, `awa eval`, `awa serve`
  and the web app use it. Without a checkpointer `build` refuses, and a workflow built in code fails
  at the approval instead of passing it.
- The decision is `{"action": "approve"}` or `{"action": "reject", "note": "..."}`. **Anything
  else, a malformed answer included, counts as a rejection.** A rejection ends the run with the
  note of the person. The step's agent does not run again when the approval is answered.
- **awa does not check who answers.** `by` records the role and shows it. Enforcing roles is up to
  the code that calls `resume` (your API, the approvals inbox of your product).
- The payload the person sees is `{kind: step_approval, tool: "step:<name>", step, title, agent,
  reason, output, args, by}`. The approval card of the web app names the step and the agent.
  `ApprovalRequestedEvent` and `ApprovalDecidedEvent` carry the tool `step:<name>`, so the audit
  log and the traces record who decided.
- Tool approvals (`approvals:`, policies) stay as they are. A step approval gates the result of a
  step; a tool approval gates a call. A step whose agent has a gated tool can pause twice: first
  inside the agent, then at the step approval.

## What the mask covers

`mask: placeholders` or `drop` hides personal data in the **digest** that is put into the step
input. It does not cover what a tool reads from the file itself: a read tool of the agent returns
the original.

So `check` reports an error when a masked document is attached to an agent with a tool that looks
like a file reader (`builtin:read_file`, `connector:files.<op>` and similar names). Remove the
tool, or set the document to show everything and say so in the step. This is judged from the tool
reference alone; a tool with another name is not recognized.

## Events, results and traces

- `StepEvent` (`type: step`) reports `started`, `checking`, `needs_approval`, `checked`,
  `failed`, `retrying`, `skipped` and `rejected` per step, with the checks run so far. Its `agent`
  is the step's agent; `attempt` is the try (for a step with a `retry`), and `case` the id of the
  special case that decided (`<step>.case.<row>`, `<step>.case.else`, or
  `<step>.case.<row>.unclear`). After an approval pause, `started` can appear again for a step
  whose agent paused on a tool approval. See [Events](../reference/api/core.md#awa.contracts).
- The result's `metadata["steps"]` lists every step with `name`, `title`, `agent`, `status`
  (`checked`, `skipped`, or `failed` for a step that continued elsewhere after its last try),
  `checks`, `run_id` and `duration_ms`, plus `attempts` (with a retry), `case` (the special case
  that decided, in words too), `message` and `continued_at` when they apply.
- Traces show one line per step with its try and its special case, and the failed checks;
  `awa runs trace` renders them. OpenTelemetry (and Langfuse) gets an `awa.step` span event on the
  workflow span with `awa.step.attempt` and `awa.step.case` (the message text only with content
  capture).

## Checks of the blueprint

`awa validate` and `Blueprint.check` report these. Errors stop `build`; warnings do not. "Writes
or sends" is judged from the tool's risk (`external_action`), the same judgment as the approval
check of `tool_risks`.

| Finding | Severity |
| --- | --- |
| `process` and `workflow` both set; a process without steps; invalid or duplicate step or check names | error |
| A step without an agent, or with an unknown one (the closest name is suggested) | error |
| A document name that `data:` does not declare (step or agent) | error |
| A `schema` check without any schema; an expression that does not parse or reads a name that is not an output field of the agent; a rule without a `message` | error |
| A masked document attached to an agent with a file-reading tool | error |
| `special_cases.<name>` for a step the process does not have; a `go` to an unknown or earlier step, or one that jumps over a step with a rule, an approval or special cases | error |
| A `retry` or `after_last_try: go` for an agent that writes or sends, or `after_last_try: go` into a step whose agent does | error |
| A `when` on a step with a rule, an approval or special cases (they are skipped with it) | warning |
| A document whose file is missing (when the files are checked), a document attached to an agent that cannot read it, a result file attached as input, a document attached twice | warning |
| A step with only the automatic checks; an agent on no step; no `limits` on a process of two or more steps; more than 12 steps | warning |
| A field the next agent's instructions name in backticks that an earlier step returns but the previous one does not (with `carry: previous`) | warning |

## Commands

- `awa show <blueprint>` prints the steps with their agents, documents and checks in words;
  `awa show --mermaid` draws the chain.
- `awa show cases <blueprint>` prints the special cases with their boundary tests.
- `awa show dry-run <blueprint> --input VALUES` walks the process without a model (exit code 1
  when a step would fail).
- `awa run` and `awa eval` run a process like any workflow. `awa eval` cases may carry
  `metadata: {step: <name>}`, and a `decisions:` entry with `tool: "step:<name>"` answers a step
  approval. A run that stops at a check ends with an error, so a case cannot expect it; test those
  stops in code.

## How it is tested

The flow is proven offline with scripted models: order, checks, documents, the pause and its
resume, the stops, running one step again, retries, `when`, special cases and the dry run. The
tests are `tests/unit/test_workflows_process.py`, `tests/unit/test_workflows_process_retry.py`,
`tests/unit/test_blueprints_process_build.py`, `tests/unit/test_blueprints_process_routing.py`,
`tests/unit/test_cli_show_dry_run.py` and `tests/examples/test_process_invoice_check.py`.

Not proven: how well a real model does a step, and whether a retry helps a real model pass a check
it failed. A live result comes from one model and a small number of runs; read it as that.

## Related

[Blueprints](blueprints.md), [Workflows](workflows.md), [Human in the loop](safety.md#approvals),
[Data from your files](blueprints.md#data-from-your-files), [Events](../reference/api/core.md#awa.contracts).
