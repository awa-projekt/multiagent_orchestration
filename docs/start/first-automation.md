# First automation

This page gets a first process running in about ten minutes. You create a support desk of three
[agents](../reference/glossary.md#agent) from a template, test it, try it with your model and let
a [trigger](../reference/glossary.md#trigger) start it when a file arrives.

**You need** awa installed ([Install](install.md)). Steps 1 to 3 need no model key. From step 4
on, you need the key of a model provider.

## 1. Create the project

A template writes a complete starting project: the [blueprint](../reference/glossary.md#blueprint)
(the file that describes the process), its tools, [eval cases](../reference/glossary.md#eval-case)
and offline tests.

<!-- guide:run -->
```console
$ awa init my-desk --template support
created my-desk (template support):
  blueprint.yaml
  tools.py
  tests/conftest.py
  tests/test_blueprint.py
  evals/cases.yaml
  .env.example
  .gitignore
  README.md
  pyproject.toml

next:
  cd my-desk
  pip install pytest         if it is missing
  pytest                     offline tests, no key
  awa setup                  set your provider's key in .env (or: awa ui, then Set up)
  awa chat blueprint.yaml    tools.py next to the blueprint is loaded by itself
```

| File | What it holds |
| --- | --- |
| `blueprint.yaml` | the agents, their tools, how they work together, approvals and limits |
| `tools.py` | the Python functions the agents call (`lookup_order`, `track_shipment`, `issue_refund`) |
| `evals/cases.yaml` | eval cases: an input and what the desk must do with it |
| `tests/` | offline tests with a scripted model |

The tools work on sample orders. For your own process, you replace them.

## 2. Read what you have

`awa validate` checks the blueprint. `awa show` explains it.

<!-- guide:run -->
```console
$ cd my-desk
$ awa validate blueprint.yaml
my-desk: valid
$ awa show blueprint.yaml
my-desk 0.1.0
Answers customer requests about orders, deliveries and refunds.
entrypoint: team

agent             model    tools                         skills               description
triage            default  -                             delegation-briefs    Receives every customer request, gets the facts and write...
order_specialist  default  lookup_order, track_shipment  -                    Looks up orders and shipments and reports their status.
refunds           default  lookup_order, issue_refund    safe-tool-execution  Handles refund requests end to end and talks to the custo...

team my-desk: orchestrator triage
  delegates: triage -> order_specialist, refunds
  handoffs: triage -> refunds
  handoffs: refunds -> triage
  shared: messaging

approvals: issue_refund
```

The desk is a [team](../reference/glossary.md#team):

- `triage` answers every request.
- It delegates order questions to `order_specialist`.
- It hands refund conversations to `refunds`.
- Every call of `issue_refund` waits for a person ([approval](../reference/glossary.md#approval)).

`awa show blueprint.yaml --mermaid` draws the same as a diagram.

## 3. Test it offline

```console
python -m pip install pytest
python -m pytest
```

The five tests run the desk with a scripted model: no key, no network. They take under a minute,
even on the first run, when Python compiles awa. They check the wiring (who delegates to whom,
that a refund waits for approval), not the quality of real answers. That is step 5.

`python -m` uses the Python that awa is installed in. In an environment made with `uv venv`,
install with `uv pip install pytest` instead.

## 4. Give awa your key

```console
awa setup
```

`awa setup` asks for the provider and the key and writes them into `my-desk/.env`. It never prints
the key. `awa doctor` then ends with `Result: usable.` Every provider:
[Install](install.md#connect-a-model).

## 5. Try it and test it

```console
awa chat blueprint.yaml
```

1. Write *Order 1001 arrived broken, I want my money back.*
2. The desk looks the order up and hands you to `refunds`.
3. It stops before `issue_refund`. Answer `y` to approve or `n` to refuse.
4. `/exit` ends the chat.

Then run the eval cases against your model:

```console
awa eval blueprint.yaml evals/cases.yaml
```

The report lists every case as passed or failed, and the pass rate. It ends with exit code 1 when
the rate is below the target. Each case checks what the desk did (delegated, called a tool, asked
for approval), not only what it said.

Models are not deterministic. Rerun the cases after every change and add cases from your own
requests. `--repetitions 3` repeats each case to measure
[pass^k](../reference/glossary.md#pass-k).

## 6. Let a trigger start it

A trigger starts a run on an event. This one starts a run for every text file dropped into the
folder `inbox`:

<!-- guide:run -->
```console
$ awa add trigger blueprint.yaml --kind file-drop --directory inbox --pattern "*.txt" --input "Answer the customer request in {payload.path}."
added trigger 'file-drop' to blueprint.yaml:
  - name: file-drop
    file_drop:
      directory: inbox
      pattern: '*.txt'
    input: Answer the customer request in {payload.path}.
...
```

Start the worker and drop a file into `inbox`:

```console
awa worker blueprint.yaml
```

- The worker runs until you stop it with Ctrl+C.
- Handled files move to `processed/`.
- A run that reaches `issue_refund` waits. Answer it with `awa runs decide RUN_ID --approve`, or
  on the **Approvals** page of the web app (`awa ui`).
- The blueprint's `limits:` cap every run the trigger starts
  ([limits](../reference/glossary.md#usage-limit)).

Your first process is running.

<!-- guide:do cd . -->

## Make it your own

The template works on sample orders. A process of your own takes longer than ten minutes, because
you adapt it to your data, systems and rules. Before it handles real requests:

1. Connect your own systems in place of the sample tools ([Connect systems](../guide/connect.md)).
2. Add eval cases from real requests and run them again.
3. Check the approvals and limits with the people responsible.

## Next

| You want to | Read |
| --- | --- |
| Do the same in the web app, from your own description and files, screen by screen | [The guide](../guide/tour.md) |
| Describe your own process instead of a template | [Describe a process](../guide/describe.md) |
| Write agents and teams in Python | [Quickstart](../develop/quickstart.md) |
