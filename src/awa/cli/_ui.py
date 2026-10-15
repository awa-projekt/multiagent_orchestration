"""`awa ui`: serve the local web app on loopback and open it with a one-time access link."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from awa.cli._common import EXIT_OK, LOOPBACK_HOSTS, Commands, add_command

DEFAULT_PORT = 8765


def add_ui(commands: Commands) -> None:
    parser = add_command(commands, "ui")
    parser.add_argument(
        "--project",
        default=None,
        metavar="DIR",
        help="project folder (created when missing; default: awa --project, else the current folder)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="interface (default 127.0.0.1)")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port (default {DEFAULT_PORT}; a free one when it is taken)",
    )
    parser.add_argument(
        "--example",
        metavar="ID",
        help="open a shipped use case or recipe as a new project first (its data and process "
        "description; in DIR when it is empty, else in DIR/ID)",
    )
    parser.add_argument(
        "--examples",
        metavar="DIR",
        help="the examples folder (default: the one of the awa source folder, else ./examples)",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a non-loopback --host (anyone with the link on that network gets access)",
    )
    parser.set_defaults(handler=_ui)


def _ui(args: argparse.Namespace) -> int:
    """`awa ui`: serve until interrupted; refuses a non-loopback `--host` without `--allow-remote`."""

    from awa.errors import ConfigurationError

    if args.host not in LOOPBACK_HOSTS and not args.allow_remote:
        raise ConfigurationError(
            f"awa ui serves on this computer only; --host {args.host} would let other "
            "computers reach it. Add --allow-remote if that is intended."
        )
    from awa.server._app import import_uvicorn
    from awa.ui import create_ui_app
    from awa.ui._security import new_token

    uvicorn = import_uvicorn()
    port = _free_port(args.host, args.port)
    token = new_token()
    project = Path(args.project or args.project_folder or ".")
    if args.example:
        from awa.ui._examples import example_target, open_example

        target = example_target(project, args.example, beside=False)
        opened = open_example(
            args.example, target, examples=args.examples, settings_from=project.resolve()
        )
        print(f"Opened the example {opened['title']} in {opened['folder']}")
        project = target
    app = create_ui_app(project, token=token, host=args.host, port=port, examples=args.examples)
    shown = f"[{args.host}]" if ":" in args.host else args.host
    url = f"http://{shown}:{port}/?token={token}"
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=port, log_level="warning"))
    print(f"awa is running for {app.state.project.directory}")
    print(f"Open this link in your browser: {url}")
    print("Keep this window open while you use awa; close it or press Ctrl+C to stop.")
    sys.stdout.flush()
    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(server, url), daemon=True).start()
    server.run()
    return EXIT_OK


def _free_port(host: str, preferred: int) -> int:
    """`preferred` when it can be bound, else a free port the system picks."""

    from awa.errors import ConfigurationError

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    for port in (preferred, 0):
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return int(probe.getsockname()[1])
    raise ConfigurationError(f"No free port on {host}")


def _open_when_ready(server: Any, url: str) -> None:
    while not server.started and not server.should_exit:
        time.sleep(0.1)
    if server.started:
        webbrowser.open(url)
