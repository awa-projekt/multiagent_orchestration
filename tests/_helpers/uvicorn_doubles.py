"""A stand-in for uvicorn that records which project a command would serve, and serves nothing."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def recording_uvicorn(served: list[Path]) -> Any:
    """A `uvicorn` module double: `Server(config).run()` returns at once and appends the
    project folder of the served app to `served`."""

    class FakeServer:
        def __init__(self, config: Any) -> None:
            served.append(config.app.state.project.directory)
            self.started = False
            self.should_exit = True

        def run(self) -> None:
            return None

    class Uvicorn:
        Config = staticmethod(lambda app, **kwargs: type("C", (), {"app": app})())
        Server = FakeServer

    return Uvicorn
