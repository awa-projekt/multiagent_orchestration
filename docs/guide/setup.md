# Set up a project

`awa setup` connects a project folder to a model. It writes the model provider and its key into
the project's `.env` file, and optionally a database and tracing. It writes nowhere else and never
prints a value.

In a terminal, run it without options. It asks section by section; then `awa doctor` checks the
result:

```console
awa setup
awa doctor
```

The page **Set up** of the web app does the same for the model ([The web app](web-app.md#set-up)).

## In a script

In a script, pass the answers as options. A key is read from standard input, never as a
command-line value:

```console
awa setup --provider openai --key-stdin < key.txt
awa setup --database postgres-docker --yes
```

Without a terminal and without options, `awa setup` changes nothing. It prints the state of every
section and the options to use.

## The sections

| Section | What it does |
| --- | --- |
| provider | Saves the key of one provider: `anthropic`, `openai`, `azure_openai` (key, endpoint and deployment), `google_genai`, `mistralai`, `groq`, or `ollama` (where it listens). The key is typed hidden or read with `--key-stdin`. |
| test | `--test` makes one tiny call (one output token, 15 seconds at most) to the `default` model and records whether it answered. |
| database | `sqlite` (the default): local files next to the blueprint. `postgres-docker`: PostgreSQL in Docker on this computer, with a generated password. `url`: your own PostgreSQL, read with `--database-url-stdin` and tested with `SELECT 1` before it is saved. |
| tracing | `off`, `langfuse-docker` (Langfuse in Docker with its first user and project keys generated; Docker needs at least 4 GB of memory), or `url` (your own Langfuse: `--langfuse-url`, `--langfuse-public-key`, and the secret key with `--langfuse-secret-stdin`). |

Most projects need only the provider. The defaults for the database (local files) and tracing
(off) work without setup.

### Which model is used

You usually do not set a model. When `AWA_DEFAULT_MODEL`, `AWA_FAST_MODEL` and `AWA_STRONG_MODEL`
are not set, awa takes them from the provider whose credentials are complete, in this order:
Anthropic, OpenAI, Azure OpenAI, Google, Mistral, Groq, Ollama.

- With only Azure OpenAI set up, every profile uses your deployment, and nothing asks for
  another key.
- When you choose a provider that is not first in that order, `awa setup` also writes the three
  model settings ([Models](../develop/models.md)).

### Changing a value

- A value already in `.env` is changed only after you confirm it: answer the question in a
  terminal, or pass `--yes`.
- Before it writes, `awa setup` keeps one copy of the old file as `.env.bak`. Like `.env`, only
  you can read it.
- Without a terminal and without `--yes`, it stops with exit code 3 and names the settings.

## Local services

PostgreSQL and Langfuse can run in Docker on this computer, one set per project:

```console
awa setup services up --profile db
awa setup services status
awa setup services down
awa setup services down --volumes --confirm awa-my-desk-1a2b3c4d
```

- Each project gets its own Compose project name, `awa-<folder>-<8 characters of a hash of its
  path>`. Two projects never share or delete each other's containers or data.
- Every port is bound to `127.0.0.1`, so nothing is reachable from other computers. A taken port
  is skipped for the next free one.
- The Compose file is `.awa/compose.services.yaml` and holds no password. The generated passwords
  are kept in `.env` only (`AWA_SETUP_*`).
- `down` keeps the data. `down --volumes` deletes it and needs the project name typed, or
  `--confirm`.
- Without Docker, `awa setup` says so in one sentence. Use `--database url` or `--tracing url`
  with a server you have.

## See and change single settings

- `awa env list` shows every setting: its value (secrets only as `set` or `not set`), where it
  comes from (`env`, `.env:<line>` or `default`) and what uses it.
- `awa env set NAME` changes one setting in `.env`. A secret is typed hidden or read with
  `--stdin`.

Every setting is in the [Settings reference](../reference/settings.md).

## Common mistakes

| You see | Fix |
| --- | --- |
| `credentials ABSENT` in `awa doctor` | The key is not in the `.env` of the folder you run awa in; run `awa setup` there. |
| Exit code 3 | A value would be replaced without a terminal: add `--yes`, or run it in a terminal. |
| Langfuse in Docker does not start | Give Docker at least 4 GB of memory. |

<!-- guide:do cd . -->

!!! example "In code"

    `awa.setup()` is the same in a notebook or a script: without arguments it reports the state of every section and
    changes nothing; what you pass, such as `awa.setup(provider="openai", api_key=key)`, is written to `.env`.

    <!-- guide:python -->
    ```python
    import awa

    plan = awa.setup()
    for section in plan.status():
        print(section.section, section.state, section.detail)
    ```

    Before a key is set, it prints:

    <!-- guide:expect -->
    ```text
    provider missing no model key is set
    test missing set a key first
    database set local files next to the blueprint (SQLite and JSON)
    tracing set off
    ```
