---
name: sop-writing
description: >-
  Writes standard operating procedures that a trained person can follow without help:
  purpose, scope, roles, prerequisites, numbered imperative steps with expected results,
  exceptions, controls, and version history. Use when documenting a recurring task as
  an SOP, work instruction, or runbook, or when revising an existing procedure.
metadata:
  version: "1.0.0"
  category: operations
  tags: sop procedures work-instructions runbook documentation standard-operating-procedure
  risk: low
---
# SOP Writing

An SOP is tested by the person who follows it at 3 a.m. or on their first week. If they
must guess, ask someone, or improvise, the procedure is not finished.

## Before writing

- Watch or walk through the task with someone who does it well; note every decision,
  system screen, and workaround they use. Do not write from the ideal process alone.
- Define the audience (role and training level) and write for it.
- Confirm the policies and controls the procedure must implement and cite them.

## Structure

1. **Title, ID, version, owner, effective date, next review date.**
2. **Purpose**: one or two sentences on what the procedure achieves.
3. **Scope**: when it applies and when it does not.
4. **Roles**: who performs, approves, and is informed, by role.
5. **Prerequisites**: access, tools, inputs, and conditions before starting.
6. **Steps**: numbered, one action per step, starting with a verb.
7. **Exceptions**: what to do when a step fails or a condition is not met.
8. **Records**: what must be saved, where, and for how long.
9. **References and version history**: related policies, forms, and changes.

## Writing the steps

- Imperative and specific: "Enter the order number in the Search field and press Enter",
  not "the order should be located".
- Give the expected result after steps where the user could go wrong ("The status
  changes to Approved"), so they know it worked.
- Put decisions as explicit branches: "If the amount is over 5,000, go to step 9;
  otherwise continue."
- Put warnings before the step they apply to, not after.
- Use exact names for screens, fields, buttons, and forms; include values and limits.
- Mark control steps (approvals, second checks) so they are not skipped.

## Quality check

- A person of the target role, new to the task, can complete it using only the SOP.
- Every exception ends in a defined state (retry, escalate to a role, stop).
- No step depends on knowledge that is not written down or linked.

```text
SOP-FIN-014 v1.2 | Owner: AP lead | Effective: <date> | Review: <date + 12 months>
Purpose: Pay approved supplier invoices in the weekly payment run.
Scope: all entities; excludes urgent manual payments (see SOP-FIN-015).
Roles: AP clerk (performs), finance lead (approves the run)
Prerequisites: payment role access; invoices approved by Thursday 12:00
Steps:
 1. Open the payment proposal for this week's run date.
    Result: the list shows only invoices with status Approved.
 2. If any supplier's bank details changed in the last 30 days, remove that line and
    send it to vendor verification (control).
Exceptions: proposal empty -> check the approval queue, then inform finance lead.
```

## Common failure modes

- Steps that describe goals ("ensure data is correct") instead of actions.
- A procedure that is out of date because nobody owns its review.
