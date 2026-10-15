---
name: code-review
description: >-
  Reviews a code change for correctness, safety, and maintainability and reports
  prioritized findings, each with a concrete location and fix. Use when asked to review a
  diff, pull request, patch, or newly written code.
metadata:
  version: "1.0.0"
  category: engineering
  tags: code-review pull-request diff correctness review
  risk: low
---
# Code Review

- Review the change against its stated intent first: does it do what it claims on every
  code path it touches, not only the one it was tested on?
- Rank correctness and safety above style. A missing error path outranks a naming
  preference; report findings in that order.
- Check the boundaries where defects live: empty and single-element inputs, off-by-one
  ranges, missing or null fields, concurrent access to shared state, and failure paths
  that leave partial state behind.
- Trace at least one caller of every changed public function, schema, or serialized
  format. A change that looks correct in isolation can break a caller that relied on the
  old behavior.
- Confirm that tests cover the new behavior and would fail without the change. Read the
  code the tests exercise; do not approve on test names or the description alone.
- Separate blocking issues (incorrect behavior, data loss, security gaps) from
  suggestions (clarity, conventions), and label each finding accordingly.
- Give every finding a location (file and line or symbol), what is wrong, and a specific
  fix. "This seems risky" without a location or alternative is not a finding.
- If the change is sound, say so plainly; do not invent issues.

For a systematic pass, work through `references/review-checklist.md`.

## Output

```text
Verdict: approve / approve with suggestions / changes required
Blocking:
1. <file:line or symbol> - <what is wrong> - fix: <specific change>
Suggestions:
1. <file:line> - <clarity or convention point>
Tests: <new behavior covered? would the test fail without the change?>
```
