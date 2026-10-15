---
name: project-status-reporting
description: >-
  Writes a project status update with an honest RAG status by dimension (scope,
  schedule, budget, risks), milestones against baseline, top risks and issues with
  owners, decisions and asks for sponsors, and next steps. Use when preparing a weekly
  or monthly project status report, steering committee update, or program dashboard.
metadata:
  version: "1.0.0"
  category: planning
  tags: project status rag milestones risks steering-committee updates
  risk: low
---
# Project Status Reporting

A status report is for decisions, not reassurance. Readers should see in ten seconds
whether the project is on track and what they must do.

## RAG rules

Rate each dimension against the approved baseline, not against effort or intentions:

| Status | Meaning |
| --- | --- |
| Green | On track; any deviation is within agreed tolerance and needs no help |
| Amber | Off track or at risk, but the team has a credible recovery plan within its authority |
| Red | Off track and recovery needs a decision, money, people, or scope change from above |

- Overall status is at least as bad as the worst dimension that threatens the
  project's objective; do not average colors.
- A status that changes must say why. Green after weeks of amber needs the evidence.
- "Green with concerns" is amber.

## Content

1. **Headline**: overall RAG and one sentence on why.
2. **Dimensions**: scope, schedule, budget, and quality or resources, each with RAG and
   a one-line reason.
3. **Milestones**: baseline date, forecast date, status. Show slips against the
   baseline, not the last forecast only.
4. **Risks and issues**: the top three to five, with impact, owner, and the next action.
5. **Decisions and asks**: what you need, from whom, by when, and the consequence of no decision.
6. **Done and next**: key progress since the last report and the plan to the next one.

## Rules

- Use the same format and period each time so readers can compare.
- Numbers need sources: budget from the finance record, progress from the plan.
- Report bad news in the first report you know it, with a plan or an ask.
- Keep it to one page; link to detail.

## Template

```text
Project: <name> | Period: <week/month> | Owner: <role> | Overall: AMBER
Why: integration testing is 2 weeks late; go-live date holds if the ask below is approved.
Scope GREEN - no changes | Schedule AMBER - testing late | Budget GREEN - 62% spent, 60% done
Milestones: design (base 01 Mar, done 01 Mar) | testing (base 15 Apr, fc 29 Apr, late)
            go-live (base 31 May, fc 31 May, at risk)
Risks: R3 vendor API instability - high - vendor lead - fix due 20 Apr
Asks: approve 2 extra testers for 3 weeks (sponsor, by 12 Apr) - otherwise go-live slips 2 weeks
Done: data migration rehearsal 1 | Next: rehearsal 2, test cycle 2
```

## Common failure modes

- Watermelon reporting: green outside, red inside.
- Forecast dates that move silently each week with the baseline hidden.
- Risks listed with no owner or next action, repeated unchanged every period.
- Asks buried in the details instead of stated up front with a deadline.
