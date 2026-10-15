"""Prove that generated code imports, in an interpreter that has not loaded awa."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from awa.exporter._report import ExportError

IMPORT_SECONDS = 60
"""Longest wait for the generated graph to import."""


def import_graph(folder: Path, package: str) -> None:
    """Import the generated graph of `package` in a fresh interpreter.

    Raises:
        ExportError: If the import fails or takes longer than `IMPORT_SECONDS`.
    """

    code = f"import sys; sys.path.insert(0, 'src'); import {package}.graph"
    try:
        done = subprocess.run(
            [sys.executable, "-c", code],
            cwd=folder,
            capture_output=True,
            text=True,
            check=False,
            timeout=IMPORT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise ExportError(
            f"the generated code did not import within {IMPORT_SECONDS} seconds; check the "
            "tools file for work done at import time"
        ) from None
    if done.returncode:
        raise ExportError(f"the generated code does not import: {done.stderr.strip()[-400:]}")
