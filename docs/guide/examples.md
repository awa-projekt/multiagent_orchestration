# Examples

The examples are small, finished processes you can copy. Each one already has its approvals, limits and eval cases.
Copy the folder closest to your process, run it on its sample data, then adapt it.

## Try an example

The examples are in the awa folder under `examples/`; a wheel does not contain them. Copy a folder to your work
folder and open a terminal in it. `awa validate` checks it and `awa show` describes it; neither needs a model:

<!-- guide:do cd . -->
<!-- guide:do copy awa:examples/recipes/invoice-processing invoice-processing -->
<!-- guide:do cd invoice-processing -->
<!-- guide:run -->
```console
$ awa validate blueprint.yaml
invoice-processing: valid
$ awa show blueprint.yaml
invoice-processing 1.0.0
Extracts, validates, policy-checks and books incoming supplier invoices.
entrypoint: team

agent           model    tools                                              skills                                  description
ap_coordinator  default  book_invoice, hold_invoice, notify_vendor          delegation-briefs, approval-requests    Runs each invoice through extraction, validation and the ...
extractor       default  fetch_invoice_document                             document-extraction, structured-output  Reads an invoice document and extracts its fields exactly...
validator       default  get_vendor, get_purchase_order, get_goods_receipt  data-validation                         Matches invoice fields against the vendor master, purchas...
policy_checker  default  get_ap_policy, get_vendor, find_booked_invoices    policy-compliance-check                 Checks an invoice against the AP policy (duplicates, bank...

team invoice-processing: orchestrator ap_coordinator
  delegates: ap_coordinator -> extractor, validator, policy_checker
  shared: none

approvals: book_invoice (when tools:booking_needs_approval), notify_vendor (when tools:vendor_note_needs_approval)
```

With a model key you can go further:

- `awa run blueprint.yaml "Process invoice document DOC-1002." --stream` runs the recipe on its sample systems. It
  stops before it books the invoice above 10,000 EUR.
- `awa eval blueprint.yaml cases.yaml` runs its eval cases.

In the web app, **Start from an example** on the page **Data** opens an example as a new project. From a terminal,
`awa ui --example ID` does the same.

!!! note "What the release checks prove"

    Every example is checked offline on scripted models with every release. The blueprint is valid, a realistic case
    runs from start to end including the pause for approval, and every eval case passes. That proves the flow, not
    the quality of a real model's answers. Measure that with `awa eval blueprint.yaml cases.yaml --repetitions 3`
    before you rely on an example.

## Recipes

Recipes are multi-agent starting points in `examples/recipes/`. Their tools work on small built-in sample systems.
Each tool's description says what it does against the external system.

| Recipe | Process | Structure | Waits for a person |
| --- | --- | --- | --- |
| `invoice-processing` | Read the invoice, match it with order and receipt, check the AP policy, book or hold, tell the vendor | Team | Booking above 10,000 EUR; free-text messages to vendors |
| `customer-support-desk` | Triage, billing, technical and returns agents, refunds, escalation | Team with handoffs | Refunds above 50.00 |
| `lead-qualification` | Research, fit score, CRM update, first email | Team | Every email |
| `employee-onboarding` | Accounts, hardware, payroll, buddy, desk, badge, first-week plan | Network on a task board | In the systems of record |
| `contract-review` | Legal, compliance and commercial review, verdict with proposed changes | Workflow, reviews side by side | None (advice only) |
| `it-incident-triage` | Severity, paging, diagnosis from the runbook, status updates, notes afterwards | Team | Paging for the most severe incidents; public status updates |
| `market-research-report` | Research in parallel, analysis, sourced report, review loop | Network on a task board | None (internal report) |
| `content-production` | Brief, draft and edit until approved, compliance check, publication | Workflow with a loop | Every publication |
| `deep-research` | Plan, parallel web researchers, a citation agent that checks every quote, saved report | Team with a shared blackboard | Saving the report |

