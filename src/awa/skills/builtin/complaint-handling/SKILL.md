---
name: complaint-handling
description: >-
  Handles a customer complaint from first reply to resolution: acknowledges the problem,
  separates facts from feelings, finds the cause, offers a remedy within authority and
  records the follow-up. Use when a customer is unhappy with a product, service, invoice
  or earlier support answer.
metadata:
  version: "1.0.0"
  category: support
  tags: complaints customer-service resolution apology remedy follow-up escalation
  risk: medium
---
# Complaint Handling

A complaint is information about a failure and a chance to keep the customer. Reply
quickly and honestly, fix what you are allowed to fix, and hand the rest to a person with
the full facts so the customer does not have to repeat them.

## Steps

1. **Read for the facts**: what happened, when, which order or ticket, what the customer
   expected, what they want now. Note the stated impact (money, time, safety, health).
2. **Check the record**: order, delivery, earlier contacts, promises made. Say plainly if
   the customer is right, partly right, or if the record shows something different.
3. **Acknowledge** in the first lines: name the specific problem in your own words,
   apologize for the part that was our failure, without admitting liability for things
   you cannot verify.
4. **Resolve within authority**: correct the error, resend, repair, credit within your
   limit (use `refund-and-credit-requests` for money). Offer only remedies that exist.
5. **Escalate** immediately for safety or health claims, legal threats, press or
   regulator mentions, discrimination or harassment, large amounts, or a third contact on
   the same issue. Escalate with a summary, do not argue the case.
6. **Close the loop**: say what happens next and by when, then record cause, remedy and
   customer response, and tag the root cause (product, process, communication, supplier).

## Decision rules

- Do not blame the customer, a colleague, or a supplier in the reply.
- No promises beyond authority; "I will check and come back by <date>" is better than a
  guess.
- Do not copy template text that ignores what the customer wrote.
- One apology, specific and short; avoid repeated apologies and defensive explanations.

## Output

```text
Complaint: <one sentence> | Customer: <name> | Ticket 4471 | Contact no. 2 on this issue
Facts: delivered 2026-09-28; customer reports missing part; record shows part not packed
Assessment: customer is right (our packing error)
Remedy: part resent, express, within authority
Escalation: none / <who and why>
Reply draft: <text with acknowledgement, remedy, date>
Root cause tag: process (packing check)
```

## Common failure modes

- Answering the policy question and ignoring the emotional state of the message.
- Offering a remedy that another team must carry out without telling that team.
- Closing the ticket before the customer confirmed or the date passed.
