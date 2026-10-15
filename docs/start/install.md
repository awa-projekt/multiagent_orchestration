# Install

This page gets awa ready on your computer in three steps: install it, connect one model
provider, and check the result with `awa doctor`. It usually takes a few minutes. Then you can run your
first process.

## 1. Install awa

Pick the row that matches what you have:

| You have | Do this | You get |
| --- | --- | --- |
| Python 3.11 or newer | `pip install "awa-agents[all] @ git+https://github.com/awa-projekt/multiagent_orchestration"` | the `awa` command and the Python library in your project's environment |
| Python 3.11 or newer and the awa folder | `pip install ".[all]"` in the awa folder ([details](#with-pip-or-uv)) | the same |
| Nothing installed | the [double-click installer](#double-click-installer) in the awa folder's `scripts/` | awa with its own Python, a desktop shortcut, the web app opens |
| A wheel file only | `pip install "<the wheel file>[all]"` | the same, without `docs/`, `examples/` and `tests/` |

awa is not on a package index. It installs from its GitHub repository or from a copy you have.

!!! warning "Windows: keep the install folder short"
    Windows refuses file paths longer than 260 characters unless long paths are enabled, and some
    of awa's dependencies have long file names. In a deep folder, every `awa` command fails with
    an `ImportError` (often from `langsmith`) that does not say why. pip may also stop with
    `No such file or directory` while it installs.

    This applies to the virtual environment too. Put it in a short folder such as `C:\awa\.venv`.
    Or enable long paths once as administrator (`New-ItemProperty -Path
    HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1
    -PropertyType DWORD -Force` in PowerShell), then sign in again. `awa doctor` warns about a
    deep install folder.

## 2. Connect a model { #connect-a-model }

awa needs one model provider. In a terminal, `awa setup` asks for the provider and the key and
writes them into the project's `.env` file. It never prints the key. In the web app, the first
page, **Set up**, asks the same.

The providers are Anthropic, OpenAI, Azure OpenAI, Google Gemini, Mistral, Groq and Ollama.
Ollama runs a model on your computer and needs no key. You can also write the `.env` file
yourself:

```text
ANTHROPIC_API_KEY=...
```

Every provider's settings: [Models](../develop/models.md). More about `awa setup`:
[Set up a project](../guide/setup.md).

## 3. Check the installation

<!-- guide:run exit=78 -->
```console
$ awa doctor
awa doctor
...
Result: usable...
doctor checks this machine and the settings only; `--check-models` calls the models, `awa check` Microsoft 365.
```

`awa doctor` prints one line per check:

- `OK`: the check passed.
- `WARN`: something you will need later.
- `FAIL`: this blocks awa. The line under it says how to fix it.

Without a key, it ends with a warning and exit code 78. Once the key is set, it ends with
`Result: usable.` and exit code 0. It calls a model only when you add `--check-models`.

You are ready. [First automation](first-automation.md) builds, tests and runs a first process.

## With pip or uv

In the awa folder, with Python 3.11 or newer, make a virtual environment, activate it and install:

```console
python -m venv .venv
.venv\Scripts\activate
python -m pip install ".[all]"
awa ui
```

- On macOS and Linux, the second line is `source .venv/bin/activate`.
- With uv: run `uv venv`, activate it the same way, then `uv pip install ".[all]"`. Or skip the
  activation and put `uv run` in front of each command (`uv run awa ui`).
- The `awa` command exists only in a terminal where the environment is active.
- The first command after the install takes up to a minute while Python compiles awa.
  `uv pip install --compile-bytecode` does that during the install.

The distribution is called `awa-agents`; the command and the Python import are `awa`.

`[all]` adds the Anthropic and OpenAI providers, the web app and HTTP server, MCP, PostgreSQL,
OpenTelemetry, Langfuse, the SQL drivers, the PDF, Word and Excel readers and the certificate
sign-in of Microsoft 365. Smaller installs and every extra:
[Quickstart, extras](../develop/quickstart.md#extras).

Google Gemini, Mistral, Groq and Ollama need their LangChain package as well:
`pip install langchain-google-genai` (or `langchain-mistralai`, `langchain-groq`,
`langchain-ollama`).

## Double-click installer

Use the installer when you have no Python yet. It brings its own.

| System | Installer | If the system refuses it |
| --- | --- | --- |
| Windows | `install-windows.cmd` | "Windows protected your PC": choose **More info**, then **Run anyway** |
| macOS | `install-macos.command` | right-click the file, choose **Open** and confirm |
| Linux | `bash scripts/install-linux.sh` in the awa folder | - |

1. Start the installer for your system from the awa folder's `scripts/`. When you received only
   a wheel, put the installer next to the `.whl` file first; it then installs that wheel.
2. Answer **Y**.
3. The installer sets up [uv](https://docs.astral.sh/uv/) and a private Python 3.13 and installs
   awa with all extras.
4. It puts an **awa** shortcut on your desktop (and in the Start menu, `~/Applications` or the
   applications menu) and opens the web app in your browser.

The installer changes only folders in your own user profile. It never changes your `PATH`, your
shell profile or a system setting. Your projects go to `Documents/awa`.

To update, run the installer of the newer awa folder or wheel. Your projects and settings stay.

The options most people need:

| Option | Effect |
| --- | --- |
| `--yes` | do not ask before installing or uninstalling |
| `--home DIR` | install awa into this folder instead of the default one |
| `--project DIR` | the folder the web app opens (default `Documents/awa`) |
| `--no-launch` | do not open the web app at the end |
| `--uninstall` | remove awa and its shortcuts; your project folder stays |

`--help` lists every option, for example for shortcuts, the uv program and the dependency
versions. Behind a company proxy, set `HTTPS_PROXY` first. For a company package mirror, set
`UV_INDEX_URL`.

??? note "What the installer downloads and what is verified"
    awa itself is never downloaded. From the awa folder, the dependencies are installed exactly
    as locked in `uv.lock`, each checked by hash. From a wheel there is no lock, so the newest
    versions within awa's ranges are installed.

    Python is fetched by uv, which checks it against hashes it knows. uv is downloaded as the
    install script of a fixed release over TLS from astral.sh; that script checks the uv archive
    against a checksum it contains. The script itself is verified only when you pass
    `--uv-sha256`. For a fully controlled install, put an approved uv on the machine and pass
    `--uv`.

    The installer's default folder is short, and it warns when `--home` is deeper than 90
    characters. It refuses a `--home` folder that already holds files it did not make.

### Use awa in a terminal

The shortcut needs nothing more. A terminal needs the installer's environment on its `PATH`.
Run this once per terminal window (the installer prints the same line for the folder it used):

| System | Line |
| --- | --- |
| Windows (PowerShell) | `$env:PATH = "$env:LOCALAPPDATA\awa\env\Scripts;$env:PATH"` |
| macOS | `export PATH="$HOME/Library/Application Support/awa/env/bin:$PATH"` |
| Linux | `export PATH="$HOME/.local/share/awa/env/bin:$PATH"` |

## If something goes wrong

| You see | Do this |
| --- | --- |
| `awa` is not found | Use the **awa** shortcut, or run the `PATH` line [above](#use-awa-in-a-terminal) in this terminal. After an install with pip or uv, activate the environment ([above](#with-pip-or-uv)) or use `uv run awa` |
| An `ImportError` on Windows | The install folder is too deep: reinstall into a short folder, see the warning [above](#1-install-awa) |
| `FAIL python` | awa needs Python 3.11 or newer; the double-click installer brings its own |
| The browser says *Open the link from the awa window* | Copy the link that `awa ui` printed into the address bar |

More: [Troubleshooting](../guide/troubleshooting.md).

## Next

[First automation](first-automation.md) builds, tests and runs a first process.
