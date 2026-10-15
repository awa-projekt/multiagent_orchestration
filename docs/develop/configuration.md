# Configuration

awa reads a few values from the environment: your model key, which models to use and where to keep
state. You set them once, usually in the `.env` file of your project folder. Agents, tools and
limits are not set here: they live in code or in a [blueprint](blueprints.md).

Read this page when a value does not take effect, when you move a project to a server, or when you
configure awa from Python. Install awa first ([Install](../start/install.md)).

## Set a value

Write a value into the project `.env` and check where each value comes from:

```console
awa env set AWA_TRACE_LEVEL=full      # write one into the project .env
awa env list                          # every setting, its value and where it comes from
```

`awa env set` checks a value before it writes it. A secret, such as a model key, takes only the
name (`awa env set ANTHROPIC_API_KEY`): awa asks for the value without showing it, and never repeats
a secret in its messages. A setting that decides where requests go or switches a protection (marked
*confirm* in the [Settings reference](../reference/settings.md)) needs `--confirm`. `awa doctor`
shows the same sources and checks the values.

In Python, the values are one `Settings` object:

```python
from awa.core.settings import get_settings

settings = get_settings()            # read on first use, then cached
print(settings.default_model)        # a provider:model string, see Models
print(settings.anthropic_api_key)    # ********** (a SecretStr, see below)
```

## Where values come from

A value is taken from the process environment, else from the project `.env`, else the default.
Every `awa` command and the library use this rule. `awa env list` and `awa doctor` show the source
of each value: `env`, `env (empty)`, `.env:<line>` or `default`.

For `Settings` in code, keyword arguments come first:

1. **Keyword arguments** in code: `Settings(checkpointer_backend="postgres")`.
2. **Environment variables** of the process. A variable that is set but empty counts as set for the
   loader (it is not filled in from `.env`); `Settings` treats it as unset and takes the `.env`
   value.
3. **The project `.env`.**
4. **The defaults** in the [Settings reference](../reference/settings.md).

### The project folder

awa looks for the `.env` in the project folder. It finds that folder like this:

- `--project DIR` (or `AWA_PROJECT_DIR`) names it.
- Otherwise awa starts at the folder of the blueprint a command names, else at the working
  directory. It walks up to the nearest folder that has `blueprint.yaml`, a `pyproject.toml` with
  `[tool.awa]`, or an `.awa/` folder. A `.env` makes only the start folder itself a project.
- The search stops at the home folder (never a project), at a repository root (a folder with
  `.git`) and at the drive root. Without a match, the start folder is the project.

### What reaches `os.environ`

The `awa` command line exports the values of the project `.env` that the process environment
lacks. So provider SDKs that read the environment themselves (LangChain providers, LangSmith) see
them too.

Only names awa knows are exported: the [registry](../reference/settings.md), its aliases, the names
your blueprint declares in `*_env` keys (a trigger's `secret_env`, a mailbox's `username_env`), and
the variable `--api-key-env` names. `PATH`, `PYTHON*`, proxies, `LD_*`, `GIT_*`, `DOCKER_*`, `PIP_*`,
`UV_*`, `BASH_ENV`, `TEMP` and similar names are never exported, whatever declares them.

Library code (`get_settings()`, `awa.config.env_value`) reads the `.env` without changing
`os.environ`. Call `awa.config.load_environment(export=True)` to export as the command line does.

### Unknown names

An `AWA_*` name that no setting has (in the environment or in `.env`), and an unknown
`ANTHROPIC_*`, `OPENAI_*`, `AZURE_OPENAI_*` or `LANGSMITH_*` name in `.env`, gives one warning with
the closest known name:
`warning: AWA_DEFUALT_MODEL is not a known setting (did you mean AWA_DEFAULT_MODEL?)`.
With `AWA_STRICT_SETTINGS=true` the command stops with exit code 78 instead.

### Settings in code

`get_settings()` caches the object for the whole process. It does not see later changes to the
environment until you call `get_settings.cache_clear()`.

To configure something explicitly, create your own `Settings` and pass it to the functions that
take one:

```python
from awa import Settings
from awa.models import ModelRegistry
from awa.persistence import open_checkpointer

settings = Settings(_env_file=None, checkpointer_backend="postgres",
                    postgres_dsn="postgresql://app:secret@db:5432/app")
models = ModelRegistry(settings)
async with open_checkpointer(settings) as checkpointer:
    ...
```

`_env_file` picks another file (`_env_file=".env.staging"`; triggers and the names a blueprint
declares then read that file too), or none at all (`_env_file=None`).

## The settings most projects need

The [Settings reference](../reference/settings.md) lists every variable awa reads, generated from
the code. These are the ones you are most likely to set:

