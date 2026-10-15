# How awa works

This page is the overview in pictures: how agents work together in awa, how they talk to each
other, where the limits sit, and why the library is built the way it is. Every diagram shows what
the code does today; the linked pages have the details and the tested examples.

## The idea: a hybrid multi-agent system under control

awa combines two ways of working together. A **lead** (the orchestrator of a team, or the lead of a
network) plans and delegates, and the **members** also work with each other directly: they consult
a colleague, leave a note, share state on a blackboard or claim work from a task board. Models
decide *what* to do; Python code decides *how much* they may do and *which* tool calls wait for a
person.

```mermaid
flowchart LR
    IN(["Request<br/>a person, a trigger,<br/>an HTTP call, your code"]) --> LEAD
    subgraph RUN["One run tree: shared limits, one usage total, one event stream"]
        direction TB
        LEAD(["Lead / orchestrator"])
        C["Member C"]
        A["Member A"]
        B["Member B"]
        BB[("Blackboard and task board<br/>opt-in")]
        LEAD -->|handoff| C
        LEAD -->|delegate_task| A
        LEAD -->|delegate_task| B
        A <-.->|consult_peer<br/>send_message| B
        A --- BB
        B --- BB
    end
    RUN -->|tool call<br/>from any agent| POLICY{"Tool policy"}
    POLICY -->|allowed| TOOLS["Tools and<br/>connected systems"]
    POLICY -->|gated| APPROVAL{{"Approval<br/>a person says<br/>yes, no or edit"}}
    APPROVAL -->|yes or edit| TOOLS
    RUN ---> OUT(["Result<br/>output, usage per agent,<br/>pending approvals"])
```

- **Hierarchical:** the lead delegates a self-contained brief; the member never sees the caller's
  conversation, and its result comes back to the lead.
- **Peer to peer:** members connected as peers ask each other (`consult_peer`) or send notes
  (`send_message`) without going through the lead.
- **Shared state:** a versioned blackboard (`blackboard=True`) and a task board (`task_board=True`)
  with owners, dependencies and atomic claims; both are opt-in for a team, and a network has a task
  board by default.
- **Under control:** every edge is declared; an agent gets exactly the tools its edges allow, and the
  whole run tree shares one set of limits. Any agent in the run can call a tool; a gated tool waits
  for a person.

Details: [Teams and networks](../develop/teams.md), [Agents and teams](../guide/agents-and-teams.md).

## How agents talk to each other

Every way of reaching another agent is a tool that the team gives to an agent only when an edge
allows it:

```mermaid
sequenceDiagram
    participant L as Lead
    participant A as Member A
    participant B as Member B (peer of A)
    L->>A: delegate_task(task, context, expected_output)
    activate A
    A->>B: consult_peer(question)
    B-->>A: answer
    A--)B: send_message(note)
    Note over B: the note arrives before B's next model call, no answer
    A-->>L: result, cut at max_result_chars
    deactivate A
    Note over L,A: if the team has a blackboard, the full text is stored there
    L-)B: delegate_task(..., background=True)
    B--)L: result, handed back before the lead's next model call
    L->>B: transfer_to_<agent> (handoff)
    Note over B: B takes over the conversation
```

| Edge | Declared with | Tool | What happens |
| --- | --- | --- | --- |
| Delegation | `delegates` (default: the orchestrator to every member) | `delegate_task` | The member runs on a brief and returns a result |
| Consultation | `peers` | `consult_peer` | A question to a colleague; the answer comes back |
| Note | `peers` with messaging (default) | `send_message` | Delivered into the recipient's context, no answer |
| Handoff | `handoffs` | `transfer_to_<agent>` | The target takes over the conversation |

Whatever another agent wrote (results, notes, task text, board entries) reaches a model as labeled
data, never as instructions.

## Four ways to work together

```mermaid
flowchart TD
    Q1{"Is the work small<br/>and one skill?"} -- yes --> A["One agent<br/>Agent"]
    Q1 -- no --> Q2{"Same steps<br/>every time?"}
    Q2 -- yes --> W["Workflow<br/>Sequential, Parallel, Loop"]
    Q2 -- no --> Q3{"Does it split into<br/>independent parts?"}
    Q3 -- yes --> N["Network<br/>AgentNetwork on a task board"]
    Q3 -- no --> T["Team<br/>orchestrator, members, peers, handoffs"]
```

