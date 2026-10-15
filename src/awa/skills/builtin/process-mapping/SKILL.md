---
name: process-mapping
description: >-
  Turns a described business process into an explicit map of trigger, steps, roles,
  inputs and outputs, handoffs, decision points, exceptions, and controls, and lists the
  gaps that must be clarified. Use when documenting how work flows today, or when
  designing a process to automate with agents, tools, and human approvals.
metadata:
  version: "1.0.0"
  category: planning
  tags: process mapping workflow roles handoffs decisions exceptions automation
  risk: low
---
# Process Mapping

A process description usually covers the happy path. The map is useful only if it also
shows who owns each step, where work changes hands, and what happens when things go wrong.

## 1. Frame the process

- **Name and purpose**: what outcome the process delivers, and for whom.
- **Trigger**: the event that starts one instance (a received invoice, a new request).
- **End states**: every way an instance can finish (completed, rejected, cancelled,
  timed out).
- **Boundaries**: where the process starts and stops, and what is out of scope.
- **Volume and timing**: how often it runs and any deadlines or service levels.

## 2. List the steps

For each step record, in order:

- a verb-first name ("Check invoice against order");
- the **role** that performs it (a job role or system, not a person's name);
- **inputs** it needs and **outputs** it produces, with their format;
- the **systems or tools** used;
- whether it is judgment work, rule-based work, or an approval.

Keep steps at one level of detail: one role, one outcome per step. Split a step that
has two roles; merge steps that are always done together by the same role.

## 3. Mark handoffs, decisions, and exceptions

- **Handoffs**: every point where work moves between roles or systems. State what is
  passed, how, and what the receiver needs to continue. Handoffs are where delays and
  lost information occur.
- **Decision points**: the question, the rule or criteria, each outcome, and where each
  outcome leads. "Amount over 10,000? yes: finance approval; no: continue."
- **Exceptions**: for each step, what can go wrong (missing data, invalid input, system
  down, no response, rejection) and the path taken. Every exception ends in a handled
  state: retry, correct, escalate, or close.
- **Controls**: approvals, checks, and records required by policy, with who performs
  them. Never drop a control because it seems slow.

## 4. Find the gaps

List every unanswered question instead of filling it with an assumption: unclear owner,
undefined rule, missing exception path, unknown timeout, conflicting descriptions.
Mark assumptions you had to make and ask the process owner to confirm them.

## Output

```text
Process: <name> | Trigger: <event> | End states: <list>
#  | step                     | role         | input -> output          | type
1  | Receive invoice          | system       | email -> invoice record  | rule
2  | Extract fields           | clerk        | record -> fields         | rule
3  | Amount over 10,000?      | -            | fields                   | decision: yes 4, no 5
4  | Approve payment          | finance lead | fields -> approval       | approval
Handoffs: 2->3 clerk to system (fields record); 3->4 system to finance lead (request).
Exceptions: 2 unreadable -> request new copy; 4 no reply in 2 days -> escalate.
Gaps: who approves when the finance lead is absent?
```

## For automation design

- Mark each step as automatable (rule-based, structured input), assistable (judgment
  with a human check), or human-only (authority, accountability, sensitive judgment).
- Keep humans on approvals, irreversible actions, and exceptions without a clear rule.
- Each automated step needs defined inputs, outputs, a failure path, and an owner who is
  notified when it fails.
