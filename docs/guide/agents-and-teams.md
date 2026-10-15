# Agents and teams

Each step of a process is done by an [**agent**](../reference/glossary.md#agent): an AI model with one job, its
tools and its rules. Read this page when your process needs more than one agent and you must decide how they work
together: as a [workflow](../reference/glossary.md#workflow), a [team](../reference/glossary.md#team) or a
[network](../reference/glossary.md#network). Code enforces the limits of every structure; a model only decides
within them.

The shortest form is two agents in a fixed order, written in `blueprint.yaml`:

```yaml
agents:
  - name: drafter
    description: Drafts the reply.
  - name: editor
    description: Improves the draft until it is approved.
workflow:
  sequential: [drafter, editor]
```

More than one agent helps when the jobs differ:

- An agent with one job and short instructions makes fewer mistakes.
- A checker can be allowed to read, while only the clerk may change something.
- Two reviewers can work at the same time.
- Every delegation and handoff shows in the trace.

Every agent makes its own model calls, so use as few agents as the process needs.

## Choose the structure

| Structure | How it works | Use it when |
| --- | --- | --- |
| **One agent** | A single agent does the whole job. | The job is small and needs one skill. |
| **Workflow** | Fixed steps in a fixed order; steps can run side by side or repeat. | The steps are always the same. The invoice desk of the [tour](tour.md) is a workflow. |
| **Team** | An **orchestrator** receives the request, hands parts to **members** and writes the answer. | The orchestrator must decide what happens next, and members help. |
| **Network** | A lead puts tasks on a shared **task board**; teammates take the tasks they can do, at the same time. | The work splits into parts that do not depend on each other. |

When in doubt, start with a workflow or a team. They are the easiest to read and to test.

In the web app, the page **Describe** helps you choose: **Suggest patterns** proposes up to three structures for
your description, and **Test the proposals** runs the same eval cases through each
([The web app](web-app.md#describe)).

## Workflow: steps in order

A workflow runs its steps in a fixed order. A step is one of three things:

- one agent;
- several agents at the same time (`parallel`);
- one agent that repeats until a condition holds (`loop`).

<!-- guide:do cd . -->
<!-- guide:file review-desk/blueprint.yaml -->
```yaml
name: review-desk
description: Drafts a reply, has two reviewers check it at the same time and polishes it.
agents:
  - name: drafter
    description: Drafts the reply.
  - name: legal_reviewer
    description: Checks the draft for legal problems.
  - name: compliance_reviewer
    description: Checks the draft against company rules.
  - name: editor
    description: Improves the draft until it is approved.
workflow:
  sequential:
    - drafter
    - parallel: [legal_reviewer, compliance_reviewer]
    - loop: editor
      until_contains: APPROVED
      max_iterations: 3
```

The drafter goes first. Both reviewers then read the draft at the same time. The editor repeats until its answer
contains `APPROVED`, at most three times. An agent without `instructions` follows its description.

Check the file with `awa validate`. `awa show --mermaid` draws the workflow as a diagram that many wikis and editors
can display:

<!-- guide:run -->
```console
$ awa validate review-desk/blueprint.yaml
review-desk: valid (tool names not checked; no tools.py beside it)
$ awa show review-desk/blueprint.yaml --mermaid
flowchart LR
    input([input])
    output([output])
    n1["drafter"]
    n2["legal_reviewer"]
    n3["compliance_reviewer"]
    n1 --> n2
    n1 --> n3
    n4["editor"]
    n4 -.->|until 'APPROVED', max 3| n4
    n2 --> n4
    n3 --> n4
    input --> n1
    n4 --> output
```

Less common steps:

- A `map:` step runs one agent over every item of a list ([Workflows](../develop/workflows.md#a-list-of-items)).
- A judge decides whether a worker's draft may go on
  ([Roles in a blueprint](../develop/teams.md#roles-in-a-blueprint)).
- Output checks send an answer that breaks a rule back to the model
  ([Agents](../develop/agents.md#structured-output-and-output-checks)).

## Team: an orchestrator and members

In a team, one agent leads: the **orchestrator**. It hands a task to a member and reads the result; this is a
**delegation**. A member can ask a **peer** a question directly. An agent can also pass the whole conversation on
(**handoff**), so the next agent answers the person directly.

The example `01_market_brief` in the awa folder is a team of four: an orchestrator, a researcher who reads web pages, an
analyst who checks the findings, and a writer. Copy `examples/benchmarks/core/01_market_brief` to your work folder as
`market-brief`, then look at it:

<!-- guide:do copy awa:examples/benchmarks/core/01_market_brief market-brief -->
<!-- guide:do cd market-brief -->
<!-- guide:run model=bench:01_market_brief -->
```console
$ awa show blueprint.yaml
market-brief 1.0.0
Researches a short list of products on the web and delivers a sourced brief.
entrypoint: team

agent       model    tools                  skills  description
brief_lead  default  -                      -       Runs the brief from the question to the structured, sourc...
researcher  default  web_search, fetch_url  -       Finds public pages about the requested items and reads them.
analyst     default  -                      -       Checks research findings for gaps and contradictions.
writer      default  -                      -       Writes the executive summary of a brief.

team market-brief: orchestrator brief_lead
  delegates: brief_lead -> researcher, analyst, writer
  peers: analyst -> researcher
  shared: none
```

Run it with `--stream` to see every step. `=>` marks a delegation or a question to a peer. The indentation shows who
asked whom. The output below comes from a scripted model and a stand-in for the internet, and its lines are cut short:

<!-- guide:run model=bench:01_market_brief -->
```console
$ awa run blueprint.yaml "Brief on Debian, Ubuntu and Fedora: year of the first release." --stream
> market-brief started
  > brief_lead started
    brief_lead -> delegate_task({"agent": "researcher", "task": "Research: Debian, Ubuntu, Fedora."})
    brief_lead => researcher (delegate): Research: Debian, Ubuntu, Fedora.
    > researcher started
      researcher -> web_search({"query": "Debian, Ubuntu, Fedora, first release"})
...
    < researcher finished (310 ms)
...
    brief_lead => analyst (delegate): Check: ...
    > analyst started
      analyst -> consult_peer({"peer": "researcher", "question": "Confirm the year for Fedora."})
      analyst => researcher (consult): Confirm the year for Fedora.
...
    < analyst finished (150 ms)
...
    brief_lead => writer (delegate): Findings: ...
...
< market-brief finished (900 ms)
{
  "topic": "Debian, Ubuntu, Fedora",
  "executive_summary": "Debian first appeared in 1993. Ubuntu first appeared in 2004. Fedora first appeared in 2003.",
...
```

Limits keep a team in bounds. You set them under `team: limits:`:

<!-- guide:contains blueprint.yaml -->
```yaml
team:
  orchestrator: brief_lead
  members: [researcher, analyst, writer]
  peers: {analyst: [researcher]}
  messaging: false
  limits:
    max_delegation_depth: 2
    max_calls_per_member: 3
    delegation_timeout: 180
```

Here a member may be called at most three times, and delegation goes at most two levels deep. A member that takes
longer than 180 seconds is given up. When a limit refuses a delegation, the agent is told, so it can adapt.

A limit you do not set keeps its default:

| Team limit | Default |
| --- | --- |
| `max_calls_per_member` | 20 |
| `max_delegation_depth` | 4 |
| `max_parallel_delegations` | 8 |
| `delegation_timeout` (seconds) | 300 |
| `max_handoffs` | 5 |

## Network: teammates on a task board

In a network, a lead writes tasks on a shared **task board**. Each teammate takes the tasks that fit its
`capabilities`, at the same time as the others.

The example `05_agent_network` writes a supplier risk memo. Two analysts work in parallel, and a writer waits for
both:

<!-- guide:do cd . -->
<!-- guide:do copy awa:examples/benchmarks/core/05_agent_network supplier-network -->
<!-- guide:do cd supplier-network -->
<!-- guide:run model=bench:05_agent_network -->
```console
$ awa show blueprint.yaml --mermaid
flowchart TD
    a0(["risk_lead"])
    a1["spend_analyst"]
    a2["logistics_analyst"]
    a3["risk_writer"]
    a0 -->|tasks| a1
    a0 -->|tasks| a2
    a0 -->|tasks| a3
    a1 <-.-> a2
    a1 <-.-> a3
    a2 <-.-> a3
$ awa run blueprint.yaml "Supplier risk memo for Q3."
Highest spend: Nordwind GmbH with 48230.50 EUR. Slowest delivery: Alpha Logistik with 6.33 days late on average. Risk: a key supplier and a late one need different levers. Next step: review delivery terms of the slowest supplier.

[risk_lead] 13 model call(s), 12339 tokens (12100 in, 239 out), 1400 ms
```

The solid arrows are the tasks the lead hands out. The dashed lines show that teammates can talk to each other.

- The order in which teammates finish varies from run to run; the answer does not.
- A teammate without `capabilities` only takes tasks given to it by name. `awa validate` warns about it.

## How agents talk to each other

| What | Meaning | In the stream |
| --- | --- | --- |
| Delegation | The orchestrator gives a member a task and waits for the result. | `orchestrator => member (delegate): ...` |
| Consult | A member asks a peer a question and waits. | `analyst => researcher (consult): ...` |
| Handoff | An agent passes the whole conversation on. | `triage >> refunds: ...` in `awa chat` |
| Blackboard | Agents write to and read from shared, versioned entries. | `write_shared`, `read_shared` |
| Task board | Tasks with owners and dependencies. | `create_tasks`, `... completed` |

Everything one agent tells another reaches it as information, never as an order. Instructions come only from the
blueprint. [How awa works](../start/how-awa-works.md#how-agents-talk-to-each-other) shows the tools behind each way.

## Write good instructions

- **One job.** The `description` says in one sentence what the agent is for. The orchestrator reads it to decide whom
  to ask.
- **Steps and rules.** `instructions` list the steps and the rules in plain words, with numbers: "never guess a
  missing value", "at most three searches".
- **Name the delegations.** Tell the orchestrator which member gets which part, in which order.
- **Calculated rules are code.** Thresholds go into an approval condition or a rule in `tools.py`
  ([Describe a process](describe.md#many-conditions-and-special-cases)).

## Common mistakes

| Mistake | What you see | Fix |
| --- | --- | --- |
| Too many agents for a small job | A slow, costly run that gains nothing | Merge agents with the same skill. |
| Two agents with the same task | The orchestrator picks one by chance | Give each a different, specific description. |
| A step that must happen in a fixed order is left to a team | The orchestrator sometimes skips it | Use a workflow. |
| `delegation refused` or `max_calls_per_member` | A limit stopped the run | Raise the limit if the process needs it; otherwise sharpen the orchestrator's instructions. |
| `awa validate` reports an unknown agent | A name in `workflow`, `team` or `network` differs from the agent's `name` | Use the same spelling. |

!!! example "In code"

    The same structures are Python objects: `Agent`, `Sequential`, `Parallel`, `Loop`, `Team` and `AgentNetwork`
    ([Teams](../develop/teams.md), [Workflows](../develop/workflows.md)). This workflow runs offline with scripted
    models:

    <!-- guide:python -->
    ```python
    from awa import Agent, Sequential
    from awa.testing import FakeChatModel

    drafter = Agent("drafter", instructions="Draft a short reply.", model=FakeChatModel(["Thank you, your order ships today."]))
    editor = Agent("editor", instructions="Polish the draft.", model=FakeChatModel(["Thank you! Your order ships today."]))
    reply = Sequential("reply", [drafter, editor])
    print(reply.run_sync("Customer asks when order 1001 ships.").output)
    ```

    <!-- guide:expect -->
    ```text
    Thank you! Your order ships today.
    ```
