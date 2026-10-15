# Contributing

Thank you for helping improve awa. A suspected vulnerability is reported as described in
[SECURITY.md](SECURITY.md), never in a public issue. This file covers the development setup,
the checks every change must pass, and the conventions the code follows.

## Development setup

Python 3.11 or newer (`.python-version` pins 3.13 for uv). With
[uv](https://docs.astral.sh/uv/), which installs Python too when it is missing
(uses `uv.lock`):

```bash
uv sync --extra dev --extra docs       # creates .venv
uv run awa doctor
```

With pip:

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,docs]"
```

`uv.lock` is committed: after changing dependencies in `pyproject.toml`, run
`uv lock` and commit the updated lock file. Never commit or copy a `.venv`; it
only works on the machine and for the user that created it.

## Checks

Every change must pass, from the repository root:

```bash
python -m pytest -n auto --fast                # the fast subset while you work (minutes)
python -m pytest -n auto --ignore=tests/docs   # every code and example test, offline
python -m pytest -n auto tests/docs            # the checks of the README and the docs
python -m mypy src                             # strict
python -m mypy tests/typing                    # assert_type checks of the public API types
python -m ruff check .
python -m ruff format --check src tests examples scripts
lint-imports                                   # layers, private modules, outer boundary (.importlinter)
python -m mkdocs build --strict                # when you touch docs or public docstrings
```

Format the files you change with `python -m ruff format`.

The architecture guards (`python -m pytest tests/architecture`) count their
findings and compare them with `tests/architecture/baseline.json`: a count may
only go down. A count that rises fails with the findings; a count that falls
fails until the new count is recorded in that file. The imports that break a layer
rule today are listed under `ignore_imports` in `.importlinter`, and that list
only shrinks.

After you change a docs page or the navigation in `mkdocs.yml`, run
`python scripts/gen_llms.py` to refresh `docs/llms.txt` and `docs/llms-full.txt`
(the index and the full text of the docs for language-model tools);
`tests/docs/test_llms_txt.py` fails while they are stale.

CI runs the same checks on GitLab (`.gitlab-ci.yml`) and on GitHub
(`.github/workflows/`): lint and format, the import contracts, `mypy src`,
`mypy examples/*.py` and `mypy tests/typing`, the test suite with a PostgreSQL
service and the coverage gate on Python 3.11 to 3.14 (plus a Windows job), the
documentation checks, `mkdocs build --strict`, the package build with
`twine check` and a clean wheel and sdist install, and the install tests against
the built wheel. Merge and pull requests and the default branch also run the
tests against the newest allowed versions of all dependencies, LangChain and
LangGraph among them; nightly and by hand the other upstream-compatibility jobs
run too. On a version tag the release jobs run (build, audit and SBOM, and PyPI
publishing once it is turned on), by hand on GitLab.
Run the commands above yourself before you push.

Tests that call a real model are marked `@pytest.mark.integration` and need
provider credentials: `python -m pytest -m integration`. The fast run
(`--fast`) leaves out the example and documentation tests and every test marked
`slow` (more than about ten seconds); mark a new test `slow` when it takes that
long. Other markers: `process` (a whole business process end to end, offline),
`docs` (every test under `tests/docs`) and `live_microsoft` (a real Microsoft 365
tenant, run explicitly). Shared test helpers
live in `tests/_helpers/`. The pre-commit hooks (`pre-commit install`) run ruff
and mypy.

## Conventions

**Tests run offline.** The test suite blocks real model requests
(`allow_model_requests(False)` in `tests/conftest.py`) and removes provider
credentials from the environment. Drive agents with `awa.testing.FakeChatModel`
or `FunctionChatModel`, and replace models in composed systems with
`Agent.override`. A test that needs a real model belongs in the `integration`
suite. Every example in `examples/` has an offline test in `tests/examples/`.
A test fails at teardown when a `FakeChatModel` it called still has unreturned
items (`expect_scripts_consumed`): assert on those steps or shorten the script;
mark the rare test that ends early on purpose with `unconsumed_script`. A test
run fails when it leaves a new untracked file in the checkout (caches aside):
write into `tmp_path`.

**Limits are deterministic and live in code.** A model may recommend; Python
decides. Every loop, fan-out, retry and wait has an explicit bound (a counter,
a timeout or a budget) that is enforced in code and tested, never a sentence
in a prompt. When a limit rejects a model's action, report it as tool text the
model can react to; raise only for limits that must stop the whole run.

**Content from other agents is data.** Results, messages, task text and
skill resources reach a model framed as labeled data, never spliced into
instructions. Keep it that way in new features.

**Typed contracts at boundaries.** Public inputs and outputs are pydantic
models, dataclasses or precise type hints. `src/` passes `mypy --strict`, and
the package ships `py.typed`.

**Errors.** Errors raised on purpose derive from `awa.errors.AwaError`
(or a feature-local subclass of it). Argument mistakes use `ValueError` and
`TypeError`. Observers (hooks, watchers, listeners) must never be able to fail
the run they observe.

**Docstrings** use the Google style (`Args:`, `Returns:`, `Raises:`,
`Example:`), describe behavior rather than implementation history, and are
rendered into the API reference by mkdocstrings. Every public function,
class and module has one. Examples in docstrings must be correct against the
current API.

**Writing.** Documentation and docstrings describe the library on its own
terms, in English. Do not mention or compare to other agent products,
frameworks or assistants by name (LangChain and LangGraph, the foundation, may
be named), and do not narrate how something was found or changed; state what
is true now.

**Layout.** The package uses a `src/` layout (`src/awa/`); tests never import
from the working directory by accident. Nothing importable sits at the
repository root. `examples/` and `tests/` are not part of the wheel.

**Compatibility.** The public API is everything in `awa.__all__` and the
`__all__` of each subpackage. A removal
follows the deprecation policy in `docs/about/stability.md`.
`tests/fixtures/public_api.json` records every public name, and
`tests/unit/test_public_api_snapshot.py` fails when one disappears or a new
one is not recorded. After a deliberate change, regenerate the snapshot in the
same change:
`UPDATE_PUBLIC_API_SNAPSHOT=1 python -m pytest tests/unit/test_public_api_snapshot.py`.

## When you touch a model, API version or dependency: check for a newer one first

Whenever a change names a model, an API version, a library or a service, look up
whether something newer or better exists before you write it down, and use the
current one or avoid naming a version at all. Model ids live in one place, the
Current models table in [Models](docs/develop/models.md#current-models); everywhere else
use `default`, `fast`, `strong` or a placeholder (`<provider>:<model>`,
`<your-deployment>`, `my-model` for made-up prices). `tests/unit/test_model_names.py`
fails on any other id. At every release, re-verify the table against the providers' pages
and update it.

## Changes

awa is hosted on GitLab and on GitHub, and every release is built from a release
tag. Changes reach the main
development branch through a merge request on GitLab or a pull request on
GitHub; both pass the same checks.

- Keep a change focused on one thing and include tests.
- Run the checks above before you hand a change on.
- Update the relevant docs page with the same change.

Dependency updates come as one grouped change that updates `pyproject.toml` and `uv.lock`
together: from Renovate (`renovate.json`, as the Renovate app or a scheduled Renovate pipeline
of the GitLab project) or by hand with `uv lock --upgrade`. Each passes the same checks as any
other change.

### Commit messages

Write [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): description`, with the package or area as the scope.

```text
feat(server): add a health endpoint to awa serve
fix(blueprints): reject an approval that names an unknown tool
docs(user): explain the approval inbox
```

Types: `feat` (a new capability), `fix` (a bug fix), `docs`, `test`, `refactor`,
`perf`, `build`, `chore` and `release` (only for the version commit
`release: vX.Y.Z`). A breaking change has `!` after the type and a
`BREAKING CHANGE:` line in the body; it ships only in a major release, after
the deprecation period ([Stability](docs/about/stability.md#what-a-version-number-promises)).

| Type | Version it implies |
| --- | --- |
| `feat` | minor |
| `fix` | patch |
| `perf`, `refactor`, `docs`, `test`, `build`, `chore` | none |
| `release` | the release itself (`release: vX.Y.Z`) |

Release tags are annotated and named `vX.Y.Z`, release candidates `vX.Y.Zrc1`;
no other tag starts with `v`.

### Branches

Development is trunk-based: the main development branch is always releasable
(the checks above pass on it). Work on a short-lived branch named
`feat/<topic>` or `fix/<topic>` and merge it quickly through a merge or pull request, as a
squash or a fast-forward. A branch `release/X.Y` exists only when an old minor version needs a
fix: create it from that version's tag, cherry-pick the fix, and cut the patch
release from it. Releases are tags named `vX.Y.Z`.
