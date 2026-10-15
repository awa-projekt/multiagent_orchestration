---
name: contract-obligation-tracking
description: >-
  Extracts dates, obligations, notice periods, renewals and payment terms from a
  contract into a tracking list with deadlines and owners. Use when a contract is signed
  or renewed, when preparing a deadline calendar, or when asked what a contract requires
  of us and by when.
metadata:
  version: "1.0.0"
  category: legal
  tags: contracts obligations deadlines renewal notice-period termination calendar
  risk: medium
---
# Contract Obligation Tracking

Turn the text of a contract into a list that people can act on. This is an extraction of
what the contract says, with the clause number as evidence, not a legal opinion.

## Steps

1. **Identify** the contract: parties, title, effective date, term, version. Note if the
   text is unsigned, a draft, or has amendments you were not given.
2. **Extract dates**: start, end, renewal date, notice deadline for termination or
   non-renewal, price review dates, milestones, reporting dates. Compute the notice
   deadline as a calendar date from the rule in the clause, and show the rule.
3. **Extract obligations**: for each, who must do what, by when, with what consequence
   if missed (penalty, termination right, price effect). Mark which side is us.
4. **Extract money terms**: price, indexation, invoicing and payment terms, liability
   caps, penalties. Quote amounts with currency.
5. **Assign owners** as a role (for example procurement, finance, service owner); leave
   the person blank when you do not know it.
6. **Flag** automatic renewals, one-sided termination rights, unusual notice periods and
   anything ambiguous, with the clause text. Ambiguity goes to a person, not to a guess.

## Decision rules

- Every row cites a clause number or page. A row without a citation is not allowed.
- If a deadline depends on an event (delivery, signature), store the rule and the
  unknown trigger date; do not invent a date.
- Do not interpret enforceability or advise on negotiating; hand those to legal.
- Missing amendments, annexes or schedules are listed as gaps.

## Output

```text
Contract: <title> | Parties: <A> and <B> | Effective <date> | Ends <date>
| Type | What | Due | Owner | Clause | Note |
| Deadline | Notice of non-renewal | 2027-03-31 (90 days before 2027-06-30) | procurement | 12.2 | auto-renews 12 months |
| Obligation | Quarterly usage report | each quarter end + 10 days | service owner | 7.1 | late fee 1% |
Gaps: annex B pricing not provided
```

## Common failure modes

- Counting a notice period from the wrong date, or in working days instead of calendar
  days without saying so.
- Merging two obligations into one row, which loses the separate deadlines.
