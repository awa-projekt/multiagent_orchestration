# Observability

Observability means you can see what a run did: which agent acted, which model and tool calls it
made, what it cost, and where it stopped. Every run reports this as one stream of typed events.
`stream()`, hooks, logs, spans and traces all use these same events, so there is one vocabulary to
learn.

Read this page when you want to watch a run, send runs to Langfuse, keep a readable trace of each
run, or keep a tamper-evident audit trail.

The examples on this page share a scripted team. Save it as `desk.py`:

```python title="desk.py"
from awa import Agent, Team
from awa.testing import FakeChatModel, tool_call


def search_web(query: str) -> str:
    """Search the web."""
    return f"results for {query}"


def build_desk(hooks=()) -> Team:
    researcher = Agent(
        "researcher",
        description="Finds facts.",
        tools=[search_web],
        model=FakeChatModel([tool_call("search_web", {"query": "Q3 sales"}), "Sales grew 4 percent."]),
    )
    lead = Agent(
        "lead",
        description="Answers questions.",
        model=FakeChatModel(
            [tool_call("delegate_task", {"agent": "researcher", "task": "Find the Q3 sales"}), "Q3 sales grew 4 percent."]
        ),
    )
    return Team("desk", orchestrator=lead, members=[researcher], hooks=list(hooks))
```

## Watch a run

`stream()` yields every event while the run happens:

```python title="stream_tree.py"
import asyncio

from awa.contracts import DelegationEvent, RunCompletedEvent, ToolCallEvent
from desk import build_desk


async def main() -> None:
    async for event in build_desk().stream("Prepare the Q3 report."):
        match event:
            case DelegationEvent():
                print(f"{'  ' * event.depth}{event.agent} -> {event.target}")
            case ToolCallEvent():
                print(f"{'  ' * event.depth}{event.agent} calls {event.tool_name}")
            case RunCompletedEvent():
                print(event.result.output)


asyncio.run(main())
```

```text title="stream_tree.out"
  lead calls delegate_task
  lead -> researcher
    researcher calls search_web
Q3 sales grew 4 percent.
```

The stream also holds the events of nested runs. Every event has `run_id`, `root_run_id`, `agent`,
`depth` and `timestamp`, so you can rebuild the run tree. Closing the iterator early cancels the
run.

### Hooks

Hooks see the same events without changing how you run. `hooks=[...]` on an agent, team, network
or workflow takes `AgentHooks` subclasses or plain callables, sync or async. `EventRecorder` keeps
the events in memory:

```python title="recorder.py"
from awa.contracts import ToolCallEvent
from awa.observability import EventRecorder
from desk import build_desk

recorder = EventRecorder()  # keeps every event in memory
result = build_desk(hooks=[recorder]).run_sync("Prepare the Q3 report.")
print([event.tool_name for event in recorder.of_type(ToolCallEvent)])
print(sorted(set(recorder.types())))
print({name: usage.requests for name, usage in sorted(result.usage.by_agent.items())})
```

```text title="recorder.out"
['delegate_task', 'search_web']
['agent_finished', 'agent_started', 'delegation', 'delegation_result', 'model_response', 'tool_call', 'tool_result']
{'lead': 2, 'researcher': 2}
```

- A hook that raises is logged and skipped. A hook that blocks slows the run.
- To see every run of the process (runs of a server, a worker or a trigger), subscribe globally:
  `awa.runtime.subscribe_globally(subscriber)` until you unsubscribe, or
  `with use_global_subscriber(subscriber):` for a block. `create_app(..., subscribers=[...])` does
  it for a served app.
- `EventLogger()` writes one redacted log line per event to the `awa.events` logger.

### The events

| Event `type` | When |
| --- | --- |
| `agent_started`, `agent_finished`, `error` | A run starts, finishes or fails |
| `model_response`, `text_delta` | A model call returned; streamed text (only in `stream()`) |
| `tool_call`, `tool_result` | A tool was requested; it finished |
| `delegation`, `delegation_result`, `handoff`, `agent_message`, `message_undelivered` | Collaboration in a team or network |
| `model_retry`, `model_fallback`, `output_retry` | A model call is retried or moved on; a rejected answer goes back |
| `approval_requested`, `approval_decided` | A gated tool paused the run; a decision was applied |
| `guardrail_blocked`, `limit_exceeded`, `run_cancelled` | A guardrail blocked; a limit was hit; a run was stopped |
| `run_completed` | The last event of `stream()`, with the `RunResult` |

