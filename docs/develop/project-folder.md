# The project folder

A project is one plain folder that holds everything a process needs: the blueprint, your tools,
your files, the tests and the settings. The web app (`awa ui`), `awa build` and `awa init` write it.
Every command, the web app and `awa.load` read it the same way, so you can switch between them at
any step.

Read this page to know what a project consists of, or to work on it from Python or a terminal
instead of the web app. You need awa installed ([Install](../start/install.md)).

## What is in it

| Path | What it holds | Written by |
| --- | --- | --- |
| `blueprint.yaml` | The agents, how they work together, the connectors, the approvals and the triggers. The only file a project needs. | all |
| `tools.py` | Your own tools (and the rules and approval steps of a built project). Loaded by itself, next to the blueprint. | all, when needed |
| `skills/<name>/SKILL.md` | Your own skills. Loaded by itself, next to the blueprint, in addition to the built-in skills. | `awa new skill`, you |
| `plugins.py`, `tools/*.py`, `connectors/*.py` | More parts of the project: `register(...)` calls for models, parsers and the like, more tool files, connector specs ([Plugins](extend.md#plugins)). | you, when needed |
| `data/` | Your files (`data/files/`), the results (`data/output/`), their profile and the sample data that stands in for external systems not yet connected. | web app, `awa build` |
| `evals/cases.yaml` | The eval cases. | all |
| `tests/` | Offline pytest tests of `tools.py` (and of the blueprint after `awa init`). | all, when needed |
| `CONNECT.md` | How to connect each external system, step by step (read by `awa connect`). | web app, `awa build` |
| `.env.example` | The names of the settings the project reads, without values. | all |
| `.env` | The values: model keys, addresses, passwords. awa writes it readable by you only and never shows a value again. | web app, `awa setup`, `awa connect`, you |
| `.awa/` | Local run data: runs, approvals, uploads, traces. | web app, `awa serve`, `awa worker` |
| `.gitignore` | Keeps `.env`, `.env.bak` and `.awa/` out of version control. | all |

`awa init` also writes a `pyproject.toml` and a `README.md`; `awa build` and the web app write a
`README.md` that sums up the design.

Settings are read from the process environment first, then from the project's `.env`.

!!! warning "Open only projects you trust"
    `tools.py` and `skills/` are code and instructions that run on your computer.

## Use it from Python

`awa.load` takes the folder (or its `blueprint.yaml`) and builds the system with the project's
`tools.py` and `skills/`, exactly as the commands and the web app do. Run it, answer an approval,
and evaluate the cases:

```python
import asyncio

import awa
from awa.evals import Dataset, evaluate
from awa.persistence import StateInMemorySaver

desk = awa.load("invoice-desk", checkpointer=StateInMemorySaver())
result = asyncio.run(desk.run("Check invoice INV-1042."))
if result.is_interrupted:  # a tool call waits for your approval
    result = asyncio.run(desk.resume(result.thread_id, {"action": "approve"}))
print(result.output)

report = asyncio.run(evaluate(desk, Dataset.load("invoice-desk/evals/cases.yaml")))
print(f"{report.pass_rate:.0%} of the cases pass")
```

- Pass `tools=` or `skill_registry=` to use other tools or skills than those of the folder.
- The settings come from the process environment, then from the `.env` of the project folder that
  contains the working directory. To run the script from elsewhere, call
  `awa.config.load_environment("invoice-desk")` first.

## Every step without the web app

Each step of the web app has a command and a Python call:

| Web app | Command line | Python |
| --- | --- | --- |
| Set up: the model key | `awa setup`, `awa env set NAME` | `awa.setup` |
| Start from an example | `awa ui --example ID`, `awa init DIR --template NAME` | `awa.blueprints.scaffold.create_project` |
| Data: your files | `awa data inspect FILES` | `awa.data.profile` |
| Data: external systems | `awa data add-system`, `awa data remove-system` | `awa.blueprints.read_declared_systems` lists them |
| Describe | `awa build "<process>" --data FILES` | `awa.blueprints.build_project`, `awa.blueprints.design_project` |
| Suggest and test patterns | none | `awa.blueprints.suggest_patterns`, `awa.blueprints.try_patterns` |
| Review | `awa validate`, `awa show` | `awa.Blueprint.check`, `awa.Blueprint.explain` |
| Connect | `awa connect DIR` | the values in `.env`, the connector in `blueprint.yaml`, as `CONNECT.md` says |
| Test | `awa eval blueprint.yaml evals/cases.yaml` | `awa.evals.evaluate` |
| Try it | `awa chat blueprint.yaml`, `awa run blueprint.yaml "<task>"` | `awa.load` |
| Use it: triggers | `awa add trigger`, `awa trigger run`, `awa worker`, `awa serve` | `awa.blueprints.triggers.add_trigger`, `awa.automation.build_automation`, `awa.automation.Worker` |
| Use it: runs | `awa runs list`, `awa runs retry` | `awa.automation.SqliteRunStore` |
| Approvals | `awa runs decide` | `awa.automation.Automation.decide` |
| Assistant | `awa brief` | `awa.blueprints.write_brief` |

The web app's download is the folder itself without `.env`, `.awa/` and the databases; in git,
the `.gitignore` keeps `.env` and `.awa/` out. To leave awa, `awa export` and `awa.exporter.export`
turn a one-agent project into a standalone LangGraph project ([Export](export.md)).

Every command is on [Command line](../reference/cli.md); every page of the web app is on
[The web app](../guide/web-app.md).
