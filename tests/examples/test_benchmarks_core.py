"""Offline tests of the core benchmark in `examples/benchmarks/core/`.

Every scenario runs with scripted models and a fake network, so the benchmark never rots: the
blueprints validate, the sample data generators are deterministic and the files read with the
toolkits, the tools work, the cases parse and their expected numbers follow from the data, every
scenario passes end to end, a wrong answer fails its scorer, and the runner writes its results and
stops at the call cap.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from awa.blueprints import Blueprint
from awa.cli import main as awa_main
from awa.evals import Dataset, parse_scorer
from awa.toolkits import data_toolkit, document_toolkit

ROOT = Path(__file__).resolve().parents[2] / "examples" / "benchmarks" / "core"
for folder in (ROOT, ROOT.parent.parent):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import data_gen  # noqa: E402
import harness  # noqa: E402
import mcp_client  # noqa: E402
import offline  # noqa: E402
import scorers  # noqa: E402
from scorers import CHECKS  # noqa: E402

SCENARIOS = {s.slug: s for s in harness.SCENARIOS}
BLUEPRINTS = [s.slug for s in harness.SCENARIOS if s.kind not in ("build", "mcp")]


def load_run() -> ModuleType:
    spec = importlib.util.spec_from_file_location("awa_bench_run", ROOT / "run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tools_of(slug: str) -> ModuleType:
    module = SCENARIOS[slug].tools_module()
    assert module is not None
    return module


# ------------------------------------------------------------------------ structure


def test_every_scenario_folder_is_registered_and_documented() -> None:
    folders = sorted(p.name for p in ROOT.iterdir() if p.is_dir() and p.name[:2].isdigit())
    assert folders == sorted(SCENARIOS)
    assert [s.number for s in harness.SCENARIOS] == list(range(1, 9))
    for slug in folders:
        assert (ROOT / slug / "cases.yaml").is_file() and (ROOT / slug / "README.md").is_file()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert all(f"{slug}/README.md" in readme for slug in folders)


@pytest.mark.parametrize("slug", BLUEPRINTS)
def test_blueprint_is_valid_without_warnings_and_its_readme_shows_the_diagram(
    slug: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BENCH_WEBHOOK_TOKEN", harness.TOKEN)
    scenario = SCENARIOS[slug]
    module = scenario.tools_module()
    tools = [getattr(module, name) for name in module.__all__] if module else None
    blueprint = scenario.blueprint
    assert blueprint.check(tools=tools) == []
    assert blueprint.to_mermaid() in (scenario.directory / "README.md").read_text(encoding="utf-8")
    for tool in tools or []:
        assert (tool.__doc__ or "").strip(), f"{tool.__name__} needs a docstring"


@pytest.mark.parametrize("slug", [s for s in BLUEPRINTS if (ROOT / s / "tools.py").is_file()])
def test_cli_validates_the_blueprint_with_its_tools(
    slug: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "tools", None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("BENCH_WEBHOOK_TOKEN", harness.TOKEN)
    folder = ROOT / slug
    assert (
        awa_main(["validate", str(folder / "blueprint.yaml"), "--tools", str(folder / "tools.py")])
        == 0
    )
    assert capsys.readouterr().out.strip().endswith("valid")


@pytest.mark.parametrize("scenario", harness.SCENARIOS, ids=lambda s: s.slug)
def test_cases_parse_and_their_scorers_exist(scenario: harness.Scenario) -> None:
    dataset = scenario.dataset
    assert dataset.cases
    for case in dataset.cases:
        assert case.scorers
        if scenario.kind == "eval":
            for spec in case.scorers:
                parse_scorer(spec)
            assert set(case.metadata.get("checks", ())) <= set(CHECKS)
        else:
            expected = {
                "automation": harness.AUTOMATION_CHECKS,
                "build": harness.BUILD_CHECKS,
                "mcp": mcp_client.CHECKS,
            }[scenario.kind]
            assert set(case.scorers) == set(expected)
    if scenario.kind == "eval":
        assert (
            any(spec.startswith("judge:") for case in dataset.cases for spec in case.scorers)
            or scenario.number == 2
        )


def test_judges_come_last_in_every_case() -> None:
    for scenario in harness.SCENARIOS:
        for case in scenario.dataset.cases:
            specs = list(case.scorers)
            judges = [i for i, spec in enumerate(specs) if spec.startswith("judge:")]
            assert judges == list(range(len(specs) - len(judges), len(specs))), case.id


def test_the_orders_answer_carries_what_its_judge_grades() -> None:
    """The judge sees only the structured answer: each flag needs its reason and proposed day."""

    agent = SCENARIOS["02_supplier_orders"].blueprint.agents[0]
    schema = agent.output_schema
    item = schema["properties"]["flags"]["items"]
    assert "flags" in schema["required"]
    assert set(item["required"]) == {"order_no", "reason", "proposed_date"}


# ------------------------------------------------------------------------ sample data


def test_data_generators_are_deterministic(tmp_path: Path) -> None:
    first = {
        p.relative_to(tmp_path / "a"): p.read_bytes() for p in data_gen.generate(tmp_path / "a")
    }
    second = {
        p.relative_to(tmp_path / "b"): p.read_bytes() for p in data_gen.generate(tmp_path / "b")
    }
    assert first == second and len(first) == len(data_gen.FILES)
    assert data_gen.differing(tmp_path / "a") == []
    (tmp_path / "a" / "05_agent_network" / "data" / "spend.csv").write_text("changed")
    assert data_gen.differing(tmp_path / "a") == ["05_agent_network/data/spend.csv"]


def test_text_data_compares_by_its_lines_not_its_line_endings(tmp_path: Path) -> None:
    """A checkout with `core.autocrlf` turns committed text files into CRLF: same data."""

    data_gen.generate(tmp_path)
    for name in data_gen.FILES:
        if name.endswith((".csv", ".json")):
            path = tmp_path / name
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert data_gen.differing(tmp_path) == []
    (tmp_path / "05_agent_network" / "data" / "spend.csv").write_text("changed")
    assert data_gen.differing(tmp_path) == ["05_agent_network/data/spend.csv"]


def test_committed_data_matches_the_generators() -> None:
    plain = [n for n in data_gen.FILES if n.endswith((".csv", ".json"))]
    assert data_gen.differing(ROOT) == []
    assert sorted(set(data_gen.FILES) - set(plain)) == [
        "04_document_intake/data/inbox/2024-0412_mueller.pdf",
        "04_document_intake/data/inbox/88231_zuercher.docx",
        "04_document_intake/data/inbox/ln-2024-17_meyer.xlsx",
        "04_document_intake/data/inbox/scan_0001.pdf",
    ]
    for name in set(data_gen.FILES) - set(plain):
        assert (ROOT / name).is_file()


def call(tool: Any, **arguments: Any) -> Any:
    from offline import tool_data

    return tool_data(tool.invoke(arguments))


def test_the_german_csv_files_are_windows_1252_and_read_with_their_delimiter() -> None:
    raw = (ROOT / "02_supplier_orders" / "data" / "orders.csv").read_bytes()
    assert "Müller & Söhne".encode("cp1252") in raw
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    tools = {t.name: t for t in data_toolkit(ROOT / "02_supplier_orders" / "data")}
    table = call(tools["read_csv"], path="orders.csv")
    assert (table["encoding"], table["delimiter"]) == ("cp1252", ";")
    assert table["columns"][:3] == ["Bestellnr", "Lieferant", "Land"]
    assert table["rows"][1][:2] == ["B-1002", "Müller & Söhne KG"]


def test_the_workbook_has_real_dates_and_formulas_with_their_results() -> None:
    tools = {t.name: t for t in data_toolkit(ROOT / "04_document_intake" / "data")}
    path = "inbox/ln-2024-17_meyer.xlsx"
    head = call(tools["read_excel"], path=path, sheet="Kopf", header_row=0)
    assert ["Rechnungsdatum", "2024-10-01"] in head["rows"]
    work = call(tools["read_excel"], path=path, sheet="Leistungen")
    assert work["rows"][0][0] == "2024-09-02" and work["rows"][0][4] == 640
    assert work["rows"][-3:] == [
        ["Netto", None, None, None, 4800],
        ["USt 16 %", None, None, None, 768],
        ["Brutto", None, None, None, 5568],
    ]
    import openpyxl

    formulas = openpyxl.load_workbook(ROOT / "04_document_intake" / "data" / path)["Leistungen"]
    assert formulas["E2"].value == "=C2*D2" and formulas["E10"].value == "=SUM(E2:E9)"


def test_the_pdf_word_and_scanned_files_read_as_expected() -> None:
    tools = {t.name: t for t in document_toolkit(ROOT / "04_document_intake" / "data")}
    text = str(call(tools["extract_pdf_text"], path="inbox/2024-0412_mueller.pdf"))
    assert "Müller & Söhne KG" in text and "Brutto 1.428,00 EUR" in text
    assert "no text layer" in str(call(tools["extract_pdf_text"], path="inbox/scan_0001.pdf"))
    word = str(call(tools["extract_docx_text"], path="inbox/88231_zuercher.docx"))
    assert (
        "Hydraulikpumpe HP-40 | 4 | 4.500,00 | 18.000,00" in word and "Brutto 21.420,00 EUR" in word
    )


# ------------------------------------------------------------------------ tools


def test_delivery_dates_follow_weekends_and_holidays() -> None:
    check = tools_of("02_supplier_orders").check_delivery_date
    assert check("2024-11-11", ["2024-11-11"])["next_business_day"] == "2024-11-12"
    saturday = check("2024-11-16", [])
    assert (saturday["weekday"], saturday["is_weekend"], saturday["next_business_day"]) == (
        "Saturday",
        True,
        "2024-11-18",
    )
    assert check("2024-10-04", ["2024-10-03"])["ok"] is True
    bridge = check("2024-12-24", ["2024-12-24", "2024-12-25", "2024-12-26"])
    assert bridge["next_business_day"] == "2024-12-27" and bridge["is_holiday"] is True


def test_reschedule_requests_are_recorded_and_reset() -> None:
    tools = tools_of("02_supplier_orders")
    tools.reset_sample_data()
    assert "now for 2024-11-12" in tools.request_reschedule("B-1001", "2024-11-12", "holiday")
    assert tools.REQUESTS == [{"order_no": "B-1001", "new_date": "2024-11-12", "reason": "holiday"}]
    tools.reset_sample_data()
    assert tools.REQUESTS == []


GOOD = {
    "vendor_vat_id": "DE811111111",
    "iban": "DE02 1001 0010 0000 1234 56",
    "po_number": "PO-4500",
    "net_amount": 1200.0,
    "tax_amount": 228.0,
    "gross_amount": 1428.0,
    "invoice_date": "2024-09-20",
    "due_date": "2024-10-20",
}


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({}, None),
        ({"vendor_vat_id": "DE000000000"}, "V1"),
        ({"vendor_vat_id": "DE833333333", "po_number": "PO-4500"}, "V1"),
        ({"iban": "DE99100100100000999999"}, "V2"),
        ({"po_number": "PO-9999"}, "V3"),
        ({"po_number": "PO-4501"}, "V3"),
        ({"net_amount": 1300.0, "tax_amount": 247.0, "gross_amount": 1547.0}, "V3"),
        ({"tax_amount": 200.0, "gross_amount": 1400.0}, "V4"),
        ({"gross_amount": 1430.0}, "V4"),
        ({"due_date": "2024-09-20"}, "V5"),
    ],
)
def test_validation_rules(change: dict[str, Any], rule: str | None) -> None:
    verdict = tools_of("04_document_intake").validate_invoice(**{**GOOD, **change})
    assert [v["rule"] for v in verdict["violations"]][:1] == ([rule] if rule else [])
    assert verdict["valid"] is (rule is None)


def test_the_order_limit_of_a_vendor_is_checked_and_invoices_are_booked_and_held() -> None:
    tools = tools_of("04_document_intake")
    limit = tools.validate_invoice(
        **{
            **GOOD,
            "vendor_vat_id": "DE844444444",
            "iban": "DE75512108001245126199",
            "po_number": "PO-4502",
            "net_amount": 4800.0,
            "tax_amount": 912.0,
            "gross_amount": 5712.0,
        }
    )
    assert limit["valid"] and limit["vendor_id"] == "V-400"
    tools.reset_sample_data()
    assert tools.book_invoice(
        "V-100", "2024-0412", "PO-4500", 1200.0, 228.0, 1428.0, "2024-10-20"
    ) == ("Booked invoice 2024-0412 as FI-2024-0001.")
    assert (
        tools.hold_invoice("inbox/x.pdf", None, ["V4: wrong tax"])
        == "Put inbox/x.pdf on hold as HOLD-001."
    )
    assert [e["reference"] for e in tools.LEDGER] == ["FI-2024-0001"] and tools.HOLDS[0][
        "invoice_number"
    ] is None


def test_the_ledger_desk_books_once_and_summarises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = tools_of("06_automation")
    monkeypatch.setattr(tools, "LEDGER", tmp_path / "ledger.csv")
    assert tools.ledger_summary() == "0 invoices booked, 0.00 EUR in total."
    assert "entry 1" in tools.book_invoice("RE-1", "Acme", 200)
    assert "already booked" in tools.book_invoice("RE-1", "Acme", 200)
    tools.book_invoice("RE-2", "Beta GmbH", 80.5)
    assert tools.ledger_summary() == "2 invoices booked, 280.50 EUR in total."
    with (tmp_path / "ledger.csv").open(encoding="utf-8", newline="") as ledger:
        assert [row["amount"] for row in csv.DictReader(ledger)] == ["200.00", "80.50"]


# ------------------------------------------------------------------------ expected values follow from the data


def german(text: str) -> float:
    return float(text.replace(".", "").replace(",", "."))


def test_expected_numbers_of_the_orders_follow_from_the_data() -> None:
    from datetime import date

    rows = list(
        csv.DictReader(
            (ROOT / "02_supplier_orders/data/orders.csv").open(encoding="cp1252"), delimiter=";"
        )
    )
    open_rows = [r for r in rows if r["Status"] != "storniert"]
    national = {c: {d for d, _, flag in days if flag} for c, days in offline._HOLIDAYS.items()}
    flagged = []
    for r in open_rows:
        day, month, year = (int(x) for x in r["Wunschliefertermin"].split("."))
        when = date(year, month, day)
        if when.weekday() >= 5 or when.isoformat() in national[r["Land"]]:
            flagged.append(r)
    cases = {c.id: c.expected for c in SCENARIOS["02_supplier_orders"].dataset.cases}
    assert cases["all-open-orders"]["checked_orders"] == len(open_rows)
    assert cases["all-open-orders"]["flagged_orders"] == [r["Bestellnr"] for r in flagged]
    assert cases["all-open-orders"]["flagged_total_eur"] == round(
        sum(german(r["Betrag_EUR"]) for r in flagged), 2
    )
    kowalski = [r for r in flagged if "Kowalski" in r["Lieferant"]]
    assert cases["one-supplier-rejected"]["flagged_total_eur"] == sum(
        german(r["Betrag_EUR"]) for r in kowalski
    )
    assert cases["regional-holiday-is-no-problem"]["flagged_orders"] == []


def test_expected_answers_of_the_graphql_lookup_follow_from_the_data() -> None:
    suppliers = json.loads((ROOT / "03_graphql_lookup/data/suppliers.json").read_text("utf-8"))[
        "suppliers"
    ]
    countries = offline._COUNTRIES
    euro = [s for s in suppliers if countries[s["country"]]["currency"] == "EUR"]
    outside = [s for s in suppliers if countries[s["country"]]["continent"]["code"] != "EU"]
    cases = {c.id: c.expected for c in SCENARIOS["03_graphql_lookup"].dataset.cases}
    assert cases["euro-suppliers"] == {
        "supplier_ids": [s["id"] for s in euro],
        "count": len(euro),
        "total_annual_spend_eur": sum(s["annual_spend_eur"] for s in euro),
    }
    assert cases["suppliers-outside-europe"]["total_annual_spend_eur"] == sum(
        s["annual_spend_eur"] for s in outside
    )
    assert cases["capital-and-currency"]["capital"] == countries["CH"]["capital"]


def test_expected_values_of_the_network_and_the_documents_follow_from_the_data() -> None:
    spend: dict[str, float] = {}
    for r in csv.DictReader((ROOT / "05_agent_network/data/spend.csv").open(encoding="utf-8")):
        spend[r["supplier"]] = spend.get(r["supplier"], 0) + float(r["amount_eur"])
    top = max(spend, key=lambda name: spend[name])
    late: dict[str, list[int]] = {}
    for d in json.loads(
        (ROOT / "05_agent_network/data/deliveries.json").read_text(encoding="utf-8")
    )["deliveries"]:
        late.setdefault(d["supplier"], []).append(d["days_late"])
    slowest = max(late, key=lambda name: sum(late[name]) / len(late[name]))
    (case,) = SCENARIOS["05_agent_network"].dataset.cases
    assert (top, f"{spend[top]:.2f}") == ("Nordwind GmbH", "48230.50")
    assert (slowest, f"{sum(late[slowest]) / len(late[slowest]):.2f}") == ("Alpha Logistik", "6.33")
    assert f"regex:{top}" in case.scorers and f"regex:{slowest}" in case.scorers
    documents = {c.id: c.expected for c in SCENARIOS["04_document_intake"].dataset.cases}
    assert documents["clean-pdf-booked"]["gross_amount"] == german("1.428,00")
    assert (
        documents["large-word-invoice-approved"]["gross_amount"]
        == german("21.420,00")
        == 18000 * 1.19
    )
    assert documents["excel-wrong-vat-held"]["gross_amount"] == 4800 + round(4800 * 0.16, 2)


# ------------------------------------------------------------------------ scenarios end to end


@pytest.mark.parametrize("slug", [s.slug for s in harness.SCENARIOS if s.kind in ("eval", "build")])
async def test_scenario_passes_offline_in_every_run(slug: str) -> None:
    scenario = SCENARIOS[slug]
    ledger = harness.Ledger(10_000)
    records, capped = await harness.run_scenario(scenario, 2, harness.Setup(offline=True), ledger)
    assert not capped and len(records) == 2 * len(scenario.dataset.cases)
    failed = [(r.case, r.error, r.notes) for r in records if not r.passed]
    assert failed == []
    assert all(r.calls > 0 and r.tokens > 0 and r.latency_ms >= 0 for r in records)
    assert ledger.total == sum(r.calls for r in records)


async def test_the_automation_scenario_plays_its_day_offline() -> None:
    scenario = SCENARIOS["06_automation"]
    records, _ = await harness.run_scenario(
        scenario,
        1,
        harness.Setup(offline=True),
        harness.Ledger(1000),
        cases=["large-invoice-approved", "large-invoice-rejected"],
    )
    assert len(records) == 2  # the second case decides in its own folder, not the first one's
    for record in records:
        assert record.passed, (record.case, record.error, record.notes)
        assert set(record.scores) == set(harness.AUTOMATION_CHECKS) and record.calls >= 6


async def test_an_mcp_client_calls_the_served_blueprint_offline_in_every_run() -> None:
    """Real `awa mcp serve` processes (stdio and HTTP), the real client, scripted model."""

    scenario = SCENARIOS["08_mcp_client"]
    records, capped = await harness.run_scenario(
        scenario, 1, harness.Setup(offline=True), harness.Ledger(10_000)
    )
    assert not capped and len(records) == len(scenario.dataset.cases) == 4
    assert [(r.case, r.error, r.notes) for r in records if not r.passed] == []
    transports = {c.id: c.metadata["transport"] for c in scenario.dataset.cases}
    assert set(transports.values()) == {"stdio", "http"}
    assert {c.metadata["answer"] for c in scenario.dataset.cases} == {"elicitation", "resume_tool"}
    assert all(r.calls >= 5 and r.tokens > 0 for r in records)  # the whole run, not only its end


def test_the_mcp_driver_scores_a_wrong_answer_and_the_server_protocol_revision() -> None:
    wanted = {"flagged_orders": ["B-1001"], "flagged_total_eur": 10.0}
    assert mcp_client.mismatches({**wanted, "extra": 1}, wanted) == []
    assert mcp_client.mismatches('{"flagged_orders": ["B-1001"]}', wanted) == [
        "flagged_total_eur=null"
    ]
    assert mcp_client.mismatches("not json", wanted) == ["the output is not a JSON object"]
    assert mcp_client.PROTOCOL_REVISION == "2025-11-25"


async def test_a_quote_that_is_not_on_a_fetched_page_fails_the_grounding_scorer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finish = offline.Script.finish

    def invent(self: offline.Script, agent: str, data: dict[str, Any]) -> Any:
        if agent == "brief_lead":
            data = {
                **data,
                "items": [{**i, "evidence": "A sentence nobody fetched."} for i in data["items"]],
            }
        return finish(self, agent, data)

    monkeypatch.setattr(offline.Script, "finish", invent)
    scenario = SCENARIOS["01_market_brief"]
    record = await harness.run_eval(scenario, scenario.dataset.cases[0], 1, harness.Setup(True), 60)
    assert record.scores["quotes_grounded"] is False and not record.passed
    assert "0 of 3 quotes" in record.notes["quotes_grounded"]
    assert record.scores["json_fields"] is True


async def test_a_fetched_quote_without_the_year_fails_the_grounding_scorer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finish = offline.Script.finish

    def undated(self: offline.Script, agent: str, data: dict[str, Any]) -> Any:
        if agent == "brief_lead":
            data = {
                **data,
                "items": [{**i, "evidence": i["evidence"].split(" on ")[0]} for i in data["items"]],
            }
        return finish(self, agent, data)

    monkeypatch.setattr(offline.Script, "finish", undated)
    scenario = SCENARIOS["01_market_brief"]
    record = await harness.run_eval(scenario, scenario.dataset.cases[0], 1, harness.Setup(True), 60)
    assert record.scores["quotes_grounded"] is False and not record.passed
    assert (
        "0 of 3 quotes are on fetched pages and state the year" in record.notes["quotes_grounded"]
    )


def test_a_quote_is_grounded_although_the_page_carries_footnote_markers_inside_it() -> None:
    page = (
        "Go was publicly announced in November 2009,[30] and version 1.0 was released.[31][32] Go"
    )
    quote = "Go was publicly announced in November 2009, and version 1.0 was released."
    assert scorers._plain(quote) in scorers._plain(page)
    assert scorers._plain("Go was announced in 2010") not in scorers._plain(page)


async def test_the_lead_sends_an_undated_quote_back_to_the_researcher_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_back: list[str] = []

    class SendBack(offline.BriefScript):
        def brief_lead(self, call: Any) -> Any:
            tasks = [a["task"] for a in offline.called(call, "delegate_task")]
            rows = [
                r.split(" | ")
                for r in offline.delegated_results(call).get("researcher", "").splitlines()
            ]
            if len(tasks) == 1 and any(len(r) == 5 and r[2] not in r[4] for r in rows):
                sent_back.append(tasks[0])
                return offline.tool_call(
                    "delegate_task",
                    {"agent": "researcher", "task": "Quote with the year. " + tasks[0]},
                )
            return super().brief_lead(call)

        def researcher(self, call: Any) -> Any:
            answer = super().researcher(call)
            if not str(answer).startswith("- ") or "Quote with the year" in offline.request(call):
                return answer
            head, _, quote = str(answer).rpartition(" | ")  # the last item loses its year
            return f'{head} | "{quote.strip(chr(34)).split(" on ")[0]}"'

    monkeypatch.setitem(offline.SCRIPTS, "01_market_brief", SendBack)
    scenario = SCENARIOS["01_market_brief"]
    record = await harness.run_eval(scenario, scenario.dataset.cases[0], 1, harness.Setup(True), 60)
    assert len(sent_back) == 1
    assert record.passed, (record.error, record.notes)  # within max_calls_per_member: 3
    lead = scenario.blueprint.agent("brief_lead").instructions
    assert "once more" in lead and "never again" in lead


async def test_a_run_is_stopped_at_its_call_limit() -> None:
    scenario = SCENARIOS["04_document_intake"]
    record = await harness.run_eval(scenario, scenario.dataset.cases[0], 1, harness.Setup(True), 2)
    assert not record.passed and record.calls <= 3


# ------------------------------------------------------------------------ runner


def test_offline_runner_writes_results_and_a_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = load_run().main(["--offline", "--runs", "1", "--only", "2", "3", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    [result] = tmp_path.glob("core-offline-*.json")
    data = json.loads(result.read_text("utf-8"))
    assert (data["mode"], data["runs"], data["stopped_at_max_calls"]) == ("offline", 1, False)
    assert [s["scenario"] for s in data["scenarios"]] == [2, 3]
    case = data["scenarios"][0]["detail"][0]
    assert {
        "case",
        "runs",
        "passed_runs",
        "pass_pow_k",
        "scorers",
        "calls",
        "tokens",
        "latency_ms",
    } <= set(case)
    assert (
        case["scorers"]["json_fields"] == {"passed": 1, "total": 1}
        and case["latency_ms"]["p95"] >= 0
    )
    assert result.with_suffix(".md").read_text("utf-8").strip() in out
    assert "| Scenario / case |" in out and "all-open-orders" in out
    assert not (tmp_path / "ledger.jsonl").exists()  # offline runs keep no ledger file


def test_the_runner_stops_at_max_calls(tmp_path: Path) -> None:
    code = load_run().main(
        ["--offline", "--runs", "3", "--only", "4", "--max-calls", "9", "--out", str(tmp_path)]
    )
    assert code == 2
    [result] = tmp_path.glob("core-offline-*.json")
    data = json.loads(result.read_text("utf-8"))
    assert data["stopped_at_max_calls"] is True and 9 <= data["calls"] < 9 + 8
    assert sum(c["runs"] for c in data["scenarios"][0]["detail"]) < 15


def test_the_runner_filters_cases_and_rejects_unknown_scenarios(tmp_path: Path) -> None:
    run = load_run()
    assert (
        run.main(
            [
                "--offline",
                "--runs",
                "1",
                "--only",
                "4",
                "--cases",
                "scan-held-for-manual-entry",
                "--out",
                str(tmp_path),
            ]
        )
        == 0
    )
    [result] = tmp_path.glob("core-offline-*.json")
    assert [c["case"] for c in json.loads(result.read_text("utf-8"))["scenarios"][0]["detail"]] == [
        "scan-held-for-manual-entry"
    ]
    with pytest.raises(SystemExit, match="unknown scenario"):
        run.main(["--offline", "--only", "9", "--out", str(tmp_path)])
    with pytest.raises(SystemExit):
        run.main(["--runs", "1"])  # --offline or --live is required


@pytest.mark.parametrize(
    ("answer", "ledger", "named"),
    [
        ("3 invoices were booked that week, totaling 1,530.00 EUR.", ["a,b,200.00"] * 3, False),
        (
            "3 invoices were booked that week, totaling 1,530.00 EUR.",
            ["a,A,280.00", "b,B,1250"],
            False,
        ),
        (
            "3 invoices were booked that week, totaling 1,530.00 EUR.",
            ["R,A,200.00", "S,A,1250.00", "T,B,80.00"],
            True,
        ),
        ("Two invoices booked: 2 invoices, total 280 EUR.", ["R,A,200.00", "T,B,80.00"], True),
        ("12 invoices totalling 1530.00 EUR", ["R,A,200.00", "S,A,1250.00", "T,B,80.00"], False),
    ],
)
def test_the_weekly_summary_is_scored_on_count_and_total_not_on_wording(
    answer: str, ledger: list[str], named: bool
) -> None:
    """A summary counts when it names the count and the total, whatever the wording ("3 invoices
    were booked that week")."""

    assert harness.summary_names(answer, ledger) is named


def test_the_ledger_counts_across_invocations(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    first = harness.Ledger(100, path)
    first.add(harness.RunRecord(1, "case", 1, calls=30, tokens=10))
    second = harness.Ledger(100, path)
    assert (second.total, second.remaining) == (30, 70)
    second.add(harness.RunRecord(1, "case", 2, calls=80))
    assert second.remaining == 0 and len(path.read_text("utf-8").splitlines()) == 2


def test_statistics_and_the_live_setup() -> None:
    assert [harness.percentile([5, 1, 3, 2, 4], f) for f in (0.5, 0.95, 1.0)] == [3, 5, 5]
    assert harness.percentile([], 0.5) == 0
    records = [
        harness.RunRecord(2, "a", 1, scores={"x": True, "y": True}, latency_ms=100),
        harness.RunRecord(
            2, "a", 2, scores={"x": True, "y": False}, notes={"y": "off"}, latency_ms=300
        ),
    ]
    (case,) = harness.summarize(records)["cases"]
    assert (case["passed_runs"], case["pass_pow_k"], case["scorers"]["y"]) == (
        1,
        False,
        {"passed": 1, "total": 2},
    )
    assert case["latency_ms"] == {"p50": 200, "p95": 300} and case["failures"] == ["y: off"]
    assert harness.Setup(offline=False).models(SCENARIOS["02_supplier_orders"]) == (None, None)


def test_the_blueprint_of_a_scenario_builds_with_the_scripted_models() -> None:
    for slug in BLUEPRINTS:
        blueprint: Blueprint = SCENARIOS[slug].blueprint
        script = offline.script_for(slug, blueprint)
        assert set(script.markers) == {a.name for a in blueprint.agents}
        assert {name for name in script.markers if not hasattr(script, name)} == set()
    assert Dataset.from_yaml(SCENARIOS["07_build_from_description"].directory / "cases.yaml").cases


def test_the_offline_network_takes_every_argument_of_the_real_http_client() -> None:
    # The engine passes `follow_redirects` to `fetch`; a double that lacks an argument turns
    # every connector call of the offline scenarios into a TypeError.
    import inspect

    from awa._internal.http import fetch as real_fetch

    with offline.network():
        import awa.connectors._engine as engine

        fake = engine.fetch
        assert fake is not real_fetch
        real = inspect.signature(real_fetch).parameters
        double = inspect.signature(fake).parameters
        assert set(real) <= set(double), sorted(set(real) - set(double))


def test_a_module_imported_inside_the_offline_network_gets_the_real_client_back() -> None:
    import awa._internal.http as http

    real = http.fetch
    late = ModuleType("awa_late_import")
    with offline.network():
        exec("from awa._internal.http import fetch", late.__dict__)
        sys.modules[late.__name__] = late
    try:
        assert late.fetch is real and http.fetch is real
    finally:
        del sys.modules[late.__name__]


async def test_an_automation_run_above_its_call_limit_fails() -> None:
    scenario = SCENARIOS["06_automation"]
    record = await harness.run_automation(
        scenario, scenario.dataset.cases[0], 1, harness.Setup(True), 1
    )
    assert not record.passed and "above the cap of 1" in (record.error or "")
