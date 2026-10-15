---
name: expense-review
description: >-
  Reviews employee expense claims against the expense policy: receipt present and
  matching, allowed category and limits, business purpose, correct approver, and
  anomalies such as duplicates or split claims, with a decision per line. Use when
  checking expense reports, travel claims, or card transactions before reimbursement.
metadata:
  version: "1.0.0"
  category: finance
  tags: expenses reimbursement receipts travel policy anomalies claims
  risk: low
---
# Expense Review

Most claims are honest and most errors are accidental. Check every line the same way,
decide on the policy text, and explain each rejection so the employee can fix it.

## Before reviewing

- Use the current expense policy for the claimant's entity and country, and note its
  version. Limits, per diems, and allowed categories differ by location and grade.
- Check the claim period, currency, and exchange rate basis the policy prescribes.

## Per line

1. **Receipt**: present, legible, itemised where required, and matching the claimed
   amount, date, merchant, and currency. Missing receipts follow the policy's
   declaration rule and limit.
2. **Category**: the expense type is allowed and correctly categorized (a dinner is not
   "office supplies").
3. **Limits**: within the per-item, per-day, or per-person limit; for group meals,
   attendees and their count are listed.
4. **Business purpose**: stated, specific, and plausible for the role and trip dates.
5. **Tax**: tax amount and invoice details where the organization reclaims tax.
6. Decide per line: approve, approve partly (to the limit), return for information, or reject.

## Anomalies to flag

- The same receipt, amount, or merchant and date claimed twice, or also on a company card.
- Split claims: one expense spread across lines or days to stay under a limit.
- Expenses outside travel dates, on weekends or holidays without explanation.
- Round-number amounts, handwritten or edited receipts, or merchant category mismatch.
- Personal items (family travel, entertainment, fines) and upgrades beyond policy.
- The approver is the claimant, reports to the claimant, or lacks authority for the amount.

Flag anomalies neutrally as questions. A flag is not an accusation; suspected fraud goes
to the designated owner, not to the claimant's team.

## Output

```text
Claim EXP-0932 | <employee id> | 3-5 June, trip to <city> | Policy v2026-01
Line | item          | amount  | decision       | reason
1    | Hotel 2 nts   | 310.00  | approve        | within 180/night limit
2    | Dinner (4)    | 260.00  | approve partly | 200.00 max (50 pp); attendees listed
3    | Taxi          | 45.00   | return         | receipt missing; declaration required
4    | Train         | 75.00   | return         | same fare on company card, 4 June
Flags: line 4 possible duplicate of card transaction (question, not accusation)
Total claimed 690.00 | approvable now 510.00 | Approver: <manager, within limit>
```

## Common failure modes

- Approving a claim because the total is small, without checking lines.
- Rejecting without citing the policy clause and how to fix it.
- Converting currency at a rate other than the one the policy specifies.
