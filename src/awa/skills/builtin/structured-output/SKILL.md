---
name: structured-output
description: >-
  Produces output that conforms exactly to a declared schema: all required fields,
  exact enum values, valid ranges, and explicit handling of unknown values instead of
  fabrication. Use whenever a response must match a JSON schema, typed model, or other
  fixed format.
metadata:
  version: "1.0.0"
  category: quality
  tags: json schema structured-output validation format
  risk: low
---
# Structured Output

- Treat the schema as a contract: include every required field with a value of the
  declared type, and add no fields the schema does not define.
- Never fill a field with a placeholder or guess just to satisfy the schema. Use the
  schema's way of expressing "unknown" (null, an empty list, a documented sentinel), and
  say so when the schema has no way to represent what you know.
- Use enum and literal values exactly as declared, not a close synonym.
- Respect documented ranges and limits (a score between 0 and 1, at most N items).
- Validate nested objects against their own schemas; a valid outer shape does not make
  the inner objects valid.
- Keep free-text fields consistent with the structured fields next to them.
- Before returning, check the output against the schema you were given, not your memory
  of a similar one.

## Check before returning

```text
Schema fields: <list of required fields>
Missing required: none
Unknown values expressed as: null / [] / "unknown" (as the schema defines)
Enum values copied exactly: yes
Ranges and limits respected: yes
```
