---
name: data-subject-requests
description: >-
  Prepares the handling of a data subject request (access, correction, deletion or
  export of personal data): verifies the requester, scopes the systems to search,
  tracks the legal deadline and drafts the response for the privacy owner. Use when a
  person asks what data is held about them or asks for it to be changed or removed.
metadata:
  version: "1.0.0"
  category: legal
  tags: privacy gdpr data-subject access deletion personal-data deadlines
  risk: high
---
# Data Subject Requests

Requests about personal data have a deadline and legal consequences. Prepare the work and
the response; the privacy owner decides what is released, withheld or deleted. This is a
process aid, not legal advice.

## Steps

1. **Classify the request**: access, correction, deletion, restriction, export, objection.
   One message can contain several; list each separately.
2. **Record the dates**: received on, and the response deadline from the policy you were
   given (many regimes allow about one month; use the organization's rule, not a guess).
   Mark the request `urgent` when fewer than ten days remain.
3. **Verify the requester**: the request must come from the data subject or an authorized
   representative with proof. Check against data you already hold; ask for the minimum
   extra proof. Never send personal data to an unverified requester.
4. **Scope the search**: list the systems and stores that may hold the person's data
   (customer records, mail, tickets, invoices, logs, backups, HR) and the search keys
   (name, email, customer number). Mark systems you cannot search.
5. **Collect findings** per system: what categories of data, where, why it is held, and
   whether a legal retention duty, a contract or a third party's rights limit release or
   deletion. Do not copy personal data into notes more than necessary.
6. **Prepare the response** for review: what is provided or done, what is withheld and
   the reason, the rights of the person (complaint to the authority), and the date.
7. **Log** the request, decision, who approved and when it was answered.

## Decision rules

- Do not delete anything: deletion is carried out by an authorized person after
  approval, and legal holds and retention periods are checked first.
- Data about other people in the same record is redacted before release.
- If the identity cannot be verified, the answer says so and what is needed; it does not
  reveal whether data exists.
- Escalate requests that come with a legal threat, a regulator, a dispute or an employee
  matter.

## Output

```text
Request DSR-0412 | Type: access + deletion | Received 2026-09-20 | Due 2026-10-20 (policy)
Requester: verified by registered email + customer number / not verified (reason)
Systems: CRM (found), tickets (found), invoices (found, retention 10 years), mail (not searchable)
Findings: <categories per system>
Limits: invoices kept for legal retention; deletion not possible there
Response draft: <text>
Needs decision: privacy owner
```

## Common failure modes

- Releasing data before the requester is verified.
- Missing the deadline because the clock started when the request was assigned.
- Promising full deletion while retention duties apply.
