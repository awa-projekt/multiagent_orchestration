---
name: task-planning
description: >-
  Clarifies the goal, compares approaches, and breaks a non-trivial task into small,
  independently verifiable steps with explicit done conditions. Use at the start of an
  ambiguous, multi-step, or open-ended task, and when new information means a plan needs
  revising.
metadata:
  version: "1.0.0"
  category: planning
  tags: planning scoping requirements brainstorming milestones
  risk: low
---
# Task Planning

## Scope the problem

- Restate the goal in your own words and check it against what was actually asked.
- Separate fixed constraints (interfaces, deadlines, dependencies) from choices that are
  still open. Check an assumption before treating it as fixed.
- Ask a clarifying question when two reasonable readings of the request would lead to
  materially different results. Otherwise state your assumption and proceed.
- Size the task (small fix, focused feature, larger redesign) and scale the planning
  effort to match.

## Choose an approach

- Consider at least two credible approaches before committing, even briefly.
- Pick one and record in a sentence why, and why the main alternative was rejected.

## Plan in increments

- Break the work into steps that each leave a working, checkable state.
- Give every step a done condition, written before the step starts.
- Put the riskiest or least certain step early, so a blocking problem surfaces before
  other work depends on it.
- Prefer several small steps over one large step that bundles unrelated changes.

## Execute and revise

- Keep the plan visible and mark steps done as they complete.
- When a step shows that an assumption was wrong, revise the plan and say what changed.
- Do not expand scope silently. Name any new step that turns out to be necessary.

## Plan format

```text
Goal: <restated>
Assumptions: <assumption - how it is checked>
Approach: <chosen> because <reason>; rejected: <alternative> because <reason>
Steps:
1. <step> - done when <condition> (riskiest first)
2. <step> - done when <condition>
Revisions: <what changed and why>
```
