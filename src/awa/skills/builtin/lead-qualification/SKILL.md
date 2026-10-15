---
name: lead-qualification
description: >-
  Scores inbound or outbound sales leads against the ideal customer profile (fit) and
  observed buying signals (intent), with evidence per criterion, a tier, and a routing
  decision. Use when deciding which leads sales should pursue, nurture, or disqualify,
  or when enriching a lead list before outreach.
metadata:
  version: "1.0.0"
  category: sales
  tags: leads qualification icp scoring prospects pipeline routing
  risk: low
---
# Lead Qualification

Qualification decides where sales time goes. Score on evidence you can point to, and
keep fit (is this the right kind of customer?) separate from intent (are they buying now?).

## Before scoring

- Use the organization's ideal customer profile (ICP) and scoring rules when provided;
  otherwise use `references/icp-scorecard.md` and say so in the result.
- Collect the facts first: company size, industry, region, role of the contact, current
  solution, stated need, budget or timing signals, and every interaction so far.
- Use only information the lead provided or that is publicly and legitimately available.
  Do not guess personal details, and do not score on protected characteristics.

## Scoring

1. **Fit**: score each ICP criterion (for example industry, size, region, use case,
   technical match) as met, partly met, not met, or unknown, with the source.
2. **Intent**: score observed signals (requested a demo, pricing page visits, stated
   timeline, budget holder involved, active evaluation) the same way.
3. **Disqualifiers**: check hard exclusions first (sanctioned region, unsupported
   market, existing customer handled by another team, competitor, student or job seeker).
   A disqualifier ends scoring regardless of the other points.
4. **Tier**: combine fit and intent into a tier:
   - A: high fit, high intent: route to sales now.
   - B: high fit, low intent: nurture with relevant content; recheck on new signals.
   - C: low fit, high intent: qualify manually; the need may be real but atypical.
   - D: low fit, low intent, or disqualified: close with a reason.

## Decision rules

- "Unknown" is not "not met". Many unknowns means the lead needs discovery questions,
  not a low score; list the questions.
- One strong signal does not make a lead: a large company with no stated need is B, not A.
- Never inflate a score to fill a pipeline target, and never lower one to avoid work.
- Re-score when new facts arrive, and record what changed.

## Output

```text
Lead: <company> / <contact role> | Source: <channel, date>
Fit: 3.5/5 - industry met (website), size met (220 staff, registry), region met,
     use case partly met (asked about reporting only), tech unknown
Intent: 2/4 - demo requested (form, 12 May); no timeline; budget holder unknown
Disqualifiers: none
Tier: B -> nurture; ask: timeline, current tool, who signs off
```

## Common failure modes

- Treating job title seniority as buying intent.
- Scoring from assumptions about a company instead of recorded facts.
- Passing A-tier leads without the evidence, so sales has to redo the research.
