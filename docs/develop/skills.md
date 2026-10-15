# Skills

A skill is a folder with instructions for one kind of work, such as "how we handle refunds". You
write it once and attach it to every agent that needs it. Use a skill when several agents share the
same know-how, or when an agent's instructions grow too long.

A skill is a `SKILL.md` file: YAML front matter and a Markdown body. It may bundle resource files
(`references/`, `assets/`, ...) that the agent reads only on request.

## Write and attach a skill

Save the skill as `skills/refund-policy/SKILL.md`:

```markdown title="skills/refund-policy/SKILL.md"
---
name: refund-policy
description: >-
  Applies the refund policy to a customer request: eligibility, amount and the
  reply. Use when a customer asks for money back or disputes a charge.
metadata:
  version: "1.0.0"
  category: support
  tags: refunds policy billing
  risk: medium
---
# Refund policy

1. Read `references/policy.md` before deciding.
2. A refund is allowed within 30 days of delivery and never exceeds the order total.
3. Explain the decision to the customer in two sentences.
```

and its resource as `skills/refund-policy/references/policy.md`:

```markdown title="skills/refund-policy/references/policy.md"
# Refund rules

- Within 30 days of delivery: full refund.
- After 30 days: no refund; offer a repair.
```

Load the folder next to the built-in skills and give the agent the skill on demand:

```python title="use_skill.py"
from awa import Agent, SkillRegistry
from awa.testing import FakeChatModel, tool_call

registry = SkillRegistry.from_directory("skills", include_builtin=True)
model = FakeChatModel(
    [tool_call("load_skill", {"name": "refund-policy"}), "Your refund is approved: the order arrived 12 days ago."]
)
agent = Agent(
    "refunds",
    skills=["refund-policy", "writing"],
    skill_registry=registry,
    skill_mode="on_demand",
    model=model,
)
result = agent.run_sync("Can I get my money back? The order arrived 12 days ago.")

first_prompt = model.calls[0].messages[0].content
print([tool.name for tool in agent.tools])
print("catalog:", "refund-policy" in first_prompt, "| body before loading:", "references/policy.md" in first_prompt)
print("body after loading:", "references/policy.md" in model.calls[1].last_message.content)
print(result.output)
```

```text title="use_skill.out"
['load_skill', 'read_skill_resource']
catalog: True | body before loading: False
body after loading: True
Your refund is approved: the order arrived 12 days ago.
```

Before the model asks, its prompt holds only the skill's name and description. The body arrives
with `load_skill`, and a resource only with `read_skill_resource`.

- `awa new skill refund-policy` writes a starting point with the same layout.
- In code, write `Skill(name=..., description=..., instructions=...)`.
- A blueprint finds the `skills/` folder beside it by itself. `awa validate`, `awa chat` and
  `awa.load("blueprint.yaml")` add its skills to the built-in ones, so `skills: [refund-policy]`
  works without more options. `--skills DIR` (or `skill_registry=` in Python) adds other folders.

## The manifest

- `name`: 1 to 64 lowercase letters, digits and single hyphens.
- `description`: what the skill does and when to use it ("Use when ..."). In on-demand mode it is
  the only text the model sees before loading the skill.
- `metadata`: `version`, `category`, `tags` and `risk` (`low`, `medium`, `high`);
  `allowed-tools`, `license` and `compatibility` are read too.

`awa validate` lints the skills a blueprint uses: name and description rules, body size,
references one level deep, credentials, network or export commands, instructions to ignore safety
rules, time-sensitive statements and awa tool names (a skill holds know-how; the agent decides its
tools). Resource paths are checked: absolute paths, `..` and files outside the skill folder are
refused, and long resources are cut.

## Inline or on demand

| `skill_mode` | The prompt contains | Tools added |
| --- | --- | --- |
| `"inline"` (default) | Every skill's full instructions after the agent's own | `read_skill_resource` when a skill bundles files |
| `"on_demand"` | A short catalog of names and descriptions | `load_skill`, and `read_skill_resource` when a skill bundles files |

