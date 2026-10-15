# Default Severity Matrix

A generic starting point. When the organization defines its own levels, targets, or
routing, use those instead and say which matrix you applied.

## Impact

| Level | Meaning |
| --- | --- |
| High | Many customers or a whole process stopped; significant financial loss; safety, security, personal data, or legal or regulatory exposure; public or reputational harm. |
| Medium | A group of customers or one important process degraded; a workaround exists but is costly; moderate financial effect. |
| Low | A single user or a minor function affected; an easy workaround exists; cosmetic or informational. |

## Urgency

| Level | Meaning |
| --- | --- |
| High | Harm is happening now or is getting worse, or a hard deadline is within hours. |
| Medium | Harm will occur within days, or the issue blocks planned work. |
| Low | No time pressure; can be scheduled. |

## Severity = impact x urgency

| Impact \ Urgency | High | Medium | Low |
| --- | --- | --- | --- |
| High | S1 critical | S1 critical | S2 high |
| Medium | S2 high | S3 medium | S3 medium |
| Low | S3 medium | S4 low | S4 low |

## Default targets and handling

| Severity | First response | Resolution target | Escalation |
| --- | --- | --- | --- |
| S1 critical | 15 minutes | 4 hours, or continuous work until mitigated | Notify the on-call owner and their manager immediately; post status updates at a fixed interval. |
| S2 high | 1 hour | 1 business day | Notify the owning team lead; escalate if no owner has acknowledged within the response target. |
| S3 medium | 1 business day | 5 business days | Normal queue; escalate at 75% of the target without a plan. |
| S4 low | 3 business days | Scheduled | Backlog; review periodically. |

## Always S1, whatever the matrix says

- Risk to anyone's physical safety.
- A suspected security breach or confirmed exposure of personal or confidential data.
- A possible breach of a legal, regulatory, or contractual obligation with a near deadline.

## Worked examples

- Checkout fails for all customers since 09:10: impact high, urgency high, S1.
- Monthly report export is wrong for one department, due in three days: impact medium,
  urgency medium, S3.
- A typo on an internal help page: impact low, urgency low, S4.
- One customer reports that another customer's invoice was attached to their email:
  personal data exposure, S1 regardless of scale.
