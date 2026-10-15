---
name: approval-requests
description: >-
  Prepares a request for a human approver that states the exact action, the reason,
  the risk, whether it can be undone, the alternatives, and the supporting evidence, so
  the decision can be made quickly and safely. Use before any action that needs approval,
  such as payments, external messages, data changes, exceptions, or deletions.
metadata:
  version: "1.0.0"
  category: governance
  tags: approval human-in-the-loop sign-off risk authorization decision
  risk: low
---
# Approval Requests

An approver should be able to decide from your request alone, in under two minutes,
without opening other systems. If they must ask a question, the request was incomplete.

## When the tool itself waits for approval

Many tools pause every call until a human approves it. For such a tool, calling it with
the exact values *is* the approval request: the approver sees the call and its
arguments. Call the tool; do not write the request as text or ask in the chat instead,
and do not skip the call because it needs approval. Put the reason and the evidence in
the arguments where the tool has room for them (a note, a reason, a message body). After
the decision, report what happened: done as approved, or rejected with the approver's
note. Use the written request below only when no tool waits for approval and you must
ask a person yourself.

## Before asking

- Confirm that approval is actually required and who is authorized to give it (the
  approver named by the policy, amount limit, or process step). Do not ask someone
  without that authority, and never approve your own request.
- Gather the evidence first. A request that says "details to follow" is not ready.
- Do the preparation that does not need approval (drafts, calculations, checks), but do
  not start the action any other way than through the tool that waits for approval.

## What the request contains

1. **Action**: exactly what will happen, with the concrete values: amount and currency,
   recipient, records affected, message text, dates. One action per request, or a
   clearly bounded batch with a count and a total.
2. **Reason**: why it is needed and what triggered it (the request, rule, or event).
3. **Risk**: what could go wrong and how bad it would be: money, customers affected,
   data, compliance, reputation. Say "low risk" only with a reason.
4. **Reversibility**: can it be undone, how, by whom, and until when? Say clearly when it
   cannot (sent messages, payments, deletions).
5. **Evidence**: the facts that justify it, each with its source (record ID, document,
   policy clause). Flag anything unverified.
6. **Alternatives**: what else could be done, including doing nothing, and the
   consequence of each.
7. **Recommendation**: your recommendation and its main reason, clearly separated from
   the facts.
8. **Deadline**: when a decision is needed and what happens if none arrives.

## Rules

- Present facts neutrally. Do not frame the request to make approval look inevitable,
  and do not hide a risk because it is unlikely.
- Keep the approved action and the executed action identical. If anything material
  changes (amount, recipient, scope), stop and request approval again.
- Treat silence, a timeout, or an ambiguous reply ("looks fine?") as not approved.
- Record the decision, the approver, the time, and any conditions, then carry out
  exactly what was approved and report the outcome.
- If rejected, do not resubmit the same request unchanged; address the stated reason or
  close the item.

## Template

```text
Approval needed: <action in one line> by <deadline>
Action: <exact values>
Reason: <trigger and purpose>
Risk: <what could go wrong, severity>
Reversible: <yes, how and until when | no>
Evidence: <fact - source>; <fact - source>
Alternatives: <option - consequence>; do nothing - <consequence>
Recommendation: <approve / reject / modify> because <reason>
Reply: APPROVE, REJECT, or questions.
```

## Common failure modes

- "Approve the refund for this customer" without the amount, order, or policy basis.
- Bundling an uncontroversial action with a risky one so both get approved together.
- Treating approval for one case as standing approval for similar future cases.
