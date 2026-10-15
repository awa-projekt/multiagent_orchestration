# Core benchmark

**For** anyone who wants evidence that awa's core works on realistic company processes, and a way to
re-measure it after a change. **You need** awa with the `files`, `server` and `mcp` extras (`pip
install ".[files,server,mcp]"` in the awa folder; `mcp` for scenario 8), and for live runs a model
(`AWA_DEFAULT_MODEL` and its key, in the environment or a `.env` file).

Eight scenarios, each a folder with a blueprint, sample data, cases and its own README. Together
they exercise the six parts of the core: agent communication, multi-agent structures, internet
search, connected systems and data formats, template to application, and automation with approvals;
the eighth shows that the blueprint of the second is usable from an MCP client.

| # | Scenario | Core parts exercised | Cases |
| --- | --- | --- | --- |
| 1 | [Market brief from the web](01_market_brief/README.md) | keyless search and fetch, team delegation, peer consult, structured output | 2 |
| 2 | [Supplier orders against a REST API](02_supplier_orders/README.md) | `rest` connector (public holidays API), German CSV, exact numbers, approvals decided in the case | 3 |
| 3 | [GraphQL lookup joined with JSON](03_graphql_lookup/README.md) | `graphql` connector (public countries API), JSON file, join | 3 |
| 4 | [Document intake](04_document_intake/README.md) | PDF, Word, Excel (dates, formulas), scan without text layer, rules as a tool, conditional approval | 5 |
| 5 | [Agent network](05_agent_network/README.md) | task board with dependencies, blackboard, handbacks, synthesis | 1 |
| 6 | [Automation end to end](06_automation/README.md) | webhook, file drop, schedule, `awa serve` in process, `awa runs decide`, restart | 2 |
| 7 | [Template to application](07_build_from_description/README.md) | `awa build` from a description, the written project validated and run | 1 |
| 8 | [MCP client](08_mcp_client/README.md) | `awa mcp serve` over stdio and HTTP, called by the official MCP client; approvals through elicitation and `resume_<name>` | 4 |

## Method

- **Several runs per case** (`--runs`, default 3): a model is not deterministic, so every case runs
  repeatedly and the table reports how many runs passed and whether **all** runs passed (pass^k).
- **Deterministic scorers first, a judge second.** Scorers check exact fields and numbers, which
  tools were called, delegations, approvals asked, granted and rejected, run states and files. Code
  scorers (`scorers.py`) check what a spec cannot: quotes occur on fetched pages, tasks finish in
  dependency order. The model judge only grades what is left, such as the wording of a reason. A run
  passes when every scorer passes.
- **Multi-step cases.** Cases with approvals carry `decisions` that answer the pauses (approve or
  reject, with a note), so a process is measured from the request to its end.
