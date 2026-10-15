# Troubleshooting

Use this page when something does not work. Find the message you see, then apply the fix next to it.

When you do not know where to start, go in this order:

1. Run `awa doctor`. It checks your setup. Do the `fix:` line under every `FAIL` or `WARN`.
2. When every line says `OK`, run `awa validate blueprint.yaml`. If it prints `error:`, fix the line it names.
3. When the blueprint is valid, run the process, then read what it did with `awa runs trace RUN`
   ([Keep it running](operate.md#read-a-trace)).

A few things help with every message:

- Run commands in the project folder, the one with `blueprint.yaml` and `.env`.
- Every error starts with `error:` and ends with what to do. Read it to its end.
- `awa --verbose` before a command prints the technical details for your IT team.
- The messages below are copied from awa. Your operating system words its own messages in your language.

## What `awa doctor` tells you

| Line | Meaning | What to do |
| --- | --- | --- |
| `OK` | The check passed. | Nothing. |
| `INFO` | A fact, nothing is wrong. | Read it when something else fails. |
| `WARN` | Something you need for real runs is missing, such as a key. | Do the `fix:` line under it. |
| `FAIL` | Something blocks use: an old Python, a missing package, settings that do not validate. | Do the `fix:` line; the result is `NOT usable`. |

The last line says `Result: usable`, with warnings, or `NOT usable`. The exit code is 0, 78 (a required part is
missing) or 1.

## Setup and keys

| You see | Why | Fix |
| --- | --- | --- |
| `'awa' is not recognized` | The terminal does not know where awa is | See [`awa` is not found](#awa-is-not-found). |
| `credentials ABSENT`, or an authentication error | No key in the folder you work in | See [There is no model key](#there-is-no-model-key). |
| `Cannot create chat model`, or a model not found | The model name is wrong | See [The model name is wrong](#the-model-name-is-wrong). |
| The browser says *Open the link from the awa window* | The link carries a one-time code | Copy the link that `awa ui` printed into the address bar. |
| `error while attempting to bind on address` (`address already in use`) | Another program uses the port | `awa serve ... --port 8001`, or stop the other program. `awa ui` picks a free port by itself. |
| `PermissionError: [WinError 5] Access is denied` | A protected folder such as `C:\Windows` | Work in your own folders, with short paths; quote paths with spaces. |
| `unrecognized arguments` | A text with spaces is not in quotes | See [Quotes around the text](#quotes-around-the-text). |

### There is no model key

```text
WARN  credentials:anthropic  credentials ABSENT (profiles default, fast, strong); real model calls will fail, offline tests still work
                             fix: set ANTHROPIC_API_KEY in .env or the environment
```

Every run calls an AI model, and the provider needs your key.

- **In the web app:** open **Set up** and paste the key.
- **In a terminal:** put one line `ANTHROPIC_API_KEY=...` (the setting of your provider) into `.env` in the project
  folder. awa reads `.env` only from the folder you run it in. `awa doctor` says `no .env in <folder>` when it is
  not there.

Without a key, `awa validate`, `awa show` and the offline tests of a project still work.

### The model name is wrong

```text
error: Cannot create chat model 'anthropc:<model>': Unable to infer model provider for model='anthropc:<model>'. ...
```

A model is written `provider:model-name`. Copy the model name from your provider's list. Set it as
`AWA_DEFAULT_MODEL` in `.env`, or on **Set up**. `awa doctor` prints the default model it will use. A provider that
does not know the model answers `HTTP 404, model not found`.

### `awa` is not found

The command lives in the environment the install created.

- **After the double-click installer:** use the **awa** shortcut, or run the `PATH` line the installer printed, once
  per terminal window.
- **After a manual install:** activate the environment (`.venv\Scripts\Activate.ps1` in PowerShell), or call
  `python -m awa`.
- **PowerShell says that running scripts is disabled:** run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
  once.

[Install](../start/install.md) has the details.

### Quotes around the text

```text
awa: error: unrecognized arguments: is order 1001
```

The shell splits text at spaces. Put the whole text in double quotes: `awa run blueprint.yaml "Where is order 1001?"`.

Windows PowerShell 5.1 removes double quotes inside an argument. So put JSON values (`--args`, `--payload`,
`--edit-args`, `--deps`) into a file and give it as `"@file.json"`; that works in every shell. A long description is
easier to enter on the page **Describe** of the web app.

## Building and blueprints

| You see | Why | Fix |
| --- | --- | --- |
| `target 100% not reached (no rounds left)` | The design did not pass every case in its rounds (three by default) | Read the failing cases, add the detail to the description, build again ([Describe a process](describe.md)); `--rounds N` allows more rounds. |
| `target 100% not reached (stopped: model-call limit reached)` | The build used up `--max-requests` | Raise `--max-requests`, or describe a smaller process. |
| `target 100% not reached (the architect could not repair the design)` | The repair produced no valid design | Make the rule that fails explicit in the description, with its numbers, and build again. |
| `error:` lines from `awa validate` | A name, a tool or a condition is wrong | Fix the first line it names; `agents[2]` is the third agent. |
| `error: Blueprint file not found` | The path is wrong | Check the folder you are in. |
| `unknown tool 'NAME'` | The tool is not in `tools.py` next to the blueprint | Add it there, or copy the name from `awa show`. |
| `read_csv needs toolkits.data.root (or toolkits.files.root) to be configured` | A file tool without its folder | Add `toolkits: {files: {root: ./data}}` ([Your files](files.md#the-tools-agents-read-files-with)). |
| `Tools module '...tools.py' defines no tools` | `tools.py` beside the blueprint is empty, or holds no `@tool` function and no documented function listed in `__all__` | Decorate each tool with `@tool`, or list documented functions in `__all__`; or delete the empty file. |
| A condition reads a name the tool does not have | A typo in `when:` | Use a name from the list the message gives ([Approvals](approvals.md)). |
| An approval never asks | The condition is false, or the tool name differs | Test the boundary values; copy the tool name from `awa show`. |
| `... already holds a project (blueprint.yaml)` | The folder has a system already | Add `--force`, or choose another `--out`. |

## Files and connections

| You see | Why | Fix |
| --- | --- | --- |
| A card says *Could not be read* | The file is damaged, or not what its ending says | Open it yourself; bring a good copy. `awa data inspect FILE` shows what awa sees. |
| *Looks like a scan*, or `[no text layer: ...]` | A scanned PDF holds pictures, no text | Read it through the `workspace` connector, which reads scans with a vision model ([Connect systems](connect.md)); or hold scans for manual entry, or bring a text version. |
| HTTP `415` `awa.data.type_mismatch` | The content is not what the ending says | Bring the real file. |
| `awa build` ends with exit code 2 and `"status": "needs_input"` | The description does not fit the files | Answer with `--answer "q1=..."`, or fix the description or the file ([Your files](files.md#when-the-words-do-not-fit-the-files)). |
| `"status": "blocked"`, exit code 1 | No file can be read | Bring readable files. |
| `column 'po' does not exist ... [DF-V-003]` | A file changed | Restore the file, change `expect:`, or build again ([Your files](files.md#check-that-the-files-still-fit)). |
| `Error: path ... must be relative to the allowed directory`, `must not contain '..'`, `names a hidden file` | The file tools work in one folder only | Put the file inside the folder and name it relative to it, with its extension; follow a `did you mean` hint. |
| `status: missing NAME` | A setting is not in `.env` | Enter it with `awa connect`, on **Connect**, or in `.env` ([Connect systems](connect.md)). |
| `The connection test failed` | A wrong address or key, or the system refuses | Check both; ask for a read-only login; an external system may need the VPN. |
| `plain http:// is only allowed for the local machine` | A web tool goes only to `https://` | Use the `https://` address. |
| `requests to internal networks are blocked` | The system is inside your company network | Set `allow_private_networks: true` on its connector. |
| `host '...' is not in the allowed hosts` | The address is not the system's own | Add it to `allowed_hosts` of the connector. |

## In the web app

The web app says in plain words what happened and what to do. The technical message is under **Details**.

| You see | Why | Fix |
| --- | --- | --- |
| *No key for the AI model is set up yet* | No key in the project's `.env` | Enter it on **Set up**. |
| *Your AI provider did not accept the key* | A wrong key, or a key of another resource | Check the key and, for Azure OpenAI, the endpoint on **Set up**. |
| *The AI service does not know this model* | A wrong model name | Check the model name (for Azure the deployment name) on **Set up**. |
| *Your key is not allowed to use this model* | The provider refuses this model for your key | Ask your administrator, or choose another model on **Set up**. |
| *The AI service is busy right now (too many requests)* | The provider's rate limit | Wait a minute; if it keeps happening, ask for a higher limit. |
| *That was too much text for the model at once* | The message or the files are too long for the model | Use a shorter message or smaller files. |
| *Not saved: fix the problems marked Must fix first* | The edited blueprint has errors | Fix each problem on **Review**, then **Check and save**. |
| *Nothing is running that would continue it* | An approval was answered, but no worker runs | Press **Continue it now**, or start `awa worker` (**Use it**). |
| *awa is not reachable* | The terminal of `awa ui` was closed or stopped | Start `awa ui` again and open the link it prints. |

## Tests

| You see | Why | Fix |
| --- | --- | --- |
| A case fails | The answer, the rule or the case is wrong | Read `Failures`, then fix the right one ([Test and improve](test.md)). |
| Cases pass on sample data and fail on real data | The expected values are the sample's | Change `expected` to your real values. |
| `Errors: 1` or **Could not run** | The run could not finish | The message names the key, the limit or the connection. |
| `pass^3` is lower than `pass@1` | A case is unreliable | Look at the cases that pass only some of the time. |

## Runs that a trigger starts

| You see | Why | Fix |
| --- | --- | --- |
| A run is `waiting`, or `approve? [y/N]` | An approval waits for a person | Answer it on **Approvals**, or with `awa runs decide RUN --approve` ([Approvals](approvals.md)). |
| `awa run` ends with exit code 2 | Nobody could answer the approval: no terminal | Run it in a terminal, or use the worker and `awa runs decide`. |
| Nothing starts | The worker is not running, or the folder or schedule is not what you think | Start it; `awa trigger list` shows the triggers ([Run automatically](run.md)). |
| A run stays `queued` | No worker is running | `awa worker blueprint.yaml`. |
| `Another awa process ... already runs the triggers` | Two programs run the triggers of one folder | Close one, or start the second with `--no-triggers`. |
| `No process run ... in .awa\awa.db` | You are in the wrong folder | Go to the project folder, or pass `--store`. |
| `Usage limit exceeded` | A limit was reached | Raise it if the process needs it ([Keep it running](operate.md#limits-and-costs)). |
| `checkpoint lost` | The saved state of a waiting run is gone | `awa runs retry RUN --restart`. |
| `401 Webhook verification failed` | The secret or the scheme does not match | The server log says why; check the setting named by `secret_env`. |
| `401 Missing or invalid API key` | The key is missing from the request | Send `Authorization: Bearer KEY`. |

## Errors in a run

Every deliberate error of awa carries a stable `code`: in Python as `error.code`, and in the HTTP API as `error.code`
in the error body. These are the ones you meet most:

| Message (code) | Why | Fix |
| --- | --- | --- |
| `Usage limit exceeded: requests=3 > max_requests=2` (`awa.usage_limit_exceeded`) | The run tree reached a limit of `limits:` or a default ceiling | Raise the limit if the process needs it; a loop shows in the trace ([Keep it running](operate.md#limits-and-costs), [run limits](../reference/settings.md#run-limits-and-loop-protection)). |
| `Agent 'a' exceeded max_steps=2: ...` (`awa.max_steps_exceeded`) | One agent made more model calls than `max_steps` without answering | Raise `max_steps` of the agent, or make its instructions say when it is done. |
| `Run '...' of 'a' exceeded max_duration=...` (`awa.run_timeout`) | The run took longer than its time limit | Raise `max_duration`, or find the slow step in the trace. |
| `Cannot create chat model '...'` (`awa.model_resolution`) | A model name awa cannot resolve, or real model requests are blocked in tests | See [The model name is wrong](#the-model-name-is-wrong). |
| `Blueprint file not found: '...'` (`awa.blueprint`) | The path is wrong | Run the command in the project folder, or give the full path. |
| `PDF text extraction needs pypdf; install it with ...` (`awa.optional_dependency`) | A part of awa needs a package of an extra that is not installed | Run the install command the message prints; `awa doctor` lists the extras. |

[Exceptions](../reference/exceptions.md) lists every error, its code and whether a retry can help.

## A run went wrong

| You see | Fix |
| --- | --- |
| A wrong answer | Run it again with `--trace FILE --trace-level full`, then `awa runs trace FILE` ([Keep it running](operate.md#read-a-trace)). |
| A run costs too much or takes long | Compare the brackets of the agents in the trace: one loops, or one step is slow. |
| An old run, or a thread that should start new | `/reset` in `awa chat`, **New thread** in the web app, or `awa runs cancel RUN`. |
| You want a clean start | Stop `awa serve`, `awa worker` and `awa ui`, then delete the folder `.awa`. Waiting runs are lost; the blueprint, the tests and the data stay. |

<!-- guide:do cd . -->

!!! example "In code"

    Every error awa raises on purpose derives from `awa.AwaError` and carries a stable `code`, so a program can tell
    them apart ([Exceptions](../reference/exceptions.md)):

    <!-- guide:python -->
    ```python
    import awa

    try:
        awa.load("nothing.yaml")
    except awa.AwaError as error:
        print(type(error).__name__, error.code)
    ```

    <!-- guide:expect -->
    ```text
    BlueprintError awa.blueprint
    ```
