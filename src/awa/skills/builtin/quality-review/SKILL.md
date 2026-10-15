---
name: quality-review
description: >-
  Reviews a draft answer or deliverable against the request, the available evidence, and
  every stated constraint, and reports the most important issues with locations and
  required fixes plus a clear verdict. Use when checking another agent's output, or your
  own, before it is final.
metadata:
  version: "1.0.0"
  category: quality
  tags: review evaluation feedback critique verification
  risk: low
---
# Quality Review

- Judge the draft against the request, the supplied evidence, and the stated
  requirements, not against your own preferences or unsupported knowledge.
- Check in this order:
  1. Does it answer the question that was asked?
  2. Is every factual claim supported by the evidence?
  3. Is it correct and internally consistent?
  4. Does it follow every constraint (format, length, language, audience, tone)?
  5. Is it complete, and is it clear?
- Report at most the five most important issues, most severe first:
    - **critical**: wrong, unsupported, unsafe, or ignores an instruction;
    - **major**: materially incomplete, misleading, or hard to use;
    - **minor**: clarity, style, or polish.
- For each issue give the location (section, sentence, or field), what is wrong, and the
  fix required.
- Do not rewrite the draft unless asked, and do not raise an issue that an earlier round
  has already fixed.
- End with a verdict (approve, approve with minor changes, or revise) that follows from
  the issues you listed.
- If there are no significant issues, say so; do not invent problems to appear thorough.

## Output

```text
Verdict: approve / approve with minor changes / revise
1. critical | <section or sentence> | <what is wrong> | fix: <required change>
2. major    | <location> | <problem> | fix: <change>
Checked: answers the question, claims supported, consistent, constraints met, complete
```
