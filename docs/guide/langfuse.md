# Langfuse

Langfuse is an open-source tracing tool that you can run on your own computer. Connect awa to it, and you see every
run in the browser: each agent, model call, tool call and approval, with its time, tokens and cost.

For a single run, `awa runs trace` in the terminal is enough ([Keep it running](operate.md#read-a-trace)). Use
Langfuse when you want the history: compare runs, follow a thread over several runs, or show a colleague what an agent
did.

Four lines in the `.env` of a project folder switch it on:

```text
AWA_LANGFUSE_TRACING=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=http://localhost:3000
```

With them, `awa run`, `awa chat`, `awa eval`, `awa serve`, `awa worker` and `awa build` send each run to Langfuse.
awa needs no Langfuse package for it, only the `langfuse` extra of awa.

## Start Langfuse on your computer

Langfuse runs with Docker (Docker Desktop on Windows and macOS) and needs at least 4 GB of memory.

1. Make a new folder outside the awa folder. Download Langfuse's Docker Compose file into it; the command for your
   system is on the [Langfuse self-hosting page](https://langfuse.com/self-hosting/docker-compose).
2. In that folder, run awa's helper, then start Langfuse. The helper replaces the placeholder passwords and binds
   every port to this computer. The first start downloads the images and takes longer.

    ```console
    mkdir langfuse-local
    cd langfuse-local
    python <the awa folder>/examples/observability/langfuse_local.py
    docker compose up -d
    ```

3. The helper wrote two files. Keep both in that folder:
    - `.env` holds the passwords of the databases, your login and the project's keys;
    - `awa-langfuse.env` holds the four lines above, with your keys.
4. Copy the four lines of `awa-langfuse.env` into the `.env` of your project.
5. Check the connection. `OK langfuse server answered and accepted the keys` means it works:

    ```console
    awa doctor --check-langfuse
    ```

6. Run anything. Without a model key, `python <the awa folder>/examples/observability/trace_demo.py` sends a scripted
   team run.
7. Open `http://localhost:3000`, sign in and choose **Tracing**. The run is there a few seconds after it ends.

## What Langfuse receives

By default Langfuse receives names, ids, tokens, cost, timing, errors and the metadata of each run. It receives **no
prompt, answer or tool data**.

`AWA_LANGFUSE_CAPTURE_CONTENT=true` in the project's `.env` sends them too, as in the pictures below. Do that only
where the data may be stored in Langfuse. Credentials and personal data such as email addresses and IBANs are masked
before sending, but names and free text are not recognized as personal data.

## What you see

The pictures show a run of the invoice desk of the [tour](tour.md) for invoice INV-2042. It was sent with a scripted
model (`scripts/langfuse_showcase.py`) and with content capture on. The invoice checker reads the ledger, the payment
clerk's release waits for the head of finance, and the run continues after the approval.

**One run as a tree.** The workflow, both agents nested under it, and every model and tool call, with the run's time,
cost and tokens at the top:

![Langfuse showing the run of invoice INV-2042 as a tree: the workflow, the invoice checker and the payment clerk with their model and tool calls, and the run's time, cost and tokens](../assets/screenshots/tracing-tree.png)

**A model call.** The answer, the time, the tokens and the cost of one call. The full prompt of a call is not sent.
The input of each agent and every tool result are sent, so you can follow what the model knew:

![Langfuse: the model call of the invoice checker with its answer, its time, its tokens and its cost](../assets/screenshots/tracing-model-call.png)

**A tool call.** The arguments the agent chose and what the tool returned. A wrong argument here explains most wrong
answers:

![Langfuse: the tool call invoice_ledger_query of the invoice checker, with the SQL query and the invoice number INV-2042 as input and the ledger row as output](../assets/screenshots/tracing-tool-call.png)

**The approval and the decision.** The run stops at the approval request with the exact values. When the person
decides, it continues as a second trace of the same thread; [Approvals](approvals.md#see-an-approval-in-langfuse)
shows both. awa sends the thread of a run as its Langfuse session, so the paused run and its resumption read as one
story under **Sessions**.

## Find a run again

Every observation carries the metadata of its run. Type these into the search bar of **Tracing**:

| Search | Finds |
| --- | --- |
| `metadata.run_id:...` | One run; for a run of `awa worker`, the `proc_...` id that `awa runs list` shows. |
| `metadata.thread_id:thread_...` | Every run of one thread. |
| `metadata.blueprint:invoice-desk` | The runs of one blueprint. |
| `metadata.tenant:acme` | The runs of one tenant. |
| `metadata.agent_role:orchestrator` | The agents with that role. |
| `level:ERROR` | Failed tool calls and failed runs; `level:WARNING` marks a tool call that waits for approval. |

## Good to know

- **Langfuse is free when you run it yourself.** Tracing, sessions, users, cost and token tracking, dashboards and the
  API are in its MIT-licensed core.
- **The cost** of a model call comes from Langfuse's own price list for the models it knows. For other models, give
  awa your prices (`prices:` in the blueprint).
- **The model name** the provider reports is sent with each call. With Azure OpenAI it can contain the name of your
  deployment, so mask it before you share a screenshot.
- **Stop Langfuse** with `docker compose down` in its folder. `docker compose down -v` also deletes all traces.
- awa sends keys and traces over `https`, or over plain `http` only to a server on this computer.

## Common mistakes

| awa says | Fix |
| --- | --- |
| `Langfuse keys are not set` | The four lines are not in the `.env` of the folder you run awa in. |
| `cannot use Langfuse: /api/public/health: ...` | Langfuse is not running or still starting: `docker compose ps` in its folder. |
| `the keys were rejected` | The keys belong to another project; copy them again from `awa-langfuse.env`. |
| `plain http on another computer` | Use the `https` address of your Langfuse. |
| A run is not there | Wait a few seconds, and check the time range at the top right of the page. |
| You see no prompts or answers | Content capture is off by default: add `AWA_LANGFUSE_CAPTURE_CONTENT=true`. |

!!! example "In code"

    `connect_langfuse` does the same as the setting from your own code, and needs the running Langfuse and its keys
    in the environment ([Observability](../develop/observability.md#langfuse)):

    ```python
    from awa.observability import connect_langfuse

    langfuse = connect_langfuse(capture_content=False, metadata={"blueprint": "invoice-desk"})
    ```
