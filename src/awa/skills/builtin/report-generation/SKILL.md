---
name: report-generation
description: >-
  Produces structured business reports (status, performance, review, or incident
  reports) that lead with an executive summary, back every number with its source and
  period, and separate facts from interpretation and recommendations. Use when a result
  must be written up for managers, stakeholders, or a recurring reporting cycle.
metadata:
  version: "1.0.0"
  category: communication
  tags: reports executive-summary business kpis metrics stakeholders
  risk: low
---
# Report Generation

Readers decide from the first half page. Put the conclusion there, and make every
number behind it traceable.

## Before writing

- Identify the audience, the decision or question the report serves, the period it
  covers, and the required format or template. Use a provided template exactly.
- Collect the figures first, each with its source (system, file, query, or document),
  the date it was taken, and its period. Do not write narrative around numbers you have
  not yet confirmed.
- If the data is incomplete or late, decide how to present that before you write.

## Structure

Use `references/skeleton.md` unless a template is provided:

1. **Executive summary**: three to five sentences or bullets: the headline result, the
   most important change, the main risk, and the decision or action needed.
2. **Key figures**: a short table of the metrics that matter, with period, comparison
   (previous period, target, or budget), and change.
3. **Details**: one section per topic, each opening with its main point.
4. **Risks and issues**: what could affect the outcome, with owner and status.
5. **Recommendations or next steps**: numbered, each with an owner.
6. **Data and method notes**: sources, definitions, exclusions, and known gaps.

```text
Executive summary (Q2, net revenue in EUR)
- Revenue was 4.1m, 3% below target (4.23m) and 6% above Q2 last year (source: ledger).
- The shortfall comes from one region, where two large orders moved to Q3.
- Risk: if both orders slip again, the full-year target is missed by about 2%.
- Decision needed: approve the extra sales support for that region by 15 July.
```

## Numbers

- State the unit, currency, period, and basis for every figure ("EUR, Q2, net of
  returns"). Use one number format throughout.
- Give comparisons as both absolute and relative change ("+120 orders, +8%"), and name
  what it is compared with.
- Round sensibly for the audience, but make sure totals still add up; if rounding causes
  a difference, say so.
- Never fill a gap with an estimate presented as data. Mark estimates, forecasts, and
  preliminary figures explicitly, with how they were made.
- Recompute derived figures (totals, percentages, averages) from the underlying numbers
  before publishing.
- When figures from two sources disagree, show both or explain which you used and why.

## Writing

- Lead each section with the finding, then the evidence: "Returns rose 15% because of
  one product line" rather than a chronological account.
- Separate facts ("revenue was 1.2m") from interpretation ("likely driven by...") and
  from recommendations ("we should..."), using clear wording for each.
- Report bad news plainly and early. Do not bury a missed target in the details.
- Keep it as short as the decision allows; move supporting detail to an appendix.

## Before publishing

- Does the executive summary stand alone and match the details?
- Does every number have a source, period, and unit, and do the tables add up?
- Are all known data gaps and assumptions stated?
