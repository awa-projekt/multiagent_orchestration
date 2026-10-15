# Reference

Look up a command, a setting, a tool, a skill, a word or a Python name. The command, settings,
tool and skill pages are generated from the code.

| Page | What it lists |
| --- | --- |
| [Cheat sheet](cheatsheet.md) | Every command and the Python calls used most on one page, each linked to its details. |
| [Command line](cli.md) | Every `awa` command with its options, and the exit codes. |
| [Settings](settings.md) | Every setting, its environment variable and default. |
| [Tools](tools.md) | Every built-in tool, its approval default and an example call. |
| [Skills](skills.md) | Every built-in skill and what it is for. |
| [Glossary](glossary.md) | The words used in awa, one sentence each. |
| [Exceptions](exceptions.md) | Every error awa raises on purpose, its code and whether a retry can help. |

## Python API

Generated from the docstrings. Each page starts with a runnable example.

| Page | Packages |
| --- | --- |
| [Core](api/core.md) | `awa.blueprints`, `awa.quick`, `awa.refine`, `awa.runtime`, `awa.contracts`, `awa.models`, `awa.config`, `awa.core.settings`, `awa.project_setup`, `awa.plugins`, `awa.ext` |
| [Agents and tools](api/agents.md) | `awa.agents`, `awa.tools`, `awa.toolkits`, `awa.skills`, `awa.memory`, `awa.guardrails`, `awa.mcp` |
| [Teams and workflows](api/teams.md) | `awa.team`, `awa.workflows`, `awa.messaging`, `awa.tasks`, `awa.directory`, `awa.people` |
| [Data and connectors](api/data.md) | `awa.data`, `awa.connectors`, `awa.persistence` |
| [Serve and operate](api/server.md) | `awa.server`, `awa.automation`, `awa.observability`, `awa.evals`, `awa.testing`, `awa.exporter` |

The names used most often also import from `awa` directly, for example
`from awa import Agent, Team, load`; `awa.__all__` lists them. A module whose name starts with an
underscore is internal and may change in any release.
