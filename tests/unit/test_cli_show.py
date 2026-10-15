"""`awa validate` and `awa show` on a process blueprint, and `awa show cases`."""

from __future__ import annotations

from pathlib import Path

import pytest

from awa.blueprints import Blueprint
from awa.blueprints._diagram import blueprint_diagram, blueprint_to_mermaid
from awa.cli import main

TEXT = """
name: invoice_check
description: Checks an invoice.
limits: {max_requests: 20}
agents:
  - name: extractor
    description: Reads an invoice.
    output_schema: {type: object, properties: {supplier: {type: string}, total: {type: number}}}
  - name: booker
    description: Books it.
    output_schema: {type: object, properties: {total: {type: number}}}
    documents: [price_list]
    tools: [connector:files.read_csv]
connectors: {files: {use: workspace, root: data}}
data:
  - {name: price_list, path: data/files/price_list.csv, role: reference}
process:
  - name: extract
    title: Read the invoice
    agent: extractor
    checks:
      - {kind: schema}
      - {kind: rule, fail_when: "total <= 0", message: The total is zero or less.}
  - name: approve
    title: Decide
    agent: booker
    checks:
      - {kind: approval, when: "total >= 5000", reason: Large invoices need a person., by: manager}
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "data" / "files").mkdir(parents=True)
    (tmp_path / "data" / "files" / "price_list.csv").write_text("Artikel;Preis\r\nA;1\r\n")
    (tmp_path / "blueprint.yaml").write_text(TEXT, encoding="utf8")
    return tmp_path


def test_show_prints_the_steps_agents_documents_and_checks_in_words(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show", str(project)]) == 0

    out = capsys.readouterr().out
    assert "entrypoint: workflow" in out
    assert "process: 2 step(s), each step receives the previous step's result" in out
    assert "  1. extract - Read the invoice" in out
    assert "     agent: extractor" in out
    assert "     document: price_list (reference, data/files/price_list.csv)" in out
    assert "     always checked: documents there and as expected; the agent gave an answer" in out
    assert "     check: the output has the right fields" in out
    assert "     check: fails when total <= 0: The total is zero or less." in out
    assert (
        "     check: a person decides when total >= 5000 (manager): Large invoices need a person."
        in out
    )


def test_show_says_when_a_step_has_only_the_automatic_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "blueprint.yaml").write_text(
        "name: p\nagents: {a: Does a., b: Does b.}\nprocess: [a, b]\n", encoding="utf8"
    )

    assert main(["show", str(tmp_path)]) == 0

    assert capsys.readouterr().out.count("check: (only the automatic checks)") == 2


def test_validate_prints_the_process_issues_with_their_fix_and_the_exit_code(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "blueprint.yaml").write_text(
        TEXT.replace("total <= 0", "totl <= 0"), encoding="utf8"
    )

    assert main(["validate", str(project)]) == 1

    out = capsys.readouterr().out
    assert (
        "error: process.steps[0].checks[1].fail_when: The rule of step 'extract' reads 'totl'"
        in out
    )
    assert "Fields you can use: supplier, text, total." in out


def test_validate_accepts_a_good_process_and_lists_warnings(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["validate", str(project)])

    out = capsys.readouterr().out
    assert code == 0 and "invoice_check: valid" in out
    assert "role 'manager'" not in out  # awa records the role; enforcing it is the caller's


def test_show_mermaid_draws_the_chain_with_titles_agents_and_check_notes(project: Path) -> None:
    bp = Blueprint.from_yaml_file(project / "blueprint.yaml")

    mermaid = blueprint_to_mermaid(bp)

    assert mermaid.startswith("flowchart LR")
    assert "1 Read the invoice<br/>extractor<br/>2 checks" in mermaid
    assert "2 Decide<br/>booker<br/>approval" in mermaid
    assert "input --> s1" in mermaid and "s1 --> s2" in mermaid and "s2 --> output" in mermaid


def test_the_process_diagram_has_one_node_per_step_and_names_a_step_without_checks() -> None:
    bp = Blueprint.from_yaml_text("name: p\nagents: {a: Does a., b: Does b.}\nprocess: [a, b]\n")

    diagram = blueprint_diagram(bp)

    assert [n.id for n in diagram.nodes] == ["input", "output", "s1", "s2"]
    assert diagram.nodes[2].lines == ("1 a", "a", "automatic checks only")


BLUEPRINT = """\
name: invoice_desk
agents:
  clerk: Check incoming invoices.
special_cases:
  decide:
    cases:
      - {if: "supplier in ['Blocked Supplies Ltd']", then: reject, message: Blocked supplier.}
      - {if: [{field: amount_eur, op: ">=", value: 5000}], then: ask, by: finance}
      - if: [{field: amount_eur, op: "<", value: 5000}, {field: supplier, op: one of, value: [Acme Tools]}]
        then: approve
      - {if: [{field: amount_eur, op: ">=", value: 20000}], then: ask, by: board}
    else: approve
    when_unclear: reject
