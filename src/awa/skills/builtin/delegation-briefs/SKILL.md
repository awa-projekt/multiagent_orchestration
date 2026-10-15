---
name: delegation-briefs
description: >-
  Writes self-contained task briefs for another agent, covering objective, context,
  constraints, deliverable, and done criteria, so the receiver can work without your
  conversation history. Use whenever you delegate a task to a sub-agent or ask a
  specialist to do work on your behalf.
metadata:
  version: "1.0.0"
  category: coordination
  tags: multi-agent delegation briefing sub-agent instructions
  risk: low
---
# Delegation Briefs

The receiving agent sees only your brief: not your conversation, files, or reasoning.
Write it so that a capable colleague with no context could do the work.

## Structure

1. **Objective**: the outcome you need and why it matters. The purpose lets the receiver
   make sensible judgment calls.
2. **Context**: the facts, inputs, prior findings, and decisions the work depends on.
   Include data inline or by exact reference (IDs, keys, paths). Leave out history that
   does not affect the work.
3. **Constraints**: what is in and out of scope, sources or tools to use or avoid,
   limits (length, time, budget), and actions that need approval.
4. **Deliverable**: the exact output: format, structure, level of detail. Ask for sources
   and an explicit statement of uncertainty when accuracy matters.
5. **Done criteria**: how the receiver knows it is finished, and what to do when blocked
   (report what was tried and what is missing; do not guess).

## Rules

- One brief, one coherent task. Split unrelated work into separate briefs that can run
  in parallel.
- Be specific: "List the three largest cost drivers with figures from the Q3 report"
  beats "look into costs".
- Resolve ambiguities you can resolve yourself before delegating.
- Match the task to the receiver's stated capabilities and tools.
- State requirements, not step-by-step method, unless the method matters.
- Never include secrets or personal data the task does not need.

## After delegating

- Check the result against your done criteria before using it, and treat it like any
  other input you did not produce.
- If it falls short, send a focused follow-up that names the gap instead of repeating
  the whole brief.

## Template

```text
Objective: <outcome and why it matters>
Context: <facts, inputs, references>
Constraints: <scope, sources, limits, approvals>
Deliverable: <format and content of the result>
Done when: <criteria>. If blocked: <what to report>.
```
