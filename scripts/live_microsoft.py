"""Live check of the Microsoft 365 setup on your tenant: read-only probes first, then one draft.

    python scripts/live_microsoft.py --project DIR      # live, with DIR/.env
    python scripts/live_microsoft.py --no-drafts        # live, read-only probes only

Phase 1 runs `awa check microsoft` (values, app sign-in, mailbox and its negative test, site and
its negative test, workbook read locally, Teams without sending, people search, Azure OpenAI):
every call reads. Phase 2 runs only when phase 1 has no failure: it creates ONE reply draft in
the Drafts folder of `MS_MAILBOX` (nothing is sent; delete the draft afterwards, awa does not).

The report (`report.json` and `report.md` in `--out`, default `.awa/live_microsoft/<time>`) lists
every line with its status, the fixes and the administrator message, and which FakeGraph rules
the run observed on the real tenant (candidates for `verified_live=True`, to be set by hand after
reading the report). It is evidence from one tenant and one run, not a guarantee. No value from
`.env` is printed or written. Exit code 0 when nothing failed, 1 otherwise, 2 for a usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

OBSERVED_RULES: dict[str, tuple[str, ...]] = {
    "microsoft.sign_in": ("entra.client_credentials_need_default_scope",),
    "microsoft.outlook_scope": ("outlook.rbac_roles_not_in_token",),
    "microsoft.sharepoint": ("sharepoint.sites_selected_cannot_search",),
    "microsoft.sharepoint_scope": ("sharepoint.sites_selected_cannot_read_the_root_site",),
    "microsoft.people": ("directory.user_search_needs_eventual_consistency",),
}
"""FakeGraph rules whose behavior a passing live check shows on the real tenant."""

DRAFT_TEXT = "awa live check: this is a draft, nothing was sent. You can delete it."


def draft_reply(env: dict[str, str], options: dict[str, Any]) -> dict[str, Any]:
    """Phase 2: one reply draft to the newest message of `MS_MAILBOX` (never sent)."""

    from awa.connectors import connector_tools

    config = {
        **options,
        "requires_approval": False,
        "include": ["list_messages", "create_reply_draft"],
    }
    tools = {t.name: t for t in connector_tools("outlook", env=env, config=config)}
    listing = str(tools["outlook_list_messages"].invoke({"limit": 1}))
    if not listing.startswith("<tool_output"):
        return {"status": "fail", "said": "the newest message could not be listed: " + listing}
    inner = json.loads(listing.split(">", 1)[1].rsplit("</tool_output>", 1)[0])["content"]
    items = json.loads(inner).get("items", [])
    if not items:
        return {"status": "skip", "said": "the inbox is empty, so no draft was made"}
    answer = str(
        tools["outlook_create_reply_draft"].invoke(
            {"message_id": items[0]["id"], "comment": DRAFT_TEXT}
        )
    )
    if not answer.startswith("<tool_output"):
        return {"status": "fail", "said": "the draft was refused: " + answer}
    return {"status": "ok", "said": "one reply draft is in Drafts (not sent; delete it by hand)"}


def _report(phase1: Any, phase2: dict[str, Any] | None, seconds: float) -> dict[str, Any]:
    data = phase1.to_dict()
    observed = sorted(
        {
            rule
            for run in phase1.runs
            if run.result.status == "ok"
            for rule in OBSERVED_RULES.get(run.check.id, ())
        }
    )
    return {
        "made": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "seconds": round(seconds, 1),
        "phase1_read_only": data,
        "phase2_draft": phase2,
        "rules_observed_live": observed,
        "limits": "one tenant, one run; a pass shows the setup works today, not that it always will",
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Microsoft 365 live check",
        "",
        f"Made {report['made']} in {report['seconds']} s. {report['limits']}.",
        "",
        "| Status | Check | Result |",
        "| --- | --- | --- |",
    ]
    for check in report["phase1_read_only"]["checks"]:
        said = str(check["said"]).replace("|", "/")
        lines.append(f"| {check['status']} | {check['id']} | {said} |")
    draft = report["phase2_draft"]
    lines += [
        "",
        f"Draft phase: {draft['status']}: {draft['said']}" if draft else "Draft phase: not run",
    ]
    if report["rules_observed_live"]:
        lines += [
            "",
            "FakeGraph rules observed on the tenant: " + ", ".join(report["rules_observed_live"]),
        ]
    message = report["phase1_read_only"].get("admin_message")
    if message:
        lines += ["", "## Send this to your administrator", "", "```text", message, "```"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project", default=".", help="the folder whose .env is used (default .)")
    parser.add_argument("--no-drafts", action="store_true", help="skip the draft phase")
    parser.add_argument("--out", help="report folder (default .awa/live_microsoft/<time>)")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT / "src"))
    from awa.checks import run_groups
    from awa.checks._render import print_line

    project = Path(args.project).resolve()
    if not (project / ".env").is_file():
        print(f"error: {project / '.env'} does not exist", file=sys.stderr)
        return 2
    started = time.perf_counter()
    print("phase 1: read-only checks")
    phase1 = run_groups(["microsoft"], project=project, on_result=print_line)
    phase2: dict[str, Any] | None = None
    if args.no_drafts:
        phase2 = {"status": "skip", "said": "--no-drafts"}
    elif phase1.failed:
        phase2 = {"status": "skip", "said": "phase 1 has failures, so nothing was written"}
    else:
        from awa.checks._env import project_env
        from awa.checks.microsoft import NAMES

        lookup = project_env(project)
        env = {name: value for name in NAMES if (value := lookup(name))}
        phase2 = (
            draft_reply(env, {})
            if env.get("MS_MAILBOX")
            else {"status": "skip", "said": "MS_MAILBOX is not set"}
        )
    print(f"phase 2 (drafts only): {phase2['status']}: {phase2['said']}")
    report = _report(phase1, phase2, time.perf_counter() - started)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else project / ".awa" / "live_microsoft" / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out / "report.md").write_text(_markdown(report), encoding="utf-8")
    print(f"report: {out / 'report.md'}")
    failed = bool(phase1.failed) or phase2.get("status") == "fail"
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
