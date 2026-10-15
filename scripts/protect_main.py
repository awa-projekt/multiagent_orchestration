"""Apply the rulesets in `.github/rulesets/` to a GitHub repository.

Usage: `python scripts/protect_main.py OWNER/REPO`. Needs the GitHub CLI logged in as an admin of
the repository. A public repository (or a paid plan) is required for rulesets. A ruleset with the
same name is updated in place, so the script can run again after a change to the files.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

RULESETS = Path(__file__).resolve().parent.parent / ".github" / "rulesets"


def _gh(*args: str, payload: str | None = None) -> str:
    result = subprocess.run(
        ["gh", "api", *args], input=payload, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise SystemExit(f"gh api {' '.join(args)} failed: {result.stdout}{result.stderr}")
    return result.stdout


def main(repo: str) -> None:
    existing = {item["name"]: item["id"] for item in json.loads(_gh(f"repos/{repo}/rulesets"))}
    for path in sorted(RULESETS.glob("*.json")):
        ruleset = json.loads(path.read_text(encoding="utf-8"))
        name = ruleset["name"]
        if name in existing:
            _gh(
                "-X",
                "PUT",
                f"repos/{repo}/rulesets/{existing[name]}",
                "--input",
                "-",
                payload=json.dumps(ruleset),
            )
            print(f"updated {name}")
        else:
            _gh("-X", "POST", f"repos/{repo}/rulesets", "--input", "-", payload=json.dumps(ruleset))
            print(f"created {name}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/protect_main.py OWNER/REPO")
    main(sys.argv[1])
