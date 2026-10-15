# Run automatically

Once your process passes its eval cases, let it work without you. A [trigger](../reference/glossary.md#trigger)
starts a run on a schedule, an email, a file or a web request, and other programs can call the process over HTTP or
MCP. The shortest path: add a trigger to the blueprint, try it with `--dry-run`, then start `awa worker`.

```yaml
triggers:
  - name: new-invoice
    file_drop: {directory: inbox, pattern: "*.pdf"}
    input: "Check the invoice in {payload.name} and release it for payment. The invoice says: {payload.text}"
```

| Way | Command | Use it for |
| --- | --- | --- |
| Talk to it | `awa chat blueprint.yaml` | Trying it out; the page **Try it** of the web app does the same. |
| Give it one task | `awa run blueprint.yaml "..."` | One job from a terminal or a script. |
| Start it on an event | `triggers:` in the blueprint, then `awa worker` or `awa serve` | Work that starts with a schedule, an email, a file, a queue message or a web request. |
| Offer it to other programs | `awa serve`, `awa mcp serve` | Your own applications, desktop assistants and code editors. |

Run every command in the project folder, so awa finds `blueprint.yaml`, `.env` and the `tools.py` next to the
blueprint.

!!! info "Preview"
    Triggers, `awa worker`, `awa serve`, `awa trigger` and `awa mcp serve` work and are tested, but their options may still change in a minor release ([Stability](../about/stability.md)).

## By hand: `awa chat` and `awa run`

`awa chat` is a conversation in the terminal. Short lines show what happens:

- `orchestrator => member`: a delegation.
- `agent -> tool({...})`: a tool call.
- `approval needed:`: a step that waits for you. `y` approves; anything else says no and asks for a note.

`/reset` starts over, `/usage` shows model calls and tokens, `/exit` ends it. A workflow keeps no thread, so each
message is a new task.

`awa run` gives the system one task. `--stream` prints each step, `--json` prints the result for a script, and
`--trace FILE` records what the run did ([Read a trace](operate.md#read-a-trace)). The exit code is `0` when done, `1`
on an error, and `2` when the run waits for an approval nobody could answer.

## The five kinds of trigger

| In `blueprint.yaml` | `awa add trigger --kind` | Starts a run when ... | What the run receives (`payload`) |
| --- | --- | --- | --- |
| `schedule` | `schedule` | a point in time comes (`cron`, or `every: 15m`) | `date`, the time of the tick |
| `mailbox` | `mail` | an unread email arrives in an IMAP folder | `from`, `to`, `subject`, `date`, `text`, `attachments` (saved, with their text) |
| `file_drop` | `file-drop` | a file appears in a folder and stops changing | `name`, `path`, `text` of PDF, Word, Excel and CSV files |
| `webhook` | `webhook` | another system sends a signed web request | the JSON the sender posted |
| `queue` | `queue` | a message arrives on a queue | the message |

Each start is a **run** with an id (`proc_...`), a status and a history. The blueprint never holds secrets, only the
names of the settings that hold them (`*_env`).

## Add triggers

`awa add trigger` writes a valid entry and prints how to start and try it. Copy `invoice-desk` of the tour to
`desk-triggers` and add a schedule and a mailbox:

<!-- guide:do cd . -->
<!-- guide:do copy invoice-desk desk-triggers -->
<!-- guide:do cd desk-triggers -->
<!-- guide:run -->
```console
$ awa add trigger blueprint.yaml --kind schedule --name morning-check --cron "0 7 * * MON-FRI" --timezone Europe/Berlin --input "Check all open invoices of {payload.date}."
added trigger 'morning-check' to blueprint.yaml:
  - name: morning-check
    schedule:
      cron: 0 7 * * MON-FRI
      timezone: Europe/Berlin
    input: Check all open invoices of {payload.date}.

Start it:
  awa worker blueprint.yaml
  (It runs until you stop it with Ctrl+C. Runs that need an approval wait; answer them with `awa runs decide` or on the Approvals page of `awa ui`.)

morning-check: runs by itself:
  0 7 * * MON-FRI (Europe/Berlin)
  (Nothing to send; the worker fires it.)

Try morning-check without waiting for a real event:
  awa trigger run blueprint.yaml morning-check --payload "@morning-check.json" --dry-run
  (First save a sample event as morning-check.json in this folder, for example {"date": "..."}. Shows the task the system would get; leave out --dry-run to run it once.)
```

The `cron` text has five fields: minute, hour, day of the month, month, day of the week. `0 7 * * MON-FRI` is 7:00 on
every weekday. A schedule fires once per tick, even when several workers run. After downtime it runs the latest
missed tick (`catch_up: latest`; the alternatives are `all` and `skip`).

<!-- guide:run -->
```console
$ awa add trigger blueprint.yaml --kind mail --name ap-inbox --host imap.example.com --input "New email from {payload.from}: {payload.subject}"
added trigger 'ap-inbox' to blueprint.yaml:
  - name: ap-inbox
    mailbox:
      host: imap.example.com
      username_env: AWA_IMAP_USER
      password_env: AWA_IMAP_PASSWORD
    input: 'New email from {payload.from}: {payload.subject}'

Start it:
  awa worker blueprint.yaml
  (It runs until you stop it with Ctrl+C. Runs that need an approval wait; answer them with `awa runs decide` or on the Approvals page of `awa ui`.)

ap-inbox: send an email to the mailbox INBOX on:
  imap.example.com
  (Set AWA_IMAP_USER and AWA_IMAP_PASSWORD in .env.)

Try ap-inbox without waiting for a real event:
  awa trigger run blueprint.yaml ap-inbox --payload "@ap-inbox.json" --dry-run
  (First save a sample event as ap-inbox.json in this folder, for example {"from": "...", "subject": "..."}. Shows the task the system would get; leave out --dry-run to run it once.)
```

Put the values of `AWA_IMAP_USER` and `AWA_IMAP_PASSWORD` into `.env`. In the web app, the page **Use it** has the
same choice as a form under **Add another trigger** ([tour, step 9](tour.md#step-9-let-a-trigger-start-it)):

![The Use it page, the form Add a trigger: the kind file-drop, the folder inbox, the pattern *.pdf, the task text and the Add this trigger button](../assets/screenshots/guide/a09-trigger-form.png)

--8<-- "docs/assets/screenshots/guide/a09-trigger-form.legend.txt"

For Microsoft 365 mail use the `outlook` connector instead ([Microsoft 365](microsoft-365.md)).

## Try a trigger first

<!-- guide:run -->
```console
$ awa trigger list blueprint.yaml
NAME           KIND       DETAILS
new-invoice    file_drop  directory=C:\awa-guide-work\desk-triggers\inbox, pattern=*.pdf
morning-check  schedule   cron=0 7 * * MON-FRI, timezone=Europe/Berlin, catch_up=latest
ap-inbox       mailbox    host=imap.example.com, folder=INBOX
$ awa validate blueprint.yaml
warning: triggers.ap-inbox: environment variable AWA_IMAP_USER is not set
warning: triggers.ap-inbox: environment variable AWA_IMAP_PASSWORD is not set
invoice_desk: valid with 2 warning(s)
```

`awa validate` names every setting that is not set yet; a template that cannot work is an error. To see what an event
becomes, without running anything or setting anything, save two sample events, `morning-check.json`:

<!-- guide:file morning-check.json -->
```json
{"date": "2026-10-05"}
```

and `ap-inbox.json`:

<!-- guide:file ap-inbox.json -->
```json
{"from": "anna@example.com", "subject": "Invoice INV-2041"}
```

<!-- guide:run -->
```console
$ awa trigger run blueprint.yaml morning-check --payload "@morning-check.json" --dry-run
input:     Check all open invoices of 2026-10-05.
thread_id: (one per run)
dedup_key: morning-check@2026-10-05T00:00:00Z
$ awa trigger run blueprint.yaml ap-inbox --payload "@ap-inbox.json" --dry-run
input:     New email from anna@example.com: Invoice INV-2041
thread_id: (one per run)
dedup_key: (none)
```

The events come from files because Windows PowerShell 5.1 removes double quotes inside an argument; `"@file.json"`
works in every terminal. Without `--dry-run`, the trigger fires once and the process runs in your terminal, asking
for approvals there.

## From an event to a task

| Text | Meaning | Example |
| --- | --- | --- |
| `input` | What the system is asked to do. | `"Release invoice {payload.invoice_no}."` |
| `thread_id` | Runs with the same thread continue one conversation. | `"inv-{payload.invoice_no\|slug}"` |
| `dedup` | A business key: a second event with the same key starts no second run. | `"{payload.invoice_no}"` |

Put them on a trigger, or under `automation:` for all triggers. In the text:

- `{payload.subject}` is a field of the event, `{payload}` the whole event as JSON.
- `{today}` and `{now}` are the date and time.
- A filter after a bar changes the value: `|slug` makes it safe for an id; also `|lower`, `|upper`, `|trim`.
- `{payload.priority|"normal"}` gives a fallback.

A name that is not in the event, without a fallback, is an error, so a typo never starts runs with an empty key.

## A web request that starts a run

A webhook is an address another system calls. awa accepts a request only when it proves it comes from the sender.
The `scheme` says how: `timestamped` (the default, a signature of time and body), `body` (a `sha256=` signature) or
`token` (the secret itself in the header `X-Awa-Token`). Copy `invoice-desk` to `desk-hook` and add one:

<!-- guide:do cd . -->
<!-- guide:do copy invoice-desk desk-hook -->
<!-- guide:do cd desk-hook -->
<!-- guide:run -->
```console
$ awa add trigger blueprint.yaml --kind webhook --name supplier-invoice --input "Release invoice {payload.invoice_no}." --scheme token
added trigger 'supplier-invoice' to blueprint.yaml:
  - name: supplier-invoice
    webhook:
      secret_env: SUPPLIER_INVOICE_SECRET
      scheme: token
    input: Release invoice {payload.invoice_no}.

Start it (receives the web requests and runs everything):
  awa serve blueprint.yaml
  (It runs until you stop it with Ctrl+C. Runs that need an approval wait; answer them with `awa runs decide` or on the Approvals page of `awa ui`.)

supplier-invoice: send a POST request to:
  http://127.0.0.1:8000/v1/automations/invoice_desk/webhooks/supplier-invoice
  (Headers: X-Awa-Token: the secret in SUPPLIER_INVOICE_SECRET itself. Set SUPPLIER_INVOICE_SECRET in .env.)

Try supplier-invoice without waiting for a real event:
  awa trigger run blueprint.yaml supplier-invoice --payload "@supplier-invoice.json" --dry-run
  (First save a sample event as supplier-invoice.json in this folder, for example {"invoice_no": "..."}. Shows the task the system would get; leave out --dry-run to run it once.)
```

With `SUPPLIER_INVOICE_SECRET=a-long-secret-text` in `.env` and `awa serve blueprint.yaml` running, send the body
`{"invoice_no": "INV-2041"}` saved as `invoice.json`, first with a wrong secret, then with the right one (`curl.exe`
comes with Windows 10 and 11; elsewhere type `curl`):

```console
curl.exe -i -X POST http://127.0.0.1:8000/v1/automations/invoice_desk/webhooks/supplier-invoice -H "Content-Type: application/json" -H "X-Awa-Token: wrong" --data "@invoice.json"
curl.exe -i -X POST http://127.0.0.1:8000/v1/automations/invoice_desk/webhooks/supplier-invoice -H "Content-Type: application/json" -H "X-Awa-Token: a-long-secret-text" --data "@invoice.json"
```

The same two requests against the same server, checked on every test run of this page:

<!-- guide:python -->
```python
import os
import time

from fastapi.testclient import TestClient

from awa.server import build_blueprint_app

os.environ["SUPPLIER_INVOICE_SECRET"] = "a-long-secret-text"   # in real life: in .env
url = "/v1/automations/invoice_desk/webhooks/supplier-invoice"

with TestClient(build_blueprint_app("blueprint.yaml")) as client:
    refused = client.post(url, json={"invoice_no": "INV-2041"}, headers={"X-Awa-Token": "wrong"})
    print(refused.status_code, refused.json()["error"]["message"])

    sent = client.post(url, json={"invoice_no": "INV-2041"}, headers={"X-Awa-Token": "a-long-secret-text"})
    print(sent.status_code, sent.json()["status"])

    run_id = sent.json()["id"]
    for _ in range(100):  # the server executes the run in the background
        run = client.get(f"/v1/process-runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    print(run["status"], "|", run["input"])
    print(run["output"])
```

<!-- guide:expect -->
```text
Webhook invoice_desk/supplier-invoice refused: Webhook token does not match
401 Webhook verification failed
202 queued
succeeded | Release invoice INV-2041.
Invoice INV-2041 from Nordwind Office GmbH (1,180.00 EUR) is released for payment.
```

The first line is the server's own log. The caller only sees `401 Webhook verification failed`, the same answer for
every refused request, so it learns nothing. The accepted request gets `202 queued` at once, and the server runs it in
the background. A copy of a signed request within the replay window (five minutes) starts no second run; still add a
`dedup` key such as the invoice number. Requests larger than 1 MiB are refused (`413`).

## Keep the triggers running: `awa worker` or `awa serve`

| | `awa worker` | `awa serve` |
| --- | --- | --- |
| Runs the triggers (schedule, folder, mailbox, queue) | yes | yes; with a PostgreSQL database it leaves them to `awa worker` |
| Receives web requests (webhooks, the API) | no | yes |
| Use it for | Folders, email and schedules | Webhooks, the HTTP API, or both |

Run one of the two per project folder. The one that runs the triggers holds a lock, and a second one refuses to
start. An extra `awa worker --no-triggers` may run next to it; it only executes queued runs. `--once` handles what is
ready and stops, for a scheduled task or a test. Ctrl+C stops gracefully: running work gets up to 30 seconds, the rest
goes back to the queue. [Keep it running](operate.md) covers what happens to the runs.

| Where it goes | |
| --- | --- |
| Handled files of a file drop | `processed/` next to the watched folder; a refused one goes to `failed/`. Nothing is overwritten. |
| Attachments of emails | `attachments/` next to the blueprint; one larger than 25 MiB is listed but not saved. |
| Every run, and every waiting approval | `.awa/awa.db` and `.awa/checkpoints.db` in the project folder. |

## Offer it to other programs

**As a web API.** `awa serve blueprint.yaml --api-key-env AWA_API_KEY` answers on `http://127.0.0.1:8000`; callers
send the key of the setting `AWA_API_KEY` as `Authorization: Bearer ...`. A request that reaches an approval stops,
and a person answers through the same door:

| Request | What it does |
| --- | --- |
| `POST /v1/systems/NAME/runs` | Run one request. Body: `{"input": "...", "thread_id": "..."}`. |
| `POST /v1/systems/NAME/runs/stream` | The same, as a stream of events. |
| `GET /v1/systems/NAME/threads/ID/interrupts` | The approvals a thread waits for. |
| `POST /v1/systems/NAME/threads/ID/resume` | Answer them. Body: `{"decision": {"action": "approve"}}`. |

`--approver-role` limits who may answer approvals. `--rate-limit` and `--max-concurrency` keep the server in bounds.
`--api-keys-file` gives each application its own key. Keep the server on this computer, or behind a proxy your IT
team runs. Every option: [Serving](../develop/serving.md).

**Over MCP**, a desktop assistant or code editor uses the system as a tool and asks its user for approvals. The client
starts the server itself. Give it the command, with absolute paths and the model's settings in `env`:

```json
{
  "mcpServers": {
    "invoice-desk": {
      "command": "awa",
      "args": ["mcp", "serve", "C:\\awa-guide-work\\invoice-desk\\blueprint.yaml"],
      "env": {"AWA_DEFAULT_MODEL": "<provider>:<model>", "ANTHROPIC_API_KEY": "your-key-here"}
    }
  }
}
```

The client then lists the tool `run_invoice_desk` and, because the desk has approvals, `resume_invoice_desk`. Keep
that configuration file private. [MCP](../develop/mcp.md) has the HTTP transport and the details.

## Common mistakes

| Mistake | What you see | Fix |
| --- | --- | --- |
| A secret written into the blueprint | You can read it in the file | Use `secret_env` and put the value in `.env`. |
| No `limits:` on a trigger that runs at night | `awa add trigger` and `awa validate` warn | Add `limits:` ([Keep it running](operate.md#limits-and-costs)). |
| Two workers on one folder | `Another awa process ... already runs the triggers` | Run one, or `awa worker --no-triggers` for the second. |
| The same invoice arrives twice | Two runs | Add `dedup: "{payload.invoice_no}"`. |
| `401 Webhook verification failed` | The secret or the scheme does not match | The server log says why; check the setting named by `secret_env`. |
| The worker starts nothing | No run appears | Check `awa trigger list` and that the worker is running; `--dry-run` shows what an event would become. |

<!-- guide:do cd . -->
<!-- guide:do cd invoice-desk -->

!!! example "In code"

    `build_blueprint_app` is the application `awa serve` runs, so a test can call it without a network
    ([Serving](../develop/serving.md)). The second request stops at the approval and names the tool that waits:

    <!-- guide:python -->
    ```python
    from fastapi.testclient import TestClient

    from awa.server import build_blueprint_app

    key = {"Authorization": "Bearer a-long-key"}
    with TestClient(build_blueprint_app("blueprint.yaml", api_key="a-long-key")) as client:
        print(client.get("/v1/systems").status_code)
        small = client.post("/v1/systems/invoice_desk/runs", json={"input": "Please release invoice INV-2041 for payment."}, headers=key)
        print(small.json()["status"], small.json()["text"])
        big = client.post("/v1/systems/invoice_desk/runs", json={"input": "Please release invoice INV-2042 for payment.", "thread_id": "inv-2042"}, headers=key)
        print(big.json()["status"], big.json()["interrupts"][0]["value"]["tool"])
    ```

    <!-- guide:expect -->
    ```text
    401
    completed Invoice INV-2041 from Nordwind Office GmbH (1,180.00 EUR) is released for payment.
    interrupted finance_approval
    ```
