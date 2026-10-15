# Data

Before awa designs a process, it reads your files and writes a data profile: the tables, columns,
number formats and problems they have. You need this page when you build from your own files, or
when a web frontend uploads files over HTTP.

The profile is made by code only. No model is called, nothing is guessed (an unclear date order
becomes a flag, not a decision), and the files are only read. The same files always give the same
profile. A model later sees a short digest of it, never the files.

!!! info "Preview"
    Data profiles (`awa.data`, `awa data`) work and are tested, but their API may still change in a minor release ([Stability](../about/stability.md)).

## Profile some files

Take a CSV as an accounting system writes it, with semicolons, decimal commas and a totals row:

```text title="files/orders.csv"
Bestellnr;Kunde;Betrag EUR;Datum
1001;Müller AG;1.250,00;03.09.2026
1002;Meyer KG;89,90;04.09.2026
Summe;;1.339,90;
```

```python title="profile_it.py"
from awa.data import DataProfile, Privacy, profile, render_for_model

result = profile("files")  # a file, a folder (recursive), a glob, or a list of them
print(result.summary)

column = result.find_column("orders.csv", "Betrag EUR")
print(column.type, column.format.decimal, column.format.thousands)

table = result.files[0].tables[0]
print(table.header_row, table.rows, table.totals_rows)
print([flag.code for flag in result.files[0].flags])

again = DataProfile.from_json(result.to_json())  # the profile is plain data
print(again.files == result.files)

view = render_for_model(result, budget_tokens=1500, privacy=Privacy(mask="drop"))
print(view.splitlines()[0])
```

```text title="profile_it.out"
files=1 tables=1 rows=2 warnings=3 errors=0
decimal , .
1 2 [4]
['DF-W-TOTALS-ROW', 'DF-W-DECIMAL-COMMA', 'DF-W-DATE-AMBIGUOUS']
True
# Data profile: 1 file (1 csv), 1 table, 2 rows, 3 warnings, 0 unreadable
```

What the profile found:

- The totals row (row 4) is not counted in the 2 rows.
- `1.250,00` uses a decimal comma and `.` as thousands separator.
- `03.09.2026` is ambiguous (no day is above 12), so awa flags it instead of choosing.

Every flag has a code (`DF-E-...` error, `DF-W-...` warning, `DF-I-...` information) and a message
that says what to do. `awa.data.FLAGS` lists them, and `fix_hint(code)` gives the advice.

The same from the command line:

```console
$ awa data inspect files                                    # a readable report
$ awa data inspect files --json --out profile.json
$ awa data inspect files --model-view --mask placeholders   # exactly what a model would see
$ awa data inspect files --strict                           # exit code 1 on any warning
```

`--mask` is `none` (default), `placeholders` (stand-ins such as `<IBAN_1>` for personal data) or
`drop` (no sample values). The mask applies to the digest that the design model sees. Agents that
read your files through the file tools see the file content unmasked at run time. So give an agent
only the files it needs, or use the redaction guardrail (`RegexRedactor.pii()` hides email addresses and IBANs, not
names).

`awa data check PROJECT` tests a project's `data:` section against the files on disk.
`awa data run PROJECT` runs a built process on its input files.

## What a profile holds

| Part | Holds |
| --- | --- |
| `DataProfile` | `files`, `relations` between files, `flags`, `summary`, the `limits` and `privacy` used, `awa_version`, `created` |
| `FileProfile` | `kind`, `status` (`ok`, `partial`, `error`, `unsupported`), `sha256`, `size`, `flags`, and the facts of its kind (`tables`, `xlsx`, `pdf`, `docx`, `mail`, `json_doc`, `sqlite`, `text`); mail attachments are `children` |
| `TableProfile` | `columns`, `rows` (exact), `header_row`, `totals_rows`, `key_candidates`, `sample_rows` |
| `ColumnProfile` | `type`, number `format`, `date_format`, `ambiguous`, `unit`, `min`, `max`, `top` values, `pii` kinds |
| `Flag` | `code`, `severity`, `message`, `where` (an id such as `f1.t1.betrag_eur`) |

