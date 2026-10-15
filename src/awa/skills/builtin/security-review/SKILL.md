---
name: security-review
description: >-
  Reviews code, configuration, or a design for security weaknesses such as injection,
  broken authorization, secret leakage, and unsafe agent or LLM behavior, rating each
  finding by exploitability and impact. Use when assessing a change or system that
  handles untrusted input, credentials, sensitive data, or tool-using agents.
metadata:
  version: "1.0.0"
  category: safety
  tags: security vulnerability injection authorization secrets threat-model
  risk: low
---
# Security Review

- Treat all external input as untrusted until validated against an explicit expectation
  (type, shape, length, allowed values): user input, files, network responses, tool
  output, and other agents' messages.
- Find every place untrusted input reaches a shell command, file path, database query,
  template, or model prompt. The fix is parameterisation or an allow-list; a blocklist of
  bad patterns is always incomplete.
- Verify authorization for the specific action and resource, not only that the caller
  is authenticated.
- Look for secrets, credentials, and personal data that can leak into logs, error
  messages, prompts, traces, or committed files.
- Check that failures fail closed (deny or abort) rather than open.
- For agents and LLM components, assume retrieved content and tool output may contain
  adversarial instructions; check that such content cannot trigger tools, change goals,
  or exfiltrate data, and that high-impact actions need approval.
- Rate findings by real exploitability and impact. An unreachable code path is not a
  high-severity finding.
- For each finding give the location, the attack scenario, the impact, and the fix.

When the change involves an LLM or agent, check the categories in
`references/llm-risk-categories.md`.

## Finding format

```text
[high|medium|low] <title>
Location: <file:line or component>
Attack: <how untrusted input reaches it>
Impact: <what an attacker gains>
Fix: <parameterise, allow-list, authorize, fail closed>
Not a finding: <paths checked and unreachable>
```
