# Glossary

The words used in awa, one sentence each. The docs, the web app, the command line and the code
use one word per concept; the name in `code` is the Python or blueprint name.

## Building blocks

Agent { #agent }
:   One model with instructions, tools and limits that works in a loop (think, call a tool, read
    the result) until it can answer. `awa.Agent`

Instructions, description { #instructions }
:   The *instructions* tell an agent how to do its job; the *description* is one line that says
    what it does, so other agents and people know when to use it.

Structured output { #structured-output }
:   An answer in a fixed format, such as the fields of an invoice, checked before the run
    returns. `Agent(output_type=...)`

Output validator { #output-validator }
:   A function that checks an agent's answer; when it raises `ModelRetry`, the reason goes back
    to the model, which answers again in the same run. `Agent(output_validators=[...])`

Model, model profile { #model }
:   The language model an agent works with, written `provider:model`; a *model profile*
    (`default`, `fast`, `strong`) is a short name for one, set in [Settings](settings.md).

Tool { #tool }
:   A function an agent may call to look something up or to act, such as reading an order or
    sending an email. `@awa.tool`

Toolkit { #toolkit }
:   A tested set of built-in tools for one kind of data or service, named `builtin:<tool>` in a
    blueprint ([Tools](tools.md)). `awa.toolkits`

Toolbox { #toolbox }
:   A way to package your own tools: a class whose settings (address, credentials) configure all
    its tools at once. `awa.tools.Toolbox`

Connector { #connector }
:   The part that gives agents the tools of one external system (a database, a mailbox, a
    REST service), configured with an address and credentials. `awa.connectors`

Connection { #connection }
:   The configured and tested settings of one connector in a project.

External system { #external-system }
:   A database, API or service outside awa that a connector reaches.

MCP { #mcp }
:   The Model Context Protocol: awa agents use the tools of MCP servers (`mcp:<server>` in a
    blueprint), and `awa mcp serve` offers a system to MCP clients. `awa.mcp`

Skill { #skill }
:   A folder with a `SKILL.md` file of instructions for one kind of work; an agent gets them in
    its prompt or, with `skill_mode: on_demand`, loads them when it needs them
    ([Skills](skills.md)). `awa.Skill`

Preview { #preview }
:   A command, option or module that works and is tested but whose names may still change in a
    minor release; its help starts with `[preview]` ([Stability](../about/stability.md)).

## Working together

Team { #team }
:   Agents that handle one request together: an *orchestrator* receives it, hands parts to its
    *members* and writes the answer. `awa.Team`

Role { #role }
:   The job an agent has in a blueprint: `orchestrator` (plans and delegates), `worker`, `judge`
    or `reviewer`; it is not another word for agent. Blueprint key `role`

Judge, reviewer { #judge }
:   A *judge* decides whether a worker's draft may be delivered (accept, revise or escalate to a
    person) and never rewrites it; a *reviewer* only reports what it finds. `role: judge`,
    `role: reviewer`

Delegation { #delegation }
:   The orchestrator gives a member a self-contained task and gets the result back; several
    delegations can run at the same time. Tool `delegate_task`

Handback { #handback }
:   The structured result a delegated agent returns to its caller; a background delegation hands
    it back before the caller's next step.

Peer consult { #peer-consult }
:   An agent asks another agent of its team a question and waits for the answer, without going
    through the orchestrator. Tool `consult_peer`

Handoff { #handoff }
:   An agent passes the whole conversation to another agent, which then answers the user
    directly.

Network { #network }
:   Agents that work at the same time and hand work to each other: a lead plans tasks on a task
    board and each agent takes the tasks it can do. `awa.AgentNetwork`

Workflow { #workflow }
:   A fixed order of steps decided by code, not by a model: one after another, at the same time,
    or repeated until a condition holds. `awa.Sequential`, `awa.Parallel`, `awa.Loop`

Map step { #map-step }
:   A workflow step that runs one agent over every item of a list, in parts, and checks that
    every item gets exactly one result. Blueprint step `map:`, `awa.workflows.Map`

Step { #step }
:   One unit of a workflow or of a build, or one recorded unit of a run.

Pattern { #pattern }
:   One of the seven ways agents work together that awa builds and suggests: one agent,
    pipeline, review loop, fan-out, coordinator team, handoff desk, parallel network.
    `awa.blueprints.PATTERNS`, `awa.blueprints.suggest_patterns`

Task board, blackboard { #task-board }
:   Shared, typed boards that agents read and write as data: the *task board* holds tasks with
    their dependencies and owners, the *blackboard* holds named, versioned entries.
    `awa.TaskBoard`, `awa.Blackboard`

Message bus { #message-bus }
:   How agents send each other messages; each agent has a mailbox, and every message reaches its
    context as data, never as an order. `awa.messaging`

Agent directory, agent card { #agent-directory }
:   The directory lists every agent with its *card* (name, description, capabilities) and whether
    it is busy. `awa.AgentDirectory`

## Describing and building

Process { #process }
:   The business task, in your own words, that a system automates; `process:` in a blueprint is
    the section of checked steps ([The process section](../develop/process-section.md)).

System { #system }
:   What a blueprint builds and runs: the agents and how they work together for one process.

Blueprint { #blueprint }
:   A YAML or JSON file that describes a system (agents, tools, structure, approvals, connected
    systems, triggers), which awa checks and builds. `awa.Blueprint`, `awa.load`

Project { #project }
:   The folder that holds a blueprint, its `tools.py`, eval cases, `.env`, data and run records.
    `--project`

Template { #template }
:   A starter project that `awa init` copies; its tests run offline.

Example { #example }
:   A shipped sample project to learn from, opened with "Start from an example" in the web app
    or `awa ui --example`.

Build { #build }
:   `awa build` (or *Create my system* in the web app) turns a process description into a tested
    project: eval cases and sample data first, then a design, then rounds of evaluation and
    repair.

Auto-matching { #auto-matching }
:   awa gives each agent the built-in skills and tools its name and task call for, by
    word matching and without a model call; tools that act outside the system are only
    suggested.
    `awa.quick.explain`

Stand-in { #stand-in }
:   A small local copy with sample records that takes the place of an external system that is
    not connected yet, so the tests run before anything real is touched.

Data map { #data-map }
:   Where each agent's data comes from: a live system, a copy of your file, a stand-in or the
    results folder, and the tool that reaches it.

## Your files

Data profile { #data-profile }
:   What awa found in your files without a model (kind, columns and their types, number and date
    formats, scans, flags), kept in `data/PROFILE.json`. `awa.data.profile`

File role { #file-role }
:   What a file is for in the `data:` section of a blueprint: `input` (the process runs on each
    such file), `reference` (it looks things up in it), `template` (a layout the results copy)
    or `output` (results go there).

Masking { #masking }
:   Replaces names, bank numbers and email addresses with placeholders in the digest of your
    files that the design model reads; agents at run time see the files unmasked.

## Safety and control

Approval { #approval }
:   A person's decision (approve or reject) before a gated tool call runs, optionally only when
    a condition holds, such as `amount >= 5000`; the person who decides is the *approver*.
    `awa runs decide`

Interrupt { #interrupt }
:   The saved pause of a run that waits for an approval; `resume` continues exactly there with the
    decision. `RunResult.interrupts`

Confirmation { #confirmation }
:   The `--confirm` flag or typed name a command needs before a change to a security setting or a
    request address; it is not an approval.

Guardrail { #guardrail }
:   A check in code that stops or cleans an input before the model sees it, or an answer before
    it is returned. `awa.Guardrail`

Limits, usage limit { #usage-limit }
:   A hard cap, enforced by code and not by the model, on model calls, tokens, tool calls, cost
    or time of a run, set in `limits:` of a blueprint or in code. `awa.UsageLimits`,
    `awa.TeamLimits`, `awa.NetworkLimits`

Ceiling { #ceiling }
:   The limit on model calls, tool calls, tokens, time and depth that every run tree has even
    when no usage limit is set; the values are settings
    ([Run limits](settings.md#run-limits-and-loop-protection)). `AWA_RUN_MAX_*`

Special cases { #special-cases }
:   A rule table for one step that says what happens with a result (approve, ask a person,
    reject, or go to another step); the first matching row decides. awa checks the table and
    shows it (`awa show cases`). Blueprint key `special_cases`

Tool policy { #tool-policy }
:   A rule that decides for each tool call whether it runs, waits for an approval, is changed or
    is refused. Blueprint key `policies`

Risk flags { #risk }
:   The labels of a tool: does it read untrusted input, touch private data or act outside the
    system; a blueprint check flags an agent that combines all three without an approval.
    `@tool(risk=...)`

## Running

Run { #run }
:   One execution of an agent, team or system from input to result; a run started by a trigger
    is also kept as a record that survives restarts. `awa.RunResult`, `awa runs list`

Attempt { #attempt }
:   One try of a run; a retried run has several. `awa runs trace --attempt N`

Run context, deps { #run-context }
:   What every agent and tool of a run can see: its ids, limits, usage and *deps*, your own
    objects such as the current customer. `awa.RunContext`

Event { #event }
:   A typed record of something that happened in a run, such as a tool call or an approval
    request, read by streams, logs, traces and the web API. `awa.RunEvent`

Trace { #trace }
:   The recorded tree of events of one run, with the model calls, tokens, cost and time of each
    step. `awa runs trace`

Thread { #thread }
:   A chat with a system that continues over several runs, identified by a thread id.
    `thread_id`

Checkpointer { #checkpointer }
:   The storage that saves the state of a thread after every step, so it continues and
    paused runs resume, in memory, in a local file or in PostgreSQL.

Memory, compaction { #memory }
:   *Memory* is what an agent keeps across threads; *compaction* replaces the older turns of a
    long conversation with one summary. `awa.AgentMemory`, `awa.memory.Compaction`

## Quality

Eval case { #eval-case }
:   One test of a system: an input, the expected outcome, the checks and the decisions that
    answer its approvals.

Dataset { #dataset }
:   A file of eval cases, such as `evals/cases.yaml`. `awa eval BLUEPRINT DATASET`

pass@1, pass^k { #pass-k }
:   Two numbers of an eval report: *pass@1* is the share of runs that passed; with
    `--repetitions k`, *pass^k* is the share of cases that passed in all k runs, so it shows how
    reliable a process is when nobody checks each answer ([Test and improve](../guide/test.md)).

Scorer { #scorer }
:   One check of a run, scored from 0 to 1: the answer contains a text, a tool was called, work
    went to the right agent, or a model judge grades the answer. `awa.evals`

## Operating

Trigger { #trigger }
:   What starts a run without a person: a webhook, a schedule, a file in a folder, an email or a queue
    message. `awa add trigger`

Automation { #automation }
:   A blueprint with its triggers, executed by `awa serve` or `awa worker`. `awa.automation`

Worker { #worker }
:   The program that executes queued runs, retries temporary failures and watches triggers and
    deadlines. `awa worker`

Dead letter { #dead-letter }
:   A run that failed on every retry and is set aside instead of being tried again.
    `awa runs dlq`

SLA { #sla }
:   Deadlines for automated runs under `automation: sla:` in a blueprint: finish within a time,
    answer an approval within a time, and remind at an interval.

Serving { #serving }
:   Offering a system as a web API (`awa serve`) with runs, event streams, approvals, API keys
    and limits. `awa.server`

Principal, tenant { #principal }
:   Who calls a served system, and the organization it belongs to; threads and runs belong to
    their tenant.

Web app { #web-app }
:   The local browser app of `awa ui`, for the whole path from a description to a tested,
    connected system with forms instead of code; you still read and approve what it builds.

## Extending

Extension { #extension }
:   An optional package on top of the core that nothing below it needs, such as the web app,
    the connectors or background jobs; the [project map](../develop/project-map.md) marks
    each one.

Plugin { #plugin }
:   Tools, connectors, skills, models, parsers or erasure handlers that a project folder or an
    installed package adds through entry points `awa.<kind>`; `awa plugins switch` turns a
    built-in part on or off in the blueprint's `components:` section. `awa.plugins`

Backend protocol, backend { #backend }
:   The small interface awa's code needs for one technology, such as a run store or a queue,
    and the code that implements it for a real technology; your own backend replaces a shipped
    one ([Extend](../develop/extend.md#backend-protocols)). `awa.testing.contracts`

Engine { #engine }
:   What builds and runs the graph behind every agent, team and workflow, reached through one
    port; LangGraph unless `use_engine` sets another
    ([Architecture](../develop/architecture.md#the-engine-port)). `awa.runtime.Engine`
