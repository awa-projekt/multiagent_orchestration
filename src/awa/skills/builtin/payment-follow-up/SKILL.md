---
name: payment-follow-up
description: >-
  Reviews overdue customer invoices and prepares the next reminder step: checks payment
  and dispute status, picks the reminder level from the dunning policy and drafts a firm,
  polite message. Use when working an overdue receivables list or answering a customer
  about an unpaid invoice.
metadata:
  version: "1.0.0"
  category: finance
  tags: dunning reminders overdue receivables collections payment accounts-receivable
  risk: medium
---
# Payment Follow-up

The goal is to get a justified invoice paid without damaging the customer relationship.
Check the facts before you write, follow the organization's dunning policy, and leave
fees, interest, legal steps and write-offs to a person.

## Steps

1. **Verify the facts** for each invoice: amount, due date, days overdue, payments and
   credit notes received (including unallocated ones), open disputes or tickets, a
   promised payment date.
2. **Stop and route** when any of these holds: the invoice is disputed, a payment
   arrived but is unallocated, the customer is in an insolvency or legal process, or the
   customer is a key account with a named owner. Do not send a reminder.
3. **Pick the level** from the dunning policy you were given by days overdue and number of
   earlier reminders. Without a policy use: friendly reminder at 1-14 days, reminder at
   15-30, final notice at 31 and more, and say that no policy was provided.
4. **Draft the message**: invoice number, date, amount, due date, how to pay, the
   requested action and date, a contact. Tone by level: helpful, firm, formal. Name no
   fees or interest unless the policy states them and a person approved them.
5. **Record** what was prepared, for which level, and the next planned step and date.

## Decision rules

- Never threaten legal steps, collection agencies or credit reporting yourself; flag the
  case for the accountable person.
- Do not include other customers' data or the full invoice list unless the recipient is
  the account contact for them.
- A customer who disputes the invoice gets a reply that asks for details and starts the
  dispute process, not a reminder.
- One message per customer for several overdue invoices, with a table.

## Output

```text
Customer: <name> | Overdue: 2 invoices, 1,540.00 EUR | Oldest: 34 days
Decision: final notice for INV-2291 (policy level 3); INV-2305 held (disputed, ticket 4471)
Draft:
Subject: Final notice: invoice INV-2291
<message text>
Next step: <date> escalate to accounts receivable lead if unpaid
```

## Common failure modes

- Sending a reminder for an invoice that was paid yesterday and not yet allocated.
- A tone that escalates faster than the policy.
- Copying the whole dunning history into the customer message.
