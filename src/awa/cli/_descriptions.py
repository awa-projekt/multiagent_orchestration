"""The `--help` description of every command and subcommand that does not set its own.

Plain text only: importing this module loads nothing else."""

from __future__ import annotations

_KEY = "Needs a model key (awa setup sets it, awa doctor checks it)."
_SERVER = "Needs the server extra (awa doctor prints the install command)."

DESCRIPTIONS: dict[str, str] = {
    "validate": "Check a blueprint for errors without running it or calling a model: the "
    "schema, the agents, the tool names (with the tools.py beside it) and the triggers. "
    "Exit code 0 when it is valid (warnings are printed), 1 on errors.",
    "show": "Describe a blueprint: its agents, models, tools, skills, triggers, approvals and "
    "how the agents work together. `show cases` lists the special cases and their tests, "
    "`show dry-run` walks the path a run would take; neither calls a model.",
    "eval": "Run every case of a dataset (evals/cases.yaml) against the system and print the "
    f"pass rate as a table. {_KEY} Exit code 1 below --min-pass-rate (default 1.0).",
    "run": "Give the system one task and print the answer, with --stream also each event. "
    f"{_KEY} Exit code 2 when the run waits for an approval (answer it in awa chat).",
    "chat": "Talk to the system in the terminal; tool calls that need an approval are asked "
    "inline. A team or single agent keeps the conversation; a workflow or network answers "
    f"each message on its own. {_KEY} /help lists the chat commands.",
    "serve": "Offer the system as a web API (HTTP, server-sent events), receive its webhooks "
    "and run its other triggers. Paused runs are kept in .awa/ next to the blueprint, or in "
    f"PostgreSQL with --dsn. {_KEY} {_SERVER}",
    "ui": "Open the web app in the browser, on this computer only: Set up, Data, Describe, "
    "Review, Connect, Test, Try it, Use it, Approvals and Assistant. Keep the window open while you use "
    f"it; Ctrl+C stops it. {_SERVER}",
    "mcp": "Use the system with MCP: pin the tool definitions of its MCP servers, list "
    "them, or offer the system itself as an MCP server.",
    "mcp lock": "Connect to the blueprint's MCP servers and write their tool definitions to "
    "mcp.lock.json, so a changed tool is noticed before it is used.",
    "mcp list": "List the tools of the blueprint's MCP servers and whether each matches the "
    "pin in mcp.lock.json. Exit code 1 when a tool changed or is not pinned.",
    "mcp serve": "Offer the system as an MCP server (stdio by default, or http), so MCP "
    f"clients can send it tasks. {_KEY}",
    "add": "Add to a blueprint: a trigger that starts it, or imported LangChain tools, "
    "LangGraph graphs and skills.",
    "add tool": "Import a LangChain tool, toolkit or tool factory: writes the "
    "wrapper code into a Python file and registers it in the blueprint (--dry-run shows it).",
    "add graph": "Add a compiled LangGraph graph as an agent others can delegate to.",
    "add skills": "Import skill folders or Markdown files into the skills folder.",
    "plugins": "Plugins and switchable parts: list, show, check and switch them.",
    "plugins list": "List every plugin (project folder, installed packages, built-in) and "
    "switchable part. Installed plugins are not imported; the project folder's Python files "
    "are, as when the project runs.",
    "plugins show": "Show one plugin or part: source, status, switch key.",
    "plugins check": "Run the contract test of a parser, connector, tool or sql_guard (the "
    "built-in, or --target module:Object). Exit code 1 when it fails.",
    "plugins switch": "Switch a part on, off or back to its default in the blueprint's "
    "components: section (--dry-run prints the result).",
    "new": "Write a starting point from a template: a blueprint for a process, a tool, a "
    "toolbox (a configurable connector) or a skill, each with a test where it applies.",
    "new tool": "Write a typed, documented tool function and its test into --dir.",
    "new toolbox": "Write a configurable connector (several tools with one prefix) and its "
    "test into --dir.",
    "new skill": "Write a skill folder: SKILL.md with metadata, and references/.",
    "tools": "List the tools of a Python file or module, or the builtin catalog "
    "(awa tools list builtin), or call one tool directly without a model.",
    "tools list": "List each tool with its risk, approval rule and description. The source "
    "is a .py file, a module, or `builtin` for the tools awa ships.",
    "tools call": "Call one tool directly, without a model, and print its result. Exit code "
    "2 when the tool needs an approval (give --approve), 1 when it fails.",
    "trigger": "List a blueprint's triggers, or fire one once with a sample payload to try it "
    "without waiting for a real event.",
    "trigger list": "List the triggers of a blueprint: name, kind and what starts it.",
    "trigger run": "Fire a trigger once with --payload and run the process here; --dry-run only "
    "shows the task it would get. Exit code 2 when the run waits for an approval.",
    "runs": "List and operate the process runs of a project: the same store awa worker and "
    "awa serve use (.awa/awa.db next to the blueprint, or PostgreSQL with --dsn).",
    "runs list": "List the process runs, newest first, with their status.",
    "runs show": "Show one run: its status, input, output, waits and history.",
    "runs retry": "Queue a failed or dead-lettered run again; a running worker picks it up.",
    "runs cancel": "Cancel a run that has not finished; the reason is stored with it.",
    "runs dlq": "List the dead-lettered runs: those that failed every retry.",
    "runs migrate": "Move runs that wait under an older version of the blueprint to its "
    "current version (--dry-run lists them).",
    "connectors": "List, show and test the connectors to your external systems (databases, files, "
    "Microsoft 365 and more) and whether their settings are present.",
    "connectors list": "List the connectors with category, stability (core or preview) and "
    "whether their settings are present.",
    "connectors show": "Show a connector's settings (secrets only as set or not set) and tools.",
    "connectors test": "Test connections with one small real call each. Exit code 1 when one "
    "fails; each failure says how to fix it.",
    "export": "Write a standalone LangChain and LangGraph project from a blueprint with one agent. "
    "It runs without awa and is edited with LangChain and LangGraph only; nothing syncs back. "
    "A blueprint that uses anything the export does not carry over is refused as a whole.",
    "skills": "List the skills agents can use, or print one.",
    "skills list": "List the available skills: the built-in ones and those in --skills folders.",
    "skills show": "Print a skill's SKILL.md.",
    "data": "Work with your files: see what is in them (changes nothing), check them against "
    "a project, run a project on them, and declare the databases and APIs it uses.",
    "data inspect": "Show what is in your files (tables, columns, pages, personal data) and "
    "what awa made of them; changes nothing. Exit code 1 on errors (--strict: on warnings).",
    "data check": "Check a project's `data:` section against the files on disk. Exit code 1 "
    "when a file is missing or no longer matches.",
    "data run": "Run a project built with --data on its input files; the results go to "
    f"data/output/. {_KEY} Exit code 2 when a run waits for an approval.",
    "data add-system": "Declare a database or API the process uses: test it read-only, save "
    "its settings in the project .env and read its structure.",
    "data remove-system": "Forget a declared system; its settings stay in .env.",
    "env unset": "Remove a setting from the project .env.",
    "setup services up": "Start this project's local services in Docker (database, "
    "tracing) and wait until they answer.",
    "setup services status": "Show whether this project's local services run.",
    "cache": "Show or clear what the caches hold (tool results, steps, examples, plans, "
    "replayed model answers), or write the project's cache salt.",
    "store": "Where process runs, steps and results are kept: the project's SQLite file "
    "(.awa/awa.db) or PostgreSQL. Show it, switch it, back it up, erase or sweep old runs.",
    "store status": "Show the active store, its schema version and the row counts.",
    "store use": "Move the project's data to PostgreSQL or SQLite, verify the copy and only then "
    "switch the setting. Starts a local PostgreSQL when no address is set.",
    "store migrate": "The move of `store use` with every option (--dry-run counts only).",
    "store upgrade": "Apply pending schema migrations (servers and workers also do it on start).",
    "store backup": "Back up the local SQLite data into a new folder (PostgreSQL: see the docs).",
    "store erase": "Delete runs for good: one run, every run of an automation, or every run a "
    "person started or decided on; --all and --person need --confirm.",
    "store sweep": "Delete finished runs older than the retention (AWA_RUN_RETENTION_DAYS).",
    "cache stats": "Show how many entries each cache holds and how large it is.",
    "cache clear": "Delete cache entries: all, or one tenant or kind; --replay the recordings of "
    "awa eval --replay.",
    "cache salt": "Write a random AWA_CACHE_SALT into the project's .env, so cache keys stay "
    "stable across processes (the value is never shown).",
    "people": "Find colleagues in Entra ID (or a local list) and pin them to the roles "
    "a blueprint names, in config/people.yaml. Search results are not stored.",
    "people search": "Find people by name or mail address start; nothing is stored.",
    "people pin": "Store a person in config/people.yaml with the roles they hold.",
    "people list": "List the pinned people and their roles.",
    "people check": "Ask the directory whether the pinned people still exist. Exit code 1 when "
    "one is gone.",
    "people unpin": "Remove a person and all their roles from config/people.yaml.",
    "new parser": "Write a reader for another file kind (a parser) and its test into --dir.",
    "new connector": "Write a connector to one of your external systems and its test into --dir.",
}
"""Command path (without `awa`) -> its description."""