- **Agent:** one model with tools, skills, memory and typed output.
- **Workflow:** fixed steps in code; a step can be an agent, a team or another workflow.
- **Team:** an orchestrator delegates to members; members consult each other and hand over the
  conversation.
- **Network:** a lead plans the goal as tasks on a board; teammates work concurrently and claim the
  next ready task they can do.

All four are `AgentLike` (`name`, `card`, `run`), so each one can be a team member, a workflow step,
a served system or an evaluation target.

## One run, step by step

```mermaid
sequenceDiagram
    participant U as You or a trigger
    participant S as System (team, workflow or network)
    participant AG as Agent
    participant P as Tool policy
    participant H as Person
    U->>S: run(input)
    S->>AG: one turn of the agent holding control
    AG->>AG: model call, counted against the limits
    AG->>P: tool call
    alt the tool needs approval
        P-->>S: pause, the state goes to the checkpointer
        S-->>U: result with interrupts
        H->>S: resume(thread_id, approve, reject or edit)
        S->>P: continue where the run stopped
        P->>AG: approve or edit: run the tool, reject: return the reason
    else the tool is allowed
        P->>AG: run the tool (timeout, output limit)
    end
    AG-->>S: answer or delegation
    S-->>U: RunResult: output, usage per agent, handoff path
```

A run-tree limit that is reached (model calls, tool calls, tokens, cost, time) stops the run with an
error you can handle (`UsageLimitExceededError`, `RunTimeoutError`). A team limit (delegation depth,
parallel delegations, calls per member) refuses that one delegation in code, and the model gets the
refusal as a tool result. No limit is ever a request in the prompt. Details:
[Safety](../develop/safety.md), [Approvals](../guide/approvals.md).

## The architecture in layers

awa has six layers; each may import only the layers below it (`lint-imports` checks this on every
change). The boxes name the main packages of each layer:

```mermaid
flowchart TB
    I["Interfaces<br/>cli, ui, server, automation, testing"]
    C["Compiler<br/>blueprints, evals, exporter"]
    G["Integrations<br/>connectors, data, toolkits, mcp"]
    O["Orchestration<br/>team, workflows, tasks, messaging, directory"]
    R["Runtime<br/>agents, guardrails, memory, skills, tools,<br/>persistence, observability, models"]
    F["Foundation<br/>config, core, contracts, errors"]
    UP[("LangChain create_agent<br/>and LangGraph")]
    I -->|imports| C
    C -->|imports| G
    G -->|imports| O
    O -->|imports| R
    R -->|imports| F
    R -.->|built on| UP
```

A blueprint (`blueprint.yaml`) describes a whole process as data: agents, tools, how they work
together, approvals and limits. `awa validate` checks it, `awa show` explains it, `awa eval` tests it
on eval cases, and `awa export` turns a one-agent blueprint into plain LangChain and LangGraph code
that runs without awa. Details: [Architecture](../develop/architecture.md),
[Blueprints](../develop/blueprints.md), [Export](../develop/export.md).

## The decisions behind it

| Decision | Why |
| --- | --- |
| Limits are counters in code, not words in a prompt | A model cannot talk its way past a limit on steps, tokens, cost, delegation depth, parallelism or time |
| Every edge between agents is declared | An agent can only reach the agents its edges allow; the topology is checked when the team is built |
| Content from other agents is data | A result, note or task written by an agent cannot give another agent instructions |
| One run tree | Delegations, consultations and handoffs share one usage total, one set of limits and one event stream, so a whole run is traced and stopped as one |
| Failures inside collaboration are results | A failing member returns an error result its caller can act on; only run-tree limits, cancellation and approvals cross run boundaries |
| Approvals pause, they do not block | A gated tool call saves the run in a checkpointer; the run continues after the decision, also in another process |
| Built on LangChain and LangGraph | Models, tools and graphs come from the ecosystem; a one-agent project can leave awa with `awa export` |
| Offline first | Every system runs with scripted models in tests, without a key or a network |

## Where to go next

| You want to | Read |
| --- | --- |
| Install awa | [Install](install.md) |
| Build and test a first team in ten minutes | [First automation](first-automation.md) |
| Write agents, teams and networks in Python | [Quickstart](../develop/quickstart.md), [Teams and networks](../develop/teams.md) |
| Understand the code base | [Architecture](../develop/architecture.md) |
