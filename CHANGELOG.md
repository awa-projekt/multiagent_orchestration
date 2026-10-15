# Changelog

All notable changes to awa are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow the rules on
[Stability](docs/about/stability.md#what-a-version-number-promises).

## 1.0.0b1 - First public beta

### Added

- Agents (`Agent`, `tool`) on LangChain `create_agent` and LangGraph, with typed output,
  streaming, skills, memory and compaction.
- Multi-agent forms: teams with an orchestrator, delegation and handoffs; workflows
  (`Sequential`, `Parallel`, `Loop`); networks of agents on a shared task board with peer messages.
- Limits enforced by code for every run tree: model calls, tool calls, tokens, wall time, nesting
  depth and fan-out; an optional money limit with `UsageLimits(max_cost=..., prices=...)`.
- Approvals: gated tool calls pause the run, keep its state in a checkpointer and continue after a
  person's decision (approve, reject or edit).
- Blueprints: a whole process as `blueprint.yaml`, checked with `awa validate` and explained with
  `awa show`; `awa init` templates; `awa build` drafts a blueprint with eval cases from a
  description and sample files.
- Evaluation: eval cases, scorers, model judges, `awa eval` with pass rate and pass^k,
  `awa runs export-cases`.
- `awa export`: a one-agent blueprint as a standalone LangChain and LangGraph project.
- Connectors and toolkits: REST, OpenAPI, GraphQL, OData, SQL, files (CSV, Excel, PDF, Word,
  scanned pages), web pages, SMTP, IMAP and webhooks; preview connectors for Outlook, Teams,
  SharePoint, Microsoft Graph, Slack and Jira; MCP tools and `awa mcp serve`.
- Running a process (preview): `awa serve` (HTTP API with API keys, server-sent events and
  approvals over HTTP), `awa worker` with schedules, webhooks, file drops, mailboxes and queues,
  and the local web app `awa ui`.
- Tracing with Langfuse and OpenTelemetry; `awa doctor` checks an installation.
- Offline testing with scripted models (`awa.testing.FakeChatModel`, `FunctionChatModel`) and
  contract suites.
- Reference deployment with Docker Compose (`deploy/`): API, worker, PostgreSQL, a TLS edge and a
  self-hosted Langfuse.
