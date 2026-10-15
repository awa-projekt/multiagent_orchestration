---
name: data-validation
description: >-
  Validates records against explicit rules (required fields, types, formats, ranges,
  allowed values, cross-field and cross-record constraints) and reports every violation
  deterministically with record, field, rule, and value. Use before importing, approving,
  or passing on data, or when asked whether a dataset is clean.
metadata:
  version: "1.0.0"
  category: data
  tags: validation data-quality rules records checks violations
  risk: low
---
# Data Validation

Validation answers one question per record and rule: does it pass? The same input and
rules must always give the same report.

## Establish the rules first

- Write down every rule before checking any record, each with an identifier, the field
  or fields it covers, and the exact condition. Example:
  `R3 amount: decimal, > 0, at most 2 decimal places`.
- Take rules from the schema, the policy, or the requester. If a rule you need is not
  stated (is an empty string the same as missing? which date format?), ask, or state
  the assumption you applied in the report.
- Do not add rules from intuition ("this value looks too high"). Report such
  observations separately as anomalies, not as violations.

## Rule types, in checking order

1. **Structure**: the expected columns or keys exist; no unexpected ones if the schema is
   closed.
2. **Required**: required fields are present and not empty.
3. **Type and format**: numbers parse, dates are valid calendar dates in the stated
   format, identifiers match their pattern, codes are from the allowed list.
4. **Range**: values fall within the stated minimum and maximum.
5. **Cross-field**: relations inside one record (end after start, total equals the sum of
   its parts, country and postal code agree).
6. **Cross-record**: uniqueness of keys, references to records that exist, totals across
   records.

Check every rule on every record; do not stop at the first failure unless told to. A
record that fails structure or type checks may skip the later checks that depend on the
broken field; list those as `not checked`.

## Being deterministic

- Compare exact values. Do not trim, round, change case, or reinterpret unless a rule
  says so, and apply the same normalization to every record.
- Evaluate rules in a fixed order and report records in input order.
- Never fix data while validating. Suggested corrections go in a separate column and are
  clearly marked as suggestions.

## Report format

```text
Checked: 1,240 records against 9 rules (R1-R9). Passed: 1,198. Failed: 42.
Violations by rule: R2 required (18), R5 date format (15), R8 duplicate key (9).

record | field      | rule | value        | problem
row 17 | email      | R2   | ""           | required field is empty
row 52 | start_date | R5   | "2024-13-02" | month 13 is not a valid date
row 88 | order_id   | R8   | "A-1009"     | also used by row 12
```

- Give counts per rule so the reader sees systemic problems at a glance.
- Quote the offending value exactly; mask personal or secret data the reader does not
  need (show `j***@example.com`).
- If there are more violations than fit, report the counts in full and list the first N
  per rule, saying that the list is truncated.
- End with a verdict: `pass`, `pass with warnings` (anomalies only), or `fail`.

## Common failure modes

- Treating a blank, `N/A`, `0`, and a missing field as the same without a rule saying so.
- Accepting `2024-02-30` because it matches the date pattern.
- Reporting a sample of violations as if it were the complete list.
