---
name: root-cause-debugging
description: >-
  Diagnoses bugs and unexpected results by reproducing, isolating, and tracing them to
  the root cause before proposing the smallest fix. Use when investigating an error,
  failing test, crash, regression, or behavior that differs from what was expected.
metadata:
  version: "1.0.0"
  category: engineering
  tags: debugging bug error traceback regression diagnosis
  risk: low
---
# Root-Cause Debugging

- Reproduce the failure first. A fix proposed before a reliable reproduction is a guess.
- State expected and actual behavior concretely: inputs, outputs, error text.
- Narrow it down: find the smallest input and the most recent change that trigger it.
- Trace the failure to its root cause, not its first visible symptom. Keep asking "why"
  until the answer names something that, if changed, would prevent the failure.
- When several hypotheses fit, design a check that tells them apart before writing a fix.
- Never suppress a symptom (a broader exception handler, a retry, a loosened assertion)
  instead of understanding it.
- Make the smallest change that addresses the root cause, and add a test that reproduces
  the original failure.
- After fixing, rerun the original reproduction, not only the existing tests.
- If the same pattern can fail elsewhere, say so.

For a worked example and diagnostic questions, read `references/checklist.md`.

## Output

```text
Failure: <expected vs actual, with the error text>
Reproduction: <smallest input and the command that shows it>
Hypotheses: <A - check that distinguishes it>; <B - check>
Root cause: <what, and the evidence>
Fix: <smallest change> | Test: <test that failed before the fix>
Also check: <other places with the same pattern>
```