"""

EXPECTED = """\
special cases decide: 4 row(s), first match wins
  1. If supplier is one of "Blocked Supplies Ltd": rejected. Blocked supplier.
  2. If amount is 5,000 EUR or more: a person decides (finance)
  3. If amount is less than 5,000 EUR and supplier is one of "Acme Tools": approved without asking a person
  4. If amount is 20,000 EUR or more: a person decides (board)
  Otherwise: approved without asking a person
  When a value is missing or unclear: rejected
  warning: else is 'approve': every case no row decides is approved without a person; check that the rows catch every case that needs a person, or use else: ask
  note: Row 4 never applies: row 2 already decides every amount it covers.

row  probe                                          input                                             expected
1    supplier in the list                           supplier="Blocked Supplies Ltd", amount_eur=4999  row 1: rejected. Blocked supplier.
1    supplier not in the list                       supplier="other value", amount_eur=4999           else: approved without asking a person
1    supplier "Blocked Supplies Ltd" in other case  supplier="bLOCKED sUPPLIES lTD", amount_eur=4999  else: approved without asking a person
1    supplier missing                               amount_eur=4999                                   unclear at row 1 (supplier has no value): rejected
1    supplier two-way number 5.000                  supplier="5.000", amount_eur=4999                 else: approved without asking a person
2    amount_eur just below 5,000 EUR                supplier="other value", amount_eur=4999           else: approved without asking a person
2    amount_eur exactly 5,000 EUR                   supplier="other value", amount_eur=5000           row 2: a person decides (finance)
2    amount_eur just above 5,000 EUR                supplier="other value", amount_eur=5001           row 2: a person decides (finance)
2    amount_eur missing                             supplier="other value"                            unclear at row 2 (amount_eur has no value): rejected
2    amount_eur two-way number 5.000                supplier="other value", amount_eur="5.000"        unclear at row 2 (the number '5.000' in amount_eur can be read two ways (decimal or thousands)): rejected
3    amount_eur just below 5,000 EUR                supplier="Acme Tools", amount_eur=4999            row 3: approved without asking a person
3    amount_eur exactly 5,000 EUR                   supplier="Acme Tools", amount_eur=5000            row 2: a person decides (finance)
3    amount_eur just above 5,000 EUR                supplier="Acme Tools", amount_eur=5001            row 2: a person decides (finance)
3    supplier not in the list                       supplier="other value", amount_eur=4999           else: approved without asking a person
3    supplier "Acme Tools" in other case            supplier="aCME tOOLS", amount_eur=4999            else: approved without asking a person
3    amount_eur missing                             supplier="Acme Tools"                             unclear at row 2 (amount_eur has no value): rejected
3    amount_eur two-way number 5.000                supplier="Acme Tools", amount_eur="5.000"         unclear at row 2 (the number '5.000' in amount_eur can be read two ways (decimal or thousands)): rejected
3    supplier missing                               amount_eur=4999                                   unclear at row 1 (supplier has no value): rejected
3    supplier two-way number 5.000                  supplier="5.000", amount_eur=4999                 else: approved without asking a person
4    amount_eur just below 20,000 EUR               supplier="other value", amount_eur=19999          row 2: a person decides (finance)
4    amount_eur exactly 20,000 EUR                  supplier="other value", amount_eur=20000          row 2: a person decides (finance)
4    amount_eur just above 20,000 EUR               supplier="other value", amount_eur=20001          row 2: a person decides (finance)
4    amount_eur missing                             supplier="other value"                            unclear at row 2 (amount_eur has no value): rejected
4    amount_eur two-way number 20.000               supplier="other value", amount_eur="20.000"       unclear at row 2 (the number '20.000' in amount_eur can be read two ways (decimal or thousands)): rejected
"""


@pytest.fixture
def blueprint(tmp_path: Path) -> Path:
    path = tmp_path / "blueprint.yaml"
    path.write_text(BLUEPRINT, encoding="utf-8")
    return path


def test_show_cases_prints_rows_notes_and_the_boundary_table(
    blueprint: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show", "cases", str(blueprint)]) == 0
    assert capsys.readouterr().out == EXPECTED


def test_show_cases_takes_the_project_folder(
    blueprint: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show", "cases", str(blueprint.parent)]) == 0
    assert capsys.readouterr().out == EXPECTED


def test_show_without_a_view_still_describes_the_blueprint(
    blueprint: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show", str(blueprint)]) == 0
    assert capsys.readouterr().out.startswith("invoice_desk 1.0.0\n")


def test_show_cases_says_when_there_are_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "plain.yaml"
    path.write_text("name: plain\nagents:\n  clerk: Check invoices.\n", encoding="utf-8")
    assert main(["show", "cases", str(path)]) == 0
    assert capsys.readouterr().out == "plain: no special cases\n"


def test_show_cases_refuses_when_unclear_approve_with_the_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        BLUEPRINT.replace("when_unclear: reject", "when_unclear: approve"), encoding="utf-8"
    )
    assert main(["show", "cases", str(path)]) == 1
    error = capsys.readouterr().err
    assert "special_cases.decide.when_unclear" in error
    assert "use 'ask' (a person decides) or 'reject'" in error
