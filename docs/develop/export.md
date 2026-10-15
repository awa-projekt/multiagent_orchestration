# Export to plain LangChain and LangGraph

`awa export` turns a project with one agent into a standalone LangChain and LangGraph project that
runs without awa installed. Use it when the agent should live on as plain LangGraph code.

The export is a one-way exit. You edit the new folder with LangChain and LangGraph only, and
nothing is synced back. Staying an awa project remains the default, and no feature of awa is
reduced to make the export possible. The export of a single agent is part of the
[stable core](../about/stability.md).

## Export a project

The project is a blueprint with one agent and its `tools.py`:

```yaml title="refund-desk/blueprint.yaml"
format_version: awa/v1
name: refund-desk
agents:
  - name: clerk
    instructions: Answer refund questions. Refund only after approval.
    tools: [issue_refund]
approvals:
  - tool: issue_refund
    when: "amount > 50"
limits:
  max_requests: 20
```

```python title="refund-desk/tools.py"
from langchain_core.tools import tool

__all__ = ["issue_refund"]


@tool
def issue_refund(order_id: str, amount: float) -> str:
    """Refund an order."""
    return f"refunded {amount:.2f} EUR for {order_id}"
```

```python title="export_it.py"
from pathlib import Path

from awa.exporter import export

export("refund-desk", "refund-desk-graph")  # raises ExportUnsupportedError for a blueprint it refuses
out = Path("refund-desk-graph")
for name in ("langgraph.json", "run.py", "src/refund_desk/graph.py", "src/refund_desk/approvals.py", "tests/test_graph.py"):
    print(name, (out / name).is_file())
code = "\n".join(path.read_text(encoding="utf-8") for path in out.rglob("*.py"))
print("imports awa:", "import awa" in code or "from awa" in code)
```

```text title="export_it.out"
langgraph.json True
run.py True
src/refund_desk/graph.py True
src/refund_desk/approvals.py True
tests/test_graph.py True
imports awa: False
```

On the command line:

```console
awa export refund-desk --out refund-desk-graph          # write the folder
awa export refund-desk --check                          # render it and import the graph; write nothing
awa export refund-desk --out refund-desk-graph --force  # replace an earlier export (only an export)
awa export refund-desk --out refund-desk-graph --json   # the result as JSON
```

The destination must not exist or must be empty. `--force` replaces a folder only when it holds
the `NOTICE.md` of an earlier export. The folder is written all at once or not at all.

The export holds:

| In the export | Holds |
| --- | --- |
| `pyproject.toml`, `requirements.txt` | dependency ranges, and the exact versions awa ran with |
| `langgraph.json` | LangGraph Studio and `langgraph dev` |
| `README.md`, `NOTICE.md`, `SHA256SUMS` | how to run, test and extend; where it came from; the copied files |
| `run.py` | one run from the terminal, approvals asked on the terminal |
| `src/<package>/graph.py` | `create_agent(...)`: `build_graph(model, checkpointer)` and `graph` |
| `src/<package>/settings.py`, `prompts/<agent>.md` | the model, its settings and the limits; the system prompt as awa rendered it |
| `src/<package>/tools.py`, `discover.py` | your `tools.py`, copied unchanged, and how its tools are found |
| `approvals.py`, `conditions.py`, `limits.py`, `skills/`, `schemas.py` | approval conditions, run limits, skills and the output schema, when the blueprint uses them |
| `tests/` | offline tests that drive the real graph with a scripted chat model |

Importing `graph.py` needs no credentials: the chat model is created on first use through
`init_chat_model`.

The export is a code generator, separate from the engine a run uses. It reads the validated
blueprint and writes LangGraph source; it does not run through the
[engine port](architecture.md#the-engine-port).

## How a blueprint maps

| Blueprint | In the export |
| --- | --- |
| one agent, its instructions, `skills:` | the rendered system prompt in `prompts/<agent>.md` |
| `defaults.model`, `model_settings` | `init_chat_model("provider:model", **settings)`; a profile is resolved at export, and the variable `MODEL` overrides it |
| `max_steps`, `limits.max_requests` | `ModelCallLimitMiddleware` with the smaller of the two |
| `limits.max_tool_calls` | `ToolCallLimitMiddleware` |
| `limits.max_total_tokens`, `limits.max_duration` | `RunLimits` in `limits.py` |
| `approvals:`, with or without `when:` | `HumanInTheLoopMiddleware` (approve, edit or reject), the condition evaluated by the safe parser of `conditions.py`; a call it cannot decide asks |
| `policies: deny:` | the tool is left out |
| `output_schema` | a pydantic model in `schemas.py`, as `response_format` |
| `compaction` | `SummarizationMiddleware` |

**What stays the same.** In the tests, the same scripted model answers drive the awa agent and
the exported graph. Both call the same tools in the same order, pause on the same call, handle a
rejection the same way and return the same structured output. A fresh environment with only the
export's `requirements.txt` cannot import awa and still passes the exported tests.

**What is not reproduced.** awa's own extras: tool timeouts, retries, idempotency and output
framing, caches, run records, redaction and guardrails.

## What is refused

A blueprint that uses any of these is refused as a whole. The command prints one line per
feature, `export.unsupported <blueprint>: <feature> - <what to do>`, writes nothing and exits with
code 4. In Python, `export` raises `ExportUnsupportedError`.

| Feature | What to do |
| --- | --- |
| `team`, `workflow`, `process`, `network`, more than one agent, roles | Export a blueprint with exactly one agent. |
| `connectors`, `builtin:*`, `mcp:*`, `plugin:*` tools, import references | Write the calls as tools in `tools.py`. |
| `data`, `documents`, `special_cases`, `components`, `memory`, `refine` | awa only; move the logic into a tool or the prompt. |
| `auto` | Write the matched skills and tools into the blueprint first and set `auto: false`. |
| `limits.max_cost` | Use a token limit. |
| `policies` with `ask` or `use`, an approval `when` that is an import reference | Use `approvals` with an expression. |
| Model settings `effort`, `retry`, `fallbacks`, `circuit_breaker` | Configure the provider client in the exported code. |
| A `tools.py` that imports anything but the standard library, LangChain, LangGraph or pydantic | Keep the tools free of awa and local modules; the finding names the line. |

Every shipped example is exported or refused with exactly the findings in
`tests/export/expected_refusals.json`.

## Extend the export

| To change | Edit |
| --- | --- |
| the prompt | `prompts/<agent>.md` |
| the model and the limits | `settings.py` |
| anything else: tools, middleware, your own `StateGraph` instead of `create_agent` | `graph.py` |

For persistence, pass a LangGraph checkpointer to `build_graph`; `langgraph dev` supplies its own.
To start again from the blueprint, export into a new folder.
