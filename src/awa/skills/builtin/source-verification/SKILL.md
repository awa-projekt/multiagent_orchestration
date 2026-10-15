---
name: source-verification
description: >-
  Verifies the claims in a draft against the supplied evidence one at a time, keeping
  "does the evidence support it" separate from "how trustworthy is the source", and flags
  unsupported critical claims. Use when fact-checking a draft, auditing citations, or
  deciding whether an answer is supported.
metadata:
  version: "1.0.1"
  category: research
  tags: fact-checking verification citations evidence claims
  risk: low
---
# Source Verification

- Split the draft into individual, checkable claims and verify each on its own.
- Judge support only from the supplied evidence, not from what you believe is true.
  Classify each claim as supported, partially supported, contradicted, or unsupported.
- Keep two judgments apart: support (does the cited passage say this?) and source
  quality (how authoritative, current, and independent is the source?). A strong source
  that does not say the claim provides no support.
- Check that each citation points to the passage that supports the claim, not just a
  related document, and check numbers, dates, names, and quotations exactly.
- Watch for overreach: claims broader, stronger, or more certain than their evidence,
  such as a general rule drawn from one case or causation from correlation.
- Call a source primary or official only when the evidence identifies it as such.
- A page that only shows that a search, index or registry found nothing (an empty result
  list, "no results", "did not match") is no evidence about the subject: record the point
  as not found and never cite such a page as support for a claim.
- List critical claims (those the answer depends on) that are unsupported or
  contradicted first, as blockers.
- For each claim report the verdict, the evidence reference, and a one-line reason;
  finish with an overall verdict.

## Output

```text
Claim 1: "<claim>" | supported | [evidence ref] | <one-line reason>
Claim 2: "<claim>" | unsupported | - | no passage states this
Blockers: <critical claims that are unsupported or contradicted>
Overall: reliable / needs changes / not supported
```
