---
name: vendor-onboarding
description: >-
  Checks a new supplier's registration data and documents against the onboarding policy
  and prepares the vendor record for approval, with gaps, risk flags and a verification
  plan for bank details. Use when a new vendor must be set up or an existing vendor
  changes legal or payment details.
metadata:
  version: "1.0.0"
  category: procurement
  tags: vendors suppliers onboarding due-diligence vendor-master bank-details verification
  risk: high
---
# Vendor Onboarding

A vendor record controls where money goes. Collect and check evidence, flag what is
missing or inconsistent, and leave approval and activation to the accountable people.
Never activate a vendor or change bank details yourself.

## Steps

1. **Identify the request**: who needs the vendor, for what spend, expected annual
   volume, whether it is new or a change to an existing record (search for duplicates by
   legal name, tax number and bank account first).
2. **Collect the required data**: legal name, registered address, registration and tax
   numbers, contacts, payment terms, currency, bank account holder and account number.
3. **Check the documents** you were given against the policy: registration or trade
   extract, tax certificate, bank confirmation, insurance or certificates where the spend
   category requires them. Compare names, addresses and numbers across documents; every
   mismatch is a finding.
4. **Check the risk flags**: sanctions or watchlist screening result if you have a tool
   for it (otherwise say it is not done), conflict of interest declared by the requester,
   spend above the due-diligence threshold, high-risk country or category, data
   protection (does the vendor process personal data: a contract addendum is needed).
5. **Plan the bank verification**: call-back to a contact already known from an
   independent source, or the vendor portal; never rely on a number or address in the
   documents that came with the request.
6. **Prepare the record** and the decision request for the vendor master owner.

## Decision rules

- A bank account holder that differs from the legal name is a hold, not a note.
- A request that is urgent and asks to skip checks is a fraud signal: escalate.
- Missing documents are listed with the exact document name needed; do not accept
  screenshots of documents that must be originals when the policy says so.
- Do not store more personal data than the vendor record needs.

## Output

```text
Vendor request: <legal name> | Type: new | Requester: <name> | Category: IT services
Duplicate check: no match (name, tax no., bank account)
Documents: registration ok; tax certificate missing; bank letter ok
Findings: account holder "Example Trading" differs from legal name "Example GmbH"
Risk flags: sanctions screening not run; personal data processing: yes
Bank verification: call-back to contact from vendor website, by <role>
Status: hold, not ready for approval (2 blocking findings)
```

## Common failure modes

- Verifying the bank details with the contact written on the same request.
- Approving a vendor with a near-duplicate of an existing record.
- Treating a missing screening as a pass.
