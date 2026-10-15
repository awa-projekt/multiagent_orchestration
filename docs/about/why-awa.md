# Why awa

awa gets a business process running with a few AI agents that are tested before they run and
controlled while they run. Use it when a process needs judgment and people approve its important
actions. Use something else when one chat agent, plain automation without AI, or your own
LangGraph code already does the job.

## What you do with it

1. Describe the process and let awa draft a blueprint, one readable file. Or start from a
   template.
2. Check and adapt the draft: agents, tools, approvals and limits.
3. Test it on eval cases before you rely on it.
4. Run it in a chat, from a trigger, or over an HTTP API. Gated actions wait for a person, and
   every run leaves a trace.

[First automation](../start/first-automation.md) walks through these steps in about ten minutes.

## What awa adds

awa is a layer for business processes on top of LangChain and LangGraph. It does not replace
them: the agent loop, middleware, checkpoints and interrupts are theirs.

| You get | Where |
| --- | --- |
| A process as one readable blueprint file, drafted from a description and your files | [Blueprints](../develop/blueprints.md), the web app |
| Agents, teams, networks and workflows with limits enforced by code | [Teams and networks](../develop/teams.md), [Safety](../develop/safety.md#limits) |
| Eval cases that you run before you rely on a process, measured over repeated runs | [Testing](../develop/testing.md) |
| Approvals before the actions you gate (connector writes and the built-in tools that write or send are gated by default), and a stop for every run | [Safety](../develop/safety.md#approvals) |
| Connectors, files and MCP servers, with credentials kept out of the model's reach | [Connectors](../develop/connectors.md), [Data](../develop/data.md), [MCP](../develop/mcp.md) |
| Triggers, a worker, an HTTP API and traces of every run | [Serving](../develop/serving.md), [Observability](../develop/observability.md) |
| An exit to plain LangGraph code for a single agent | [Export](../develop/export.md) |

## When something else fits better

| Your situation | Better fit |
| --- | --- |
| One chat agent on one model provider, no process around it | the provider's own agent toolkit, or LangChain `create_agent` directly |
| Fixed steps without judgment (copy a field, send a mail on a schedule) | a workflow automation tool without AI |
| A team that masters LangGraph and needs no process layer, tests or approvals | LangGraph itself |
| Steps that read documents, decide and handle exceptions, with people approving actions | awa |

## Status

awa 1.0.0b1 is a beta: a stable core, and preview parts that work and are tested but whose API
may still change ([Stability](stability.md)). What is planned next is on the
[Roadmap](roadmap.md).
