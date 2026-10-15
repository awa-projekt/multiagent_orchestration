# Safety

In awa a model only chooses within what your code allows. Every hard limit and every approval is
enforced by deterministic Python, not by asking the model.

This page shows how to make a tool wait for a person, how to bound and stop a run, and how awa
protects credentials and personal data. Read it before a process may change data or send something.

## Approvals

A tool call that changes data or sends something can wait for a person. Mark the tool with
`approval=`:

```python title="approval.py"
import asyncio

from awa import Agent, tool
from awa.persistence import StateInMemorySaver
from awa.testing import FakeChatModel, tool_call


@tool(approval="amount > 50")  # small refunds run directly
def issue_refund(order_id: str, amount: float) -> str:
    """Refund an order. Irreversible."""
    return f"refunded {amount:.2f} EUR for {order_id}"


model = FakeChatModel(
    [tool_call("issue_refund", {"order_id": "A-7", "amount": 80.0}), "The refund of 80 EUR is on its way."]
)
refunds = Agent("refunds", tools=[issue_refund], model=model, checkpointer=StateInMemorySaver())


async def main() -> None:
    paused = await refunds.run("Refund 80 EUR for order A-7, it arrived broken.")
    request = paused.interrupts[0]["value"]
    print(paused.is_interrupted, request["tool"], request["args"], request["condition"])
    done = await refunds.resume(paused.thread_id, {"action": "approve", "reviewer": "ana"})
    print(model.calls[1].last_message.content)
    print(done.output)


asyncio.run(main())
```

```text title="approval.out"
True issue_refund {'order_id': 'A-7', 'amount': 80.0} amount > 50
refunded 80.00 EUR for A-7
The refund of 80 EUR is on its way.
```

`FakeChatModel` plays the model, so the example runs offline. With a real model, the gate is just
the decorator, `Agent(...)`, `run` and `resume`. In a blueprint it is two lines under `approvals:`,
`- tool: issue_refund` and `when: "amount > 50"`. The web app then shows the request with its
buttons ([Approvals](../guide/approvals.md)).

What happens:

1. The run pauses with LangGraph's `interrupt()` and the checkpointer saves it.
2. A person decides. With a durable checkpointer this can be days later, in another process.
3. `resume` applies the decision.

| Decision | Effect |
| --- | --- |
| `{"action": "approve"}` | The call runs. |
| `{"action": "reject", "note": "Delivered 45 days ago."}` | The tool returns a rejection with the note, so the agent can explain it. |
| `{"action": "edit", "args": {"amount": 40.0}}` | The call runs with the arguments merged over the original ones, after validation against the tool's schema. |

Anything else counts as a rejection, with a note that says why: a malformed payload, an edit when
edits are off, a decision after the request expired. Add `"reviewer"` to name who decided. Served
systems replace it with the authenticated caller, so the audit log names the real reviewer.

There are four ways to gate a call. All use the same mechanism:

