---
name: escalation-triage
description: >-
  Classifies incoming issues, requests, and incidents by impact and urgency, assigns a
  severity, routes each to the right owner or queue, and tracks response and resolution
  deadlines. Use when deciding how quickly and by whom something must be handled, or
  whether it needs to be escalated.
metadata:
  version: "1.0.0"
  category: operations
  tags: triage escalation severity priority routing sla incidents
  risk: low
---
# Escalation Triage

Triage decides order and ownership, not the fix. Be fast, consistent, and explicit
about why you chose a severity, so the decision can be checked and corrected.

## Steps

1. **Identify** the issue in one sentence: what is affected, for whom, since when.
2. **Assess impact**: how many people, customers, or processes are affected; whether
   money, safety, personal data, legal obligations, or reputation are at stake; whether a
   workaround exists.
3. **Assess urgency**: is it getting worse, is there a deadline, when does harm occur?
4. **Assign severity** from impact and urgency using `references/severity-matrix.md`.
   Use the organization's own matrix instead whenever one is provided.
5. **Route** to the owner using the routing rules you were given. If none match, route to
   the default queue or your coordinator; never leave an item unassigned.
6. **Set deadlines** from the applicable service level: first response and resolution
   targets, measured from when the issue was reported, not when you saw it.
7. **Record** the triage note (below) and notify the owner through the agreed channel.

## Decision rules

- When impact or urgency is unclear, choose the higher severity and say what would lower
  it. Downgrading later is cheap; a missed critical issue is not.
- Anything involving physical safety, a suspected security breach, exposure of personal
  data, or a possible legal or regulatory breach goes to the highest severity and to the
  designated human owner immediately, even if details are incomplete.
- Severity follows impact, not the loudness of the reporter or their seniority. Note
  customer importance separately if routing rules use it.
- Group duplicates: link new reports to an existing open issue instead of opening
  another, and raise its severity if the new reports widen the impact.
- Re-triage when facts change (a workaround stops working, more users affected, a
  deadline moves) and record why.
- Escalate before, not after, a deadline is missed: at roughly 75% of the elapsed target
  with no clear path to resolution, notify the next level.

## Triage note

```text
Issue: <one sentence>
Impact: <who/what is affected, scale, workaround yes/no>
Urgency: <trend, deadline, time to harm>
Severity: <level> because <reason>
Owner: <team or person> via <routing rule>
Deadlines: first response by <time>, resolution target <time>
Open questions: <what is unknown and who can answer>
```

## Common failure modes

- Assigning the reporter's requested priority without assessing impact.
- Routing to a person who is unavailable instead of a staffed queue.
- Closing duplicates without telling their reporters where the main issue is tracked.
- Letting the clock start when the item was picked up rather than reported.
