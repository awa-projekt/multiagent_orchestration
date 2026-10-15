"""The GitHub workflow mirrors the GitLab pipeline in what its test job requires."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def test_the_github_test_job_sets_every_test_variable_of_the_gitlab_one() -> None:
    gitlab = yaml.safe_load((REPO / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    github = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    wanted = {
        k: str(v) for k, v in gitlab["test"]["variables"].items() if k.startswith("AWA_TEST_")
    }
    given = {k: str(v) for k, v in github["jobs"]["test"]["env"].items()}

    assert "AWA_TEST_REQUIRE_BROWSER" in wanted
    assert {name: given.get(name) is not None for name in wanted} == dict.fromkeys(wanted, True)
    assert given["AWA_TEST_REQUIRE_BROWSER"] == wanted["AWA_TEST_REQUIRE_BROWSER"]
