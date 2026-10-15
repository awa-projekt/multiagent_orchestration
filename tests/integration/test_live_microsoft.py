"""Read-only probes of a real Microsoft 365 tenant, run explicitly with `-m live_microsoft`.

Set `AWA_TEST_MICROSOFT_PROJECT` to a project folder whose `.env` holds the tenant settings
(`docs/guide/microsoft-365.md`). The test runs `scripts/live_microsoft.py --no-drafts`: every
call reads, nothing is written to the tenant, and no value of the `.env` file is printed.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live_microsoft,
    pytest.mark.integration,
    pytest.mark.model_requests_allowed,
]

PROJECT_VARIABLE = "AWA_TEST_MICROSOFT_PROJECT"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live_microsoft.py"


def test_the_read_only_probes_pass_on_the_tenant(tmp_path: Path) -> None:
    project = os.environ.get(PROJECT_VARIABLE)
    if not project:
        pytest.skip(f"set {PROJECT_VARIABLE} to a project folder with the tenant's .env")
    spec = importlib.util.spec_from_file_location("live_microsoft", SCRIPT)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    out = tmp_path / "report"

    assert script.main(["--no-drafts", "--project", project, "--out", str(out)]) == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["phase1_read_only"]["counts"]["fail"] == 0
    assert report["phase2_draft"]["status"] == "skip"
