---
name: meeting-and-decision-notes
description: >-
  Turns a meeting transcript, chat thread, or rough notes into a short record of
  decisions, action items with owners and deadlines, open questions, and key context.
  Use when summarizing a meeting or discussion so that people who were not there know
  what was decided and who does what by when.
metadata:
  version: "1.0.0"
  category: communication
  tags: meetings minutes decisions action-items summary notes
  risk: low
---
# Meeting and Decision Notes

Notes exist so that decisions stick and actions happen. Record outcomes, not the
conversation.

## Extract, in this order

1. **Decisions**: what was agreed, stated as a complete sentence someone could act on,
   with who decided when it matters. "We will move the launch to 14 May" is a decision;
   "discussed launch timing" is not.
2. **Action items**: one verb-first task per line, with exactly one owner and a deadline.
3. **Open questions**: what is still undecided, who will answer, and by when.
4. **Key context**: only the facts, figures, or constraints needed to understand the
   decisions (for example the budget limit that ruled out an option).
5. **Risks or blockers** raised, with who is tracking them.

## Rules

- Only record a decision if it was actually made. Proposals, preferences, and "let's
  think about it" are open questions, not decisions.
- If an owner or deadline was not stated, write `owner: unassigned` or
  `due: not set` rather than guessing; list these under open questions.
- Use names or roles as they appear in the source; do not attribute a statement to
  someone unless the source shows who said it.
- Keep disagreement visible when it was not resolved: "A and B disagree on X; decision
  deferred to <meeting/person>".
- Preserve exact figures, dates, and names. Convert relative dates ("next Friday") to
  calendar dates using the meeting date, and state the meeting date.
- Leave out small talk, repeated points, and who said what, unless attribution matters
  for a decision or commitment.
- Mark anything confidential or personal so it is not forwarded by accident, and omit it
  if the notes go to a wider audience.
- If the source is incomplete (a partial transcript, missing speaker names), say so at
  the top.

## Format

```text
<Meeting title> - <date> - Attendees: <names or roles>

Decisions
1. <decision> (decided by <who>, if relevant)

Action items
- [ ] <verb-first task> - owner: <name> - due: <date>

Open questions
- <question> - to answer: <name> - by: <date>

Context
- <fact needed to understand the decisions>
```

Keep the whole record to a single screen where possible. For long meetings, add a
two-sentence summary above the decisions.

## Before sending

- Could someone who missed the meeting act on every action item without asking?
- Does every action item have one owner and a date, or an explicit "not set"?
- Does each decision match what the source says, without being stronger or weaker?
