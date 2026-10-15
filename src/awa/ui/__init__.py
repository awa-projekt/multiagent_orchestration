"""Local web app (extension) behind `awa ui`: set up, add data, describe, review, connect,
test, try and use a system in the browser. `create_ui_app` builds the app (loopback by default,
token and origin protected); `Project` is the project folder it works on. Needs the `server` extra.

Preview: it works and is tested, but its API may still change in a minor release.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from awa.ui._project import Project

if TYPE_CHECKING:
    from awa.ui._app import create_ui_app


def __getattr__(name: str) -> Any:
    # FastAPI loads on first use, so `import awa.ui` needs no extras.
    if name == "create_ui_app":
        from awa.ui._app import create_ui_app

        return create_ui_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__stability__ = "preview"

__all__ = ["Project", "create_ui_app"]
