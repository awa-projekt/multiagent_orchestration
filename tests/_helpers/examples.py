"""Import the scripts of `examples/` as modules, for their offline tests."""

from __future__ import annotations

import functools
import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage

from awa.testing import ModelCall

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
LEARNING_PATH = sorted(path.stem for path in EXAMPLES_DIR.glob("[0-9][0-9]_*.py"))


@functools.cache
def load_example(name: str) -> ModuleType:
    """Import `examples/<name>.py` once; the scripts import `_support` and `blueprints` beside them."""

    if str(EXAMPLES_DIR) not in sys.path:
        sys.path.insert(0, str(EXAMPLES_DIR))
    spec = importlib.util.spec_from_file_location(
        f"awa_example_{name}", EXAMPLES_DIR / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def system_prompt(call: ModelCall) -> str:
    """The system prompt a scripted model received."""

    first = call.messages[0]
    assert isinstance(first, SystemMessage)
    return str(first.text)


def tool_results(messages: Sequence[BaseMessage]) -> list[str]:
    """The contents of the tool results among `messages`, in order."""

    return [str(m.content) for m in messages if isinstance(m, ToolMessage)]
