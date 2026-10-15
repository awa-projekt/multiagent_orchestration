# Agent spec

Instead of one block of free-text `instructions`, an agent in a blueprint can describe its job in
sections: a goal, the context, how to work, the rules. awa puts the sections in a fixed order and
adds what the code already knows, such as the step, its documents and the tools that need an
approval. `awa validate` then lints the result.

The fields are optional. A blueprint that leaves them out loads, builds and validates all the same.
The other agent fields are in [Blueprints](blueprints.md).

## An agent with sections

With `goal` or `system` set, the system message is rendered from sections:

```yaml
agents:
  - name: check_invoice
    role: worker
    description: Checks supplier invoices against price list and order. Use for every incoming supplier invoice before payment approval.
    goal: Decide for each invoice line whether price and quantity match the order and the price list.
    system:
      context: Wrong prices cost money only if they reach payment; finance approves or blocks payment on this result.
      how_to_work: |
        1. Match each invoice line to the order line by article number.
        2. Compare the unit price with price_list; a difference above 1% is a deviation.
        3. Compare the quantity with goods_receipt.
      tools_in_step: "Use `read_csv` once per input file and filter by article number."
      rules:
        - Mark an article missing from price_list as missing_reference, because finance decides those manually.
      output: auto
    tools: ["builtin:read_csv"]
    output_schema: {$ref: schemas/invoice_check.json}
    escalation: {to: person, when: ["parse failure after retries", "deviation above 10%"], how: 'status "escalate" with a reason'}
```

## Agent fields

| Field | Type | What it does |
| --- | --- | --- |
| `goal` | text, one sentence | What the agent achieves. |
| `system` | mapping | The sections of the system message (below). |
| `tools`, `skills` | list | Tool references and skill names. |
| `escalation` | `{to: person or orchestrator, when: [...], how}` | When and how the agent hands a case on. |
| `acknowledge_ungated` | list of tool references | Hand-written agents only: acting tools that run without an approval on purpose (lint T14 becomes a note). |
| `output_schema` | mapping, or `{$ref: <file>}` | A `$ref` names a JSON or YAML schema file inside the blueprint's folder; it is read when the blueprint is loaded from its file. |

The sections of `system`:

| Section | Holds |
| --- | --- |
| `context` | The background of the work. |
| `how_to_work` | The method, for example numbered steps. |
| `tools_in_step` | When to use which of the agent's tools in this step (not a tool manual). |
| `done_when` | What must hold before the agent answers. |
| `rules` | A list; each rule with its reason (`"..., because ..."`). |
| `special_cases` | Extra special-case lines beyond those generated from the process. |
| `output` | What the answer must contain; `auto` renders the fields of `output_schema`. |
| `examples` | 0 to 3 of `{input, output, why}`. |
| `delegation_brief` | Orchestrator only: what every brief to a member states. |

For a judge and a reviewer, `judges:` is set on the judge and `reviews:` on the reviewer. The
judge's verdict schema comes from the role contract and is never written in YAML.

Special cases stay in the top-level `special_cases: {<step>: <table>}`
([Blueprints: special cases](blueprints.md#special-cases)). The renderer turns the table into the
lines of the "Special cases and escalation" section.

## The system message

The sections come in a fixed order, and empty ones are left out:

1. Role and goal
2. Context
3. How to work
4. Tools in this step
5. Done when
6. Rules
7. Special cases and escalation
8. Output
9. Before you answer
10. Example(s)

Then come the judge's or reviewer's rules with the rubric, Delegation for an orchestrator, and four
house rules.

The code fills in:

- the role and the step (`step 2 of 3`);
- the documents of the step input;
- one sentence per gated tool: the system asks for the approval, the agent does not;
- the messages of the step's checks, the special-case table and the retries;
- the output fields;
- "Before you answer": at most three checks generated from the required fields of
  `output_schema`, the step's checks and the gated tools (none for a judge or reviewer).

An agent with free-text `instructions` keeps the text verbatim as its Instructions section. It
still gets Context, How to work, Tools in this step, Done when, and Special cases and escalation
when the spec sets them, so a refinement adds a method without rewriting the text.

The message stays the same for the whole run, so a provider can cache it.

## Lint (`awa validate`)

`awa validate` lints with the `authored` profile, where E01 and the A-rules are warnings.
`--profile generated` makes every rule as strict as the table lists. Free-text `instructions` get a
note and only E06, E10 and E11.

| Code | Severity | Rule |
| --- | --- | --- |
| E01 | error | no goal, no output section, or an acting agent with no escalation |
| E02, E03 | error | the text names a tool or skill the agent does not have |
| E04 | error | an unrendered `{placeholder}` |
| E05 | error | the text names a document that is not declared |
| E06 | error | pasted data (more than 15 table lines, or a `<<<DATA` block) |
| E10 | error | more than 8,000 characters |
| E11 | error | something shaped like a key, token or password |
| W13 | warning | an orchestrator without a delegation brief |
| W15 | warning | a system message above 4,000 characters (6,000 for an orchestrator), or more than 10 tools |
| A02 | error | a document named in the text that no step of the agent gets |
| A03 | warning | a later step decides on fields but this step has no `output_schema` |
| A04 | warning | more than three skills |
| T01 | error | a tool name that is not 1-64 letters, digits, `_` or `-` |
| T03 | error, note | description missing or under 40 characters; fewer than three sentences is a note |
| T05 | error, note | input schema root is not an object; without `additionalProperties: false` is a note (awa does not send strict mode) |
| T13 | error | MCP hints contradict each other, or say read-only for a tool awa's risk data says acts |
| T14 | error (generated), warning (hand-written) | an acting tool without an approval; a note when listed in `acknowledge_ungated` |
| T21 | error | a judge or reviewer with an acting tool, also when pinned |
| T22 | error (generated), warning (hand-written) | the text speaks of an approval ("for the manager's approval"), but none of the agent's tools is gated and no step of it has an approval check |

Full examples of a worker, an orchestrator, a judge and a reviewer are in
`tests/fixtures/agentspec/examples/`.