- **Real endpoints only where they are keyless and stable**: the DuckDuckGo result pages and
  Wikipedia for scenario 1, [Nager.Date](https://date.nager.at) public holidays (REST) for scenario
  2, the [Countries GraphQL API](https://countries.trevorblades.com) for scenario 3. They need no
  key; each run makes a handful of requests and nothing is cached or stored. Expected values only
  use facts that do not change (holidays and country data of past years). Nothing but the model
  needs a secret.
- **Failures are findings.** A failing run is read, the root cause is fixed (prompt, tool
  description, default or library bug) with an offline regression test, and the benchmark is run
  again.
- **A budget.** Every run reports its model calls to a running ledger; the benchmark stops at
  `--max-calls` (default 400), and a single run may use `--max-run-calls` (default 60, 150 for the build;
  scenarios 6 and 8 are not stopped at the cap but fail when they go above it). Live runs keep
  the ledger in `results/ledger.jsonl`, so the cap counts across invocations (`--fresh-ledger` starts
  over). A full live run with 3 runs per case needs roughly 350 calls (the scripted offline run
  uses 136 per round of runs and a real model needs a little more).

## Run it

```console
# Offline: scripted models and a fake network, no key, no internet (what the test suite runs)
python examples/benchmarks/core/run.py --offline --runs 1

# Live: the model of the settings (AWA_DEFAULT_MODEL), the real endpoints
python examples/benchmarks/core/run.py --live --runs 3
python examples/benchmarks/core/run.py --live --only 2 4 --runs 3 --max-calls 120
python examples/benchmarks/core/run.py --live --only 4 --cases scan-held-for-manual-entry
```

`--only` takes scenario numbers or folder names, `--cases` case ids. Each invocation writes
`results/core-<mode>-<time>.json` (per case and scorer: runs, passed runs, pass^k, scorer pass
counts, calls, tokens, latency p50 and p95, failure reasons) and `.md` (one row per scenario and per
case), and prints the table. The exit code is 0, or 1 for failed offline runs, or 2 when the call
cap stopped the benchmark.

To run one blueprint by hand, validate and run it as any blueprint, for example `awa run
examples/benchmarks/core/02_supplier_orders/blueprint.yaml "Check order B-1006."`. The sample files
are recreated with `python examples/benchmarks/core/data_gen.py` (`--check` compares them with a
fresh generation).

## Layout

| File | Purpose |
| --- | --- |
| `run.py` | the runner: arguments, ledger, results files, table |
| `harness.py` | scenarios, the four drivers (evaluation, served automation, build, MCP client), statistics |
| `mcp_client.py`, `mcp_launcher.py` | scenario 8: the client session and its checks; the server process with scripted model and network (offline) |
| `scorers.py` | code scorers named in `metadata.checks` of the cases |
| `offline.py` | the fake network and the scripted model of each scenario (offline mode and tests) |
| `data_gen.py` | recreates every `data/` folder byte for byte |
| `NN_name/` | `blueprint.yaml`, `cases.yaml`, `README.md`, `tools.py` where real logic is needed, `data/` |

## Measured results

Measured live on 2026-10-04 and 2026-10-05 with one Azure OpenAI deployment (one model; its name is
not recorded), against the real keyless endpoints of scenarios 1 to 3 (see Method). Three runs per
case, except scenarios 7 and 8, which ran once. Runs passed is the number of runs in which every
scorer passed; cases with pass^k is the number of cases whose runs all passed. To measure again, run
the live commands under "Run it" with your own model; the numbers differ a little from run to run.
Results from one model on two days: evidence, not a reliability guarantee.

| Scenario | Runs per case | Runs passed | Cases with pass^k | Model calls | Tokens | Median latency (s) |
| --- | --- | --- | --- | --- | --- | --- |
| 1. Market brief from the web | 3 | 6 of 6 | 2 of 2 | 110 | 491,764 | 43.0 and 54.8 |
| 2. Supplier orders against a REST API | 3 | 9 of 9 | 3 of 3 | 45 | 163,964 | 11.6 |
| 3. GraphQL lookup joined with JSON | 3 | 9 of 9 | 3 of 3 | 30 | 33,775 | 3.8 |
| 4. Document intake | 3 | 15 of 15 | 5 of 5 | 66 | 109,079 | 5.1 |
| 5. Agent network | 3 | 3 of 3 | 1 of 1 | 47 | 128,540 | 16.3 |
| 6. Automation end to end | 3 | 6 of 6 | 2 of 2 | 48 | 15,483 | 18.4 |
| 7. Template to application | 1 | 1 of 1 | not measured (one run) | 70 | 267,820 | 162 |
| 8. MCP client | 1 (two of the four cases: HTTP with `resume_<name>`, stdio with elicitation) | 2 of 2 | not measured (one run) | 10 | 26,310 | 12.8 |

The per case and per scorer table of each run is in its `results/*.md`. Scenarios 7 and 8 are one
run each, which do not measure a rate. The served flow of scenario 6 (webhook, wrong token refused,
pause at the approval, `awa runs decide`, restart, resume, file drop, schedule through `awa trigger
run`) worked in every run.
