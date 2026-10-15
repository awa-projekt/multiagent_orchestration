---
name: research-synthesis
description: >-
  Synthesizes several sources into a structured, traceable answer: decomposes the
  question, tracks what each source says, surfaces disagreements, and separates findings
  from inference. Use for research questions, literature or market overviews, and any
  answer that combines multiple sources.
metadata:
  version: "1.0.0"
  category: research
  tags: research synthesis literature sources comparison analysis
  risk: low
---
# Research Synthesis

- Break the question into the sub-questions it depends on before gathering sources.
- Record each source's contribution separately (which sub-question it addresses and what
  it actually says) before writing prose, so every claim stays traceable.
- When sources disagree, present both positions with their sources. Do not pick one
  silently or blend them into a claim neither makes.
- Weigh sources by relevance, quality, and recency. Many sources repeating one original
  claim are not independent confirmation.
- Label your own inferences across sources as synthesis, distinct from what any single
  source states.
- Stop searching when new sources only repeat what you already have, and say where
  coverage remains thin.
- Organize the answer around the sub-questions, not the order in which sources were
  found, and close with the direct answer and its confidence.

## Output

```text
Question: <the question and its sub-questions>
| Sub-question | Source | What it says |
| 1 | <source> | <finding> |
Disagreements: <source A says X; source B says Y>
Synthesis (my inference): <statement>
Answer: <direct answer> | Confidence: high / medium / low because <reason>
Gaps: <what is thin or missing>
```
