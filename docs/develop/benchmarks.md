# Benchmark

The core benchmark measures awa on eight scenarios, from company processes to automation and MCP.
Run it before and after a change: offline with scripted models, or live with your model. It needs
the `files`, `server` and `mcp` extras, and a model for live runs.

```console
python examples/benchmarks/core/run.py --offline --runs 1          # scripted models, no network
python examples/benchmarks/core/run.py --live --runs 3             # the model of the settings
python examples/benchmarks/core/run.py --live --only 2 4 --max-calls 120
```

| # | Scenario | What it measures |
| --- | --- | --- |
| 1 | Market brief from the web | keyless `web_search` and `fetch_url`, delegation, a peer consult, quotes that really stand on fetched pages |
| 2 | Supplier orders against a REST API | the `rest` connector with a public API, a CSV in Windows-1252, exact totals, approvals decided in the case |
| 3 | GraphQL lookup joined with JSON | the `graphql` connector with a public API, a JSON file, exact ids and sums |
| 4 | Document intake | PDF, Word and Excel, a scan without a text layer, validation rules, a conditional approval approved and rejected |
| 5 | Agent network | a task board with dependent tasks, the blackboard, handbacks, a synthesis |
| 6 | Automation end to end | webhook, file drop and schedule, an approval across a restart, `awa runs decide` |
| 7 | Template to application | `awa build` from a description; the written project validated and run |
| 8 | MCP client | `awa mcp serve` as a separate process, called by the official MCP client over stdio and HTTP |

## How a case is judged

Every case runs several times (`--runs`, default 3). A run passes when every scorer passes, and the
table shows the runs passed and whether all runs of a case passed (pass^k), because a model that is
right two times in three is not reliable. Scorers are deterministic first: exact fields and numbers,
the tools called, delegations, approvals asked and decided, run states and files, and code for what a
spec cannot say (a quote stands on the fetched page, tasks finish in dependency order). A model judge
grades only what is left, such as the wording of a reason. Cases with approvals carry `decisions`, so
a process is measured from the request to its end.

`--offline` replaces the model with a script per scenario and the keyless endpoints with canned
answers; it runs anywhere and is part of the test suite (`tests/examples/test_benchmarks_core.py`).
`--live` uses `AWA_DEFAULT_MODEL` and the real keyless endpoints (search result pages, a holiday API
and a countries API). The runner writes the results per case and scorer (runs passed, model calls,
tokens, latency p50 and p95) to `results/` as JSON and a table, and stops at `--max-calls` (default
400; live runs count across invocations unless `--fresh-ledger`). The exit code is 0 when everything
passed, 1 when an offline run failed and 2 when the call cap stopped the run.

`tests/proof/core` proves the core agent system on its own with scripted models: delegation, handoff,
peer consult, approval pause and resume after a rebuilt checkpointer, every limit, tool errors,
structured output, the event trace, nested teams and cancellation. Each test asserts numbers (model
calls per agent, tool runs, events, usage) and that nothing continues after a limit.

## Add or change a scenario

A scenario is a folder of `examples/benchmarks/core` with `blueprint.yaml`, `cases.yaml`,
`README.md`, a `tools.py` only for real custom logic, and `data/` files that `data_gen.py` recreates
byte for byte. It needs an entry in `harness.SCENARIOS` and a scripted model in `offline.py`; the
offline test then runs it end to end. When a live run fails, fix the cause (a prompt, a tool
description, a default or a bug), add an offline regression test, and run the benchmark again.

| You see | Cause and fix |
| --- | --- |
| `--live` ends at once with a message about the provider package or key | The default model cannot be called: install its package, set its key, or set `AWA_DEFAULT_MODEL`. |
| `stopped: the ledger reached --max-calls` and exit code 2 | Raise `--max-calls`, or start over with `--fresh-ledger`. |
| Scenario 8 fails with an import error | Install the `mcp` extra. |
| A live case of scenarios 1 to 3 fails once | A keyless endpoint was slow or down. Run it again with `--only N --cases ID`; a failure that repeats is a finding. |
