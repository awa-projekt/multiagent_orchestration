---
name: context-management
description: >-
  Keeps long-running, multi-step work within a usable context by carrying forward only
  what matters, recording durable facts outside the conversation, and compacting history
  at checkpoints. Use for long tasks with many tool calls, resumed sessions, or when
  passing work to a fresh context or another agent.
metadata:
  version: "1.0.0"
  category: coordination
  tags: context memory long-horizon summarization compaction
  risk: low
---
# Context Management

- Carry forward what the next step needs: the goal, decisions made so far, key facts,
  and open questions. Drop the history of attempts that no longer matter.
- Record durable facts (identifiers, decisions, results) in a structured place such as
  a notes entry, shared workspace, or state field as soon as they are established,
  instead of relying on finding them later in the conversation.
- Summarize at natural checkpoints (a phase completed, a question resolved) rather than
  waiting until the context overflows and is truncated arbitrarily.
- Keep large tool outputs out of the working context: extract the part you need and
  note where the rest can be found.
- When passing work to another agent or a fresh context, give it only the inputs its
  step needs, stated so that it can act without the prior history.
- When resuming after a gap, rebuild the working context from your records, and verify
  facts that may have changed, instead of assuming earlier reasoning still holds.
- Structure long work as a sequence of checkpoints; each one is a safe place to
  summarize because its results are already recorded and verifiable.

## Checkpoint note

```text
Goal: <one sentence>
Decisions: <decision - reason>
Facts: <identifier or figure - where it came from>
Done: <steps finished and recorded>
Open: <questions, next step>
Re-check on resume: <facts that may have changed>
```