| Variable | What it does (default) |
| --- | --- |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY` | The key of your model provider. Azure OpenAI also needs `AZURE_OPENAI_ENDPOINT`; Entra sign-in is on [Models](models.md#azure-openai). |
| `AWA_DEFAULT_MODEL`, `AWA_FAST_MODEL`, `AWA_STRONG_MODEL` | The models of the `default`, `fast` and `strong` profiles, as `provider:model` (below). |
| `AWA_ENV` | `local`, `test`, `staging` or `production` (`local`): a label for traces only; see [below](#awa_env). |
| `AWA_POSTGRES_DSN` | The PostgreSQL connection string of every `postgres` backend (unset). |
| `AWA_DATA_BACKEND` | Where runs, steps and results are kept: `sqlite` or `postgres` (unset: `postgres` with a DSN). `awa store use` sets it. |
| `AWA_CHECKPOINTER_BACKEND`, `AWA_STORE_BACKEND`, `AWA_ARTIFACT_BACKEND` | The backends of `awa.persistence.open_checkpointer`, `open_store` and `open_artifact_repository` (`memory`). With a `memory` store, long-term memories and remembered decisions are lost when the process stops. |
| `AWA_RUN_MAX_REQUESTS`, `AWA_RUN_MAX_TOOL_CALLS`, `AWA_RUN_MAX_TOKENS`, `AWA_RUN_MAX_SECONDS`, `AWA_RUN_MAX_DEPTH` | The ceilings of every run tree; see [Limits](safety.md#limits). |
| `AWA_TOOL_PROFILE` | `cost` or `quality` (`cost`): how much of a long tool result the model sees. |
| `AWA_TRACE_LEVEL` | `off`, `summary` or `full` (`summary`): how much the traces of automated runs record; see [Run traces](observability.md#run-traces). |
| `AWA_LANGFUSE_TRACING` | `true`: the `awa` commands that run systems send every run to Langfuse (`false`); see [Langfuse](observability.md#langfuse). |
| `AWA_CACHE_ENABLED` | The master switch of every cache; see [Prompt caching and result caches](models.md#prompt-caching-and-result-caches). |
| `AWA_LOG_LEVEL` | A logging level name (`INFO`) for your own logging setup, such as `logging.basicConfig(level=settings.log_level)`; awa does not read it. |

**The model profiles.** When `AWA_DEFAULT_MODEL`, `AWA_FAST_MODEL` and `AWA_STRONG_MODEL` are unset,
the profiles follow the provider whose credentials are complete, in the order Anthropic, OpenAI,
Azure OpenAI, Google, Mistral, Groq, Ollama (see [Set up a project](../guide/setup.md)). You can
register more profiles on a `ModelRegistry`. Agents and blueprints name a profile, so switching providers is a configuration change ([Models](models.md)).

**Provider names.** Fields with an alias read the conventional variable without prefix that the
provider SDKs also use (`ANTHROPIC_API_KEY`). They also accept `AWA_<FIELD>` as a fallback; the name
without prefix wins when both are set. In code, a field is mostly the variable in lower case
without `AWA_` (`AWA_RUN_MAX_REQUESTS` is `settings.run_max_requests`); the
[`Settings`](../reference/api/core.md) reference lists every field and its alias.

`.env.example` in the repository lists every variable with a comment. It is generated from the
registry (`python scripts/gen_settings.py`), like the reference. The `.env` that `awa init` writes
lists the ones a new project needs.

Some settings sit outside `Settings`:

- The HTTP server takes `--api-key-env`, `--host`, `--port` and `--log-level` on the command line.
  See [Serving](serving.md).
- The CLI takes flags per command. See [CLI](../reference/cli.md).
- Connectors read their own variables (`JIRA_URL`, ...); see [Connectors](connectors.md). The local
  `workspace` connector, which a build from your own files uses, reads `AWA_WORKSPACE_DIR` (the
  folder with the data, default `data`) and `AWA_WORKSPACE_DB` (a SQLite file in it); see
  [Blueprints](blueprints.md#data-from-your-files).

## Secrets

Every credential is a `pydantic.SecretStr`:

- the three provider keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`) and the
  Entra client secret `AZURE_CLIENT_SECRET`,
- `LANGSMITH_API_KEY`, `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`,
- `AWA_POSTGRES_DSN`, because a DSN usually contains a password.

A secret prints as `**********` in `repr`, `str`, `model_dump()`, `model_dump_json()` and pydantic
validation errors. So logging a `Settings` object, or a traceback that shows one, does not leak it.
Only `.get_secret_value()` returns the value. awa calls it at the last possible moment: when it
creates the provider client, or opens the Postgres connection.

```python
settings = Settings(ANTHROPIC_API_KEY="sk-ant-...")
repr(settings)                                   # ... anthropic_api_key=SecretStr('**********') ...
settings.anthropic_api_key.get_secret_value()    # 'sk-ant-...'
```

Handle secrets like this:

