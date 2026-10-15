# Teams and workflows

Several agents working together: teams, networks, workflows and the boards, messages and directories
they share.

## Example

Two agents in a fixed order; the second one gets the answer of the first:

```python title="teams_example.py"
import asyncio

from awa import Agent, Sequential
from awa.testing import FakeChatModel

writer = Agent("writer", model=FakeChatModel(["Draft: the meeting moves to Friday."]))
editor = Agent("editor", model=FakeChatModel(["The meeting moves to Friday, 10 am."]))
memo = Sequential("memo", [writer, editor])  # the editor gets the writer's answer
result = asyncio.run(memo.run("Tell the team the meeting moves to Friday."))
print(result.output)
```

It prints:

```text title="teams_example.out"
The meeting moves to Friday, 10 am.
```

::: awa.team
    options:
      show_root_full_path: true
      inherited_members: [run_sync, start, stream]

::: awa.workflows
    options:
      show_root_full_path: true
      inherited_members: [run_sync, start, stream]

::: awa.messaging
    options:
      show_root_full_path: true

::: awa.tasks
    options:
      show_root_full_path: true

::: awa.directory
    options:
      show_root_full_path: true

::: awa.people
    options:
      show_root_full_path: true
