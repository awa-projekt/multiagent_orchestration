# Memory and persistence

awa keeps four kinds of state: conversations and paused runs, what agents learned, facts and
decisions, and large payloads. Read this page when an agent should remember something across runs,
when a paused run must survive a restart, or when a conversation grows too long. awa builds on
LangGraph's persistence and adds small, typed helpers.

| State | Lives in | Scope | Used for |
| --- | --- | --- | --- |
| A conversation and a paused run | **checkpointer** | one `thread_id` | multi-turn threads, approvals, resuming |
| What agents learned | **long-term memory** (`AgentMemory`, in a store) | across threads, per agent, user, team or globally | facts and preferences an agent recalls later |
| Facts and decisions | **store** (`BaseStore`) | across threads | remembered approvals, subject links |
| Large payloads | **artifact repository** | by reference | documents and outputs too big for graph state |

The runs of automations, their steps and approvals are kept in the data store
([Production](production.md#the-data-store)). Erasing a person's data and retention are on
[Safety](safety.md#data-protection).

## Long-term memory

`AgentMemory` gives an agent `remember`, `recall` and `forget` tools, scoped per agent, user, team or
globally:

```python title="remember.py"
import asyncio

from awa import Agent
from awa.memory import AgentMemory, MemoryScope
from awa.testing import FakeChatModel, tool_call

memory = AgentMemory(scope="user", auto_recall=3)
model = FakeChatModel(
    [tool_call("remember", {"content": "Invoices in EUR", "tags": ["billing"]}), "Noted.", "In EUR."]
)
assistant = Agent("account_assistant", instructions="Remember lasting preferences.", memory=memory, model=model)


async def main() -> None:
    await assistant.run("Please invoice me in EUR.", deps={"user_id": "customer-17"})
    owner = MemoryScope(agent="account_assistant", user_id="customer-17")
    print([item.content for item in await memory.recall("currency invoices", owner=owner)])

    await assistant.run("Which currency do you bill me in?", deps={"user_id": "customer-17"})
    shown = [m.content for m in model.calls[2].messages if "Invoices in EUR" in str(m.content)]
    print(len(shown), "recalled message before the question")
    print([tool.name for tool in assistant.tools])


asyncio.run(main())
```

```text title="remember.out"
['Invoices in EUR']
1 recalled message before the question
['remember', 'recall', 'forget']
```

- `remember(content, tags=[])` saves one statement (at most 4000 characters); `recall(query,
  limit=5)` returns the best matches; `forget(memory_id)` deletes one. Recalled memories reach the
  model as a JSON list in a `<memories>` frame: data, not instructions.
- With `auto_recall=k`, the `k` most relevant memories are shown once at the start of a run, as a
  context message before the user input. The message is never saved, so the system prompt stays
  the same from run to run and the provider's prompt cache keeps working.
- `recall` is semantic when the store has an embedding index, else lexical (`memory.semantic`
  says which).
- `max_items` (default 200) per owner; `ttl` lets memories expire.

| `scope` | Shared by | Namespace (prefix `("awa", "memory")`) |
| --- | --- | --- |
| `"agent"` (default) | one agent, per user | `(..., "agent", <agent>, "user", <user_id>)` |
| `"user"` | every agent serving the same user | `(..., "user", <user_id>)` |
| `"team"` | the agents of one team, per user | `(..., "team", <team>, "user", <user_id>)` |
| `"global"` | everyone using this memory | `(..., "global")` |

A run whose tenant is not the default (`run(metadata={"awa.tenant": ...})`, or the caller's tenant
on a served system) adds `("tenant", <tenant>)` after the prefix, so tenants never share memories.

The user id comes from the run's deps (`deps["user_id"]` or `deps.user_id`, or a function you pass
as `user_id=`). `scope="user"` without a user id raises `MemoryScopeError` instead of sharing.
Without a user id the `agent` and `team` scopes use an anonymous owner; set `require_user=True`
whenever several users share the system. A served system derives the user id from the caller
([Serving](serving.md#who-may-call-and-what-they-see)).

Memories live in a LangGraph `BaseStore`, an `InMemoryStore` by default. For memory that survives
restarts, pass the PostgreSQL store:

```python
from awa import Agent
from awa.core.settings import get_settings
from awa.memory import AgentMemory
from awa.persistence import open_store


async def build() -> None:
    # AWA_STORE_BACKEND=postgres, AWA_POSTGRES_DSN=postgresql://...
    async with open_store(get_settings()) as store:
        assistant = Agent("account_assistant", memory=AgentMemory(store, scope="user"))
        await assistant.run("Please invoice me in EUR.", deps={"user_id": "customer-17"})
```

Your code uses the same methods with an explicit owner: `remember`, `recall`, `forget`,
`list_items`, `clear` and `context_block` (recalled memories as a prompt block). Every memory records
the person it is about (`MemoryItem.subject`), so `erase_subject("customer-17")` finds them wherever
they are, and `prune(older_than=...)` deletes old ones.

In a blueprint:

```yaml
agents:
  - name: account_assistant
    description: Helps customers with billing questions.
    memory: {scope: user, auto_recall: 3, require_user: true, user_id: principal}
    compaction: {max_tokens: 12000, keep_last_messages: 12}
```

`user_id: principal` takes the user from the authenticated caller of a served system, `deps` from
the request. `awa validate` reports `scope: team` outside a team as an error and per-user memory
without a user source as a warning.

## Long conversations

`Compaction` keeps a thread within a token budget. Before each model call, when the conversation is
larger than `max_tokens` (approximately), everything but the last `keep_last_messages` messages is
replaced by one summary; the cut never separates a tool call from its result. With a checkpointer
the thread keeps the compacted conversation.

| Field | Default | Meaning |
| --- | --- | --- |
| `max_tokens` | 8000 | conversation size that triggers a summary |
| `keep_last_messages` | 20 | recent messages kept as they are |
| `model` | the agent's | summary model, for example `"fast"` |
| `summary_prompt` | LangChain's | a prompt with `{messages}` |
| `trim_tokens_to_summarize` | 4000 | upper bound of the turns sent to the summary model |

`Agent(compaction=Compaction(max_tokens=12_000, keep_last_messages=12))` wraps LangChain's
`SummarizationMiddleware`. Summary calls are model requests of the run: they count in
`RunResult.usage`, are held to its `UsageLimits` and respect `allow_model_requests(False)`.

## Checkpointers and threads

Give the top-level agent, team or workflow a checkpointer. Nested agents persist as part of its
thread ([Agents](agents.md#conversations)). In production, choose the backend in the settings and
keep it open while your application runs:

```python
from awa.core.settings import get_settings
from awa.persistence import open_checkpointer


async def serve_one(build_team) -> None:
    # AWA_CHECKPOINTER_BACKEND=postgres, AWA_POSTGRES_DSN=postgresql://...
    async with open_checkpointer(get_settings()) as checkpointer:
        team = build_team(checkpointer=checkpointer)
        await team.run("...", thread_id="customer-42")
```

| Backend | `AWA_CHECKPOINTER_BACKEND` | Notes |
| --- | --- | --- |
| In memory | `memory` | `StateInMemorySaver`; gone with the process. |
| SQLite | `sqlite` | `SqliteCheckpointer` (`.awa/checkpoints.db`), standard library only; keeps the newest 25 checkpoints per thread and deletes threads idle for 90 days. |
| PostgreSQL | `postgres` | The `postgres` extra; tables are created on first use, under a lock for processes that start together. On Windows it needs a selector event loop: `awa.ext.run_sync(main(), selector_loop=True)`. |

A checkpoint stores an agent's structured output by class name, and LangGraph would rebuild it by
calling that class. So every checkpointer awa creates is strict. It revives only LangGraph's safe
types and the classes registered with `awa.persistence.register_state_type`; awa registers every
agent's `output_type`. Anything else comes back as plain data, with a warning.

- Use `awa.persistence.StateInMemorySaver` in your own code, not LangGraph's plain saver.
- Let only the service write to the checkpoint store.

### History and forks

For explicit graphs, LangGraph's own calls cover time travel: `graph.aget_state_history(config)`
lists a thread's checkpoints newest first, `graph.aupdate_state(snapshot.config, values)` branches
a new checkpoint off an old one without changing the history, and `graph.ainvoke(None,
forked_config)` resumes from it.

## Artifacts

Keep graph state small and store large payloads by reference:

```python title="artifacts.py"
import asyncio

from awa.persistence.artifacts import FileArtifactRepository


async def main() -> None:
    artifacts = FileArtifactRepository(".awa_artifacts")  # or the in-memory and PostgreSQL backends
    await artifacts.put("contract:C-17", {"text": "Net 30 days", "pages": [1, 2]})
    print(await artifacts.get("contract:C-17"))
    print(await artifacts.list_refs("contract:"))


asyncio.run(main())
```

```text title="artifacts.out"
{'text': 'Net 30 days', 'pages': [1, 2]}
['contract:C-17']
```

`open_artifact_repository(settings)` picks the backend from `AWA_ARTIFACT_BACKEND` (`memory`, `file`
or `postgres`) and `AWA_ARTIFACT_DIR`. Values must be JSON. A backend of your own is held to the
contract suite of the `ArtifactRepository` protocol ([Extend](extend.md#write-a-store-backend)).

A team's blackboard, task board and message bus are per run and not checkpointed; snapshot them
yourself when they must outlive the process (`await blackboard.snapshot()`, `task_board.to_dict()`).

## What can go wrong

| You see | Cause and fix |
| --- | --- |
| `MemoryScopeError` | `scope="user"` without a user id in the deps, or `scope="team"` outside a team. |
| A paused run is gone after a restart | The checkpointer was in memory. Use SQLite or PostgreSQL. |
| `PostgresSetupTimeoutError` | Another process held the setup lock for more than 120 seconds. |
| A resumed run returns a dict instead of your class | The class was neither an `output_type` nor registered with `register_state_type`. |
| `ConfigurationError` about the `ProactorEventLoop` on Windows | Start async code that uses PostgreSQL with `awa.ext.run_sync(main(), selector_loop=True)`. |

Example: `examples/06_memory.py`.
