# Models

Every agent needs a chat model. You usually set one provider key and name a profile such as
`"fast"` or `"strong"`; awa picks the model behind it. Read on when you change the provider, add
retries and fallbacks, or cut cost with caching.

To use a real model, set the key of your provider in the environment or in `.env`
([Providers](#providers)), then run `awa doctor --check-models`. An agent's `model` is one of:

| Value | Meaning |
| --- | --- |
| `None` (default) | The `default` profile. |
| `"fast"`, `"strong"`, or a profile you registered | A named profile of the `ModelRegistry`. |
| `"provider:model"` | Passed to LangChain's `init_chat_model`: the provider, a colon, the provider's own id ([Current models](#current-models)). |
| a `BaseChatModel` instance | Used as it is. |

Nothing is created until the first run, so defining agents never needs credentials.

## Profiles

In your agents, name a model by its role: `"fast"`, `"strong"`, or nothing for `default`. The
settings decide which model plays the role:

| Profile | Setting | Default |
| --- | --- | --- |
| `default` | `AWA_DEFAULT_MODEL` | `anthropic:claude-sonnet-5-5` |
| `fast` | `AWA_FAST_MODEL` | `anthropic:claude-haiku-5-5` |
| `strong` | `AWA_STRONG_MODEL` | `anthropic:claude-opus-5-5` |

Register your own profiles, with construction settings or a factory:

```python title="profiles.py"
from awa import Agent, ModelSettings
from awa.models import ModelRegistry
from awa.testing import FakeChatModel

models = ModelRegistry()  # starts with default, fast and strong from the settings
models.register("extraction", "anthropic:claude-haiku-5-5", settings=ModelSettings(temperature=0, max_tokens=1_024))
models.register("local", lambda: FakeChatModel(["ok"]))  # a factory, called once

extractor = Agent("extractor", model="extraction", model_registry=models)  # nothing is created yet
local = Agent("local_agent", model="local", model_registry=models)
print(local.run_sync("hi").output)
print(sorted(models.profiles()))
```

```text title="profiles.out"
ok
['default', 'extraction', 'fast', 'local', 'strong']
```

- `ModelRegistry.default()` is the process-wide registry. Agents use it when you give none.
- `ModelSettings` has the fields `temperature`, `max_tokens`, `timeout`, `max_retries` and `extra`
  (provider keyword arguments). Unset fields are not sent. Its resilience fields are
  [below](#resilience).
- `Agent(model=..., model_settings=ModelSettings(...))` applies settings to one agent.

### Effort

`ModelSettings(effort=...)` sets how deeply a model reasons: `low`, `medium`, `high`, `xhigh` or
`max`.

- Where the model takes a reasoning effort (current Claude models, OpenAI reasoning models), awa
  sends it as that. On OpenAI reasoning models, `xhigh` and `max` become `high`.
- OpenAI and Azure OpenAI accept an effort together with tools only on the Responses API. It is
  opt-in: per model with `extra={"use_responses_api": True}`, or for every reasoning model with
  an effort with `AWA_OPENAI_RESPONSES_API=true`.
- On a model that takes a temperature instead, an effort pins `temperature=0` unless you set a
  temperature.
- `temperature`, `top_p` and `top_k` are never sent to models that reject them.

In a blueprint, an agent without an effort and without a temperature gets an effort by its role:
`low` for a judge or reviewer, `medium` for a worker or an agent without a role. An orchestrator
keeps the provider default. Set the effort with `model_settings`, in `defaults` or per agent:

```yaml
defaults:
  model: default
  model_settings: {effort: medium}
agents:
  - name: classifier
    model: fast
    model_settings: {effort: low}
```

## Providers

awa reads keys from the environment and from a `.env` file in the working directory. It passes
the key of every provider below to the model itself, so a key that exists only in `.env` works.
Keys are held as `SecretStr`, which masks them in `repr`, logs and error messages.

| Provider | Install | Settings |
| --- | --- | --- |
| Anthropic (default) | extra `anthropic` | `ANTHROPIC_API_KEY` |
| OpenAI | extra `openai` | `OPENAI_API_KEY` |
| Azure OpenAI | extra `openai` | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT_NAME` |
| Google Gemini | `pip install langchain-google-genai` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` |
| Mistral AI | `pip install langchain-mistralai` | `MISTRAL_API_KEY` |
| Groq | `pip install langchain-groq` | `GROQ_API_KEY` |
| Ollama | `pip install langchain-ollama` | `OLLAMA_HOST` (no key) |
| Any other `init_chat_model` provider | its LangChain integration | its own variables in the process environment, `AWA_DEFAULT_MODEL=<provider>:<model>` |

The `default`, `fast` and `strong` profiles you leave unset follow the first provider in this table
whose settings are complete. To choose a model yourself, set `AWA_DEFAULT_MODEL=openai:<model>` (or
`azure_openai:<deployment>`, `google_genai:<model>`, ...).

An OpenAI-compatible local server or gateway works with the `openai:` provider and
`OPENAI_BASE_URL` (for example `http://127.0.0.1:11434/v1`). Set `OPENAI_API_KEY` to any value the
server accepts.

### Azure OpenAI

The deployment name is the name you gave the model when you deployed it, not the model's own name.
awa uses the v1 API (`<endpoint>/openai/v1/`), which needs no API version. When
`OPENAI_API_VERSION` is set, awa uses the dated API of the Azure SDK instead.

To sign in with Microsoft Entra ID instead of a key (for resources with keys switched off):

1. Give an app registration the role **Cognitive Services OpenAI User** on the resource.
2. Replace the key with:

```text
AZURE_OPENAI_AUTH=entra
AZURE_TENANT_ID=<directory (tenant) id>
AZURE_CLIENT_ID=<application (client) id>
AZURE_CLIENT_SECRET=<secret value>
```

awa fetches and renews the token itself. When Azure refuses a parameter as unsupported (a
temperature, a token limit), awa leaves it out, repeats the request and remembers that for the
deployment. Errors carry a one-line hint:

| Azure says | Usually |
| --- | --- |
| 401 | The key is wrong or belongs to another resource. |
| 403 "key based authentication is disabled" | Use Entra ID (`AZURE_OPENAI_AUTH=entra`). |
| 403 `PermissionDenied` with Entra | The app lacks the role on the resource. |
| 404 `DeploymentNotFound` | Use the deployment name, not the model name. |
| 429 | The deployment's quota is used up: wait or raise it. |

`awa doctor --check-models` makes one tiny real call per profile and says what fails.

## Current models

Model ids change every few months. Every concrete id in the docs and the examples must be in this
table: `tests/unit/test_model_names.py` fails when a page, a docstring or an example names another
id.

<!-- current-models:start -->
| Provider (prefix) | Role | Id | Checked on | Source |
| --- | --- | --- | --- | --- |
| Anthropic (`anthropic:`) | default | `claude-sonnet-5-5` | 2026-10-09 | [Claude models overview](https://platform.claude.com/docs/en/models/overview) |
| Anthropic (`anthropic:`) | fast | `claude-haiku-5-5` | 2026-10-09 | [Claude models overview](https://platform.claude.com/docs/en/models/overview) |
| Anthropic (`anthropic:`) | strong | `claude-opus-5-5` | 2026-10-09 | [Claude models overview](https://platform.claude.com/docs/en/models/overview) |
| Anthropic (`anthropic:`) | strongest, optional | `claude-fable-5-1` | 2026-10-09 | [Claude models overview](https://platform.claude.com/docs/en/models/overview) |
| OpenAI (`openai:`), Azure OpenAI (`azure_openai:`) | default | `gpt-6.1-sol` | 2026-10-09 | [OpenAI models](https://developers.openai.com/api/docs/models), [Azure OpenAI models](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure) |
| OpenAI (`openai:`), Azure OpenAI (`azure_openai:`) | fast | `gpt-6-luna` | 2026-10-09 | [OpenAI models](https://developers.openai.com/api/docs/models), [Azure OpenAI models](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure) |
| OpenAI (`openai:`), Azure OpenAI (`azure_openai:`) | strong | `gpt-6-astra` | 2026-10-09 | [OpenAI models](https://developers.openai.com/api/docs/models), [Azure OpenAI models](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure) |
| Google Gemini (`google_genai:`) | default | `gemini-3.8-flash` | 2026-10-09 | [Gemini API models](https://ai.google.dev/gemini-api/docs/models) |
| Google Gemini (`google_genai:`) | fast | `gemini-3.5-flash-lite` | 2026-10-09 | [Gemini API models](https://ai.google.dev/gemini-api/docs/models) |
| Google Gemini (`google_genai:`) | strong (preview) | `gemini-3.1-pro-preview` | 2026-10-09 | [Gemini API models](https://ai.google.dev/gemini-api/docs/models) |
| Mistral AI (`mistralai:`) | default | `mistral-medium-3-5-26-04` | 2026-10-09 | [Mistral models](https://docs.mistral.ai/getting-started/models/) |
| Mistral AI (`mistralai:`) | fast | `mistral-small-4-0-26-03` | 2026-10-09 | [Mistral models](https://docs.mistral.ai/getting-started/models/) |
| Mistral AI (`mistralai:`) | strong | `mistral-large-4` | 2026-10-09 | [Mistral models](https://docs.mistral.ai/getting-started/models/) |
| Groq (`groq:`) | default | `llama-3.3-70b-versatile` | 2026-10-09 | [Groq models](https://console.groq.com/docs/models) |
| Groq (`groq:`) | fast | `llama-3.1-8b-instant` | 2026-10-09 | [Groq models](https://console.groq.com/docs/models) |
| Groq (`groq:`) | strong | `llama-3.3-70b-versatile` | 2026-10-09 | [Groq models](https://console.groq.com/docs/models) |
| Ollama (`ollama:`, tag `:20b`) | default | `gpt-oss` | 2026-10-09 | [Ollama library](https://ollama.com/library/gpt-oss) |
| Ollama (`ollama:`, tag `:20b`) | fast | `gpt-oss` | 2026-10-09 | [Ollama library](https://ollama.com/library/gpt-oss) |
| Ollama (`ollama:`, tag `:20b`) | strong | `gpt-oss` | 2026-10-09 | [Ollama library](https://ollama.com/library/gpt-oss) |
<!-- current-models:end -->

Write a model as `provider:id`; the id is the provider's own. Azure OpenAI uses the OpenAI ids for
the model, and your own name for the deployment.

To refresh the table:

1. Open the linked pages.
2. Change the `Id` and `Checked on` cells. When the Anthropic ids change, also change the defaults
   in `awa.core.settings.Settings`.
3. Run `pytest tests/unit/test_model_names.py`.

## Resilience

Providers rate-limit, time out and fail. Three `ModelSettings` fields let an agent's model calls
survive that, in every team, network and workflow the agent runs in:

```python title="resilience.py"
from awa import Agent
from awa.models import CircuitBreaker, ModelRetryPolicy, ModelSettings

resilient = ModelSettings(
    retry=ModelRetryPolicy(max_attempts=3, initial_delay=1.0, max_delay=30.0),
    fallbacks=("fast",),
    circuit_breaker=CircuitBreaker(failure_threshold=5, reset_after=30.0),
)
support = Agent("support", model="strong", model_settings=resilient)
print(support.name, resilient.fallbacks)
```

```text title="resilience.out"
support ('fast',)
```

- **`retry`** repeats a failed call with exponential backoff and jitter. By default only transient
  failures are retried: 429, 5xx, timeouts and connection failures. A `Retry-After` longer than
  `max_delay` moves on to the next fallback instead. Clients built from a string then get
  `max_retries=0`, so no attempt hides inside the provider SDK.
- **`fallbacks`** are tried in order when a model fails after its retries: profiles or
  `provider:model` strings.
- **`circuit_breaker`** skips a model after `failure_threshold` transient failures in a row, for
  `reset_after` seconds; then one trial call decides. The state is shared per model by every agent
  of the process.

- Every attempt is a model request. It passes the request guard and counts toward
  `UsageLimits.max_requests`, but not toward `max_steps`.
- Usage limits, `max_steps`, blocked requests and cancellation are never retried.
- Each retry emits a `ModelRetryEvent`, each fallback a `ModelFallbackEvent`.
- When every model failed, awa raises `ModelProviderError` (or `ModelRateLimitError`) with `kind`,
  `provider`, `model`, `status` and `retryable`. Keys are scrubbed from the message.
  `classify_model_error(exc)` does the same translation for your own code.
- Text streamed by an attempt that later fails is not taken back.

## Prompt caching and result caches

Four caches cut cost and time. You switch them in one block of the blueprint's `defaults`. Every
value also has a variable that wins over the blueprint (`AWA_CACHE_ENABLED`, `AWA_CACHE_PROMPT`,
`AWA_CACHE_PROMPT_PAD`, `AWA_CACHE_TOOLS`, `AWA_CACHE_TOOLS_TTL`, `AWA_CACHE_STEPS`,
`AWA_CACHE_STORE`, `AWA_CACHE_PATH`):

```yaml
defaults:
  cache:
    enabled: true        # master switch
    prompt: auto         # auto, 1h or off
    prompt_pad: true     # pad an OpenAI or Azure prefix just short of 1,024 tokens
    tools: true          # reuse results of read-only tools
    tools_ttl: null      # seconds across runs; null keeps a result for one run
    steps: true          # reuse a process step's documents check
    store: memory        # memory or sqlite
    path: null           # sqlite file; .awa/cache.db next to the blueprint
```

| Cache | What it saves | Rules |
| --- | --- | --- |
| Provider prompt cache | Input cost and time of the repeated start of a prompt | Claude gets cache markers (with `auto` only when a prefix repeats; `1h` for runs that wait for approvals). OpenAI and Azure cache by themselves from 1,024 tokens; awa sends a stable `prompt_cache_key` and, with `prompt_pad`, pads a prefix just below the minimum. Gemini caches implicitly. |
| Read-only tool results | Repeated lookups and document parsing in a run | Only tools marked read-only and not behind an approval. A tool that acts sets earlier results aside. Errors are never cached. |
| Process steps | The documents check of a process step | Reused while the files are unchanged (size, time and SHA-256). |
| Replay | Every model call of an unchanged evaluation | Only with `awa eval --replay`, and only on stand-in tools. |

- Tools are sent sorted by name, and system text with normalized line ends, so the prefix stays
  the same.
- Each model call records a prefix hash (`ModelResponseEvent.prefix_hash`). A dynamic instruction
  such as a date shows up as more than one hash.
- `awa cache salt` writes a random `AWA_CACHE_SALT` into `.env`, so others cannot recompute the
  OpenAI keys. `awa cache stats` and `awa cache clear` show and delete entries.
- A broken cache store never breaks a run: a failure counts as a miss.
- In code: `Agent(prompt_cache="1h")`, or `Agent(cache=Cache(CacheSettings(...), store))`.

## Blocking real requests

`allow_model_requests(False)` (or `set_allow_model_requests(False)`) blocks every real provider
model in the process. Offline models stay allowed. A blocked model raises `ModelResolutionError`
before any request is made:

```python title="blocked.py"
from awa import Agent
from awa.errors import ModelResolutionError
from awa.models import allow_model_requests
from awa.testing import FakeChatModel

with allow_model_requests(False):
    offline = Agent("offline", model=FakeChatModel(["fine"]))
    print(offline.run_sync("hi").output)  # offline models stay allowed
    try:
        Agent("real").run_sync("hi")  # the default profile is a real provider
    except ModelResolutionError as error:
        print(error)
```

```text title="blocked.out"
fine
Real model requests are blocked (set_allow_model_requests(False)); refusing to create model 'default'
```

Test suites put this in an autouse fixture ([Testing](testing.md#block-real-requests)).

## Your files and the provider

A build from your own files sends the model a short, budgeted digest, not the files.
`awa data inspect <files> --model-view` shows exactly that text and calls no model.
`--mask placeholders` replaces personal data such as IBANs by stand-ins; `--mask drop` leaves out
every value ([Data](data.md)).

## What can go wrong

| You see | Cause and fix |
| --- | --- |
| `ModelProviderError: no API key for anthropic ...` | Set the key in the environment or `.env`; `awa doctor` shows which variables are missing. |
| `ModelResolutionError: Cannot create chat model '...'` | The provider's package is missing (install its extra) or the `provider:model` string is misspelled. |
| `ModelResolutionError: Real model requests are blocked ...` | `allow_model_requests(False)` is active. Give the agent an offline model. |
| `ModelRateLimitError` after several attempts | The provider keeps rate limiting. Add `retry`, `fallbacks` and a `circuit_breaker`, or lower the load. |

`resolve_model(model, settings=None, registry=None)` turns any reference above into a
`BaseChatModel` the way agents do, for graphs and `create_agent` agents you build yourself.
Reference: [Models](../reference/api/core.md#awa.models).
