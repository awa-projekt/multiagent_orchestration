---
name: risk-assessment
description: >-
  Identifies risks to an objective, rates each by likelihood and impact on a defined
  scale, records existing controls and residual risk, and maintains a risk register with
  owners, treatments, and review dates. Use when assessing the risks of a project,
  process, vendor, change, or new initiative, or when updating a risk register.
metadata:
  version: "1.0.0"
  category: risk
  tags: risk register likelihood impact controls mitigation assessment
  risk: low
---
# Risk Assessment

A risk register is useful when every risk is specific, scored the same way, and owned.
A long list of vague worries is not risk management.

## Frame

- State the objective at risk (deliver the project by Q4, keep the process compliant,
  protect customer data) and the scope and time horizon of the assessment.
- Use the organization's risk scales and appetite; otherwise use
  `references/risk-matrix.md` and say so.

## Identify

- Write each risk as cause, event, and consequence: "Because the supplier has one
  factory (cause), a plant outage (event) would stop deliveries for weeks (consequence)."
- Cover the usual categories: people, process, technology, suppliers, finance, legal
  and compliance, security and data, health and safety, reputation, external events.
- Draw on evidence: past incidents, audit findings, near misses, stakeholder input.
- Keep issues (already happening) separate from risks (might happen); send issues to
  the issue log or triage.

## Assess

1. Record existing controls and whether they are evidenced as working.
2. Rate **inherent** likelihood and impact (without controls), then **residual** (with
   the controls that actually work), each 1-5 with a one-line reason.
3. Score = likelihood x impact; band it (low, medium, high, critical) per the matrix.
4. Rate impact on the worst credible outcome, not the worst imaginable one.

## Treat

- Choose per risk: reduce (new control), transfer (insurance, contract), avoid (stop the
  activity), or accept (within appetite, accepted by an authorized owner).
- Each treatment action has an owner, a due date, and the expected residual score.
- Risks above appetite must be escalated to the named level; accepting them is not an
  agent decision.

## Register

```text
R01 single factory -> plant outage -> deliveries stop for 3 weeks
    inherent L3 x I5 = 15 high | control: 2 weeks safety stock (checked monthly)
    residual L2 x I5 = 10 high | treat: reduce, qualify second source by Q3
    owner: procurement lead | review: monthly
R02 manual payment entry -> typing error -> payment to wrong payee
    inherent L3 x I4 = 12 high | control: second-person check (tested in audit)
    residual L2 x I3 = 6 medium | treat: accept, within appetite (finance lead)
    owner: finance lead | review: quarterly
```

## Common failure modes

- Risks named as topics ("IT", "suppliers") with no event or consequence.
- Taking credit for controls that exist on paper but are not operating.
- Scores that never change because the register is never reviewed.
