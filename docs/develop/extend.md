# Extend

You can add your own parts to awa without changing awa: tools, skills, connectors, models, agents,
parsers and storage backends. Each is a plain Python object or file that you hand in with one call.
You can also switch built-in parts off or replace them with your own.

This page lists the extension points, shows how a project folder or a package ships parts as
plugins, and builds the two backends you need most often when your data does not fit: a parser for
a file layout and a store backend for a database. Tools, skills, connectors and MCP have their own
pages.

| You want to add | Extension point | Page |
| --- | --- | --- |
| A tool for your code or API | a function with `@tool`, a `Toolbox` | [Tools](tools.md) |
| Know-how | a `SKILL.md` folder | [Skills](skills.md) |
| An external system | a `ConnectorSpec`, `register_connector`, `openapi_connector` | [Connectors](connectors.md) |
| The tools of an MCP server | `MCPServer` and `load_mcp_tools`, or `mcp:<server>` in a blueprint | [MCP](mcp.md) |
| A model or provider | a `provider:model` string, a chat model, a profile in a `ModelRegistry`, `register("models", ...)` | [Models](models.md), [below](#plugins) |
| An agent or a team of agents | an `Agent`, a `Team`, or any object with `name`, `card` and `run` (`AgentLike`) | [Agents](agents.md), [Teams](teams.md) |
| LangChain tools, LangGraph graphs, prompt files | `from_langchain`, `from_langgraph`, `import_skills` | [below](#langchain-tools-langgraph-graphs-and-prompts) |
| Parts for a project or for everyone who installs a package | `plugins.py` in the project folder, entry points `awa.<kind>` | [below](#plugins) |
| A built-in part switched off | `components:` in the blueprint, `awa plugins switch` | [below](#plugins) |
| Rules for tool calls at run time | a tool policy | [Tools](tools.md#tool-policies-at-run-time) |
| A safety check | an object with `name` and `check(candidate)` | [Safety](safety.md#guardrails) |
| An observer | a hook, or a global subscriber | [Observability](observability.md) |
| A reader for a file layout | a parser | [below](#write-a-parser) |
| Storage, queues, audit sinks, uploads | a backend of a backend protocol, held to its contract suite | [below](#backend-protocols) |

## Scaffold your own parts

The fastest start is a generated file. These commands write working code with a passing test:

| Command | Writes or does |
| --- | --- |
| `awa new tool <name> [--async] [--approval] [--dir D]` | `<name>.py`, a typed tool with a `Deps` dataclass, and `tests/test_<name>.py` with `ToolContract` |
| `awa new toolbox <name> [--dir D]` | A `Toolbox` with a read tool and an approval-gated write tool, and its test |
| `awa new skill <name> [--category C] [--dir skills]` | `<name>/SKILL.md` and `references/guide.md` |
| `awa tools list <module or file> [--json]` | Each tool's name, risk flags, approval and description |
| `awa tools call <module or file> <tool> --args JSON [--deps ...] [--approve]` | Runs one tool locally; a call that needs approval is refused (exit code 2) without `--approve` |

Point the generated files at your data and keep the tests green.

## Plugins

A plugin is a tool, connector, skill, model, parser or erasure handler that awa finds by name. awa
looks in three places, in this order: the project folder, the installed packages, its built-in
parts. `register(kind, name, obj)` adds one in Python:

```python title="own_model.py"
from awa import Agent
from awa.plugins import register
from awa.testing import FakeChatModel

register("models", "scripted", lambda: FakeChatModel(["Hello from the scripted model."]))
print(Agent("greeter", model="scripted").run_sync("Hi").output)
```

```text title="own_model.out"
Hello from the scripted model.
```

### In a project folder

awa reads `tools.py` and `tools/*.py`, `connectors/*.py`, `skills/<name>/SKILL.md`, and `plugins.py`
for `register(...)` calls like the one above. So `model: scripted` then works in the project's
blueprint.

### In a package

A package ships its parts to everyone who installs it, through one entry-point group per kind. The
entry name is the part's name:

```toml
[project.entry-points."awa.tools"]
lookup_order = "acme_awa.tools:lookup_order"   # a tool, a list of tools or a documented function

[project.entry-points."awa.connectors"]
tickets = "acme_awa.connectors:TICKETS"        # a ConnectorSpec

[project.entry-points."awa.skills"]
acme = "acme_awa.skills"                       # a package of skill folders

[project.entry-points."awa.models"]
scripted = "acme_awa.models:scripted"          # a chat model factory without arguments

[project.entry-points."awa.parsers"]
text = "acme_awa.parsers:read_note"            # a parser; the entry name is the file kind

[project.entry-points."awa.erasure"]
tickets = "acme_awa.erasure:TicketErasure"     # an object with erase(request)
```

- A blueprint names an installed package's tools as `plugin:<dist>/<name>`, or `plugin:<dist>` for
  all of them. They are risk-annotated, and approval stays the blueprint's decision.
- A project or a package takes the name of a built-in part only when it says so (`replace=True`, or
  `__awa_replace__ = True` on the object).
- Two installed packages with one name are an error that names both.

### See and switch parts

- `awa plugins list` shows every plugin with its source and status, without importing installed
  plugins. (The project folder's Python files are imported, as when the project runs.)
- `awa plugins show KIND.NAME` shows one.
- `awa plugins check KIND.NAME --target module:Object` runs the contract test of a replacement
  parser, connector, tool or SQL guard.

Built-in parts, including the guards, caches and tracers that are only switches, are switched in the
blueprint:

```yaml
components:
  parser:
    pdf: off
  guard:
    sql_guard: off
```

`awa plugins switch parser.pdf off` writes that entry for you, and `awa.plugins.switches.override`
switches parts for a `with` block in Python. Switching a guard off is reported as a governance
finding.

## LangChain tools, LangGraph graphs and prompts

The importers bring parts you already have into awa and make them first-class. They are preview:
they work and are tested, and their names may still change in a minor release.

### LangChain tools

```python title="import_tools.py"
from langchain_core.tools import BaseTool, BaseToolkit, tool

from awa.tools.importing import import_langchain_tools


@tool
def create_issue(title: str) -> str:
    """Create an issue in the tracker."""
    return f"created {title}"


@tool
def get_issue(number: int) -> str:
    """Return one issue."""
    return f"issue {number}"


class TrackerToolkit(BaseToolkit):  # stands for any LangChain toolkit you already use
    def get_tools(self) -> list[BaseTool]:
        return [create_issue, get_issue]


report = import_langchain_tools(TrackerToolkit(), prefix="gh_")
print(report.render())
```

```text title="import_tools.out"
gh_create_issue  [toolkit TrackerToolkit]
  risk: external_action (name)
  approval: required (inferred: external action (name))
  note: calls pause for a human decision; the agent needs a checkpointer
gh_get_issue  [toolkit TrackerToolkit]
  risk: none
  approval: not required (no external action inferred)
```

`from_langchain(...)` returns the tools themselves. It takes a tool, a toolkit, a function, a
`Runnable`, a list, or an import path such as `"package.module:Attribute"`. The package's code runs,
so import only what you trust.

Every tool keeps its name, description and schema. On the way in, its risk is annotated: an explicit
`risk=` wins over the tool's own annotation, which wins over the name and description. A tool that
acts outside is wrapped with an approval. `requires_approval=True` gates every tool, `False` none.

### LangGraph graphs

A compiled LangGraph graph with a messages channel, `create_agent` graphs included, becomes a member
of a team, a workflow step or a tool:

```python title="import_graph.py"
from langchain.agents import create_agent
from langchain_core.tools import tool

from awa import Agent, Team
from awa.agents.graph_agent import from_langgraph
from awa.persistence import StateInMemorySaver
from awa.testing import FakeChatModel, tool_call


@tool
def issue_refund(order_id: str) -> str:
    """Issue a refund for an order."""
    return f"refunded {order_id}"


billing_graph = create_agent(  # a LangGraph agent you already have; its model is scripted here
    FakeChatModel([tool_call("issue_refund", {"order_id": "A-7"}), "Refunded the second charge of order A-7."]),
    tools=[issue_refund],
    system_prompt="You answer billing questions and issue refunds.",
    name="billing",
)
billing = from_langgraph(billing_graph, name="billing", description="Answers billing questions and issues refunds.")
lead = Agent(
    "lead",
    description="Routes questions to the right agent.",
    model=FakeChatModel(
        [tool_call("delegate_task", {"agent": "billing", "task": "Why was order A-7 charged twice?"}), "We refunded the second charge."]
    ),
)
team = Team("desk", orchestrator=lead, members=[billing], checkpointer=StateInMemorySaver())
result = team.run_sync("Why was I charged twice?")
print(billing.card.tools, "|", result.output)
```

```text title="import_graph.out"
('issue_refund',) | We refunded the second charge.
```

The `GraphAgent` reads its tools from the graph's tool node. It tracks usage, limits, cancellation
and events through LangChain callbacks, passes `thread_id` to the graph's own checkpointer, and
pauses the team when the graph is interrupted.

### Prompt files

`import_skills("team-prompts/", into="skills/")` turns `SKILL.md` folders and Markdown prompt files
into validated skills. A name or description it had to derive is flagged for review. Nothing
imported is executed.

### On the command line

`awa add tool`, `awa add graph` and `awa add skills` do the same and write the result into the
blueprint. The inferred risk becomes `tool_risks:` and a needed approval an entry of `approvals:`,
so `awa validate` sees exactly what will run. `--dry-run` only prints the change.

```console
awa add tool my_tools:make_crm_tools --blueprint support.yaml --agent researcher
awa add graph billing.graph:graph --name billing --blueprint support.yaml
awa add skills ./prompts --into ./skills
```

## Write a parser

awa reads each file with the parser registered for its kind (`csv`, `xlsx`, `pdf`, `docx`, `mail`,
`json`, `sqlite`, `text`). The kind is decided from the bytes. When the shipped parser cannot see a
layout, replace the parser of that kind.

The example is a bank statement that arrives as plain text:

```text title="statements/2026-09.txt"
BANK STATEMENT 2026-09
2026-09-01  Rent                      -1.200,00
2026-09-03  Salary                     3.100,00
2026-09-07  Coffee machine               -89,90

printed on 2026-10-01 by branch 14
2026-09-12  Refund                       15,00
```

```text title="statements/notes.txt"
Remember to ask the bank for the October file.
```

A parser gets the path and a `ParseContext`, adds what it learns to `ctx.profile` and returns it:

```python title="statement_parser.py"
"""A parser for the bank's plain-text statements."""

import re
from pathlib import Path

from awa.data import FileProfile, ParseContext, TableProfile, infer_column

LINE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(.+?)\s+(-?[\d.]*\d,\d{2})$")
NAMES = ["Date", "Text", "Amount"]


def profile_statement(path: Path, ctx: ParseContext) -> FileProfile:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines or not lines[0].startswith("BANK STATEMENT"):
        return ctx.fail(
            "DF-E-PARSE-FAILED", "not a bank statement: the first line must start with BANK STATEMENT"
        )
    rows: list[list[str]] = []
    for number, line in enumerate(lines[1:], start=2):
        ctx.check_deadline()  # stop when the time limit of this file is used up
        if not line.strip():
            continue
        match = LINE.match(line)
        if match is None:
            ctx.flag("DF-W-STATEMENT-LINE", f"line {number} is not a transaction and was skipped")
            continue
        rows.append([cell[: ctx.limits.max_chars_per_cell] for cell in match.groups()])
    columns = [infer_column([row[i] for row in rows], name=NAMES[i], index=i) for i in range(3)]
    ctx.profile.tables.append(
        TableProfile(
            id=ctx.table_id(1),
            header_row=0,  # the file has no header row: the names above are ours
            data_start_row=2,
            rows=len(rows),
            columns=columns,
            sample_rows=rows[:3],
        )
    )
    return ctx.profile
```

- `ctx.fail(code, message)` marks the whole file `error` and returns the profile. `ctx.flag(code,
  message)` adds a warning and goes on. Write the message for the person who reads it.
- `infer_column(values, name=..., index=...)` is the inference awa uses for CSV: it decides the type
  and number and date formats, and flags ambiguity instead of guessing.
- `ctx.check_deadline()` raises when the file has used its time limit. Call it in every long loop.

`parsers_override` replaces a parser for the length of a `with` block:

```python title="try_parser.py"
from awa.data import parsers_override, profile
from statement_parser import profile_statement

with parsers_override({"text": profile_statement}):
    result = profile("statements")

for file in result.files:
    flags = ", ".join(flag.code for flag in file.flags) or "none"
    print(f"{file.path}: {file.status}, flags: {flags}")
    for table in file.tables:
        print(f"  {table.rows} rows")
        for column in table.columns:
            print(f"  {column.name}: {column.type} {column.date_format or ''}".rstrip())
print(result.summary)
```

```text title="try_parser.out"
2026-09.txt: ok, flags: DF-W-STATEMENT-LINE
  4 rows
  Date: date YYYY-MM-DD
  Text: text
  Amount: decimal
notes.txt: error, flags: DF-E-PARSE-FAILED
files=2 tables=1 rows=4 warnings=1 errors=1
```

The line "printed on ..." is reported, not dropped. The note is an error with a reason, and the
other file goes on. A test holds the parser to the rules every parser follows:

```python title="test_statement_parser.py"
from pathlib import Path

from awa.data import parsers_override, profile
from statement_parser import profile_statement


def read(tmp_path: Path, name: str, text: str):
    (tmp_path / name).write_text(text, encoding="utf-8")
    with parsers_override({"text": profile_statement}):
        return profile(tmp_path).files[0]


def test_a_statement_becomes_a_table_with_typed_columns(tmp_path: Path) -> None:
    file = read(tmp_path, "s.txt", "BANK STATEMENT\n2026-09-01  Rent  -1.200,00\n")

    (table,) = file.tables
    amount = table.columns[2]
    assert (table.rows, amount.type, amount.format.decimal) == (1, "decimal", ",")  # type: ignore[union-attr]


def test_bad_content_is_a_flag_not_an_exception(tmp_path: Path) -> None:
    file = read(tmp_path, "s.txt", "BANK STATEMENT\nthis is no transaction\n2026-13-45  Rent  abc\n")

    assert file.status == "ok" and [f.code for f in file.flags] == ["DF-W-STATEMENT-LINE"] * 2


def test_a_file_that_is_no_statement_is_an_error_with_a_reason(tmp_path: Path) -> None:
    file = read(tmp_path, "s.txt", "hello\n")

    assert file.status == "error" and "BANK STATEMENT" in file.flags[0].message


def test_the_same_file_gives_the_same_profile_and_is_only_read(tmp_path: Path) -> None:
    path = tmp_path / "s.txt"
    path.write_text("BANK STATEMENT\n2026-09-01  Rent  -1,00\n", encoding="utf-8")
    before = path.read_bytes()
    with parsers_override({"text": profile_statement}):
        first, second = profile(tmp_path), profile(tmp_path)

    assert first.files == second.files and path.read_bytes() == before
```

For a server or worker, register the parser once at start-up:
`register_parser("text", profile_statement, replace=True)`.

- A parser replaces the reader of a kind that exists. A new kind (a `.xls` file, a binary vendor
  format) is not detected yet, so it never reaches a parser.
- Parsers run in a thread of the calling process. Only the PDF parser runs in a child process with a
  time and memory limit.

## Backend protocols

awa keeps every technology behind a small interface owned by the code that needs it: the **backend
protocol**. The code that does the work with a real technology is a **backend**. awa's logic never
imports a backend; the command line, the server or your code chooses one.

So you can replace a technology without touching awa, tests need no network or database, and every
backend is held to the same promise by a contract suite.

| Backend protocol | Import | Backends that ship | Contract suite |
| --- | --- | --- | --- |
| `Parser` | `awa.data` | CSV, Excel, PDF, Word, mail, JSON, SQLite, text | the rules [above](#write-a-parser) |
| `UploadScanner` | `awa.data` | none: you bring a virus scanner | none |
| `ArtifactRepository` | `awa.persistence` | memory, folder, PostgreSQL | `ArtifactRepositoryContract` |
| `RunStore`, `EventQueue` | `awa.automation` | memory, SQLite (runs only), PostgreSQL | `RunStoreContract`, `EventQueueContract` |
| `AuditLogSink` | `awa.observability` | memory, JSON Lines file | `AuditSinkContract` |
| `Notifier` | `awa.automation` | `WebhookNotifier` | `NotifierContract` |
| `Transport` | `awa.connectors` | the hardened HTTP client, `MockTransport` | [Connectors](connectors.md#write-a-connector) |
| `UploadStore`, `BuildStore`, `BuildEngine`, `SystemFactory` | `awa.server.ports` | folders on disk | none |
| `MessageBus`, `BaseBlackboard`, `BaseTaskBoard` | `awa.messaging`, `awa.tasks` | memory | none |
| `Authenticator`, `BaseRateLimiter`, `BaseThreadIndex`, `BaseIdempotencyCache` | `awa.server` | API key, bearer token, token bucket, memory, PostgreSQL | none |
| `Clock`, `IdSource` | `awa.runtime` | `SystemClock`, `UuidIds`; `FakeClock`, `FixedIds` in `awa.testing` | none |

The contract suites are in `awa.testing.contracts` (preview, extra `testing`). A backend protocol
changes by addition only, which the public API snapshot checks.

Time and ids are backend protocols too, so code that waits or numbers things never sleeps or patches
in a test:

```python title="waiting.py"
import asyncio

from awa.runtime import Clock, as_clock
from awa.testing import FakeClock, FixedIds


async def wait_for_approval(ask, *, clock: Clock | None = None, timeout: float = 3600) -> str:
    clock = as_clock(clock)  # None means the system clock
    started = clock.monotonic()
    while not ask():
        if clock.monotonic() - started >= timeout:
            return "expired"
        await clock.sleep(60)
    return "approved"


clock = FakeClock()
print(asyncio.run(wait_for_approval(lambda: False, clock=clock)), clock.monotonic())

ids = FixedIds()
print(ids.new_id("run"), ids.new_id("run"), ids.new_id("evt"))
```

```text title="waiting.out"
expired 3600.0
run_000000000001 run_000000000002 evt_000000000001
```

An hour of waiting took no time, and the ids are predictable.

## Write a store backend

The `ArtifactRepository` protocol (in `awa.persistence`) stores any JSON value under a text key:

- `put` replaces. A value that is not JSON raises `TypeError`; NaN raises `ValueError`.
- `get` returns the value as stored, or raises `KeyError`.
- `exists` says whether a key is there; `delete` returns whether it was there.
- `list_refs(prefix)` returns sorted keys.

A backend on SQLite:

```python title="sqlite_artifacts.py"
"""Artifacts as JSON text in one SQLite table: a backend for `ArtifactRepository`."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from awa.persistence import ArtifactRepository


class SqliteArtifactRepository(ArtifactRepository):
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS artifact (ref TEXT PRIMARY KEY, value TEXT NOT NULL)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=30)) as db, db:  # closes; commits or rolls back
            yield db

    async def put(self, ref: str, value: Any) -> None:
        text = json.dumps(value, allow_nan=False)  # TypeError and ValueError, as the backend protocol says
        await asyncio.to_thread(self._write, ref, text)

    def _write(self, ref: str, text: str) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO artifact (ref, value) VALUES (?, ?)", (ref, text))

    async def get(self, ref: str) -> Any:
        text = await asyncio.to_thread(self._read, ref)
        if text is None:
            raise KeyError(ref)
        return json.loads(text)

    def _read(self, ref: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM artifact WHERE ref = ?", (ref,)).fetchone()
        return None if row is None else str(row[0])

    async def exists(self, ref: str) -> bool:
        return await asyncio.to_thread(self._read, ref) is not None

    async def delete(self, ref: str) -> bool:
        return await asyncio.to_thread(self._delete, ref)

    def _delete(self, ref: str) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM artifact WHERE ref = ?", (ref,)).rowcount > 0

    async def list_refs(self, prefix: str = "") -> list[str]:
        return await asyncio.to_thread(self._list, prefix)

    def _list(self, prefix: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute("SELECT ref FROM artifact ORDER BY ref").fetchall()
        return [ref for (ref,) in rows if ref.startswith(prefix)]
```

Run the contract suite against it:

1. Subclass the suite.
2. Set `adapter` and `capabilities`.
3. Say how to open the backend on a `place`: a fresh folder and name per test. Opening twice on the
   same place is a restart.

```python title="test_sqlite_artifacts.py"
from contextlib import asynccontextmanager

from awa.testing.contracts import (
    ArtifactRepositoryCapabilities,
    ArtifactRepositoryContract,
    Place,
)
from sqlite_artifacts import SqliteArtifactRepository


class TestSqliteArtifactRepository(ArtifactRepositoryContract):
    adapter = SqliteArtifactRepository
    capabilities = ArtifactRepositoryCapabilities(survives_restart=True, refs_with_nul=True)

    @asynccontextmanager
    async def opened(self, place: Place):
        yield SqliteArtifactRepository(place.path / "artifacts.db")
```

The suites are async tests, so pytest needs `asyncio_mode = auto`:

```ini title="pytest.ini"
[pytest]
asyncio_mode = auto
```

`python -m pytest` runs every promise of the protocol against the backend. Take `allow_nan=False` out
of `put` and run it again: `test_values_json_cannot_hold_are_refused` fails, because the backend
stored a value JSON cannot hold. Behavior a backend may lack is a capability flag, and its test is
skipped rather than failed.

The backend then goes wherever awa takes an `ArtifactRepository`:

```python title="use_backend.py"
import asyncio

from sqlite_artifacts import SqliteArtifactRepository


async def main() -> None:
    artifacts = SqliteArtifactRepository("artifacts.db")
    await artifacts.put("contract:C-17", {"text": "Net 30 days", "pages": [1, 2]})
    print(await artifacts.get("contract:C-17"))
    await artifacts.put("export:C-17:2026", {"rows": 3})
    print(await artifacts.list_refs())


asyncio.run(main())
```

```text title="use_backend.out"
{'text': 'Net 30 days', 'pages': [1, 2]}
['contract:C-17', 'export:C-17:2026']
```

A backend contributed to awa itself follows the rules on [Architecture](architecture.md#rules-for-a-change):
drivers imported only in backend modules and, when optional, inside the function; backends never
import each other; defaults are safe; time and ids are injected.

## What can go wrong

| You see | Why | Fix |
| --- | --- | --- |
| `AdapterError: Cannot import ...: no module ...` | The package behind an import path is not installed | Install it, or pass the tool object |
| `AdapterError: from_langgraph needs a compiled LangGraph graph` | A `StateGraph` that is not compiled | Call `.compile()` first, or pass the result of `create_agent(...)` |
| An imported tool stops the run | It was inferred to act outside and waits for approval | Give the agent a checkpointer, or pass `requires_approval=False` and gate it in the blueprint |
| Every plain text file is now an `error` | The parser replaced the reader of the whole kind `text` | Return `ctx.fail(...)` only for files that are surely not yours, or keep the override in a `with` block |
| `ValueError: a parser for 'text' is already registered` | `register_parser` refuses to replace by accident | Pass `replace=True` on purpose |
| `async def functions are not natively supported` | pytest does not know the tests are async | `asyncio_mode = auto` and `pytest-asyncio` |
| Your backend passes your tests but the suite fails | Your tests describe your idea of the protocol, the suite its promise | Fix the backend; a protocol changes by addition only |
