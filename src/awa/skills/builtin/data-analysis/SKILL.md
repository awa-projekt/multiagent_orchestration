---
name: data-analysis
description: >-
  Answers business questions from data: pins down the exact question and metric, checks
  the data before trusting it, states assumptions, sanity-checks results, and presents
  findings as clear tables or described charts with their limits. Use when asked to
  analyze figures, compare periods or groups, explain a trend, or answer a question with data.
metadata:
  version: "1.1.0"
  category: data
  tags: analysis data metrics statistics trends tables charts
  risk: low
---
# Data Analysis

Never report "no data" or a zero without the period the data covers. When a filter
matches nothing, confirm with one unfiltered count first. Filter on the spellings the
schema lists: one country can be stored as "DE" and "Germany".

A correct answer to the wrong question is still wrong.
Settle the question first, then let the data answer it, and say how far it can be trusted.

## 1. Frame the question

- Restate the question as something measurable: which metric, for which population,
  over which period, compared with what. "Are sales down?" becomes "Is net revenue for
  March below February and below March last year, by region?"
- Define each metric precisely (gross or net, which statuses count, which date field).
  Use the organization's definition when one exists.
- Note the decision the answer will inform; it tells you how precise you need to be.

## 2. Check the data

- Inspect before computing: row counts, date range covered, missing values, duplicates,
  obvious outliers, and units or currencies.
- Confirm the data covers the question: the full period, all relevant groups, and the
  latest load. A missing week can look like a drop.
- Count every spelling of a category: list a text column's distinct values before
  filtering on it, and group the variants of one value (codes, names, languages).
- Reconcile one total with a trusted figure (a published report or the source system)
  when you can.

## 3. Analyze

- Start simple: counts, totals, averages, and medians by the obvious dimensions before
  anything elaborate.
- Compare like with like: the same length of period, the same definitions, seasonally
  comparable periods.
- Look at distributions, not only averages; one large value can drive a mean.
- Segment a surprising change to see whether it is broad or comes from one group.
- Correlation is not cause. Say "coincides with", not "caused by", unless the data
  design supports a causal claim.
- With small numbers, report counts alongside percentages and avoid strong claims.

## 4. Sanity-check the result

- Is the order of magnitude plausible? Do parts sum to the total?
- Would the conclusion survive a reasonable change in an assumption or definition?
- Does it agree with what else is known? If not, re-check before reporting a surprise.

## 5. Present

- Lead with the answer in one or two sentences, with the key number.
- Show supporting numbers in a compact table: labeled columns, units, the period, and
  the comparison.

```text
Net revenue by region, March vs February (EUR thousands)
region | Feb   | Mar   | change | change %
North  | 410.2 | 398.7 | -11.5  | -2.8%
South  | 355.0 | 301.4 | -53.6  | -15.1%
Total  | 765.2 | 700.1 | -65.1  | -8.5%
```

- When a chart would help, specify it in text: chart type, axes, series, period, and
  the one point it should make (for example "line chart, weekly orders, last 12 weeks,
  shows the drop starting in week 9").
- List assumptions, exclusions, and data limits after the findings, and say what further
  data would settle open points.

## Common failure modes

- Reporting "there is no data for X" when the filter value is spelled differently or the
  period lies outside the data.
- Comparing a partial current month with a full previous month.
- Mixing currencies, gross and net, or order date and ship date.
- Reporting a percentage change on a tiny base as a trend.
- Presenting an exploratory finding as a confirmed conclusion.
