# Test and improve

Tests tell you, with numbers, whether your process still does the right thing. You need them before real work and
after every change to an instruction, a tool, a model or a rule. A test is a list of
[**eval cases**](../reference/glossary.md#eval-case): example requests and the checks their result must pass.

```yaml
- id: large-invoice
  input: Please release invoice INV-2042 for payment.
  scorers:
  - approval_requested:finance_approval
  - contains:INV-2042
  decisions:
  - action: approve
    tool: finance_approval
```

The shortest path:

1. `awa build` already wrote the first cases to `evals/cases.yaml`.
2. Run them: `awa eval blueprint.yaml evals/cases.yaml`, or **Run all tests** on the page **Test** of the web app.
3. Add cases from your own data, and run them again after every change.

Each case calls the model and costs a little, so run the tests when something changes, not all the time. The
[tour](tour.md#step-7-run-the-tests) runs them once.

## Write an eval case

Copy the folder `invoice-desk` of the tour to `desk-tests`. Add a fourth case for when the head of finance says no:

<!-- guide:do cd . -->
<!-- guide:do copy invoice-desk desk-tests -->
<!-- guide:do cd desk-tests -->
<!-- guide:file evals/cases.yaml -->
```yaml
name: invoice_desk
cases:
- id: small-invoice
  input: Please release invoice INV-2041 for payment.
  scorers:
  - tool:invoice_ledger_query
  - no_approval:finance_approval
  - contains:INV-2041
- id: large-invoice
  input: Please release invoice INV-2042 for payment.
  scorers:
  - approval_requested:finance_approval
  - contains:INV-2042
  decisions:
  - action: approve
    tool: finance_approval
- id: paid-invoice
  input: Please pay invoice INV-2043.
  scorers:
  - no_approval:finance_approval
  - contains:paid
- id: large-invoice-rejected
  input: Please release invoice INV-2042 for payment.
  tags: [safety]
  decisions:
  - action: reject
    note: Wait for the delivery note.
    tool: finance_approval
  scorers:
  - approval_requested:finance_approval
  - approval_rejected:finance_approval
  - contains:hold
```

| Field | Meaning |
| --- | --- |
| `id` | A short name that does not change; reports and comparisons match cases by it. |
| `input` | The request, as a person would write it. |
| `scorers` | The checks of this case. |
| `decisions` | The answers to the approvals the case runs into (below). |
| `tags` | Labels, to run a group with `--tag`. |
| `expected` | The expected answer, for the checks that need one. |

Write the cases a new colleague would need: everyday requests, odd ones (an unknown invoice), the exceptions that go
wrong today, and the ones where the system **must not** act alone.

### The checks

| Check | Passes when |
| --- | --- |
| `contains:<text>` | The answer contains the text, in any case. |
| `exact` | The answer is exactly `expected`. |
| `regex:<pattern>` | The answer matches a text pattern. |
| `tool:<name>`, `no_tool:<name>` | The tool was used, or never used. |
| `delegated:<agent>`, `handoff:<agent>` | Work was handed to this agent. |
| `approval_requested:<tool>`, `no_approval:<tool>` | A person was asked about this tool, or never. |
| `approval_granted:<tool>`, `approval_rejected:<tool>` | A decision approved, or rejected, this tool. |
| `approval:<tool>` | The run ended waiting for approval of this tool. |
| `max_latency:<ms>`, `max_tokens:<n>` | The run stayed under a time or a size. |
| `json_fields` | A structured answer has the values in `expected`. |
| `judge:<criteria>` | A model grades the answer against your words. |

Checks on tools, delegations, handoffs and approvals look at *how* the answer came about. A correct answer that
skipped the approval fails. A `judge:` check is itself a model and can vary, so prefer the other checks.

### Decisions in a process with several approvals

A case answers its approvals like a reviewer would, in the order they are asked:

```yaml
- id: large-invoice
  input: Book invoice INV-7 (6,200 EUR) and tell the supplier.
  decisions:
    - approve                                        # the booking
    - {action: reject, note: Wrong contact person.}  # the email to the supplier
  scorers:
    - approval_granted:book_invoice
    - approval_rejected:notify_supplier
```

A decision is `approve`, `reject`, `{action: reject, note: ...}` or `{action: edit, args: {...}}`. Add `tool:` when
approvals can come in any order, and `repeat: true` with `tool:` when one tool asks many times. With fewer decisions
than approvals, the run ends waiting at the first unanswered one, which `approval:<tool>` checks.

## Run the tests

In the web app, **Run all tests** on the page **Test** runs every case and shows each result:

![The Test page: the button Run all tests, the result 3 of 3 eval cases passed with the meter full, and the card of an eval case with what it sends, its files and its result](../assets/screenshots/guide/a07-test.png)

--8<-- "docs/assets/screenshots/guide/a07-test.legend.txt"

In a terminal, run only the new case, three times, to see how steady it is:

<!-- guide:run -->
```console
$ awa eval blueprint.yaml evals/cases.yaml --repetitions 3 --tag safety
Evaluation: invoice_desk
========================

System invoice_desk on dataset invoice_desk: 1 case(s) x 3 repetition(s).

- Pass rate: 100.0% (3/3 runs)
- Errors: 0
- Usage: 12 model call(s), 3537 tokens (3267 in, 270 out), 6 tool call(s)
- Duration: 0.3 s

Reliability
-----------

  measure                                 value
  pass@1 (one run passes)                 100.0%
  pass@3 (at least one of 3 runs passes)  100.0%
  pass^3 (all 3 runs of a case pass)      100.0%
  latency p50 / p95                       121 ms / 123 ms
  cost                                    not computed (no prices given)
  cache hit rate                          0.0%

Caching
-------

  measure                               value
  model calls answered by the provider  12
  model calls replayed (no cost)        0
  model call p50 / p95                  6 ms / 20 ms
  tool results reused / stored          0 / 3
  prompt prefix of invoice_checker      6c23893e5dbddce8 (stable)
  prompt prefix of payment_clerk        b6fbbf4322da619c (stable)

Scorers
-------

  scorer                               mean  pass rate  passed  failed
  approval_requested:finance_approval  1.00     100.0%       3       0
  approval_rejected:finance_approval   1.00     100.0%       3       0
  contains:hold                        1.00     100.0%       3       0

Cases
-----

  case                    passed  scores                                                                                                  ms  tokens
  large-invoice-rejected  3/3     approval_requested:finance_approval 1.00, approval_rejected:finance_approval 1.00, contains:hold 1.00  121    1179
```

| Part | What it tells you |
| --- | --- |
| Pass rate | The share of runs that passed every check. |
| `pass@1` | How often one run is right. |
| `pass@3` | Whether each case worked at least once in three runs: what the system can do at best. |
| `pass^3` | Whether each case worked **every time** in three runs. When nobody checks each answer, watch this one. |
| `latency p50 / p95` | The typical run, and the one slower than 19 in 20 runs. |
| Scorers and Cases | Which kind of check fails most, and each case with its scores. |
| Failures | Which case failed which check, and why; only when something fails. |

`awa eval` ends with exit code 1 when fewer cases pass than `--min-pass-rate` (default: all). With `--repetitions 3
--min-pass-hat-k 0.9` it also fails unless 90 percent of the cases passed every time. `--case ID` runs one case and
`--concurrency N` runs several at once. Every case works on a fresh copy of the project's files, so cases never
affect each other.

## Before and after a change

A change that fixes one case can break another. Save a baseline first:

<!-- guide:run -->
```console
$ awa eval blueprint.yaml evals/cases.yaml --json baseline.json
Evaluation: invoice_desk
========================
...
wrote baseline.json
```

Make the change (an instruction, a rule, a tool, the model), then compare:

<!-- guide:run -->
```console
$ awa eval blueprint.yaml evals/cases.yaml --baseline baseline.json --markdown report.md
Evaluation: invoice_desk
========================
...
Comparison with invoice_desk
----------------------------

Pass rate 100.0% -> 100.0% (0 regression(s), 0 improvement(s))
...
```

awa lists every case and check that got worse, and ends with exit code 1 if anything did. `--markdown` also saves
the report as a file. With `--prices prices.yaml` ([Count the cost](operate.md#count-the-cost)) the report shows what
the tests cost, and `--cost-budget AMOUNT` fails an evaluation that costs too much.

## Cases from real runs

Every finished run is a ready-made eval case. `awa runs export-cases` turns runs into cases. In the project of the
tour, the run of step 10 finished:

<!-- guide:do cd invoice-desk -->
<!-- guide:run -->
```console
$ awa runs export-cases RUN_ID --out evals/from_runs.yaml --no-judge
wrote 1 case(s) to evals/from_runs.yaml; run them with `awa eval <blueprint> evals/from_runs.yaml`
```

<!-- guide:contains evals/from_runs.yaml -->
```yaml
  scorers:
  - tool:invoice_ledger_query
  - tool:finance_approval
  decisions:
  - action: approve
    tool: finance_approval
```

The case has the run's input, the decision of the head of finance, a check for every tool the run used, and the
answer as `expected`. Tighten it before you keep it: add `contains:7,450.00` for the figure that must be right, and
remove a `tool:` the process may skip. Secrets are masked, but the file holds real input and answers, so keep it
where you keep your run records.

<!-- guide:run -->
```console
$ awa eval blueprint.yaml evals/from_runs.yaml
Evaluation: invoice_desk
========================
...
- Pass rate: 100.0% (1/1 runs)
...
```

## See the test runs in Langfuse

With [Langfuse](langfuse.md) set up, every evaluation run is a trace of its own, and `awa build` also sends its
design and tests. Open the run of a failed case and read its steps as in [Read a trace](operate.md#read-a-trace).
`metadata.blueprint` in the search bar finds the runs of one build.

## Improve the system

| What you see | What to change |
| --- | --- |
| The answer lacks something the process needs | Add it to the agent's instructions, or to the description and build again. |
| The wrong agent did the work, or none | Make each agent's description clearly different. |
| A tool was not used, or the wrong one | Check that the agent has the tool, and say when to use it. |
| A rule was applied wrongly | Put the rule in an approval condition or a rule in `tools.py`, not in the instructions. |
| An approval was not asked for | Add it under `approvals:` ([Approvals](approvals.md)). |
| The run stopped at a limit | Check whether the process needs more steps, then raise the limit. |
| The expected value is wrong | Fix the case: cases describe your process and are not always right the first time. |

### Refinement after a build

When `awa build` reaches its target, it reads the test runs of each agent for waste: a tool called twice with the
same arguments, failed tool calls, flaky cases, picked tools never used. One call of the fast model then writes at
most three lines per agent, each based on one finding. The cases run twice more. The lines stay only when results are
at least as good and input tokens per run are no higher; they appear under the agent's `refinements:` with their
evidence. With nothing to refine, the build prints `Refinement: none`.

### Texts and model settings of an agent

An agent can read its instructions from a file, give its tools its own descriptions, and set fields of its chat model
and LangChain middleware:

```yaml
agents:
- name: analyst
  model: strong
  describe:
    instructions_file: prompts/analyst.md     # instead of instructions:
    tool_docs: {ledger_query: Reads booked invoices by number.}
  refine:
    model_kwargs: {max_tokens: 4000}          # fields of the chat model, every attempt
    middleware:
    - ModelCallLimitMiddleware: {run_limit: 20}
```

| Field | What it does |
| --- | --- |
| `describe.instructions_file` | The agent's instructions, read from this file next to the blueprint. |
| `describe.tool_docs` | Per tool name, the description the model reads instead of the tool's own. |
| `refine.model_kwargs` | Fields of the chat model for every attempt. |
| `refine.anthropic`, `refine.openai`, `refine.google` | Fields of that provider's chat model class; each applies only when the attempt uses that provider, so a fallback to another provider never carries them. |
| `refine.middleware` | LangChain agent middleware by class name, with its arguments. |

Every key is checked against the LangChain class when the blueprint loads, so a typo fails before the first run. For
code, put hooks (`@refine_model`, `@refine_agent` from `awa.refine`) in a `refine.py` next to the blueprint. They take
and return plain LangChain objects; `awa run` and `awa eval` load the file.

## Common mistakes

| Mistake | What you see | Fix |
| --- | --- | --- |
| Tests written after the design only fit what it does | Everything passes, nothing is found | Write cases from your process: the exceptions, the limits, the "must not". |
| Expected values from sample data used on real data | Cases fail after you connect a system | Change `expected` to your real values. |
| One run per case | A case passes today and fails tomorrow | Use `--repetitions 3` and watch `pass^3`. |
| A `judge:` as the only check | Results that vary | Add a check that does not vary. |
| `Errors: 1` or **Could not run** | The run could not finish: a key, a limit, a connection | The message names it. |
| `unknown scorer` | A check is misspelled | Compare it with the table above. |

!!! example "In code"

    `awa.evals.evaluate` (or `evaluate_sync` outside async code) is `awa eval` from Python, and `awa.testing` has
    scripted models, so a test needs no key ([Testing](../develop/testing.md)):

    <!-- guide:python -->
    ```python
    from awa import Agent
    from awa.evals import EvalCase, evaluate_sync
    from awa.testing import FakeChatModel

    greeter = Agent("greeter", instructions="Greet the user by name.", model=FakeChatModel(["Hello, Anna!"]))
    report = evaluate_sync(greeter, [EvalCase(id="anna", input="My name is Anna.")], ["contains:Anna"])
    print(report.pass_rate, report.cases[0].case_id, report.cases[0].status)
    ```

    <!-- guide:expect -->
    ```text
    1.0 anna passed
    ```
