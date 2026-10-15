---
name: budget-variance-analysis
description: >-
  Compares actuals with budget or forecast by cost center and line, separates timing,
  volume, price, and one-off effects, explains material variances with evidence, and
  states the full-year impact and actions. Use when explaining budget versus actual
  results, preparing a monthly variance commentary, or reviewing a forecast.
metadata:
  version: "1.0.0"
  category: finance
  tags: budget variance actuals forecast cost-center commentary month-end
  risk: low
---
# Budget Variance Analysis

A variance commentary answers three questions: how big is the gap, why did it happen,
and does it change the year? Explain the material lines well rather than every line briefly.

## Before analyzing

- Confirm both sides use the same basis: period, entity, currency and rates, accounting
  treatment, and the budget version (original, reforecast). Mismatched bases create
  false variances.
- Check that actuals are complete for the period: accruals posted, late invoices, and
  intercompany charges. Note anything still open.
- Use the organization's materiality threshold; otherwise investigate lines that differ
  by more than 10% and a meaningful absolute amount for their size.

## Analysis

1. Compute variance per line as actual minus budget, in amount and percent, and state
   the sign convention: say "favorable" or "unfavorable", never just "+" or "-".
2. Sort by absolute amount; explain from the largest down until the explained part
   covers most of the total variance.
3. Classify each material variance:
   - **Timing**: spend or income moved between periods; reverses later.
   - **Volume**: more or fewer units, hires, or transactions than planned.
   - **Price or rate**: different unit cost, salary, exchange rate, or discount.
   - **One-off**: non-recurring events (settlement, write-off, grant).
   - **Error or misposting**: wrong account or cost center; correct, do not explain.
4. Support each explanation with evidence (invoice, headcount report, order data, the
   budget owner's confirmation). An unconfirmed explanation is marked as such.
5. Estimate the full-year effect: timing nets to zero; volume and price effects usually
   continue; one-offs do not.

## Rules

- Do not describe the number as the explanation ("costs were higher due to higher costs").
- Offsetting variances inside a total must be shown, not netted away.
- Reforecast only through the agreed process; commentary does not change the budget.

## Output

```text
Cost center: <name> | Period: <month, YTD> | Budget: <version> | Currency: <code>
Total: actual 412k vs budget 380k, 32k (8%) unfavorable
Line         | variance         | type   | explanation (evidence)            | full year
Contractors  | 25k unfavorable | volume | 2 extra contractors from March (PO) | +75k
Travel       | 9k favorable    | timing | conference moved to Q3 (event invoice) | 0
Software     | 16k unfavorable | price  | license renewal +12% (contract)     | +16k
Actions: <owner - action - date> | Open: accrual for March audit fee not yet posted
```

## Common failure modes

- Comparing actuals with an outdated budget version.
- Explaining small lines at length while the largest variance is "under review".
- Reporting a favorable variance that is only a late invoice.
