# Your files

Use this page when your process works on files: invoices, orders, lists, emails. awa reads your files, builds the
process on what they really contain, tests it on copies, and then runs it on each file. Your originals are never
changed.

Three commands get you from a folder of files to results:

```console
awa data inspect invoices/
awa build "<your process>" --out my-desk --data invoices/
awa data run my-desk --all
```

1. `awa data inspect` shows what is in the files.
2. `awa build` designs and tests the process on them.
3. `awa data run` runs the process once for each file.

The sections below walk through each step. Some of them use a finished example from the awa folder,
`examples/benchmarks/core/04_document_intake`: an invoice inbox with a PDF, a Word file, an Excel file and a scan.
Copy it to your work folder as `intake-desk`:

<!-- guide:do cd . -->
<!-- guide:do copy awa:examples/benchmarks/core/04_document_intake intake-desk -->
<!-- guide:do cd intake-desk -->

!!! info "Preview"
    `awa data` and building on your files work and are tested, but their options may still change in a minor release ([Stability](../about/stability.md)).

## See what awa finds

Start here. awa reads every file without a model, says what is in it, and flags what is unusual. Nothing is changed.

- In the web app, open the page **Data**: one card per file, as in
  [step 3 of the tour](tour.md#step-3-bring-your-files-and-your-ledger).
- In a terminal, run `awa data inspect` with files, folders or patterns:

<!-- guide:run -->
```console
$ awa data inspect data
6 files, 3 tables, 20 rows, 6 warnings, 0 errors

file                         kind  content                status  flags
inbox/2024-0412_mueller.pdf  pdf   1 page (born digital)  ok      -
inbox/88231_zuercher.docx    docx  7 paragraphs           ok      -
inbox/ln-2024-17_meyer.xlsx  xlsx  16 rows                ok      3 warnings
inbox/scan_0001.pdf          pdf   1 page (scanned)       ok      1 warning
purchase_orders.json         json  -                      ok      -
vendors.csv                  csv   4 rows                 ok      2 warnings
...
  [warning] 1 of 1 analyzed pages have no usable text layer; they need OCR
...
```

A warning is something to know, not a failure. `--verbose` also prints the code of each flag. The flags in this
example:

| Flag | In file | What it means for you |
| --- | --- | --- |
| `DF-W-NO-TEXT-LAYER` | `scan_0001.pdf` | The page is a picture without a text layer. The `workspace` connector reads it with a vision model, one model call per page; without that, hold it for manual entry. |
| `DF-W-TOTALS-ROW` | `ln-2024-17_meyer.xlsx` | Rows 10 to 12 are totals and are not counted as data. |
| `DF-W-NO-HEADER` | `ln-2024-17_meyer.xlsx` | The sheet `Kopf` has no row with column names; the columns are named A, B, and so on. |
| `DF-W-ENCODING-ASSUMED` | `vendors.csv` | The file is not UTF-8 and was read as Windows-1252. Check that the umlauts look right. |
| `DF-W-DECIMAL-COMMA` | `vendors.csv` | Numbers like `25.000,00`, read with a decimal comma. |

### The file types

| What | File endings | What awa reads |
| --- | --- | --- |
| Excel | `.xlsx`, `.xlsm` | The sheets, tables, columns and a totals row. |
| Tables as text | `.csv`, `.tsv` | The columns, the separator and the encoding, also numbers like `4.250,00`. |
| PDF | `.pdf` | The text. A scanned page has none, and awa says so; the `workspace` connector reads such pages with a vision model. |
| Word | `.docx` | The text and the tables. |
| Email | `.eml`, `.mbox` | Sender, subject, date, the start of the text and the names of attachments. |
| Data and text | `.json`, `.txt`, `.md` | The structure, or the text. |
| Database file | `.db`, `.sqlite` | The tables of a copy. To read a database where it is, add it as a system ([Connect systems](connect.md)). |
| Zip archive | `.zip` | The files inside, when every file in it is safe. |

awa looks at what is inside a file, not at its ending. A program renamed to `invoice.pdf` is refused. A file awa
cannot read, such as an old `.xls` or a picture, is listed with the reason. The web app takes up to 200 files, 50 MB
each and 500 MB in all.

### What the model sees

The model that designs your process never reads your files. It reads only a short **digest** of them. `--model-view`
prints exactly that digest. `--mask placeholders` replaces names, tax numbers and bank numbers with placeholders:

<!-- guide:run -->
```console
$ awa data inspect data/vendors.csv --model-view --mask placeholders
...
Personal data is replaced by placeholders of the form <KIND_1>: the same value has the same placeholder in every file.
...
| Lieferantennr | Name | USt-IdNr | IBAN | Status | Bestelllimit_EUR |
| V-100 | <NAME_1> | <TAXID_1> | <IBAN_1> | aktiv | 25.000,00 |
| V-300 | <NAME_2> | <TAXID_2> | <IBAN_2> | gesperrt | 5.000,00 |
| V-400 | <NAME_3> | <TAXID_3> | <IBAN_3> | aktiv | 10.000,00 |
...
```

The same value gets the same placeholder in every file, so lines that belong together still match. `--mask drop`
leaves out the sample rows, so the model sees only the structure. In the web app, the switch **Hide personal data
from the AI** on the page **Data** does the same.

### What the mask covers

The mask applies to the digest that the design model sees. It replaces personal data in what the *design* model
reads: the digest of your files and the example rows of your systems. You still see everything, and the files are
never changed.

awa recognizes names, tax numbers, bank numbers, email addresses and phone numbers by their form. A name inside free
text may be missed.

The mask does not protect the running process. Agents that read your files through the file tools see the file
content unmasked at run time, and so do agents that read a system with its tools. Give an agent only the files it
needs, or use the redaction guardrail (`RegexRedactor.pii()` hides email addresses and IBANs, not names). See
[Where your data goes](#where-your-data-goes).

## Build on your files

Give `awa build` your description and the files. `--data` takes a file, a folder or a pattern, and you can repeat
it. This example uses the folder `invoice-files` from the tour:

<!-- guide:do cd . -->
<!-- guide:run model=files -->
```console
$ awa build "Supplier invoices arrive as PDF files. Check each invoice against the open purchase orders in purchase_orders.csv: the purchase order must be open and the amount must match. A matching invoice is released for payment. From 5,000 EUR the head of finance must approve the payment first." --out files-desk --data invoice-files --no-input
[profile] Reading your files
[profile] INV-2041.pdf: pdf (ok)
[profile] INV-2042.pdf: pdf (ok)
[profile] purchase_orders.csv: csv (ok)
[profile] Read 3 file(s): 1 table(s), 2 row(s), 0 warning(s), 0 unreadable
[design] Writing the acceptance tests, then the design
[design] The acceptance tests are written: 2 case(s)
[design] The blueprint 'invoice_check' is designed
[evaluate] Round 1: running the cases
[evaluate] Case small-invoice: passed
[evaluate] Case large-invoice: passed
[evaluate] Round 1: 2 of 2 cases pass
[write] Writing the project to files-desk
[write] The project is written
[done] Target reached
invoice_check: 2 of 2 eval cases pass (100%) on the local workspace after 1 round(s); target reached.
Project folder: files-desk
Where the data comes from:
  - Your files (CSV file, PDF document): your files (INV-2041.pdf, INV-2042.pdf, purchase_orders.csv), copied into the project (data/files/) and read there by the file tools; the originals are never changed.
    details: blueprint.yaml connectors: files: use workspace; root data/files; tools files_list_files, files_query_csv, files_read_pdf
  - Results folder (folder data/output/): where the process writes its results (data/output/ in the project folder); a person approves each write.
    details: blueprint.yaml connectors: output: use workspace; root data/output; tools output_write_file
Refinement: none (the traces show nothing to refine).
Model usage: 12 call(s) (design 2, evaluation 10, judges 0, repair 0), 12371 tokens.
Next: read README.md and CONNECT.md in the project folder.
```

What happens, in order:

1. awa reads the files (`[profile]`) and checks that your words fit them.
2. It writes the tests from the real rows and designs the process.
3. It runs the tests on copies of your files.

The project folder gets the copies, what awa found, and a `data:` section in `blueprint.yaml`:

<!-- guide:tree files-desk -->
```text
.env.example
.gitignore
CONNECT.md
README.md
blueprint.yaml
data/PROFILE.json
data/PROFILE.md
data/files/INV-2041.pdf
data/files/INV-2042.pdf
data/files/purchase_orders.csv
evals/cases.yaml
tests/test_tools.py
tools.py
```

The `data:` section lists every file with its [role](../reference/glossary.md#file-role) and what awa found in it.
`input` files are processed one by one, `reference` files are looked up, and `output` is where results go:

<!-- guide:contains files-desk/blueprint.yaml -->
```yaml
- name: purchase_orders
  path: data/files/purchase_orders.csv
  role: reference
  description: 'The open purchase orders: number, vendor, amount and status.'
  expect:
    columns:
    - po
    - vendor
    - amount
    - status
    header_row: 1
    decimal: .
    min_rows: 1
  handling: Look the purchase order up by its number; the amount must match.
```

The options for files (all options: [`awa build`](../reference/cli.md#awa-build)):

| Option | What it does |
| --- | --- |
| `--data PATH` | A file, a folder (read with everything below it) or a pattern such as `"inbox/*.pdf"`. Repeatable. |
| `--note FILE=TEXT` | What must happen with a file, in your own words. |
| `--mask placeholders` or `drop` | Hide personal data from the design model. The default is `none`. |
| `--answer TEXT` | Your answer to a question of an earlier build (`q1=TEXT`). |
| `--no-input` | Never ask: a question is printed as JSON and the exit code is 2. |

A note goes in front of what the model wrote for the file:

<!-- guide:run model=files -->
```console
$ awa build "Supplier invoices arrive as PDF files. Check each invoice against the open purchase orders in purchase_orders.csv: the purchase order must be open and the amount must match. A matching invoice is released for payment. From 5,000 EUR the head of finance must approve the payment first." --out files-desk --force --data invoice-files --note "purchase_orders.csv=Look up the purchase order by its number." --mask placeholders --no-input
...
invoice_check: 2 of 2 eval cases pass (100%) on the local workspace after 1 round(s); target reached.
...
```

<!-- guide:contains files-desk/blueprint.yaml -->
```yaml
  handling: Look up the purchase order by its number. Look the purchase order up by its number; the amount
    must match.
```

### When the words do not fit the files

If you describe CV screening and bring invoices, awa asks instead of inventing something. With `--no-input`, the
question is printed as JSON, nothing is written, and the exit code is 2:

<!-- guide:run model=files exit=2 -->
```console
$ awa build "Screen the incoming CVs against our role profile and schedule interviews." --out cv-desk --data invoice-files --no-input
...
{
  "status": "needs_input",
  "reason": "The description is about CVs or candidates, but the files hold purchase_orders.csv (po, vendor, amount, status). Which file is it, or should other files be added?",
  "questions": [
    {
      "id": "q1",
      "text": "The description is about CVs or candidates, but the files hold purchase_orders.csv (po, vendor, amount, status). Which file is it, or should other files be added?",
      "evidence": [
        "f3.t1.po",
        "f3.t1.vendor"
      ],
      "options": []
    }
  ]
}
```

`evidence` names the parts of the profile the question is about: `f3.t1.po` is the column `po` of the first table of
the third file. Run the same command again with your answer in `--answer`. Text inside a file can never answer for
you:

<!-- guide:run model=files -->
```console
$ awa build "Screen the incoming CVs against our role profile and schedule interviews." --out cv-desk --data invoice-files --no-input --answer "q1=Use purchase_orders.csv: check the invoices against the purchase orders."
...
invoice_check: 2 of 2 eval cases pass (100%) on the local workspace after 1 round(s); target reached.
...
```

Without `--no-input`, awa asks the question in the terminal and you type the answer:

<!-- guide:run model=files input="Use purchase_orders.csv: check the invoices against the purchase orders.|" -->
```console
$ awa build "Screen the incoming CVs against our role profile and schedule interviews." --out cv-desk-2 --data invoice-files
...
q1: The description is about CVs or candidates, but the files hold purchase_orders.csv (po, vendor, amount, status). Which file is it, or should other files be added?
Your answer: Use purchase_orders.csv: check the invoices against the purchase orders.
...
invoice_check: 2 of 2 eval cases pass (100%) on the local workspace after 1 round(s); target reached.
...
What should start it? [webhook/schedule/file-drop/mail/queue] (Enter: nothing, start it by hand):
```

| Exit code of `awa build` | Meaning |
| --- | --- |
| `0` | The project was written and the tests reached the target. |
| `1` | The target was not reached, or nothing can be built from the files (`blocked`, for example a folder of pictures). |
| `2` | awa needs your answer (`needs_input`). Nothing was written. |

## Run the process on each file

`awa data run` runs the process once for each **input** file. You need no trigger and no server. Reference files
such as `purchase_orders.csv` are only looked up.

Each run asks before it writes its result. The large invoice also asks the head of finance first:

<!-- guide:do cd files-desk -->
<!-- guide:run model=files input="y|y|y" -->
```console
$ awa data run . --all
Approval needed:
{
...
}
Answer here: y approves the step; anything else rejects it.
Approve? [y/N] y
INV-2041.pdf: completed; wrote data/output/INV-2041.pdf/INV-2041.txt (runs/run_a02e274e7f8d.json)

Approval needed:
{
...
  "condition": "amount_eur >= 5000"
}
Answer here: y approves the step; anything else rejects it.
Approve? [y/N] y

Approval needed:
{
...
}
Answer here: y approves the step; anything else rejects it.
Approve? [y/N] y
INV-2042.pdf: completed; wrote data/output/INV-2042.pdf/INV-2042.txt (runs/run_7a32fa51d2f3.json)
```

Every run:

- works on a fresh copy of `data/`;
- writes only below `data/output/`, in a folder named after the input file;
- leaves a report in `runs/` with the file's fingerprint (`sha256`), what it was asked, the tool calls, the
  approvals, the outputs and the usage.

<!-- guide:tree files-desk/data/output -->
```text
INV-2041.pdf/INV-2041.txt
INV-2042.pdf/INV-2042.txt
```

<!-- guide:contains data/output/INV-2041.pdf/INV-2041.txt -->
```text
INV-2041: purchase order PO-7710 is open and the amount 1,180.00 EUR matches.
```

In a script, add `--no-input` so awa never asks. A run that needs an approval then ends **paused** with exit code 2.
`--json` prints the outcomes for a program, and `--file` picks files by name or pattern:

<!-- guide:run model=files exit=2 -->
```console
$ awa data run . --file "INV-2042.pdf" --json --no-input
[
  {
    "file": "INV-2042.pdf",
    "status": "paused",
    "answer": "",
    "error": "",
    "approvals": [
      "finance_approval"
    ],
    "tool_calls": [
      "files_read_pdf",
      "files_query_csv",
      "finance_approval"
    ],
    "outputs": [],
    "run_id": "run_cc5c156e55c7",
    "report": "runs/run_cc5c156e55c7.json",
    "usage": {
      "requests": 3,
      "total_tokens": 1632
    }
  }
]
A run is paused for an approval; run this command in a terminal to decide.
```

`awa data run` ends with exit code 0 when every run completed, 1 when a run failed, and 2 when a run is paused. The
limits options (`--max-requests`, `--max-duration` and the others of [Keep it running](operate.md#limits-and-costs))
apply to each run.

## Check that the files still fit

Files change: a supplier renames a column, a new export has another layout. `awa data check` tests the `data:`
section against the files in `data/files`, without a model:

<!-- guide:run -->
```console
$ awa data check .
.: data matches (0 errors, 0 warnings)
```

Now a supplier renames the column `po`:

<!-- guide:do replace files-desk/data/files/purchase_orders.csv "po,vendor" "po_number,vendor" -->

<!-- guide:run exit=1 -->
```console
$ awa data check .
warning: data[2].path: data changed since the profile was written: 'data/files/purchase_orders.csv' differs from data/PROFILE.json; the files were profiled again
error: data[2].expect.columns[0]: column 'po' does not exist in 'data/files/purchase_orders.csv' (its columns: 'po_number', 'vendor', 'amount', 'status') [DF-V-003]
.: 1 error, 1 warning
```

The message names the entry of `data:` (`data[2]`, the third), the missing column and the columns the file has now.
`--json` gives the same for a program, and `awa validate` runs the same check.

To fix it, do one of these: put the old file back, change `expect:` in `blueprint.yaml`, or build again with
`--force`.

<!-- guide:do replace files-desk/data/files/purchase_orders.csv "po_number,vendor" "po,vendor" -->

<!-- guide:run -->
```console
$ awa data check .
.: data matches (0 errors, 0 warnings)
```

## The tools agents read files with

You only need this section when you write or change a blueprint by hand. While a process runs, agents open files
with tools, one for each kind:

| Your file | The tool | What the agent gets |
| --- | --- | --- |
| CSV | `builtin:read_csv`, `builtin:query_csv` | Rows and columns; `query_csv` filters and adds up without reading everything. |
| Excel | `builtin:read_excel`, `builtin:query_excel` | Rows of one sheet; `header_row` skips a title block. |
| PDF | `builtin:extract_pdf_text` | The text of each page. |
| Word | `builtin:extract_docx_text` | Paragraphs and tables in reading order. |
| JSON | `builtin:json_query` | The part you ask for. |
| Text and Markdown | `builtin:read_file` | The text. |
| A folder | `builtin:list_files` | The names of the files in it. |

The file tools see only one folder, named in the blueprint. Absolute paths, `..`, hidden files such as `.env` and
anything outside are refused. A tool that writes, moves or deletes waits for approval:

```yaml
toolkits:
  files: {root: ./data}
```

### Example: an invoice inbox

The example `intake-desk` shows these tools at work. Its clerk reads each invoice and checks it against
`vendors.csv` and `purchase_orders.json` with rules in `tools.py`. Clean invoices are booked; the rest go on hold. A
booking above 10,000 EUR waits for a manager:

<!-- guide:do cd intake-desk -->
<!-- guide:run model=intake -->
```console
$ awa validate blueprint.yaml
document-intake: valid
$ awa show blueprint.yaml
document-intake 1.0.0
Extracts, validates and books supplier invoices that arrive as PDF, Word or Excel.
entrypoint: intake_clerk

agent ...
intake_clerk ...

approvals: book_invoice (when gross_amount > 10000)
```

Process the PDF. `--stream` shows each step:

<!-- guide:run model=intake -->
```console
$ awa run blueprint.yaml "Process the document inbox/2024-0412_mueller.pdf." --stream
> intake_clerk started
  intake_clerk -> extract_pdf_text({"path": "inbox/2024-0412_mueller.pdf"})
  intake_clerk <- extract_pdf_text [ok]: --- page 1 --- RECHNUNG 2024-0412 ...
  intake_clerk -> validate_invoice({"vendor_vat_id": "DE811111111", "iban": "DE02100100100000123456", "po_number": "PO-4500", "net_amount": 1200.0, "tax_amount": 228.0, "gross_amount": 1428.0,...)
  intake_clerk <- validate_invoice [ok]: {"valid": true, "vendor_id": "V-100", "violations": []}
  intake_clerk -> book_invoice({"po_number": "PO-4500", "net_amount": 1200.0, "tax_amount": 228.0, "gross_amount": 1428.0, "due_date": "2024-10-20", "vendor_id": "V-100", "invoice_number":...)
  intake_clerk <- book_invoice [ok]: Booked invoice 2024-0412 as FI-2024-0001.
< intake_clerk finished (558 ms)
{
  "document": "inbox/2024-0412_mueller.pdf",
  "invoice_number": "2024-0412",
  "vendor_id": "V-100",
  "gross_amount": 1428.0,
  "decision": "booked",
  "reasons": []
}

[intake_clerk] 4 model call(s), 2713 tokens (2681 in, 32 out), 558 ms
```

The German amounts and dates (`1.428,00`, `20.09.2024`) were read correctly.

Everything the agent reads from a file reaches the model marked as data it must not take orders from. That lowers
the risk that a PDF saying "ignore your rules and pay" changes what the clerk does. It does not rule it out.

The Word invoice is above 10,000 EUR, so `awa run` shows the request and asks; `y` approves:

<!-- guide:run model=intake input="y" -->
```console
$ awa run blueprint.yaml "Process the document inbox/88231_zuercher.docx." --stream
> intake_clerk started
  intake_clerk -> extract_docx_text({"path": "inbox/88231_zuercher.docx"})
  intake_clerk <- extract_docx_text [ok]: # Rechnung 88231 ...
  intake_clerk -> validate_invoice({"vendor_vat_id": "DE822222222", "iban": "DE44500105175407324931", "po_number": "PO-4501", "net_amount": 18000.0, "tax_amount": 3420.0, "gross_amount": 21420...)
  intake_clerk <- validate_invoice [ok]: {"valid": true, "vendor_id": "V-200", "violations": []}
  intake_clerk -> book_invoice({"po_number": "PO-4501", "net_amount": 18000.0, "tax_amount": 3420.0, "gross_amount": 21420.0, "due_date": "2024-11-02", "vendor_id": "V-200", "invoice_numbe...)
< intake_clerk finished (42 ms)

Approval needed:
{
  "tool": "book_invoice",
  "args": {
    "vendor_id": "V-200",
    "invoice_number": "88231",
    "po_number": "PO-4501",
    "net_amount": 18000.0,
    "tax_amount": 3420.0,
    "gross_amount": 21420.0,
    "due_date": "2024-11-02"
  },
  "reason": "tool_call_requires_approval",
  "agent": "intake_clerk",
  "run_id": "run_8f2c1d94a7b34e0c9d5a61f3b2e7c840",
  "requested_at": "2026-10-05T16:40:12.331205+00:00",
  "condition": "gross_amount > 10000"
}
Answer here: y approves the step; anything else rejects it.
Approve? [y/N] y
{
  "document": "inbox/88231_zuercher.docx",
  "invoice_number": "88231",
  "vendor_id": "V-200",
  "gross_amount": 21420.0,
  "decision": "booked",
  "reasons": []
}

[intake_clerk] 1 model call(s), 735 tokens (727 in, 8 out), 42 ms
```

The Excel invoice uses 16 percent tax instead of 19, so a rule in `tools.py` puts it on hold with the reason. The
file tools of this example read no scans, so the clerk holds the scan instead of guessing:

<!-- guide:run model=intake -->
```console
$ awa run blueprint.yaml "Process the document inbox/ln-2024-17_meyer.xlsx."
{
  "document": "inbox/ln-2024-17_meyer.xlsx",
  "invoice_number": "LN-2024-17",
  "vendor_id": "V-400",
  "gross_amount": 5568.0,
  "decision": "on_hold",
  "reasons": [
    "V4: net 4800.00 + tax 768.00 vs gross 5568.00"
  ]
}

[intake_clerk] 4 model call(s), 3896 tokens (3856 in, 40 out), 764 ms
$ awa run blueprint.yaml "Process the document inbox/scan_0001.pdf."
{
  "document": "inbox/scan_0001.pdf",
  "invoice_number": null,
  "vendor_id": null,
  "gross_amount": null,
  "decision": "on_hold",
  "reasons": [
    "no text layer, manual entry needed"
  ]
}

[intake_clerk] 3 model call(s), 1865 tokens (1841 in, 24 out), 115 ms
```

The five eval cases of the example check all four documents: the tool used, whether an approval was asked, and the
decision:

<!-- guide:run model=intake -->
```console
$ awa eval blueprint.yaml cases.yaml
Evaluation: document-intake
===========================

System intake_clerk on dataset document-intake: 5 case(s) x 1 repetition(s).

- Pass rate: 100.0% (5/5 runs)
- Errors: 0
...
```

## Where your data goes

awa runs on your computer and keeps your files there. But an agent is an AI model run by your provider, so
**whatever an agent reads with a tool is sent to that provider.** The mask changes what the design model sees, not
what an agent reads later.

- Give an agent only the files it needs.
- Use a provider your organization has approved.
- Ask your data-protection contact when you are not sure.

[Safety: data protection](../develop/safety.md#data-protection) lists the controls for personal data.

## Common mistakes

| Mistake | What you see | Fix |
| --- | --- | --- |
| The file is outside the files folder | `Error: path ... must be relative to the allowed directory` | Put the file inside the folder and name it relative to it. |
| A scanned PDF | *Looks like a scan*, or `[no text layer: ...]` | Read it through the `workspace` connector, which reads scans with a vision model ([Connect systems](connect.md)); or hold scans for manual entry, or bring a text version. |
| An Excel title block above the headers | Columns named `Unnamed` | Name the row with the column names: `header_row`. |
| Numbers read wrongly | `1.234` taken as 1.234 | Say it in the instructions: "amounts like 1.428,00 mean 1428.00". |
| `--note` without `--data` | `error: --note, --mask and --answer belong to --data: name your files` | Add the files. |
| The project folder exists | `error: 'files-desk' already holds a project` | Add `--force`, or choose another `--out`. |
| `--file` matches nothing | `error: no input file matches ...` | Use a name from the list the message gives. |
| A script hangs on an approval | `--no-input` is missing | Add it: the run ends paused with exit code 2. |

<!-- guide:do cd . -->

!!! example "In code"

    `awa.data.profile` is `awa data inspect` from Python; `awa.blueprints.run_project` is `awa data run`
    ([Data](../develop/data.md#what-a-profile-holds)).

    <!-- guide:python -->
    ```python
    from awa.data import profile

    found = profile("intake-desk/data", root="intake-desk/data")
    print(found.summary.files, "files,", found.summary.warnings, "warnings")
    for file in found.files:
        print(file.path, file.kind, file.status)
    ```

    <!-- guide:expect -->
    ```text
    6 files, 6 warnings
    inbox/2024-0412_mueller.pdf pdf ok
    inbox/88231_zuercher.docx docx ok
    inbox/ln-2024-17_meyer.xlsx xlsx ok
    inbox/scan_0001.pdf pdf ok
    purchase_orders.json json ok
    vendors.csv csv ok
    ```
