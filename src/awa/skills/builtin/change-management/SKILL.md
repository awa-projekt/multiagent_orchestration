---
name: change-management
description: >-
  Prepares a change request for systems, infrastructure, or business processes:
  classifies risk (standard, normal, emergency), defines the implementation steps,
  test and verification, a tested rollback plan, the change window, and approvals. Use
  when planning or reviewing a production change, a configuration change, or a release.
metadata:
  version: "1.0.0"
  category: operations
  tags: change-management change-request rollback release approvals risk deployment
  risk: medium
---
# Change Management

Most outages start with a change. A good change request makes the risk visible, keeps
the change small, and has a way back that has actually been tried.

## Classify

| Class | When | Approval |
| --- | --- | --- |
| Standard | Pre-approved, repeatable, low risk, documented procedure | None beyond the procedure |
| Normal | Anything else planned | Change owner plus the approvers policy names for its risk |
| Emergency | Needed now to stop or prevent serious harm | Emergency approver; full review afterwards |

Rate risk from impact (what breaks, for whom, if it fails), likelihood (novelty,
complexity, test coverage), and reversibility. When in doubt, treat as the higher risk.
Irreversible steps (data migrations, deletions, external notices) raise the risk class.

## The change request

1. **What and why**: the change, its purpose, and the systems, services, and users affected.
2. **Dependencies and conflicts**: other changes in the window, freeze periods, teams
   that must be available.
3. **Implementation steps**: numbered, with who does each and how long it takes.
4. **Test evidence**: what was tested where (test or staging environment), with results.
5. **Verification**: the checks that prove success after the change (metrics, test
   transactions, log checks), and the time limit for deciding.
6. **Rollback plan**: steps to restore the prior state, the trigger to use it, how long
   it takes, and whether it was rehearsed. Name any point after which rollback is not
   possible and what happens instead.
7. **Window and communication**: the time chosen for lowest impact, who is told before
   and after, and the status channel.

## Rules

- Do not start without the required approvals recorded; the implementer does not approve
  their own normal change.
- Execute exactly the approved steps. If reality differs, stop and roll back or
  re-approve; do not improvise in production.
- Roll back when the trigger is met, without debate; investigate afterwards.
- Close the change with the outcome, any deviations, and links to incidents it caused.

```text
Change: <title> | Class: normal | Risk: medium (impact high, likelihood low, reversible)
Affects: <service, users> | Window: <date, time, zone> | Freeze conflict: none
Steps: 1 <step> (<role>, 10m) 2 <step> (<role>, 20m)
Tested: staging, <date>, all checks passed
Verify: error rate under 1% and test order succeeds within 15m
Rollback: restore previous config (5m, rehearsed <date>); trigger: verify fails
Approvals: <service owner>, <change board> | Notify: support, affected teams
```

## Common failure modes

- A rollback plan that says "revert" with no steps or no rehearsal.
- Bundling unrelated changes so a failure cannot be traced.
- Treating an overdue planned change as an emergency to skip review.
