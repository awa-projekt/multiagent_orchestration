"""`awa.checks` and `awa check`: the engine, the Microsoft checks against FakeGraph, every scenario,
the negative tests, the write gate and the command line."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from _helpers.microsoft_world import EXPECTED, microsoft_world
from awa.checks import (
    EXIT_CONFIGURATION,
    EXIT_FAILED,
    EXIT_OK,
    Check,
    CheckContext,
    Result,
    run_checks,
)
from awa.checks.microsoft import check_values, webhook_problem
from awa.cli import main

GUID = "8f3c2a71-5d4e-4b6a-9c1f-2e7d0b9a4c35"


def ctx_of(**values: str) -> CheckContext:
    return CheckContext(lambda name: values.get(name))


# --------------------------------------------------------------------------- the engine


def test_a_check_after_a_failed_one_is_skipped_and_says_why() -> None:
    first = Check("g.first", "g", "first", lambda ctx: Result.fail("broken", "repair it"))
    second = Check("g.second", "g", "second", lambda ctx: Result.ok("fine"), after=("g.first",))
    report = run_checks([first, second], ctx_of())
    assert report.result("g.second").status == "skip"
    assert "g.first failed" in report.result("g.second").said
    assert report.exit_code == EXIT_FAILED


def test_a_missing_variable_skips_and_names_the_variable() -> None:
    check = Check("g.c", "g", "the mailbox", lambda ctx: Result.ok("x"), needs=("MS_MAILBOX",))
    result = run_checks([check], ctx_of()).result("g.c")
    assert result.status == "skip" and "MS_MAILBOX" in result.said
    assert run_checks([check], ctx_of()).exit_code == EXIT_OK


def test_a_writing_check_runs_only_when_allowed() -> None:
    ran: list[str] = []

    def post(ctx: CheckContext) -> Result:
        ran.append("posted")
        return Result.ok("sent")

    check = Check("g.post", "g", "post", post, writes=True, allow="teams")
    assert run_checks([check], ctx_of()).result("g.post").code == "check.write_not_allowed"
    assert ran == []
    allowed = CheckContext(lambda name: None, allow_post=frozenset({"teams"}))
    assert run_checks([check], allowed).result("g.post").status == "ok" and ran == ["posted"]


def test_a_check_that_raises_is_a_failure_not_a_crash() -> None:
    def boom(ctx: CheckContext) -> Result:
        raise RuntimeError("no")

    report = run_checks([Check("g.boom", "g", "boom", boom)], ctx_of())
    assert report.result("g.boom").status == "fail"
    assert report.result("g.boom").code == "check.error"


def test_the_admin_message_collects_each_step_once() -> None:
    same = Result.fail("a", "b", admin="Grant the site.")
    checks = [Check(f"g.{n}", "g", n, lambda ctx: same) for n in ("one", "two")]
    message = run_checks(checks, ctx_of()).admin_message()
    assert message is not None and message.count("Grant the site.") == 1


# --------------------------------------------------------------------------- values


@pytest.mark.parametrize(
    ("values", "status", "code"),
    [
        ({"AZURE_CLIENT_SECRET": GUID}, "fail", "ms.secret_id_copied"),
        (
            {"AZURE_CLIENT_SECRET": "", "AZURE_CLIENT_CERTIFICATE_PATH": ""},
            "fail",
            "ms.credential_missing",
        ),
        (
            {"MS_SITE_URL": "https://contoso.sharepoint.com/Shared Documents"},
            "fail",
            "ms.site_shape",
        ),
        ({"MS_MAILBOX": "support"}, "fail", "ms.mailbox_shape"),
        ({"AZURE_CLIENT_ID": "my-app"}, "fail", "ms.client_shape"),
        ({"AZURE_CLIENT_CERTIFICATE_PATH": "missing.pem"}, "fail", "ms.certificate_unreadable"),
        (
            {"TEAMS_WEBHOOK_URL": "https://contoso.webhook.office.com/webhookb2/x"},
            "fail",
            "ms.webhook_retired",
        ),
        (
            {"TEAMS_WEBHOOK_URL": "https://prod-1.westeurope.logic.azure.com/workflows/x?sig=y"},
            "warn",
            "ms.webhook_old_host",
        ),
        ({"TEAMS_TEAM_ID": GUID, "TEAMS_CHANNEL_ID": "General"}, "fail", "ms.channel_shape"),
        ({}, "ok", None),
    ],
)
def test_the_values_check_names_the_mistake(
    values: dict[str, str], status: str, code: str | None
) -> None:
    base = {
        "AZURE_TENANT_ID": "contoso.onmicrosoft.com",
        "AZURE_CLIENT_ID": GUID,
        "AZURE_CLIENT_SECRET": "abc~DefGhi.Jkl-MnoPqrStuVwx0123456789",
        "MS_MAILBOX": "support@contoso.com",
        "MS_SITE_URL": "https://contoso.sharepoint.com/sites/sales",
        "TEAMS_WEBHOOK_URL": (
            "https://default1.ab.environment.api.powerplatform.com:443/powerautomate/automations/"
            "direct/workflows/x/triggers/manual/paths/invoke?sig=abc"
        ),
    }
    result = check_values(ctx_of(**{**base, **values}))
    assert result.status == status, result.said
    assert result.code == code
    assert "abc~Def" not in json.dumps(result.to_dict())  # never a secret value


def test_an_unset_microsoft_block_is_skipped_not_failed() -> None:
    assert check_values(ctx_of()).code == "ms.not_configured"


def test_the_current_workflows_host_is_accepted() -> None:
    url = (
        "https://default1.ab.environment.api.powerplatform.com:443/powerautomate/automations/"
        "direct/workflows/x/triggers/manual/paths/invoke?api-version=1&sig=abc"
    )
    assert webhook_problem(url) is None


@pytest.mark.slow
def test_a_correct_setup_is_green_and_fast(tmp_path: Path) -> None:
    started = time.perf_counter()
    with microsoft_world() as world:
        report = world.check(tmp_path)
    seconds = time.perf_counter() - started
    failed = [(r.check.id, r.result.said) for r in report.runs if r.result.status == "fail"]
    assert not failed
    assert report.exit_code == EXIT_OK
    assert seconds < 30
    statuses = {r.check.id: r.result.status for r in report.runs}
    for check in (
        "microsoft.values",
        "microsoft.sign_in",
        "microsoft.outlook",
        "microsoft.outlook_scope",
        "microsoft.sharepoint",
        "microsoft.sharepoint_scope",
        "microsoft.excel",
        "microsoft.teams",
        "microsoft.people",
    ):
        assert statuses[check] == "ok", check
    assert statuses["microsoft.teams_card"] == "skip"


def test_the_negative_tests_prove_the_scopes(tmp_path: Path) -> None:
    with microsoft_world() as world:
        report = world.check(tmp_path)
    assert "refused for ceo@" in report.result("microsoft.outlook_scope").said
    assert "root site" in report.result("microsoft.sharepoint_scope").said
    assert "403" in report.result("microsoft.sharepoint_scope").said


def test_a_workbook_above_the_limit_is_named_with_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import awa.checks.microsoft

    monkeypatch.setattr(awa.checks.microsoft, "_MAX_WORKBOOK", 100)
    with microsoft_world() as world:
        report = world.check(tmp_path)

    excel = report.result("microsoft.excel")
    assert excel.status == "fail" and excel.code == "ms.workbook_too_large"
    assert "MS_CHECK_WORKBOOK" in (excel.fix or "")


def test_tenant_wide_mail_permission_is_a_warning_at_sign_in(tmp_path: Path) -> None:
    with microsoft_world("rbac-not-scoped") as world:
        report = world.check(tmp_path)
    sign_in = report.result("microsoft.sign_in")
    assert sign_in.status == "warn" and sign_in.code == "ms.mail_tenant_wide"
    assert "Mail.ReadWrite" in sign_in.said
    assert sign_in.admin and "New-ManagementRoleAssignment" in sign_in.admin


@pytest.mark.slow
@pytest.mark.parametrize("scenario", sorted(EXPECTED))
def test_every_scenario_turns_its_line_red_with_its_fix(scenario: str, tmp_path: Path) -> None:
    check_id, code = EXPECTED[scenario]
    with microsoft_world(scenario) as world:
        report = world.check(tmp_path)
    result = report.result(check_id)
    assert result.status == "fail", result.said
    assert result.code == code
    assert result.fix
    assert report.exit_code == EXIT_FAILED


def test_site_not_granted_carries_the_grant_for_the_admin(tmp_path: Path) -> None:
    with microsoft_world("site-not-granted") as world:
        report = world.check(tmp_path)
    admin = report.result("microsoft.sharepoint").admin
    assert admin and "/permissions" in admin and '"roles": ["write"]' in admin
    assert report.result("microsoft.excel").status == "skip"
    assert "Send this" not in (report.admin_message() or "")


def test_one_test_card_is_posted_only_with_allow_post(tmp_path: Path) -> None:
    from awa.connectors._saas_microsoft_teams import _SPACE

    with microsoft_world() as world:
        _SPACE._next.clear()
        report = world.check(tmp_path, allow_post=frozenset({"teams"}))
        assert report.result("microsoft.teams_card").status == "ok"
        posted = world.graph.calls("POST", "/workflows/*")
        assert len(posted) == 1
        assert "you can ignore this" in posted[0].body.decode()


# --------------------------------------------------------------------------- the command line


@pytest.fixture
def no_microsoft_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from awa.checks.microsoft import NAMES

    for name in (*NAMES, "AZURE_OPENAI_ENDPOINT", "OPENAI_API_VERSION", "AWA_CHECK_ALLOW_POST"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("no_microsoft_environment")
def test_cli_a_red_line_shows_the_fix_and_saves_a_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".env").write_text(
        "AZURE_TENANT_ID=contoso.onmicrosoft.com\nAZURE_CLIENT_ID=my-app\n"
        "AZURE_CLIENT_SECRET=abc~DefGhi.Jkl-MnoPqrStuVwx0123456789\n",
        encoding="utf-8",
    )
    code = main(["check", "microsoft", "--no-model", "--project", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == EXIT_FAILED
    assert out.splitlines()[0] == "awa check: live, read-only calls"
    assert "FAIL microsoft.values" in out and "fix:" in out
    assert "abc~Def" not in out
    [saved] = (tmp_path / ".awa" / "checks").glob("*.json")
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["exit_code"] == EXIT_FAILED
    assert "abc~Def" not in saved.read_text(encoding="utf-8")


def test_cli_wrong_input_exits_with_2(tmp_path: Path) -> None:
    assert main(["check", "galaxy", "--project", str(tmp_path)]) == EXIT_CONFIGURATION


@pytest.mark.usefixtures("no_microsoft_environment")
def test_cli_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--json", "--no-model", "--project", str(tmp_path)]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["fail"] == 0 and data["saved"]


def test_doctor_points_at_awa_check() -> None:
    from awa.cli._doctor import LIVE_POINTER, DoctorReport

    assert "awa check" in LIVE_POINTER
    assert DoctorReport(checks=()).render().endswith(LIVE_POINTER)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://res.openai.azure.com/",
        "https://res.services.ai.azure.com/",
        "https://res.cognitiveservices.azure.com/",
    ],
)
def test_every_azure_resource_endpoint_form_reaches_the_v1_route(endpoint: str) -> None:
    from awa.checks.microsoft import check_azure_openai

    ctx = CheckContext(lambda name: {"AZURE_OPENAI_ENDPOINT": endpoint}.get(name), no_model=True)
    result = check_azure_openai(ctx)
    assert result.status == "ok", result
    assert "/openai/v1/" in result.data["route"]


@pytest.mark.parametrize("endpoint", ["http://res.openai.azure.com/", "https://example.com/"])
def test_an_address_that_is_no_azure_resource_fails(endpoint: str) -> None:
    from awa.checks.microsoft import check_azure_openai

    ctx = CheckContext(lambda name: {"AZURE_OPENAI_ENDPOINT": endpoint}.get(name), no_model=True)
    assert check_azure_openai(ctx).status == "fail"
