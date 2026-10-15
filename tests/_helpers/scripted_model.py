"""The scripted chat model that every export carries as `tests/fakes.py`, loaded from its template."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from typing import Any

import awa.exporter

_PATH = Path(awa.exporter.__file__).parent / "templates" / "fakes.py.tmpl"


def _load() -> Any:
    loader = importlib.machinery.SourceFileLoader("exported_fakes", str(_PATH))
    spec = importlib.util.spec_from_loader("exported_fakes", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # pydantic resolves the annotations through it
    loader.exec_module(module)
    return module


ScriptedChatModel: Any = _load().ScriptedChatModel
