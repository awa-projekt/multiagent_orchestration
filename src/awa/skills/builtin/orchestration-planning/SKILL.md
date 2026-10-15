---
name: orchestration-planning
description: >-
  Plans and runs multi-agent work: decomposes a request into subtasks, chooses the right
  specialist for each from the available agents, decides what runs in parallel, checks
  results, and synthesizes them into one answer. Use when you coordinate a team of agents
  or can delegate work to sub-agents.
metadata:
  version: "1.0.0"
  category: coordination
  tags: multi-agent orchestration decomposition coordination synthesis
  risk: low
---
# Orchestration Planning

## 1. Decide whether to delegate

- Do the work yourself when it is small, needs no specialist capability, or delegating
  would cost more than doing it.
- Delegate when a subtask needs a capability, tool, or expertise another agent has, or
  when independent subtasks can run in parallel.

## 2. Decompose

- Restate the goal and the final deliverable before splitting the work.
- Split into subtasks that each have one clear output and can be done independently
  where possible.
- Identify dependencies. Run independent subtasks in parallel; sequence dependent ones
  and pass each the results it needs.
- Keep the number of subtasks proportional to the request: a simple question needs one
  or two, not a committee.

## 3. Assign

- Choose agents by their descriptions and capabilities (use directory lookup tools when
  you have them) and prefer the most specific match.
- Do not assign work outside an agent's declared capabilities. Send the same subtask to
  several agents only when you deliberately want independent opinions.
- Give each agent a self-contained brief: objective, context, constraints, deliverable,
  and done criteria. The agent has not seen your conversation.

## 4. Check and adapt

- Check each result as it arrives: does it answer the brief, is it supported, does it
  conflict with other results?
- On a failed or weak result, retry once with a sharper brief, reassign it, or continue
  with the gap stated. Do not loop.
- Revise the plan when results change what is needed, and note what changed.

## 5. Synthesize

- Build one coherent answer organized around the user's request, not around which agent
  produced what.
- Resolve conflicts between results or present them explicitly; never blend
  contradictory claims into one.
- Keep the evidence and citations from sub-results, and label your own inferences.
- State remaining gaps and uncertainty.
- You own the final answer: review it before returning it.

## Plan format

```text
Goal and deliverable: <one sentence each>
| Subtask | Agent | Depends on | Brief (objective, constraints, done criteria) |
| 1 | <agent> | - | <brief> |
| 2 | <agent> | 1 | <brief> |
Parallel: <subtasks that run together>
Final answer owner: me; review step: <what I check>
```
