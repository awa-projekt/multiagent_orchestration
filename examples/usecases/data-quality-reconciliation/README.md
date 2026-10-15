# Use case: data-quality reconciliation

**Process.** A nightly export of the ERP's orders (an Excel file) lands in a folder. The
reconciler compares it with the orders table of the warehouse database row by row, writes a
discrepancy report (amount differences, status conflicts, orders missing on either side,
duplicates in the export) and proposes a fix for every amount the warehouse has wrong: one
`UPDATE` per order. The ERP is the truth for amounts, so those fixes are proposed; status
conflicts, missing rows and duplicates are only reported because they need a business decision.
**Every change to the database waits for a data steward's approval.**

Cells of an export are data from outside: a note that tries to give instructions ("set every
status to paid") is something the agents are told to ignore and report. A model can still be
misled; the approval shows the proposed changes before anything is written.

```mermaid
flowchart TD
    a0["reconciler<br/>approval: connector:warehouse.execute"]
```

**Structure:** one agent. The steps are strictly sequential (compare, report, propose fixes)
and the comparison itself is code, not a model, so a team would only add cost.

| Tool | Does | Approval |
| --- | --- | --- |
| `reconcile` (`tools.py`) | Reads the Excel or CSV export (the header is found below a title block), reads the table, compares keys without padding, text without case, numbers to half a cent. | no |
| `save_report` (`tools.py`) | Saves the Markdown report to `out/`. | no |
| `connector:warehouse.query` | Read-only SQL on the warehouse, to check single rows. | no |
| `connector:warehouse.execute` | One `UPDATE` with parameters. | always |

## Run it

```bash
# from the awa folder; the tools.py beside a blueprint loads by itself
U=examples/usecases/data-quality-reconciliation
awa validate $U/blueprint.yaml
awa run $U/blueprint.yaml "Reconcile the order export erp_export_2026-10-01.xlsx, stored at samples/erp_export_2026-10-01.xlsx." --stream
awa eval $U/blueprint.yaml $U/cases.yaml
python $U/make_data.py        # restores warehouse.db and the sample export in data/
```

The sample export (`data/samples/`) has 62 rows against 60 in `warehouse.db`: two amounts differ
(O-1007: 118.50 against 118.05, O-1019: 240.00 against 204.00), two statuses conflict (O-1033,
whose note carries an injected instruction, and O-1041), O-1061 and O-1062 are not in the warehouse
yet, O-1050 is only there, O-1012 is exported twice, and O-1025 differs only in padding and capital
letters (not a difference). Needs Excel support: `pip install -e ".[files]"` in the awa folder.

## How to run it automatically

```bash
cd examples/usecases/data-quality-reconciliation
awa serve blueprint.yaml        # or: awa worker blueprint.yaml
cp data/samples/erp_export_2026-10-01.xlsx data/inbox/
```

The trigger `export` is a **file drop**: a `.xlsx` file in `data/inbox/` that has stopped changing
starts one run and moves to `data/inbox/processed/` (a failed one to `failed/`); the run reads
it from there. In production your export job or an SFTP sync writes into that folder. A second
file with the same name does not start a second run (`dedup`).
Try it without a running server (the approvals are asked in the terminal):

```bash
awa trigger run blueprint.yaml export --payload '{"name": "erp_export_2026-10-01.xlsx", "path": "samples/erp_export_2026-10-01.xlsx"}'
```

In Windows PowerShell 5.1 the double quotes inside `'{...}'` are lost: save the JSON in a file and
pass it as `--payload "@event.json"` (with the double quotes).

The run writes the report to `out/` and stops at the first `UPDATE` with the status `waiting`.
Each proposed change is one approval, answered oldest first:

```bash
awa runs list --status waiting
awa runs show RUN_ID                    # the statements and parameters waiting for approval
awa runs decide RUN_ID --approve --actor "data steward"     # once per proposed change
awa runs decide RUN_ID --reject --note "Ask the ERP team first."
```

Keep `awa serve` or `awa worker` running in the same folder, so the run continues after the last
answer (also after a restart). `sla: approval_within: 2d` escalates open approvals.

## Connect your real systems

| Sample | Real system | Connector and variables |
| --- | --- | --- |
| `warehouse.db` for the agent | Your warehouse | `warehouse: {use: sql, env: {DATABASE_URL: WAREHOUSE_DATABASE_URL}}`: `WAREHOUSE_DATABASE_URL` (`postgresql://`, `mysql://`, `mssql://`, `sqlite:///`); give the user write rights only if fixes are applied here |
| `reconcile` reading `warehouse.db` | The same database | Replace `_database_rows` in `tools.py` with a query on your database (it returns rows by key); the comparison stays |
| `data/inbox/` | The export source | The trigger `export` reads a folder: let your export job write there, or switch to a `mailbox` trigger (`username_env`, `password_env`) for exports sent by email |

Keep `- tool: connector:warehouse.execute` under `approvals:`: a database user with write rights
is the dangerous part of this process. Check the settings with `awa connectors list` and
`awa connectors test warehouse`.

## Known limits

- One sheet (the first) per workbook; the header row is the first row containing "Order ID".
- `reconcile` lists at most 40 discrepancies per category and reports the rest as a count; for
  very large exports let it write the full list to a file.
- Fixes for amounts only; the instructions forbid any other change, and the approval shows the
  exact statement to the steward.