- `@tool(approval=True)`, a predicate `(args)` or `(args, ctx)`, or a condition as text;
- `approvals:` in a blueprint, with an optional `when:`;
- a tool policy that returns `Ask(...)` ([Tools](tools.md#tool-policies-at-run-time));
- `create_tool_approval_wrapper(tool, ...)` around any tool, also one you did not write.

| Option of `create_tool_approval_wrapper` | Purpose |
| --- | --- |
| `requires_approval` | Decide per call: a predicate, a condition text, or always (default). |
| `build_interrupt_payload` | What the reviewer sees; awa adds `agent`, `run_id`, `requested_at` and `expires_at`. |
| `expires_after` | How long a request stays answerable, measured from the checkpoint, so it survives restarts. |
| `store`, `memory_key`, `decision_ttl` | Remember a decision so an equivalent later call is decided without asking; edits are never remembered. |
| `allow_edits` | Accept `"edit"` decisions (default `True`). |
| `redactor` | Mask the arguments shown to the reviewer; the tool still gets the real ones. |
| `valid_actor` | Check the resume payload, for example that it names an allowed reviewer. |

### Conditions

A rule like "approval from 5,000 EUR" is a condition over the call's arguments. It can live in a
blueprint, is checked before anything runs, and is shown to the reviewer:

```yaml
approvals:
  - tool: book_invoice
    when: "amount >= 5000"
  - tool: connector:mail.send_email
    when: "not endswith(to, '@example.com')"   # only outside the company
  - tool: issue_refund                          # always
```

The language has comparisons, `and`/`or`/`not`, `in` with a list, arithmetic, `true`, `false`,
`null` and the functions `contains`, `startswith`, `endswith`, `lower`, `upper`, `len`, `number`,
`empty`, `abs`, `round`, `min` and `max`. It is parsed with `ast` and evaluated over an allowlist;
nothing reaches `eval`.

A condition that cannot be evaluated asks for approval: it fails closed. That includes a comparison
that would have to guess, such as a missing value, or `5.000`, which means 5 or 5000 depending on
the country. `awa validate` checks the names against the gated tool's arguments.

### Approvals in teams and events

- Approvals emit `ApprovalRequestedEvent` (with `interrupt_id` and a redacted `args_preview`) and
  `ApprovalDecidedEvent` (decision, reviewer, latency).
- A team, network or workflow with a checkpointer pauses as a whole when any agent in it waits.
  With several pending approvals, `resume` takes a mapping of interrupt id to decision.
- If you call `resume` yourself twice on one pause, a decision can be applied twice. Served systems
  claim the pause first and refuse the second resume with 409
  ([Serving](serving.md#approvals-over-http)).

## Limits

Every run has limits. Here the agent never answers, and the run stops after three model calls:

```python title="limits.py"
from awa import Agent, UsageLimits, UsageLimitExceededError
from awa.testing import FakeChatModel, tool_call


def lookup(query: str) -> str:
    """Look something up."""
    return "nothing found"


model = FakeChatModel([tool_call("lookup", {"query": "invoice"})], repeat_last=True)  # never answers
agent = Agent("searcher", tools=[lookup], model=model)
try:
    agent.run_sync("Find the invoice.", usage_limits=UsageLimits(max_requests=3))
except UsageLimitExceededError as error:
    print(type(error).__name__, len(model.calls), "model calls")
    print(error.diagnosis.bound)
```

```text title="limits.out"
UsageLimitExceededError 3 model calls
max_requests
```

| Scope | Setting | Default | On breach |
| --- | --- | --- | --- |
| One agent run | `Agent(max_steps=...)` | 25 model calls | `MaxStepsExceededError` |
| A run tree | `run(..., usage_limits=UsageLimits(...))` | the ceilings below | `UsageLimitExceededError` |
| Wall clock of a run tree | `run(..., timeout=...)`, `UsageLimits(max_duration=...)` | the time ceiling (`AWA_RUN_MAX_SECONDS`) | the tree is cancelled, `RunTimeoutError` |
| Team delegation | `TeamLimits`: `max_delegation_depth` 4, `max_parallel_delegations` 8, `max_calls_per_member` 20, `max_background_tasks` 8 | as listed | refused as tool text |
| One delegation | `delegation_timeout` 300 s, `delegation_limits` (`max_requests` 50) | as listed | that delegation stops; the caller gets an error result |
| Team handoffs | `max_handoffs` | 5 | refused, then `HandoffError` |
| Networks | `NetworkLimits` | [Teams](teams.md#agent-networks) | tasks refused, run finished |
| Files and uploads | `awa.data.Limits`, `DataOptions` | [Data](data.md) | 413, 408, 429 or a flagged file |

A run tree is a run and every run it starts: delegations, nested agents, workflow steps.

### The ceilings

Every run tree has ceilings, also without `UsageLimits`. A field you leave `None` takes the ceiling;
a number you give replaces it. You can set a ceiling lower or higher, but never switch it off:

| Ceiling | Setting | Default |
| --- | --- | --- |
| Model calls per run tree | `AWA_RUN_MAX_REQUESTS` | 400 |
| Tool calls per run tree | `AWA_RUN_MAX_TOOL_CALLS` | 200 |
| Input plus output tokens per run tree | `AWA_RUN_MAX_TOKENS` | 1,000,000 |
| Seconds per run tree without a `timeout` | `AWA_RUN_MAX_SECONDS` | 3600 |
| Nesting depth | `AWA_RUN_MAX_DEPTH` | 16 |
| Same tool, arguments and result in one agent run | `AWA_MAX_IDENTICAL_CALLS` | 5 |
| Failures in a row of one tool in one agent run | `AWA_MAX_TOOL_ERRORS` | 5 |
| Identical `Loop` results or `Checked` drafts in a row | `AWA_MAX_IDENTICAL_OUTPUTS` | 2 |

A run that repeats itself without progress stops with `NoProgressError`. When it is a delegated
member, a `Map` item, a `Parallel` branch or a network task, only that part fails.

Every forced stop carries a diagnosis: `error.diagnosis`, `ErrorEvent.stop`, and the span attributes
`awa.stop.*`. It names the bound, the last steps (names and digests, never content) and a fix. It is
built by rules, without a model call.

### Usage limits and money

`UsageLimits` fields are `max_requests`, `max_input_tokens`, `max_output_tokens`,
`max_total_tokens`, `max_tool_calls`, `max_cost` (with `prices`) and `max_duration`.

- `max_requests` holds exactly, even for concurrent calls.
- Token, tool-call and cost limits are checked after each response, so calls in flight can
  overshoot them slightly.
- No money limit is on by default, because awa ships no prices. A money limit is one line with a
  `PriceTable` of the prices you pay:
  `UsageLimits(max_cost=2.0, prices=PriceTable(prices={"my-model": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0)}))`.
  `result.usage.cost(prices)` computes the cost.

A blueprint states the same bounds once, under `limits:`, with the `prices:` that `max_cost` needs.
`awa run`, `chat`, `eval`, `serve`, `worker` and the web app pass them to every run tree. Options on
the command line only tighten them, and a request cannot raise a served system's ceiling. In an
automation, the budget covers a process run over all its retries.

### Timeouts

`timeout=` on `run`, `stream`, `start` and `resume` covers the whole run tree; a nested run can only
shorten it. When it expires, the run tree is cancelled at once and `RunTimeoutError` (retryable)
carries `elapsed` and `limit`. Tools read the time left with
`current_run_context().remaining_time()`.

## Stop a run

`start` runs in the background and returns a handle. `awa.runtime.cancel` stops the whole run tree
from any task or thread of the process:

```python title="stop.py"
import asyncio

import awa.runtime
from awa import Agent
from awa.errors import RunCancelledError
from awa.testing import FakeChatModel, tool_call


async def reconcile(month: str) -> str:
    """Reconcile the invoices of a month."""
    await asyncio.sleep(60)
    return "done"


agent = Agent("clerk", tools=[reconcile], model=FakeChatModel([tool_call("reconcile", {"month": "March"})]))


async def main() -> None:
    handle = agent.start("Reconcile the March invoices", timeout=1800)
    while handle.run_id not in awa.runtime.active_runs():  # until the run has started
        await asyncio.sleep(0.01)
    awa.runtime.cancel(handle.run_id, "wrong customer", cancelled_by="alice")
    try:
        await handle.result()
    except RunCancelledError as error:
        print(type(error).__name__, error.reason, error.cancelled_by)


asyncio.run(main())
```

```text title="stop.out"
RunCancelledError wrong customer alice
```

- The tool in progress was interrupted. The run emits `RunCancelledEvent` before its `ErrorEvent`.
- `cancel` takes the top-level run id or the id of any run nested in it. It returns `False` when no
  such run is active.
- awa checks for a pending stop before every model and tool call. A long computation in a tool can
  call `await awa.runtime.checkpoint()`.
- A run paused for an approval is not running, so there is nothing to cancel: its thread stays
  resumable.
- Cancellation works per process. The server's `DELETE /v1/runs/{run_id}` cancels the runs it
  serves.

## Guardrails

A guardrail is a deterministic check. It runs before the first model call (`input_guardrails`) or
on the final output (`output_guardrails`). A blocking result raises `GuardrailTripwireError`:

```python title="guardrails.py"
from awa import Agent, GuardrailTripwireError
from awa.guardrails import BlocklistGuardrail
from awa.observability import RegexRedactor
from awa.testing import FakeChatModel

assistant = Agent(
    "assistant",
    input_guardrails=[BlocklistGuardrail(["password", "api key"])],
    model=FakeChatModel(["never called"]),
)
try:
    assistant.run_sync("What is the admin pass​word?")  # a zero-width space does not hide it
except GuardrailTripwireError as error:
    print(error.result.guardrail_name, "|", error.result.reason)

text = "Pay to DE89 3704 0044 0532 0130 00, questions to ana@example.com"
print(RegexRedactor.pii().redact(text))
```

```text title="guardrails.out"
blocklist | contains blocked term: 'password'
Pay to [REDACTED:iban], questions to [REDACTED:email]
```

| Guardrail | Checks |
| --- | --- |
| `LengthGuardrail` | the length of the input or output |
| `BlocklistGuardrail` | blocked terms, after folding case, compatibility forms and invisible characters |
| `PIIGuardrail` | card and bank numbers, email and phone numbers; `mode="block"` (default), `"warn"` or `"redact"` |
| `SchemaGuardrail`, `NonEmptyFieldsGuardrail`, `MustIncludeGuardrail` | an output contract; `on_fail="retry"` by default, so the model answers again before anything blocks |

A custom guardrail is any object with a `name` and a `check(candidate)` method that returns a
`GuardrailResult`. A guardrail that raises blocks the candidate. Contract checks share the
`output_retries` budget with schema repair and output validators
([Agents](agents.md#structured-output-and-output-checks)).

## Redaction

awa removes credentials wherever it writes text for operators: log lines, spans, audit records,
traces, failed delegations, approval previews and the error texts of tools.

- **Credentials, always.** The built-in patterns cover private keys, tokens, Basic and Bearer
  credentials, cookies, common API key formats, signed URLs, passwords in URLs and `password=...`
  assignments. The values of the credentials in the environment are removed by value.
- **Personal data, when you ask.** What counts as personal depends on your deployment, so it is
  opt-in. `RegexRedactor.pii()` adds email addresses, IBANs (checked with mod-97), card numbers
  (checked with Luhn), IP addresses and phone numbers.
  `RegexRedactor.pii(extra=[RedactionPattern(...)])` adds your own identifiers.
- **Your own redactor.** Regular expressions are a floor: they miss names and free text. Pass any
  object with `redact(text) -> str` (a named-entity detector, for example) wherever `redactor=` is
  accepted.

## Data protection

| Data | Where | Holds personal data when |
| --- | --- | --- |
| Threads: messages, tool calls and results, approvals | checkpointer, one thread per `thread_id` | always, for a conversation |
| Long-term memory | store, `("awa", "memory", ...)` | the model or your code remembers facts about people |
| Remembered approval decisions | store, `("awa", "tool_approval")` | the decision key or note names a person |
| Process runs, steps and approvals | the data store (`.awa/awa.db` or PostgreSQL) | people appear as an HMAC link and as principal and reviewer ids |
| Uploads and builds | one folder per tenant | always; they expire after 24 hours and 7 days |
| Run traces | `.awa/traces/` | at level `full` always; at `summary` in tool argument previews |

**Erase one person.** `awa.persistence.erase_subject(subject, store=..., checkpointer=...,
artifacts=..., ...)` deletes everything of one person from the backends you pass, and returns an
`ErasureReport`. It covers memories, remembered decisions, run log rows (through the pseudonym key
`AWA_PSEUDONYM_KEY`), process runs, threads linked with `SubjectIndex`, artifacts by prefix, run
traces, and your own namespaces.

It is idempotent and supports `dry_run=True`. A backend it could not reach is reported, not skipped
silently. It does not reach copies outside awa: exported traces, logs, provider logs and backups.

**Delete old data.** `RetentionPolicy` says how long each kind of data may stay.
`apply_retention(policy, checkpointer=..., store=..., traces=...)` deletes what is older; run it on
a schedule. `awa store sweep` and `awa store erase` do the same for the data store
([Production](production.md#the-data-store)).

The tamper-evident audit trail (`AuditLog`) is on [Observability](observability.md#audit-trail).

## Threat model

Who can send what to each entry point, what stops them, and what remains yours to do:

| Entry point | Who can send what | Controls | What remains |
| --- | --- | --- | --- |
| HTTP API (`awa serve`, `create_app`) | Anyone who reaches the port; with keys only key holders | API keys or tokens, tenant-scoped threads and runs, 1 MiB body limit, rate and concurrency limits, generic 500 answers, security headers, `Host` check without keys | Use keys on any interface other than loopback |
| Webhook triggers | Anyone on the network who proves the secret | HMAC or token, constant-time compare, 5-minute replay window, dedup by delivery id | Signature schemes without a timestamp replay after the window: use TLS and a `dedup` key |
| Data endpoints | Authenticated callers with a builder role | Tenant scoping, content-based file types, size and archive limits, PDFs read in a killable process, masking of what a model sees, optional virus scanner | Pass a `scanner`; keep `root` private |
| `awa ui` | The local browser user | Loopback only, a random token sent as a header (never a cookie), `Origin` check, CSP nonce | A project can contain code: open only projects you trust |
| `awa mcp serve` | The parent client (stdio) or key holders (HTTP) | Bearer gate, `Host` and `Origin` checks on loopback, approvals by elicitation | One shared caller unless you pass an authenticator |
| Checkpoint and run stores | Whoever can write the file or schema | Strict revival of stored types | Keep the store writable only by the service |
| Mail, file-drop and schedule triggers | Senders of mail, writers of the folder | Size caps, safe names, zip-bomb limits | Their text reaches a model: approvals and the risk check are the barrier |
| Text that tools return | Anyone who can publish text | Framing as data, risk flags, the risk check, approvals | Your own tools are framed only when annotated |
| Outbound HTTP | The model, and URLs you configured | Host allowlist, private addresses blocked, redirects checked again | Callback addresses you configure may be internal |

## Compliance controls

awa provides mechanisms. Whether a system complies depends on what it does, where it runs and how
you operate it. The table is a starting point for your own assessment, not legal advice.

| Duty or risk | Controls |
| --- | --- |
| Prompt injection, excessive agency (OWASP LLM01, LLM06) | Framed tool output and agent messages, risk check, approvals, explicit topology |
| Sensitive information disclosure (LLM02) | Secret redaction by default, `PIIGuardrail`, content capture off by default |
| Unbounded consumption (LLM10) | Ceilings, `UsageLimits`, `TeamLimits`, timeouts, `LimitExceededEvent` |
| EU AI Act Art. 12 record-keeping, Art. 26 deployer logs | `AuditLog` with a durable sink and `verify_chain` |
| EU AI Act Art. 14 human oversight | Approvals with edit and reject, `awa.runtime.cancel`, timeouts, traces |
| EU AI Act Art. 50 transparency | Your interface says that people talk to an AI system; awa does not do this for you |
| GDPR Art. 5 minimization and storage limitation, Art. 17 erasure | Content capture off, `erase_subject`, `apply_retention`, `decision_ttl` |
| GDPR Art. 32 security of processing | Audit trail, limits, approvals, server authentication |

## Licenses of dependencies

awa is MIT licensed. Its runtime dependencies are MIT, BSD, Apache, PSF or ISC licensed, except:

| Package | License | Comes with |
| --- | --- | --- |
| `psycopg`, `psycopg-binary`, `psycopg-pool` | LGPL-3.0 | extra `postgres`, so also `all` |
| `pymssql` | LGPL-2.1 | extra `sql`, so also `all` |
| `certifi`, `orjson` | MPL-2.0 | the base install |
| `langgraph-api`, `langgraph-runtime-inmem` | Elastic-2.0 | extra `studio` only |
| `forbiddenfruit` | GPL-3.0-or-later or MIT, at your choice | extra `studio` only |

Used as separately installed libraries, they place no obligation on your code. In one bundled
application you distribute, pass on their license texts and keep the LGPL libraries replaceable.
The `studio` extra is a local development tool: Elastic-2.0 forbids offering those packages to
others as a managed service, and `forbiddenfruit` may be used under its MIT option.

Check the locked versions for known vulnerabilities on a schedule:

```console
uv export --frozen --no-emit-project --extra all --hashes -o requirements-audit.txt
uvx pip-audit==2.10.1 -r requirements-audit.txt --no-deps --disable-pip --strict --desc on
```

## What can go wrong

| You see | Cause and fix |
| --- | --- |
| The run stops and `is_interrupted` is true | A gated tool waits. Resume with a decision; the run needs a checkpointer. |
| A decision was ignored and the tool did not run | The payload was malformed, an edit was invalid, or the request had expired: it counted as a rejection. The note says why. |
| `UsageLimitExceededError` or `NoProgressError` | A limit or ceiling was reached. `error.diagnosis` names the bound and a fix. |
| `RunTimeoutError` | The run tree used its `timeout` or `max_duration`. |
| `GuardrailTripwireError` | A guardrail blocked the input or output; `error.result` names it and the reason. |

Example: `examples/05_approvals_and_limits.py`.
