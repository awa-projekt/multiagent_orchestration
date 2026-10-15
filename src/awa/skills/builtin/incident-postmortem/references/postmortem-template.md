# Postmortem Template

A generic template for a blameless post-incident review. When the organization has its
own template, use that instead.

## Summary

Two to four sentences: what happened, the impact, how it was resolved, and the most
important follow-up.

## Impact

| Measure | Value | Basis |
| --- | --- | --- |
| Duration | <start - end, time zone> | <monitoring, logs> |
| Users or customers affected | <number or share> | <measured or estimated, how> |
| Transactions or requests failed | <number> | <source> |
| Data affected | <none / what, how much> | <source> |
| Service levels breached | <which, by how much> | <contract or target> |
| Financial effect | <amount or "not yet known"> | <source> |

## Timeline (time zone: <zone>)

| Time | Event | Source |
| --- | --- | --- |
| <hh:mm> | Fault starts (<what>) | <log, change record> |
| <hh:mm> | First signal (<alert or report>) | <system> |
| <hh:mm> | Incident declared, severity <level> | <ticket> |
| <hh:mm> | Key decision (<what and why>) | <chat, call notes> |
| <hh:mm> | Mitigation applied | <change record> |
| <hh:mm> | Service recovered, monitoring normal | <dashboard> |

Time to detect: <x>. Time to mitigate: <y>. Time to recover: <z>.

## Detection and response

- How was the incident detected, and could it have been detected sooner?
- Were the right people reached quickly? Was the runbook usable?
- What slowed the response (access, information, tooling, decisions)?

## Contributing factors

| Factor | How it contributed | Evidence |
| --- | --- | --- |
| <condition> | <effect on cause, detection, or duration> | <source> |

## What went well

- <practice or tool that limited impact>

## Where we were lucky

- <circumstance that limited impact but cannot be relied on>

## Corrective actions

| Action | Factor addressed | Type (prevent, detect, mitigate, process) | Owner | Due | Verification |
| --- | --- | --- | --- | --- | --- |
| <action> | <factor> | <type> | <role> | <date> | <how completion is proven> |

## Open questions

- <question> - <who will answer> - <by when>
