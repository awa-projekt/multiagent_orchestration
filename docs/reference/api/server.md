# Serve and operate

Run a system as a web API, an MCP server or a background job, start it from triggers, trace it,
evaluate it, test it offline and export it.

## Example

Serve an agent as a web API and call it (the test client needs no network):

```python title="server_example.py"
from starlette.testclient import TestClient

from awa import Agent
from awa.server import create_app
from awa.testing import FakeChatModel

desk = Agent("desk", model=FakeChatModel(["Order 1002 shipped on Monday."]))
client = TestClient(create_app(desk))  # create_app returns a FastAPI app
answer = client.post("/v1/systems/desk/runs", json={"input": "Where is order 1002?"})
print(answer.status_code, answer.json()["output"])
```

It prints:

```text title="server_example.out"
200 Order 1002 shipped on Monday.
```

::: awa.server
    options:
      show_root_full_path: true

::: awa.server.jobs
    options:
      show_root_full_path: true

::: awa.server.mcp
    options:
      show_root_full_path: true

::: awa.server.ports
    options:
      show_root_full_path: true

::: awa.automation
    options:
      show_root_full_path: true

::: awa.observability
    options:
      show_root_full_path: true

::: awa.evals
    options:
      show_root_full_path: true

::: awa.testing
    options:
      show_root_full_path: true

::: awa.testing.contracts
    options:
      show_root_full_path: true

::: awa.exporter
    options:
      show_root_full_path: true
