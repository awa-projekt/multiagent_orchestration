---
name: idempotent-retries
description: >-
  Makes retried or repeated side-effecting actions safe: checks whether an action is
  idempotent, uses stable idempotency keys, treats unknown outcomes as possibly
  successful, and bounds retries. Use before retrying a failed or timed-out write, send,
  payment, booking, or any other action with side effects.
metadata:
  version: "1.0.0"
  category: reliability
  tags: retry idempotency deduplication side-effects failure-handling
  risk: medium
---
# Idempotent Retries

- Before retrying an action with a side effect, decide whether it is idempotent: does
  running it twice leave the same end state as running it once? If not, a blind retry
  duplicates the effect.
- Use an idempotency key when the target system supports one. Derive it from the logical
  operation and reuse the same key on every retry; never generate a new one per attempt.
- Distinguish "failed" from "outcome unknown" (a timeout or dropped connection after the
  request was sent). For an unknown outcome, check whether the effect happened before
  trying again.
- Retry only transient failures (timeouts, rate limits, temporary unavailability). A
  validation or permission error will fail the same way again; fix the input instead.
- Bound retries with a small maximum and increasing delays. Never retry in an unbounded
  loop.
- Keep the checks you make between attempts free of side effects.
- When an action cannot be made safe to repeat, make one deliberate attempt and report
  failure clearly instead of retrying automatically.
- Report every attempt and its outcome so a duplicated effect can be traced.

## Attempt log

```text
Action: <operation> | Idempotent: yes (key <key>) / no
Attempt 1: <time> | outcome: failed (transient timeout) / unknown / succeeded
Check before retry: <how the effect was verified>
Attempt 2: <time> | outcome: succeeded
Final: done / gave up after <n> attempts (reason)
```