- **Keep secrets out of code and blueprints.** Supply them through the environment, or through a
  secret manager that injects environment variables (Kubernetes secrets, Docker secrets, a vault
  agent, CI secrets).
- **Never commit `.env`.** The repository's `.gitignore` excludes it.
- **Use `${VAR}` references for MCP server credentials in blueprints.** Write
  `headers: {Authorization: "Bearer ${TICKETS_TOKEN}"}`. The references are expanded when the server
  is used, and the values are redacted from errors. See [MCP](mcp.md).
- **Errors from model construction are redacted.** They never contain the key that awa passed to
  the provider.

## `AWA_ENV`

`AWA_ENV` labels the deployment: `local`, `test`, `staging` or `production`. Any other value is a
validation error.

It is **metadata only**. awa attaches it to runs as the `env:<env>` trace tag (see
`build_trace_tags`), so you can filter traces by environment. awa never changes behavior because of
it: defaults, security checks and limits are identical in every environment.

Your application may branch on it, for example to refuse an in-memory checkpointer in production:

```python
from awa.core.settings import get_settings
from awa.errors import ConfigurationError

settings = get_settings()
if settings.env == "production" and settings.checkpointer_backend == "memory":
    raise ConfigurationError("production needs AWA_CHECKPOINTER_BACKEND=postgres")
```

## One file per environment

Keep one environment file per deployment and select it at startup:

```python
import os

from awa import Settings

settings = Settings(_env_file=f".env.{os.environ.get('AWA_ENV', 'local')}")
```

In containers, prefer injecting environment variables directly. They take precedence over any
`.env` file.

## Configuration errors

- A value that does not validate fails when `Settings` is created, with a pydantic
  `ValidationError` that names the field. An example is `AWA_CHECKPOINTER_BACKEND=redis`.
- A value that is valid on its own but unusable where it is used raises `ConfigurationError`. An
  example is a `WebhookNotifier` given a URL that is not `https://`.
- `MissingConfigurationError` is a `ConfigurationError` for a value that a selected feature needs,
  such as `AWA_POSTGRES_DSN` for a `postgres` backend.

Both subclass `ValueError` as well as `AwaError`, and carry the stable codes `awa.configuration` and
`awa.missing_configuration`. See [Exceptions](../reference/exceptions.md).

```python
from awa.errors import ConfigurationError
from awa.persistence import open_store

try:
    async with open_store(settings) as store:
        ...
except ConfigurationError as exc:     # also MissingConfigurationError
    raise SystemExit(f"awa is misconfigured: {exc}")
```

## Blueprint files

A blueprint is configuration as code. Loading one follows fixed rules:

- **Format version.** `format_version: awa/v1` records the file format. It is optional when
  reading, and `to_yaml()` / `to_dict()` always write it. A blueprint that names a format this
  version of awa does not know, such as `awa/v2`, is rejected with a clear error. It is never
  misread.
- **Explicit loaders.** `Blueprint.from_yaml_file(path)` reads a file;
  `Blueprint.from_yaml_text(text)` parses a string. `Blueprint.from_yaml(source)` (and `awa.load`)
  decides by a fixed rule: a `Path`, or a one-line string that does not read `key: value` or start
  with `{`, is a file. Anything else is YAML text. So a mistyped path fails with
  `Blueprint file not found: '...'`, not with a confusing schema error.
- **Limits.** Files and texts larger than `MAX_BLUEPRINT_BYTES` (1 MB) are rejected before they are
  parsed. So are structures with more than 100,000 values, where every use of a YAML alias counts,
  and structures nested deeper than 64 levels.
- **Safe YAML only.** Blueprints are read with `yaml.safe_load`, so no YAML tag can construct Python
  objects. Import references (`"package.module:attr"`) are only resolved with `allow_imports=True`.

## What can go wrong

| You see | Cause and fix |
| --- | --- |
| `ValidationError` naming a field, for example `checkpointer_backend` | The value is not allowed (`AWA_CHECKPOINTER_BACKEND=redis`). Use one of the values in the [Settings reference](../reference/settings.md). |
| `MissingConfigurationError` for `AWA_POSTGRES_DSN` | A `postgres` backend is selected but no connection string is set. Set it, or choose `memory` or `sqlite`. |
| a value in `.env` has no effect | The environment sets the same name (it wins; `awa doctor` says `env overrides .env for ...`), the `.env` is in another folder than the project (`awa env list` shows the source), or the name is mistyped (a warning names the closest known name). |
| a changed environment variable has no effect | `get_settings()` caches. Restart the process, or call `get_settings.cache_clear()`. |
| `awa doctor` says `invalid settings` | The same `ValidationError`; `awa doctor` names the variable ([Install](../start/install.md)). |

Reference: [`Settings`](../reference/api/core.md), [Settings reference](../reference/settings.md),
[Exceptions](../reference/exceptions.md), [Blueprints](../reference/api/core.md).
