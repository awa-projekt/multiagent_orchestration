"""GT4, the built wheel: code and allowlisted product data only (rules R14 and R25).

The static half (every file under src/awa is code or on the allowlist) is the key
`GT4.outside_allowlist` of test_guards.py. This half builds the wheel and reads it back, so a
file that hatch would ship but the scan did not see (a packaging setting, a force-include) fails
too. Run it explicitly: `pytest -m packaging`. With `AWA_TEST_WHEEL` set it reads that wheel
instead of building one.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

from _helpers.wheel import prebuilt_wheel
from architecture.measures import wheel_allowed
from architecture.scan import REPO

pytestmark = pytest.mark.packaging


def _built_wheel(folder: Path) -> Path:
    if (wheel := prebuilt_wheel()) is not None:
        return wheel
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("building the wheel needs uv on the path")
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(folder), str(REPO)],
        check=True,
        capture_output=True,
    )
    return next(folder.glob("*.whl"))


def test_the_built_wheel_holds_only_the_package(tmp_path: Path) -> None:
    names = zipfile.ZipFile(_built_wheel(tmp_path)).namelist()
    version = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]

    assert {n.split("/")[0] for n in names} == {"awa", f"awa_agents-{version}.dist-info"}
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    stray = [n for n in names if n.startswith("awa/") and not wheel_allowed(n)]
    assert not stray, f"data in the wheel outside the allowlist: {stray} (rule R14)"
    # The PostgreSQL setup reads this at run time (rule R25).
    assert "awa/project_setup/compose.services.yaml" in names