Use `on_demand` for an agent with many skills of which it needs a few per request, and `inline`
for skills it always needs. In a blueprint, set `defaults.skill_mode` or `skill_mode` per agent.
The agent's own instructions come first and win: a skill adds know-how, and an output format the
agent's instructions give is the one it follows. The order also keeps a stable prefix for the
provider's prompt cache.

## Built-in skills { #builtin-skills }

awa ships 59 skills. Name them in `Agent(skills=[...])`; `SkillRegistry.builtin()` holds them all.
`awa skills list` and `awa skills show <name>` print them. The [Skills reference](../reference/skills.md)
lists every skill with its purpose and bundled files.

| Area | Skills |
| --- | --- |
| Collaboration | `orchestration-planning`, `delegation-briefs`, `agent-collaboration`, `handoff-protocol`, `task-planning`, `context-management` |
| Quality and grounding | `quality-review`, `source-verification`, `retrieval-grounding`, `research-synthesis`, `structured-output`, `writing` |
| Safety | `safe-tool-execution`, `idempotent-retries`, `prompt-injection-awareness`, `security-review` |
| Documents and data | `document-extraction`, `data-validation`, `data-analysis`, `report-generation` |
| Operations | `escalation-triage`, `approval-requests`, `policy-compliance-check`, `process-mapping` |
| Business functions | sales, customer service, finance and procurement, people, legal and risk, operations and IT playbooks such as `invoice-reconciliation`, `contract-clause-review` and `incident-postmortem` |

The business skills prepare work for the person who is accountable. `contract-clause-review` is a
first pass against your playbook, not legal advice. Payments, orders and approvals are prepared, not
decided. A skill only instructs the model; enforce authority with approvals and tool permissions.

Every built-in skill has a test that runs an agent with the skill on demand and a scripted model:
the skill is discovered, only its description is in the prompt until the model asks, and the
loaded text contains the output format. A scripted model proves the flow, not the quality of a real
model's answer. For `invoice-reconciliation` the answer is:

```text
Invoice INV-2291 | Supplier: <name> | PO 4500-118 | Receipt GR-7731
Line 1 | 10 x 45.00 | PO 45.00, received 10 | matched
Line 2 | 5 x 12.40  | PO 12.20 (+1.6%)      | within tolerance (2%)
Line 3 | 8 x 30.00  | received 6            | exception QTY -> receiving
Result: hold line 3 - 1 exception; approvable 512.00 of 752.00 (lines 1-2)
```

A test compares this block with the real output of that agent.

## The registry

| Method | Does |
| --- | --- |
| `SkillRegistry.builtin()` | A new registry with the built-in skills. |
| `SkillRegistry.from_directory(*paths, include_builtin=False)` | Load skill folders, `SKILL.md` files or roots of skill folders; later paths replace earlier skills. |
| `add(skill, replace=False)`, `get(name)`, `names()`, `all()` | Manage skills. |
| `index()`, `render_index(names=None)` | The `(name, description)` catalog. |

`compose_prompt(base, skills, registry)` and `create_skill_tools(registry, allowed=...)` are what
`Agent` uses; call them to add skills to agents you build with plain `create_agent`. Review
third-party skills before you load them: they are instructions you ship.

## What can go wrong

| You see | Cause and fix |
| --- | --- |
| `SkillNotFoundError: Unknown skill: 'refund'. Registered skills: ...` | The name is misspelled, or the skill is in your own folder: pass `skill_registry=` built with `SkillRegistry.from_directory(...)`. |
| `SkillNotFoundError: Skill location not found: skills` | The folder does not exist relative to the working directory. |
| `SkillManifestError: ... invalid skill: name: String should match pattern ...` | Use 1 to 64 lowercase letters, digits and single hyphens, not starting or ending with a hyphen. |
| The model never loads an on-demand skill | Its `description` does not say when to use it. End it with "Use when ...". |

Reference: [Skills](../reference/skills.md). Example: `examples/02_skills.py` and the skill in
`examples/skills/`. Importing prompt files as skills is on [Extend](extend.md).
