# Guidance for coding assistants

`awa` (distribution `awa-agents`, import `awa`) is a Python library for AI agents in business
processes, with hard limits in code, approvals, eval cases and a one-agent export to plain
LangGraph; it is built on LangChain `create_agent` and LangGraph. [CONTRIBUTING.md](CONTRIBUTING.md) is the full
contributor guide; this file is the short version for tools that edit the repository.

## Setup

```bash
uv sync --extra dev --extra docs       # creates .venv from uv.lock
```

Use the interpreter of that `.venv`. Never copy or commit a virtual environment.

## Checks (run from the repository root)

```bash
python -m pytest -n auto --fast                # the fast subset while you work
python -m pytest -n auto --ignore=tests/docs   # every code and example test, offline
python -m pytest -n auto tests/docs            # the checks of the README and the docs
python -m mypy src                             # strict
python -m ruff check .
python -m ruff format --check src tests examples scripts
lint-imports                                   # layers, private modules, outer boundary (.importlinter)
python -m mkdocs build --strict                # when you touch docs or public docstrings
```

## Repository map

- `src/awa/`: the package (`agents`, `team`, `workflows`, `tools`, `toolkits`, `connectors`,
  `skills`, `guardrails`, `persistence`, `server`, `cli`, `evals`, `testing`, ...)
- `tests/`: unit, property, architecture and docs tests; `tests/_helpers/` holds shared helpers
- `examples/`: numbered scripts, `recipes/`, `usecases/`, `evals/`, `benchmarks/`
- `docs/`: the MkDocs site; `mkdocs.yml` holds the navigation
- `scripts/`: generators and tools; generated docs pages have a test that fails when stale

## Conventions

- Deterministic Python enforces every limit (depth, fan-out, timeouts, usage, permissions,
  approvals); a model only chooses within what the code allows.
- Agents receive small typed projections of state. Anything another agent wrote (results,
  messages, board entries) reaches a model framed as data.
- Deliberate errors derive from `awa.errors.AwaError`.
- Public symbols are exported through `__all__`, have Google-style docstrings and tests.
- English only. Describe the library on its own terms; do not name or compare other agent
  products (LangChain and LangGraph may be named). No narrative comments about history.
- Graph node dependency parameters are never named `runtime`.
- Commit messages follow Conventional Commits; see CONTRIBUTING.md.

## Tests run offline

Tests use `awa.testing.FakeChatModel` or `FunctionChatModel`; `tests/conftest.py` blocks real
model requests and removes provider credentials. A test that needs a real model carries
`@pytest.mark.integration` and is excluded by default.

## Never

- Print, log or commit secrets. `.env` is local; only `.env.example` is committed.
- Call a real model from a test or run an example against one without the maintainers' consent.
- Loosen a check (coverage floor, import contracts, lint rules, recorded baselines) to make it pass.
- Push, publish or upload anything unless asked.
