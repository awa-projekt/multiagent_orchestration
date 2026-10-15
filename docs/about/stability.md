# Stability

awa 1.0.0b1 is a beta. Most of it is a stable core you can build on; some parts are marked
preview and may still change. This page says which part is which, what a version number promises,
and which Python, LangChain and LangGraph versions awa supports.

## The stable core

The core is what awa is for: AI agents for business processes, with hard limits enforced in code,
approvals before gated actions, eval cases, and an exit to plain LangGraph for single agents.
Nothing in the core is removed or changed incompatibly without a
[deprecation period](#what-a-version-number-promises).

| Part | What it covers | Read |
| --- | --- | --- |
| Agents | `Agent` with models, tools, skills, memory and typed output; `awa run`, `awa chat` | [Agents](../develop/agents.md) |
| Teams | `Team`: an orchestrator with members, delegation, consults and handoffs | [Teams and networks](../develop/teams.md) |
| Workflows | Sequential, parallel, loop, map, checked and process workflows | [Workflows](../develop/workflows.md) |
| Blueprints | The `awa/v1` format, `load`, `awa validate`, `awa show` and `awa init` | [Blueprints](../develop/blueprints.md) |
| Building from a description | `design`, `design_project`, `build_project`, `run_project`, `suggest_patterns`, `try_patterns` and `PATTERNS` of `awa.blueprints` | [Blueprints](../develop/blueprints.md#designing-and-building-from-a-description) |
| Approvals | `@tool(approval=True)` and `approvals:`: a gated tool call waits for a person's decision | [Safety](../develop/safety.md#approvals) |
| Limits | Hard default limits on every run, `UsageLimits` with a money limit from your prices, guardrails | [Safety](../develop/safety.md#limits) |
| Evals | Eval cases, scorers, the judge and reports; `awa eval` with pass^k | [Testing](../develop/testing.md) |
| Export for single agents | `awa export`: a project with one agent as plain LangChain and LangGraph code | [Export](../develop/export.md) |
| Generic connectors | REST, OpenAPI, GraphQL, OData, SQL, files, web, SMTP, IMAP and webhooks; `awa connectors` | [Connectors](../develop/connectors.md) |
| MCP | The tools of MCP servers as agent tools, pinned in `mcp.lock.json`; `awa mcp lock`, `awa mcp list` | [MCP](../develop/mcp.md) |
| Persistence | Checkpointers, stores, artifacts and the run log | [Memory and persistence](../develop/memory.md) |
| Observability | Events, the audit trail, local traces, Langfuse and OpenTelemetry | [Observability](../develop/observability.md) |

## Preview parts

A preview part works and is tested, but its names, parameters and output may change in a minor
release without a deprecation period. The release notes list every such change.

| Preview part | What it is | Names and commands |
| --- | --- | --- |
| Server and automation | The HTTP run API, jobs, MCP serving, triggers, the worker and stored process runs | `awa.server`, `awa.automation`; `awa serve`, `awa worker`, `awa trigger`, `awa runs`, `awa store`, `awa mcp serve`, `awa add trigger` |
| Web app and project commands | The local web app, and the commands that build, connect and brief a project | `awa.ui`; `awa ui`, `awa build`, `awa connect`, `awa brief` |
| Microsoft 365 and other SaaS connectors | Outlook, SharePoint, Teams, Microsoft Graph, Slack and Jira, tested against mock transports | the connectors `outlook`, `sharepoint`, `teams`, `microsoft_graph`, `slack`, `jira`; `awa.checks`, `awa check` |
| People directory | Colleagues from Entra ID or a local list, pinned to roles | `awa.people`, `awa people` |
| Agent networks and shared state | A lead and its agents on a task board; the message bus, blackboard, task board and agent directory | `AgentNetwork`, `NetworkLimits` (`awa.team.network`); `awa.messaging`, `awa.tasks`, `awa.directory` |
| Data profiles | What awa reads from your files before it designs anything | `awa.data`, `awa data` |
| Importers | LangChain tools, LangGraph graphs and prompts brought into a blueprint | `awa add tool`, `awa add graph`, `awa add skills` |
| Extension building blocks | The small rules, the door for extension authors and the backend contract suites | `awa.kernel`, `awa.ext`, `awa.testing.contracts` |

A `Team` uses the message bus, the task board and the agent directory inside; the team's behavior is
stable, only the names of those packages may still change.

You can recognize a preview part in four places: `__stability__ = "preview"` on the module,
`[preview]` at the start of the command's help, the list in `awa doctor`, and a note on its docs
page. When a part becomes stable, the label goes and its names join the public API. The
[Roadmap](roadmap.md#stable-core) plans to move the preview parts into add-on distributions next to
a small core after the beta.

## What a version number promises

awa follows [Semantic Versioning](https://semver.org/).

| Release | May contain | Upgrading |
| --- | --- | --- |
| Patch (`1.0.1`) | Bug and security fixes only | Safe; read **Fixed** in the release notes |
| Minor (`1.1.0`) | New features, new optional parameters, new deprecations, a new Python version | Code that uses the stable core and runs without deprecation warnings keeps running |
| Major (`2.0.0`) | Removal of deprecated features and other incompatible changes | Read the release notes first |

Pre-releases such as `1.0.0b1` and `1.0.0rc1` make no compatibility promise.

**What the promise covers:** the names in `__all__` of `awa` and its documented packages, their
documented behavior and exceptions (with the stable error codes), the blueprint format, the stored
data files, the HTTP API, the `AWA_*` variables, and the `awa` commands with their options and exit
codes. **What it does not cover:** names that start with `_`, anything undocumented, and the wording
of prompts, log lines and error messages (match on exception classes and error codes, not on text).
New optional fields, event types and enum members may appear in any minor release, so write
consumers that ignore what they do not know.

**Deprecation.** Before a feature is removed, it keeps working for at least one minor release and
emits a `DeprecationWarning` that names the replacement; the removal comes only in the next major
release. Run your tests with `python -W error::DeprecationWarning -m pytest` before you upgrade.

**Stored files.** A blueprint, checkpoint database or data store from an older awa is upgraded when
it is opened. A file written by a newer awa is never read or changed; awa raises an error that names
the file and what to do.

## Python, LangChain and LangGraph

awa 1.x supports CPython 3.11, 3.12 and 3.13 (CI also runs 3.14). Support for a Python version ends
only in a minor release, announced one release ahead.

| Package | Supported | Why the lower bound |
| --- | --- | --- |
| `langchain` | `>=1.4.3,<2` | 1.4.3 sanitizes cache settings per fallback model, which awa's fallback path relies on |
| `langchain-core` | `>=1.6.7,<2` | the provider integrations awa supports need it |
| `langgraph` | `>=1.2.14,<2` | 1.2.13 stopped answered interrupts from showing in `get_state`, which a team resume reads |
| `langgraph-checkpoint` | `>=4.1,<5` | the checkpoint format's own major version |
| `langchain-mcp-adapters` (extra `mcp`) | `>=0.3.2,<0.4` | before 1.0, minor releases may break |
| `mcp` (extra `mcp`) | `>=1.24,<2` | the MCP SDK the adapters and the server use |

- **A new minor or patch release upstream** needs nothing from you. CI tests the newest allowed
  releases on every change and nightly; if one breaks, pin the previous version until awa fixes it.
- **A new major release upstream** is outside the range, so pip keeps the last 1.x release.
- **`UpstreamCompatibilityError`** means the installed release lacks an API awa needs: run
  `awa doctor` and install versions inside the ranges above.

awa reaches LangChain and LangGraph through one private package, `awa._internal.upstream`, and the
MCP packages through `awa._compat`, so an upstream change touches few modules. `tests/upstream`
holds one test for every upstream behavior awa relies on;
[Upgrading LangGraph](../develop/architecture.md#upgrading-langgraph) lists the steps to adopt a new
release.