## Use case examples

Use cases in `examples/usecases/` are typical company processes that end in an action. They are small first tests on
sample data, and they show that the library applies to many kinds of processes. In each folder:

- connectors read the files and systems;
- `tools.py` holds only the rules, calculations and writes a model must not improvise;
- `make_data.py` writes the sample data again whenever you run it.

| Use case | Process | Waits for a person | Trigger |
| --- | --- | --- | --- |
| `purchase-requisition` | Requisition email, approved supplier, budget check, purchase order, order email | Orders above 1,000 EUR; every supplier email | no |
| `expense-report-audit` | Report lines and receipt PDFs against the travel policy, flags, reimbursement | Reports above 500 EUR or with a serious flag | no |
| `supplier-due-diligence` | Sanctions, adverse news and ownership checked on the web with cited sources | Every recorded decision | no |
| `order-to-cash-exceptions` | Orders (SQLite), invoices (Excel) and payments (CSV) compared, mismatches classified, dunning letters | Every dunning letter | no |
| `recruiting-cv-screening` | PDF and Word CVs to structured profiles, explainable score, shortlist, interview invitations | Every invitation | no |
| `meeting-notes-to-tasks` | Notes to decisions, owned tasks with due dates on the board, follow-up email | Every follow-up email | no |
| `compliance-news-watch` | Web search per topic, change detection against the previous digest, digest email with sources | Every digest email | schedule |
| `data-quality-reconciliation` | An Excel export dropped in a folder compared with a SQL table, update proposals | Every database change | file drop |
| `email-inbox-triage` | Classify mail, look up the sender, route to a team's task board, draft replies | Every outgoing reply | mailbox |
| `weekly-management-report` | KPIs from SQL against the prior week and the budget, variance explanations, summary email | The management email | schedule |
| `inventory-replenishment` | Stock over REST, demand from SQL, reorder rule, purchase orders in the ERP | Purchase orders above 5,000 EUR | schedule and webhook |
| `incident-postmortem` | Timeline from logs, tickets and deployments, blameless postmortem draft | Publishing the postmortem | webhook |
| `sales-operations` | Sales questions answered with SQL on 6,000 generated orders; expected answers computed from the data | Discounts above 10 percent | no |

The data of every use case is test data, public or generated; each `data/ORIGIN.md` names where it comes from. For
the processes on Microsoft 365 mail and files, see [Microsoft 365](microsoft-365.md).

## Adapt an example

1. **Copy the folder** and give the process its own `name:` in `blueprint.yaml`.
2. **Rewrite the instructions** for your process: your categories, formats and escalation paths.
3. **Connect your systems** one at a time. Use a [connector](connect.md) in the agents' `tools`. Or let a developer
   replace the body of a tool in `tools.py` and keep its name, inputs and description: the agents and the eval cases
   rely on them.
4. **Keep every approval.** Each one guards an action that cannot be undone or that leaves the company. Tighten a
   condition rather than removing it.
5. **Keep the rules in code.** Thresholds live in `tools.py` and in the approval conditions, never only in the
   instructions. `when: tools:booking_needs_approval` names a function in `tools.py`.
6. **Size the limits** to your volumes.
7. **Add your own eval cases**, above all the exceptions that go wrong today ([Test and improve](test.md)).

Requisitions, receipts, CVs, emails and web pages come from outside the process. Every example frames them as data,
and its `cases.yaml` has at least one document that tries to give the agent orders.

!!! example "In code"

    An example is a blueprint like any other, readable from Python ([Blueprints](../develop/blueprints.md)):

    <!-- guide:python -->
    ```python
    from awa.blueprints import Blueprint

    recipe = Blueprint.from_yaml_file("blueprint.yaml")
    print(recipe.team.orchestrator, [approval.tool for approval in recipe.approvals])
    ```

    <!-- guide:expect -->
    ```text
    ap_coordinator ['book_invoice', 'notify_vendor']
    ```
