"""Where the checks read their settings: the process environment, then the project `.env`.

`CHECK_SETTINGS` names the variables only `awa check` reads. Values are never printed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from awa.config import LoadedEnvironment

CHECK_SETTINGS: dict[str, str] = {
    "AWA_CHECK_NEGATIVE_MAILBOX": "a mailbox the app must NOT be able to read (the negative test)",
    "AWA_CHECK_ALLOW_POST": "'teams' lets awa check post one test card; empty: never post",
    "MS_CHECK_WORKBOOK": "path of the workbook the Excel check reads (default: the first .xlsx)",
}
"""Settings only `awa check` reads (all in the settings registry)."""


def project_env(project: Path) -> Callable[[str], str | None]:
    """A lookup: the process environment, else `project/.env` (read once; missing means empty)."""

    path = project / ".env"
    loaded = LoadedEnvironment(project_dir=project, dotenv_path=path if path.is_file() else None)
    return loaded.get
