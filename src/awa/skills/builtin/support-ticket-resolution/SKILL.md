---
name: support-ticket-resolution
description: >-
  Works a customer support ticket from diagnosis to confirmed resolution: reproduce and
  classify the problem, apply a fix or workaround grounded in a cited knowledge base
  article, confirm with the customer, and close with a clean record. Use when handling
  or resolving a support ticket, case, or help desk request.
metadata:
  version: "1.0.0"
  category: support
  tags: support tickets helpdesk troubleshooting knowledge-base resolution cases
  risk: medium
---
# Support Ticket Resolution

A ticket is resolved when the customer's problem is gone and they agree, not when a
reply is sent. Diagnose before answering, cite what you rely on, and confirm.

## 1. Diagnose

- Restate the problem in one sentence: what the customer tried, what they expected,
  what happened instead, since when, and how many users are affected.
- Check the account and history: plan, recent changes, earlier tickets, known incidents.
  A known incident means link the ticket and give the incident status instead of debugging.
- Reproduce or verify with the details given (error text, steps, screenshots). If
  essential facts are missing, ask for all of them in one message, numbered.
- Classify: question, defect, configuration issue, account or billing, feature request,
  or outage. Check severity and whether it must be escalated (security, data loss,
  legal, many customers).

## 2. Resolve

1. Search the knowledge base and resolved tickets for the symptom. Use an article only
   if its conditions match this case (version, plan, platform).
2. Apply or explain the fix, or a workaround with its limits. Cite the article
   (title and identifier) you relied on, internally and, if public, to the customer.
3. If no article fits, work it out, mark the solution as not yet documented, and
   propose a new or corrected article.
4. Actions on the customer's account (resets, refunds, data changes) only within your
   authority and after checking identity; otherwise request approval.
5. Hand defects to engineering with reproduction steps, impact, and ticket links.

## 3. Confirm and close

- Ask the customer to confirm the fix works, with a clear way to reopen.
- Close only after confirmation, or after the agreed waiting period with no reply.
- Record: root cause, resolution, article cited or created, time spent, and tags.

## Resolution note

```text
Problem: <one sentence> | Class: <type> | Severity: <level>
Diagnosis: <what was checked and found>
Resolution: <fix or workaround> - source: <article title/id or "undocumented">
Customer action: <what they did or need to do>
Confirmed: <yes, date | pending until date> | KB follow-up: <new or fix article>
```

## Common failure modes

- Sending a knowledge base link that answers a similar but different problem.
- Asking for information one question at a time over several days.
- Closing on "sent a fix" without confirmation, then reopening.
- Treating a repeated symptom across tickets as isolated instead of reporting a pattern.
