---
name: handoff-protocol
description: >-
  Decides when to transfer control of a conversation to another agent or escalate to a
  human, and writes the handoff note the receiver needs to continue without starting
  over. Use when a request is outside your scope or authority, when you are blocked, or
  when you have transfer or escalation tools.
metadata:
  version: "1.0.0"
  category: coordination
  tags: multi-agent handoff transfer escalation routing human-in-the-loop
  risk: low
---
# Handoff Protocol

A handoff transfers ownership: the receiver continues the conversation and you stop
working on it. Delegating or consulting, by contrast, keeps you in charge.

## Hand off when

- The request is outside your scope, capabilities, or permissions and another agent
  owns it.
- The user asks for a different specialist or a human.
- A decision needs authority you lack: approvals, policy exceptions, spending, legal or
  safety judgments.
- You are blocked after a genuine attempt: required information cannot be obtained, or
  the same approach keeps failing.

## Do not hand off when

- The task is merely difficult. Make a real attempt first.
- You only need one piece of information. Consult or delegate and keep ownership.
- You would send it straight back to the agent that just handed it to you, unless
  something has changed.

## The handoff note

Keep it short and complete enough to act on immediately:

- **Goal**: what the user wants, in their terms.
- **State**: what has been done, decided, and confirmed, with the key facts (identifiers,
  figures, commitments made to the user).
- **Open item**: the specific question, decision, or action needed from the receiver.
- **Reason**: why you are handing off.
- **Constraints**: deadlines, preferences, and anything promised or ruled out.

Pass a summary, not the transcript, and only the sensitive data the receiver needs.

```text
Handoff to: <agent or human role>
Goal: <what the user wants, in their words>
State: <done and decided, with identifiers and figures>
Open item: <the question or decision needed>
Reason: <why this is handed off>
Constraints: <deadlines, promises, exclusions>
```

## Performing the handoff

- Choose the single most appropriate receiver. If none clearly fits, hand back to your
  coordinator.
- Tell the user briefly that you are transferring them and why, unless the system does so.
- Stop after handing off; do not keep answering in parallel.
- When escalating to a human, state the decision you need, the options, and their
  trade-offs. Continue from the decision you receive instead of re-deciding it.
- Never invent a confident answer to avoid a handoff when a real unknown blocks a
  correct one.
