---
name: incident-postmortem
description: >-
  Writes a blameless post-incident review: impact, a timestamped timeline, detection and
  response, contributing factors found by asking why, what went well, and owned,
  dated corrective actions. Use when writing a postmortem or incident review after an
  outage, service disruption, security event, or operational failure.
metadata:
  version: "1.0.0"
  category: operations
  tags: postmortem incident-review outage blameless timeline corrective-actions
  risk: low
---
# Incident Postmortem

A postmortem exists to make the next incident less likely or less harmful. Blameless
means the system is examined, not the people: assume everyone acted reasonably on the
information they had.

## Gather

- Collect facts while fresh: alerts, logs, dashboards, chat and call transcripts,
  tickets, change records, and customer reports. Use timestamps with a time zone.
- Talk to the responders and ask what they saw, knew, and expected at each point.
- Quantify impact: duration, customers or users affected, transactions failed, data
  affected, money, service level breaches. Mark estimates as estimates.

## Timeline

List events in order, each with time, source, and what happened: start of the fault,
first signal, detection, escalation, key decisions, mitigation, recovery, all-clear.
Derive: time to detect, time to mitigate, time to recover.

## Analysis

1. Ask "why" from the impact backwards until you reach conditions the organization can
   change; stop at a person's action and ask why it made sense at the time.
2. Record **contributing factors**, usually several: a latent defect, a missing check,
   an unclear runbook, alert noise, a risky change process, time pressure.
3. Note what went well and should be kept, and where luck limited the damage.
4. Check earlier incidents for the same factors; repeats deserve priority.

## Actions

- Each action addresses a named factor, has one owner, a due date, and a way to verify
  it is done. Prefer actions that prevent or detect over "be more careful" or "retrain".
- Separate quick fixes from longer structural work; track both to completion.

## Rules

- No names in blame positions; refer to roles ("the on-call engineer").
- Facts, not speculation; mark open questions and who will answer them.
- Share the review with affected teams; customer-facing summaries go through the
  agreed communication path.

Use `references/postmortem-template.md` unless a template is provided.

```text
Incident: <title> | Severity: S1 | Date: <date> | Duration: 2h 10m
Impact: checkout failed for ~40% of customers; ~1,900 orders delayed (estimate)
Detected: 14:22 by customer reports (alert fired 14:48) | TTD 27m | TTR 2h 10m
Factors: config change without validation; alert threshold too high; runbook outdated
Action: add config validation to deploy pipeline - platform lead - 15 Jul - test proves block
```

## Common failure modes

- A single "root cause" that stops at human error.
- Actions without owners or dates that are never completed.
- Writing the review weeks later from memory.
