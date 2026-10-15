# Core

The packages every awa program uses: load a blueprint, run it, read the result, choose the models
and extend awa.

## Example

Load a blueprint and run it with a scripted model (no key, no network):

```python title="core_example.py"
import asyncio

import awa
from awa.models import ModelRegistry
from awa.testing import FakeChatModel

BLUEPRINT = """
name: front-desk
agents:
  - name: greeter
    description: Greets a visitor by name.
    instructions: Greet the visitor by name in one short sentence.
    model: scripted
"""

models = ModelRegistry()
models.register("scripted", FakeChatModel(["Hello, Ada, welcome!"]))  # no key, no network
system = awa.load(BLUEPRINT, model_registry=models)
result = asyncio.run(system.run("Ada has arrived."))
print(result.output)
print(result.usage.requests, "model call")
```

It prints:

```text title="core_example.out"
Hello, Ada, welcome!
1 model call
```

::: awa.blueprints
    options:
      show_root_full_path: true

::: awa.quick
    options:
      show_root_full_path: true

::: awa.refine
    options:
      show_root_full_path: true

::: awa.runtime
    options:
      show_root_full_path: true

::: awa.contracts
    options:
      show_root_full_path: true

::: awa.models
    options:
      show_root_full_path: true

::: awa.config
    options:
      show_root_full_path: true

## Settings and the guided setup

`awa.Settings` holds the configuration that awa reads from keyword arguments, the environment and `.env`
([Configuration](../../develop/configuration.md)); `awa.setup()` is the guided setup of `awa setup` for a notebook.
Both import from `awa`.

::: awa.core.settings
    options:
      show_root_full_path: true
      members: [Settings, get_settings]

::: awa.project_setup
    options:
      show_root_full_path: true
      members: [setup, SetupPlan, SectionState, Choice, EnvChanges, SetupError]

::: awa.plugins
    options:
      show_root_full_path: true

::: awa.ext
    options:
      show_root_full_path: true