Events are pydantic models; `RUN_EVENT_ADAPTER.dump_json(event)` serializes any of them. Every
field is in [Events](../reference/api/core.md#awa.contracts).

`result.usage` covers the whole run tree: requests, input, output and cache tokens, and tool calls.
`by_agent` breaks it down per agent and `by_model` per model.

## Langfuse

Langfuse shows each run in a browser as one trace: the tree of the team, workflow or network with
every agent, model call and tool call, tokens and cost per call, the approval pause and its
decision, and the session and user. The pictures of a run are on [Langfuse](../guide/langfuse.md).

awa sends its OpenTelemetry spans to Langfuse's OTLP endpoint. You need no Langfuse package, only
the `langfuse` extra.

1. Start a local Langfuse (Docker with at least 4 GB of memory):

    ```console
    mkdir langfuse-local && cd langfuse-local
    curl -L https://raw.githubusercontent.com/langfuse/langfuse/v4.53.0/docker-compose.yml -o docker-compose.yml
    python <awa folder>/examples/observability/langfuse_local.py
    docker compose up -d
    ```

    `examples/observability/langfuse_local.py` replaces every placeholder password of the compose
    file by a random one, creates the first user and a project with API keys, binds every port to
    `127.0.0.1`, and writes the lines for awa to `awa-langfuse.env` without printing a secret.

2. Put those lines into the project's `.env`:

    ```text
    AWA_LANGFUSE_TRACING=true
    LANGFUSE_PUBLIC_KEY=pk-lf-...
    LANGFUSE_SECRET_KEY=sk-lf-...
    LANGFUSE_BASE_URL=http://localhost:3000
    ```

3. Check the server and the keys with `awa doctor --check-langfuse`.

Now `awa run`, `chat`, `serve`, `worker`, `trigger`, `eval`, `ui`, `mcp`, `build` and `data` send
every run and flush at the end. To try it without a model key,
`python examples/observability/trace_demo.py` sends a scripted team run: the trace `desk` with the
lead, the delegated researcher, every model call and the tool call.

In your own code it is one call:

```python
from awa.observability import connect_langfuse
from desk import build_desk

connection = connect_langfuse(metadata={"blueprint": "desk"})  # reads the LANGFUSE_* variables
build_desk(hooks=[connection.subscriber]).run_sync("Prepare the Q3 report.")
connection.shutdown()  # flushes the spans before the process ends
```

| Setting | Meaning |
| --- | --- |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | The project's keys; the secret is never logged or put in a span. |
| `LANGFUSE_BASE_URL` (else `LANGFUSE_HOST`) | The server; `https` unless it is on this computer. |
| `AWA_LANGFUSE_CAPTURE_CONTENT` | Also send prompts, answers, tool arguments and results, masked. Off by default. |
| `LANGFUSE_TRACING_ENVIRONMENT`, `LANGFUSE_RELEASE` | Environment and release labels. |
| `AWA_LANGFUSE_STRICT` | Stop a command instead of running without tracing when Langfuse is unreachable. |

### What Langfuse receives

- **Always sent:** names of agents, tools and models, ids, tokens, cost, times, and the type of an
  error.
- **Only with content capture:** prompts, answers, tool arguments and results, delegated tasks,
  approval notes and error messages, masked by `RegexRedactor.pii()`. The full prompt of a model
  call is never sent.
- **Trace metadata:** `run_id`, `thread_id`, `tenant`, `request_id`, `actor`, the automation and
  attempt of a process run, and from the commands `blueprint` and `command`. Each agent's span
  carries `awa.agent.prompt_hash`, so a changed prompt shows as a new value. A paused run and its
  resumption are two traces of one session.
- **Cost:** usage is sent with separate buckets for uncached input, output, cache reads and cache
  writes. With `prices:` in the blueprint (or `prices=` in code), awa sends the cost per call;
  without it, Langfuse prices the model names it knows. Azure OpenAI reports your deployment name:
  `scripts/langfuse_prices.py prices.yaml` registers a price per deployment.
- **Evaluation scores:** with tracing on, `awa eval` attaches `eval.passed` and one score per scorer
  to the trace of each case. `connection.score(run_id, name, value)` attaches your own.

## OpenTelemetry

awa's traces are OpenTelemetry spans. `connect_langfuse()` sends them to Langfuse through
`OpenTelemetrySubscriber`, an ordinary hook, and any OTLP backend can receive the same spans. Here
they are collected in memory to show their structure:

```python title="spans.py"
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from awa.observability import OpenTelemetrySubscriber
from desk import build_desk

spans = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(spans))

tracing = OpenTelemetrySubscriber(provider.get_tracer("awa"))
build_desk(hooks=[tracing]).run_sync("Prepare the Q3 report.")
tracing.close()

finished = sorted(spans.get_finished_spans(), key=lambda s: s.start_time)


def show(parent_id, depth: int) -> None:
    for span in finished:
        if (span.parent.span_id if span.parent else None) == parent_id:
            print("  " * depth + span.name)
            show(span.context.span_id, depth + 1)


show(None, 0)
```

```text title="spans.out"
invoke_workflow desk
  invoke_agent lead
    chat
    execute_tool delegate_task
    invoke_agent researcher
      chat
      execute_tool search_web
      chat
    chat
```

The delegated researcher is a child of the orchestrator `lead`, next to the `delegate_task` call
that started it. The `langfuse` extra installs the SDK and the OTLP exporter; the `otel` extra
installs only the API.

| Span | From | Key attributes |
| --- | --- | --- |
| `invoke_agent {agent}` | a run of an agent | `gen_ai.agent.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens` and `output_tokens` (with everything nested), `awa.run_id`, `awa.depth` |
| `invoke_workflow {name}` | a team, network or workflow | as above, with `gen_ai.workflow.name` |
| `chat {model}` | each model response | provider, model, tokens, finish reasons, tool call names |
| `execute_tool {tool}` | a tool call | `gen_ai.tool.name`, `gen_ai.tool.call.id`; status `ERROR` when the tool failed |

- The spans follow the OpenTelemetry GenAI semantic conventions of semantic-conventions v1.44.0. A
  test checks the output against 23 named rules of those conventions.
- Delegations, handoffs, messages, approvals, guardrail blocks, limits, cancellations, model
  retries, output retries and model fallbacks are span events.
- Without `capture_content=True`, no prompt, reply, tool argument or result, task or note reaches a
  span. With it, every text is redacted first and cut to `max_content_chars` second.
- `meter=` also records the GenAI client metrics for tokens and duration.

LangChain and LangGraph send their own traces to LangSmith when `LANGSMITH_TRACING=true` and a key
are in the process environment; awa adds nothing to that path.

## Run traces

A run trace keeps the event stream of one run as a redacted JSON Lines file that a person can read
a day later. `awa serve`, `awa worker` and `awa run --trace` write them, and `awa runs trace <run>`
shows the tree:

```python title="trace.py"
from awa.observability import TraceStore, read_trace
from awa.observability.trace_view import build_tree
from awa.runtime import use_global_subscriber
from desk import build_desk

store = TraceStore("traces")
recorder = store.recorder("run_42", actor="u-42")
with use_global_subscriber(recorder):
    build_desk().run_sync("Prepare the Q3 report.")
recorder.close("ok")

tree = build_tree(read_trace(store.path("run_42")))
totals = tree.totals
print(totals["model_calls"], "model calls,", totals["tool_calls"], "tool calls,", totals["delegations"], "delegation")
```

```text title="trace.out"
4 model calls, 2 tool calls, 1 delegation
```

| Level (`--trace-level`, `AWA_TRACE_LEVEL`) | What the file holds |
| --- | --- |
| `off` | Nothing. |
| `summary` (default) | The structure: agents, model calls with tokens, tool calls with a 200-character preview of their arguments, delegations, handoffs, approvals, limits and errors. No message bodies. |
| `full` | Also the bodies (input, replies, tasks, tool results), each cut to 4000 characters. |

- Secrets are never stored: every text is redacted before it is cut, and the values of credentials
  in the environment are removed wherever they appear.
- A trace is at most 5 MB. A background thread writes it, so the run is not slowed down.
- The automation's store keeps 30 days and 2000 files.
- `awa runs export-cases` turns finished runs into eval cases.
- Traces are personal data: retention and erasure are on [Safety](safety.md#data-protection).

## Audit trail

`AuditLog` writes an append-only, hash-chained trail of what runs decided and did:

```python title="audit.py"
from awa.observability import AuditLog, InMemoryAuditSink, verify_chain
from desk import build_desk

audit = AuditLog(InMemoryAuditSink())
build_desk(hooks=[audit]).run_sync("Prepare the Q3 report.")
records = list(audit.sink.read())
print(sorted({record.event_type for record in records}))
print(verify_chain(records).valid)
```

```text title="audit.out"
['agent_finished', 'agent_started', 'delegation', 'delegation_result', 'tool_call']
True
```

- Each record stores the hash of the previous one, so `verify_chain` finds an edited, removed or
  reordered record. Keep `audit.head` somewhere else to detect the removal of the newest records.
  The chain is evidence for an honest operator, not protection against one who can rewrite the
  file.
- By default it records runs, delegations, handoffs, approvals, guardrail blocks, limits and tool
  calls. Free text is replaced by its length unless `include_content=True`, and every string is
  redacted.
- Each record names `actor`, `tenant` and `request_id` from the run metadata; the server sets them.
- `JsonlAuditSink(path, max_bytes=...)` appends one line per record, forces it to disk and rotates
  files. Implement `AuditLogSink` for a database or write-once storage.
- `audit.record("invoice_released", {...})` appends your own records.

## What can go wrong

| You see | Why | Fix |
| --- | --- | --- |
| `EventLogger` prints nothing | The `awa.events` logger is below your log level | `logging.basicConfig(level=logging.INFO)` |
| `Langfuse tracing is off for this command: ... needs a public and a secret key` | The keys are not in the `.env` of the folder you run in | Add them, or set `AWA_LANGFUSE_STRICT=true` to stop instead |
| `Langfuse did not take N span(s) after 3 tries` | The server answered too slowly three times | Raise `AWA_TRACE_EXPORT_TIMEOUT` or give Langfuse more resources |
| Nothing arrives from your own code | The connection was not flushed | `connection.shutdown()` before the process ends |
| `awa runs trace` finds no trace | Level `off`, a run kept in memory, or another folder | Run in the blueprint's folder or pass `--traces DIR` |
| `verify_chain` reports a record | A record was changed, removed or reordered, or a rotated file is missing | Read every file of the trail in order and compare with the head you kept |

Example: `examples/08_tracing.py`.