Ids are stable: files `f1`, `f2`, ..., tables `f1.t1`, columns `f1.t1.<key>`. The JSON form has a
JSON Schema (`awa.data/profile/v1`); a stored profile that a newer awa wrote is refused instead of
misread. `Limits` (files, bytes, rows, pages, seconds per file) and `Privacy` (masking, samples)
are arguments of `profile()`; a file over a limit is reported and the others go on. A PDF is read
in a separate process with a time and memory limit. A layout the readers cannot see is handled by
[your own parser](extend.md#write-a-parser).

`ingest(paths, destination)` copies files under safe names, decides the kind from the bytes (a
`.pdf` that is not a PDF raises `TypeMismatchError`), unpacks zip files only after checking entries,
sizes and the compression ratio, and keeps the originals untouched. `scanner=` runs a virus scanner
(the `UploadScanner` backend protocol: an object with `scan(path)`) on every copy.

## The data endpoints

With `create_app(..., data=DataOptions(...))` a server takes files, shows what is in them, builds a
process from them and a description, and runs what was built. Authentication, tenants (another
tenant's id answers 404), the JSON error shape, request ids, rate limits and the audit log are those
of every other endpoint ([Serving](serving.md)). Create `data_server.py`:

```python title="data_server.py"
from pathlib import Path

from awa.data import UnsupportedTypeError
from awa.server import ApiKeyAuthenticator, DataOptions, Principal, create_app


class Scanner:
    """Called with every accepted file; reject by raising an awa.data error."""

    def scan(self, path: Path) -> None:
        if b"EICAR" in path.read_bytes():
            raise UnsupportedTypeError(f"{path.name} was rejected by the scanner")


auth = ApiKeyAuthenticator(
    {
        "builder-key": Principal("web-app", tenant="acme", roles={"builder"}),
        "reader-key": Principal("viewer", tenant="acme", roles={"reader"}),
    }
)
app = create_app(
    [],  # no process to serve yet: this server only takes files and builds
    authenticator=auth,
    data=DataOptions(
        root="awa-data",              # one folder per tenant below it
        roles={"builder"},            # only these roles upload and build
        approver_roles={"approver"},  # only these roles answer approvals of built processes
        scanner=Scanner(),            # a virus scanner or content policy
        require_durable=True,         # refuse to start without a place that keeps its data after a restart
    ),
)
```

and call it in `call_it.py`:

```python title="call_it.py"
from starlette.testclient import TestClient

from data_server import app

client = TestClient(app)
builder = {"Authorization": "Bearer builder-key"}
csv = b"id;amount\n1;1,5\n2;2,5\n"

upload = client.post("/v1/uploads", headers=builder, files=[("files", ("orders.csv", csv, "text/csv"))])
print(upload.status_code, upload.json()["files"][0]["kind"])

profile = client.post("/v1/data/profile", headers=builder, json={"upload_id": upload.json()["upload_id"]})
print(profile.status_code, profile.json()["profile"]["summary"])

reader = {"Authorization": "Bearer reader-key"}
denied = client.post("/v1/uploads", headers=reader, files=[("files", ("a.csv", csv, "text/csv"))])
print(denied.status_code, denied.json()["error"]["code"])

virus = client.post("/v1/uploads", headers=builder, files=[("files", ("b.csv", b"id\nEICAR\n", "text/csv"))])
print(virus.status_code, virus.json()["error"]["code"])

fake_pdf = client.post("/v1/uploads", headers=builder, files=[("files", ("c.pdf", b"no pdf", "application/pdf"))])
print(fake_pdf.status_code, fake_pdf.json()["error"]["code"])
```

```text title="call_it.out"
201 csv
200 {'files': 1, 'tables': 1, 'rows': 2, 'warnings': 1, 'errors': 0}
403 awa.permission_denied
415 awa.data.unsupported_type
415 awa.data.type_mismatch
```

A problem with a file's content is in the profile (`status`, `flags`), never an HTTP error; a file
of a wrong or lying type is. The [OpenAPI fragment](data-api.openapi.json) of these endpoints is the
contract a frontend codes against.

| Method and path | What it does |
| --- | --- |
| `POST /v1/uploads` | Multipart, every file in the form field `files`; 201 with the id, kind, sniffed type and SHA-256 of each file. An `Idempotency-Key` makes a retry give the same upload. |
| `GET`, `DELETE /v1/uploads/{id}` | Inspect or delete an upload. |
| `POST /v1/data/profile` | `{upload_id, mask?, budget_tokens?}` (or files as multipart): the profile, and with `mask` or `budget_tokens` the `model_view`. |
| `POST /v1/builds` | `{description, upload_id, notes?, options?}`: 202 with the build id and its events URL. |
| `GET /v1/builds/{id}` | Status (`queued`, `profiling`, `designing`, `evaluating`, `repairing`, `writing`, `needs_input`, `completed`, `failed`, `cancelled`), pass rate, usage, and at the end the report or the error. |
| `GET /v1/builds/{id}/events` | Server-Sent Events with sequence ids, so `Last-Event-ID` replays what was missed. |
| `POST /v1/builds/{id}/answers` | Answers to the `questions` of a build in `needs_input`. |
| `GET /v1/builds/{id}/project.zip`, `.../files/{path}` | The finished project, without environment files, keys or the `.awa` folder. |
| `POST /v1/builds/{id}/runs`, `.../threads/{thread_id}/interrupts`, `.../resume` | Run the built process and answer its approvals. |

| `DataOptions` | What it decides | Default |
| --- | --- | --- |
| `roles` | Who may upload, profile and build (others get 403) | every authenticated caller |
| `approver_roles` | Who may answer approvals of built processes | everyone who can see the thread |
| `scanner` | A virus scanner or content policy for every file | none |
| `require_durable` | Refuse to start without `root` or both stores | `False`: a temporary folder, lost on restart |
| `root`, `uploads`, `builds` | Where uploads and builds live: a folder per tenant, or your own stores (`awa.server.ports`) | a temporary folder |
| `max_*`, `*_ttl`, `rate_limit`, `models` | 25 MB per file, 100 MB per request, 200 files, 1 GB per tenant, 2 builds at once, 60 requests per minute, uploads kept 24 h and builds 7 days, 70 model calls per build | as listed |

A build runs as a task of the process that started it, so serve `data=` from one worker. A build
that was running when the server stopped is `failed` afterwards; finished builds and builds that
wait for answers survive a restart. `erase_subject` does not reach uploads and builds: they expire,
and `DELETE /v1/uploads/{id}` removes one at once.

## What can go wrong

| What you see | Fix |
| --- | --- |
| `DF-E-MISSING-EXTRA` for a PDF, Excel or Word file | Install the `files` extra: `pip install ".[files]"` in the awa folder. |
| `DF-W-DATE-AMBIGUOUS` or `DF-W-NUMBER-AMBIGUOUS` | Not a defect: tell the build which order or format the file uses, or fix the export. |
| `DF-W-NO-TEXT-LAYER` on a PDF | It is a scan; the vision reader of the file tools reads it ([Tools](tools.md#builtin-toolkits)). |
| `DF-E-TIMEOUT` | The file needed more than `Limits.parse_timeout_s`; raise it or split the file. |
| A file is `unsupported` | awa does not read that kind (for example `.xls`); convert it. |
| 404 for an upload you just made | Another tenant's key, or the upload expired. |
| 429 `awa.data.quota_exceeded` | The tenant stores too much or runs too many builds. |

Reference: [Data](../reference/api/data.md#awa.data), [Server](../reference/api/server.md#awa.server).
