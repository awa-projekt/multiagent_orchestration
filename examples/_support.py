"""Command-line helpers shared by the examples.

`require_model_credentials` checks, before an example starts, that the
provider of the configured default model (`AWA_DEFAULT_MODEL`, by default
`anthropic:claude-sonnet-5-5`) is installed and has an API key, and exits with a
one-line explanation otherwise. Variables from a `.env` file in the current
directory are loaded first.
"""

from __future__ import annotations

import importlib.util
import os
import sys

from dotenv import find_dotenv, load_dotenv

from awa.core.providers import provider
from awa.core.settings import get_settings


def missing_model_setup(model: str) -> str | None:
    """Describe what is missing to call `model`, or return `None` if it looks usable.

    Args:
        model: A `provider:model` string.

    Returns:
        A one-line explanation, or `None` when the provider package is
        installed and its credentials are set (or the provider is unknown).
    """

    name = model.partition(":")[0] if ":" in model else ""
    spec = provider(name)
    if spec is None:
        return None
    if importlib.util.find_spec(spec.module) is None:
        install = (
            f'pip install -e ".[{spec.extra}]" (in the awa folder)'
            if spec.extra
            else f"pip install {spec.package}"
        )
        return f"The model {model!r} needs the {spec.package} package: {install}"
    missing = spec.missing(os.environ.get)
    if missing:
        return (
            f"Set {', '.join(missing)} to run this example with {model!r}, or choose another "
            "model with AWA_DEFAULT_MODEL=provider:model."
        )
    return None


def require_model_credentials() -> None:
    """Prepare the console for UTF-8 output and exit with a one-line message if
    the default model cannot be called.

    Raises:
        SystemExit: If the provider package or its API key is missing.
    """

    for stream in (sys.stdout, sys.stderr):
        # Consoles with a legacy code page would garble model output otherwise.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(find_dotenv(usecwd=True))
    problem = missing_model_setup(get_settings().default_model)
    if problem is not None:
        raise SystemExit(problem)
