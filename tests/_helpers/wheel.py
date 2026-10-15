"""The wheel the install tests check: a prebuilt one from `AWA_TEST_WHEEL`, else none."""

from __future__ import annotations

import os
from pathlib import Path

WHEEL_VARIABLE = "AWA_TEST_WHEEL"


def prebuilt_wheel() -> Path | None:
    """The wheel `AWA_TEST_WHEEL` names (CI sets it to the wheel of the `package` job), or `None`.

    Raises:
        FileNotFoundError: If the variable is set but names no file.
    """

    value = os.environ.get(WHEEL_VARIABLE, "").strip()
    if not value:
        return None
    wheel = Path(value)
    if not wheel.is_file():
        raise FileNotFoundError(f"{WHEEL_VARIABLE}={value} names no file")
    return wheel
