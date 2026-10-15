---
name: audit-evidence-collection
description: >-
  Collects, labels, and indexes evidence for an internal or external audit request:
  maps each request to the control it tests, gathers complete and unaltered records for
  the right period and sample, and tracks gaps and deadlines. Use when responding to an
  audit request list, preparing for a compliance audit, or gathering control evidence.
metadata:
  version: "1.0.0"
  category: risk
  tags: audit evidence controls compliance samples request-list documentation
  risk: low
---
# Audit Evidence Collection

Auditors test whether a control worked during a period. Evidence must show that,
completely and verifiably. Explaining a gap early is better than an incomplete file late.

## Understand each request

- Log every request with its identifier, wording, the control or requirement it tests,
  the period covered, the due date, and the owner who holds the evidence.
- Clarify ambiguous requests with the auditor through the agreed contact before
  collecting. Do not guess what "access reviews" means for this audit.
- Check whether the request asks for the full population or a sample, and who selects
  the sample. Auditors usually pick samples from a population you provide.

## Collect

1. Take evidence from the system of record: exports, reports with their parameters,
   logs, approved documents, tickets with their history.
2. Keep it original and complete: no edits, no cropped screenshots that remove dates or
   system names; show how a report was produced (filter, date run, who ran it).
3. Make sure each item covers the requested period and shows the control operating:
   who did what, when, and the approval or review trail.
4. For a population, reconcile it to a known total so completeness can be checked.
5. Share only what is requested. Redact personal or confidential data not needed for
   the test, following the data protection rules, and note each redaction.

## Organize

- Name files consistently: `<request id>_<short description>_<period>`.
- Keep an index of every item: request, file, source system, date extracted, extracted
  by, and what it demonstrates.
- Deliver through the agreed secure channel and record what was sent and when.

## Gaps and issues

- If evidence does not exist or shows the control failed, do not create, backdate, or
  alter records. Report the gap to the control owner and audit coordinator at once.
- Record gaps with the reason and any compensating evidence.

## Evidence index

```text
A-07 Quarterly access review | Q1-Q4 | A-07_access-review_Q1-Q4.pdf
     source: identity system export, run 3 Feb by ops analyst | status: sent 5 Feb
A-12 Change approval | full year | A-12_change-population_FY.csv
     source: ticket system, 1,204 changes (reconciled to report) | status: sample pending
A-15 Backup restore test | H2 | no file
     status: gap - no restore test in Q4; control owner notified 2 Feb
```

## Common failure modes

- Screenshots without dates, system names, or the user who ran the report.
- A population that cannot be reconciled, so the sample is rejected.
- Sending more data than requested, including personal data.
- Discovering a missing control on the due date instead of the first day.
