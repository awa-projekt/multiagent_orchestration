# awa examples

**For** developers learning awa by running code. **You need** awa installed
(`pip install -e ".[anthropic]"`) and, to run live, a model key; every
example also runs offline, through its tests (see [Test offline](#test-offline)).

Each script has `build_*()` functions that construct the system and a `main()`
that runs it against a real model. Read them in order: each one adds one part
of awa to what the ones before showed.

## Learning path

| # | Script | What it shows | Extras to run live |
| --- | --- | --- | --- |
| 01 | [`01_first_agent.py`](01_first_agent.py) | An `Agent` with a function as tool and a typed `output_type`, run once and streamed as events; a team from names and tasks with `awa.quick.team` and `quick.explain` | `anthropic` |
| 02 | [`02_skills.py`](02_skills.py) | Builtin and custom `SKILL.md` skills, inline and loaded on demand (the custom skill is in [`skills/`](skills/)) | `anthropic` |
| 03 | [`03_team.py`](03_team.py) | An orchestrator delegating to members in parallel, bounded by `TeamLimits`, with usage per agent; a triage agent handing the conversation to a member, threads kept by a checkpointer | `anthropic` |
| 04 | [`04_workflows.py`](04_workflows.py) | `Sequential`, `Parallel` and `Loop` composed into one workflow and used as a team member | `anthropic` |
| 05 | [`05_approvals_and_limits.py`](05_approvals_and_limits.py) | A tool call that waits for a person (pause, inspect, resume with a decision); input and output guardrails, `UsageLimits`, `max_steps` and hardened tools | `anthropic` |
| 06 | [`06_memory.py`](06_memory.py) | Memory per customer with `AgentMemory` (`remember`, `recall`, `forget`, `auto_recall`) and `Compaction` for long threads | `anthropic` |
| 07 | [`07_evaluate.py`](07_evaluate.py) | The support blueprint ([`blueprints/support_triage.yaml`](blueprints/support_triage.yaml)) evaluated on [`evals/support_triage_cases.yaml`](evals/support_triage_cases.yaml) with trace scorers and a model judge, compared with a baseline as a CI gate | `anthropic` |
| 08 | [`08_tracing.py`](08_tracing.py) | Hooks, `EventLogger`, `EventRecorder`, directory presence and OpenTelemetry spans | `anthropic,otel` and `opentelemetry-sdk` |
| 09 | [`09_mcp_tools.py`](09_mcp_tools.py) | A system behind an MCP server ([`mcp_crm_server.py`](mcp_crm_server.py)) used with `MCPToolset`, and the same server in a blueprint with an approval on one tool | `anthropic,mcp` |
| 10 | [`10_serve_api.py`](10_serve_api.py) | A refunds desk as an HTTP API: API keys mapped to principals, customer-scoped `deps`, Server-Sent Events, approvals over HTTP, usage ceilings; the same desk mounted into an existing FastAPI app with `create_router` | `anthropic,server` |
| 11 | [`11_build_from_description.py`](11_build_from_description.py) | `awa.blueprints.build_project`: acceptance tests and sample data first, design, evaluation and repair on a local workspace, a project folder with connection steps | `anthropic` |

Task boards, agent networks and peer messages are shown on the
[Teams](../docs/develop/teams.md) page and in the core benchmark; web search,
the REST, OpenAPI, GraphQL and OData connectors and the CSV, Excel, PDF and
Word tools on [Tools](../docs/develop/tools.md) and
[Connectors](../docs/develop/connectors.md). A blueprint with a `triggers:`
section runs under `awa worker` whenever a trigger fires ([Run and automate](../docs/guide/run.md)).
[`observability/`](observability/) sets up a local Langfuse and sends a scripted
team run to it without a model key ([Langfuse](../docs/guide/langfuse.md)).

## Processes to copy

| Folder | Contents |
| --- | --- |
| [`recipes/`](recipes/) | Multi-agent starting points with sample tools: teams, handoffs, networks on a task board, workflows with a loop, a shared blackboard |
| [`usecases/`](usecases/) | Company processes that end in an action, with sample data written by `make_data.py`; six of them have triggers |
| [`processes/invoice-check/`](processes/invoice-check/README.md) | A `process:` of three steps, each checked before the next starts |
| [`microsoft/`](microsoft/README.md) | Processes on Microsoft 365 mail and files, with invented test data |
| [`benchmarks/core/`](benchmarks/core/README.md) | Eight processes that measure the core with several runs per case |

[Examples](../docs/guide/examples.md) describes every recipe and use case and
how to adapt one; each folder has a `blueprint.yaml`, its `cases.yaml` and a
README. They run on their sample data with `awa run`, `awa eval` or the web
app (**Start from an example**), and offline in the tests. They are templates: adapt
the tools, rules and eval cases to your own data before you rely on one.

## Run

From the awa folder:

```bash
pip install -e ".[anthropic]"
export ANTHROPIC_API_KEY=...          # or put it in a .env file in the current folder
python examples/01_first_agent.py
```

Without a key, a script exits at once with a one-line message saying what to
set. Every agent without an explicit model uses the `default` profile
(`AWA_DEFAULT_MODEL`, default `anthropic:claude-sonnet-5-5`); any provider of
LangChain's `init_chat_model` works:

```bash
pip install -e ".[openai]"
export OPENAI_API_KEY=...
export AWA_DEFAULT_MODEL=openai:gpt-6.1-sol
python examples/03_team.py
```

To start a project of your own instead, scaffold one (`awa init my-desk
--template support`) or build one from a description (`awa build "<your
process>" --out my-desk`, as in script 11).

## Test offline

Each script has its own test module in `tests/examples/` (`test_01_first_agent.py`
to `test_11_build_from_description.py`). The tests build the system with the
script's `build_*()` functions and replace the models with deterministic fakes
(`awa.testing.FakeChatModel`, `FunctionChatModel`) through
`Agent.override(model=...)`. No key, no network:

```bash
python -m pytest tests/examples -q
```

The same technique works for your own systems; see
[Testing](../docs/develop/testing.md).
