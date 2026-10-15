"""Where awa's settings come from: the one `.env` loader and the settings registry.

A value is taken from the process environment, else from the project `.env`, else the registry
default. `load_environment` reads the project `.env` (only the `awa` command line exports it into
`os.environ`), `source_of` says where a value comes from, and `registry()` lists every setting.
"""

from __future__ import annotations

from awa.core.config import (
    LoadedEnvironment,
    ValueSource,
    current,
    env_value,
    find_project,
    load_environment,
    registry,
    source_of,
    unload_environment,
)
from awa.core.settings_registry import SettingSpec

__all__ = [
    "LoadedEnvironment",
    "SettingSpec",
    "ValueSource",
    "current",
    "env_value",
    "find_project",
    "load_environment",
    "registry",
    "source_of",
    "unload_environment",
]
