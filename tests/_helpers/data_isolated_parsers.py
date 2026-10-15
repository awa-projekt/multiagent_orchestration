"""Parsers for the isolation tests. They live in their own light module because the worker process
imports them by name (`_helpers.data_isolated_parsers:profile_mixed`)."""

from __future__ import annotations

import os
from pathlib import Path

from awa.data import FileProfile, ParseContext, make_flag


def profile_mixed(path: Path, ctx: ParseContext) -> FileProfile:
    """Behaves by file name: `loop` never ends, `raise` fails, `exit` kills the process,
    anything else reports the id of the process it ran in (and prints noise to stdout)."""

    if "loop" in path.name:
        while True:
            pass
    if "raise" in path.name:
        raise ValueError("secret content of the file")
    if "exit" in path.name:
        os._exit(3)
    print("noise on stdout that must not reach the protocol")
    ctx.profile.flags.append(make_flag("DF-I-WORKER", f"pid {os.getpid()}", ctx.file_id))
    return ctx.profile
