"""The checks too specific for a table (`Scenario.checks`): plain functions of one scenario's files.

Used by the scenario tests (`tests/examples/test_data_first_scenarios.py`) and by the live
acceptance script (`scripts/live_data_first.py`): the same code decides in both. A check raises
`AssertionError` when it does not hold.
"""

from __future__ import annotations

import asyncio
import inspect
import socket
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from awa.data import DataProfile, Privacy
from awa.testing import FunctionChatModel, structured_output

from . import generate, harness
from .scenarios import Scenario

VERDICT = "verdict"  # the field of the fit check's answer that holds ok / question / blocked
BUDGET = 6000  # tokens of the digest a model gets


@dataclass
class World:
    """A scenario's input files copied into a folder, and what the profile says about them."""

    scenario: Scenario
    folder: Path
    before: dict[str, tuple[str, int]]
    profile: DataProfile
    generated: Path


def make_world(scenario: Scenario, generated: Path, base: Path) -> World:
    """Copy the scenario's input files flat into `base/in` and profile them."""

    folder = harness.materialize(scenario, generated, base)
    return World(scenario, folder, harness.digests(folder), harness.profile_of(folder), generated)


@dataclass
class Ctx:
    world: World
    tmp_path: Path
    monkeypatch: pytest.MonkeyPatch

    @property
    def profile(self) -> DataProfile:
        return self.world.profile


CHECKS: dict[str, Callable[[Ctx], None]] = {}


def check(name: str) -> Callable[[Callable[[Ctx], None]], Callable[[Ctx], None]]:
    def register(function: Callable[[Ctx], None]) -> Callable[[Ctx], None]:
        CHECKS[name] = function
        return function

    return register


def resolved(value: Any) -> Any:
    return asyncio.run(value) if inspect.isawaitable(value) else value


def reference(ctx: Ctx, expression: str, file: str, sheet: str | None = None) -> Any:
    from _helpers.data_fixtures import computed

    return computed(expression, ctx.profile, file=file, sheet=sheet)


def view(ctx: Ctx, **options: Any) -> str:
    from awa.data import render_for_model

    return render_for_model(ctx.profile, BUDGET, **options)


@check("iban_masked_in_model_view")
def _iban_masked(ctx: Ctx) -> None:
    text = view(ctx, privacy=Privacy(mask="placeholders"))
    for iban in ("DE02100100100000123456", "DE44500105175407324931", "DE75512108001245126199"):
        assert iban not in text
    assert "<IBAN_" in text


@check("reference_order_values")
def _reference_orders(ctx: Ctx) -> None:
    # the expression language of the toolkits' filters (`where`)
    assert reference(ctx, "sum(Betrag_EUR where Status == 'offen')", "orders.csv") == pytest.approx(
        46161.25
    )
    assert reference(ctx, "count(Bestellnr where Betrag_EUR > 20000)", "orders.csv") == 1


@check("reference_values_without_totals_row")
def _reference_sales(ctx: Ctx) -> None:
    total = sum(generate.revenue(i, 1) for i in range(12))
    sheet = "Umsatz 2026"
    assert reference(ctx, "count(Kunde)", "s4_sales.xlsx", sheet) == 24
    expression = "sum(Umsatz_EUR where Monat == '2026-09-01')"
    assert reference(ctx, expression, "s4_sales.xlsx", sheet) == pytest.approx(total)


@check("reference_orders_without_address")
def _reference_json(ctx: Ctx) -> None:
    count = reference(ctx, "count(orderId where deliveryAddress == null)", "shop_orders.jsonl")
    assert count == len(generate.NO_ADDRESS) == 3


@check("relation_customers_targets")
def _relation_sales(ctx: Ctx) -> None:
    pairs = [
        r
        for r in ctx.profile.relations
        if r.from_column.endswith(".kunde") and r.to_column.endswith(".kunde")
    ]
    assert pairs and max(r.overlap or 0 for r in pairs) == pytest.approx(1.0)


@check("relation_export_database")
def _relation_erp(ctx: Ctx) -> None:
    pairs = [
        r
        for r in ctx.profile.relations
        if {r.from_column.rpartition(".")[2], r.to_column.rpartition(".")[2]} == {"order_id"}
        and r.confidence >= 0.8
    ]
    assert pairs


@check("exactly_one_injection")
def _one_injection(ctx: Ctx) -> None:
    suspects = [
        f.path
        for f in harness.walk_files(ctx.profile)
        if f.mail and any(m.injection_suspected for m in f.mail.messages)
    ]
    assert suspects == ["06_attack.eml"]


@check("html_not_fetched")
def _html_not_fetched(ctx: Ctx) -> None:
    attempts: list[object] = []

    def refuse(*args: object, **kwargs: object) -> None:
        attempts.append(args)
        raise OSError("the network is closed")

    for name in ("getaddrinfo", "create_connection"):
        ctx.monkeypatch.setattr(socket, name, refuse)
    ctx.monkeypatch.setattr(socket.socket, "connect", refuse)
    fresh = harness.profile_of(ctx.world.folder)
    html = harness.find_file(fresh, "04_html_only.eml")
    assert html is not None and html.status == "ok" and attempts == []


@check("attack_only_in_data_block")
def _attack_in_block(ctx: Ctx) -> None:
    inside, seen = False, 0
    for line in view(ctx).splitlines():
        if line.startswith("<<<DATA"):
            inside = True
        elif line.startswith("<<<END DATA>>>"):
            inside = False
        elif "attacker@example.com" in line.lower() or "ignore your instructions" in line.lower():
            seen += 1
            assert inside and line.startswith("| "), line
    assert seen >= 1  # the attack is shown, but only as marked data


@check("scan_model_view_is_short")
def _scan_view(ctx: Ctx) -> None:
    text = view(ctx)
    assert "RECHNUNG 2024-0412" in text  # the born-digital text is in the view
    assert "no text layer" in text.lower()
    scan_lines = [ln for ln in text.splitlines() if "scan_0001" in ln]
    assert scan_lines and sum(len(ln) for ln in scan_lines) < 600


@check("database_untouched_by_profile")
def _database_untouched(ctx: Ctx) -> None:
    harness.profile_of(ctx.world.folder)
    assert harness.digests(ctx.world.folder) == ctx.world.before
    shop = ctx.tmp_path / "shop.db"  # a database whose UPDATE trigger would write a log row
    shop.write_bytes((ctx.world.generated / "sqlite" / "shop.db").read_bytes())
    before = shop.read_bytes()
    result = harness.profile_of(shop.parent)
    assert harness.find_file(result, "shop.db") is not None and shop.read_bytes() == before
    connection = sqlite3.connect(f"file:{shop.as_posix()}?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM audit_log").fetchone() == (0,)
    finally:
        connection.close()


@check("no_document_files")
def _no_documents(ctx: Ctx) -> None:
    kinds = {f.kind for f in harness.walk_files(ctx.profile)}
    assert kinds == {"xlsx", "csv"}


@check("deterministic_question")
def _deterministic_question(ctx: Ctx) -> None:
    from awa.blueprints._fit import fit_check

    s = ctx.world.scenario
    lazy = FunctionChatModel(
        lambda call: structured_output("FitVerdict", {VERDICT: "ok", "questions": []})
    )
    answer = resolved(fit_check(s.description, ctx.profile, lazy))
    assert lazy.call_count <= 1
    assert getattr(answer, VERDICT) in ("question", "blocked"), answer
    assert answer.questions and all(q.evidence for q in answer.questions)


@check("question_needs_evidence")
def _question_evidence(ctx: Ctx) -> None:
    from awa.blueprints._fit import fit_check

    s = ctx.world.scenario
    assert s.question is not None
    ids = {f"{t.id}.{c.key}" for _, t in ctx.profile.tables() for c in t.columns}
    assert set(s.question.evidence) <= ids  # the scenario's evidence ids are real column ids
    invented = {"id": "q1", "text": "Which role profile do you mean?", "evidence": []}
    cited = {
        "id": "q2",
        "text": "These are invoices: what shall happen with them?",
        "evidence": list(s.question.evidence),
    }
    for question, kept in ((invented, False), (cited, True)):
        model = FunctionChatModel(
            lambda call, q=question: structured_output(
                "FitVerdict", {VERDICT: "question", "questions": [q]}
            )
        )
        answer = resolved(fit_check(s.description, ctx.profile, model))
        texts = [q.text for q in answer.questions]
        assert (question["text"] in texts) is kept, texts
        assert all(q.evidence and set(q.evidence) <= ids for q in answer.questions)


@check("lone_png_blocked")
def _lone_png(ctx: Ctx) -> None:
    from awa.blueprints._fit import fit_check

    folder = ctx.tmp_path / "png"
    folder.mkdir()
    (folder / "logo.png").write_bytes((ctx.world.generated / "image" / "logo.png").read_bytes())
    data = harness.profile_of(folder)
    refusing = FunctionChatModel(lambda call: (_ for _ in ()).throw(AssertionError("a model call")))
    answer = resolved(fit_check("Reconcile the bank statement with the invoices.", data, refusing))
    assert getattr(answer, VERDICT) == "blocked" and refusing.call_count == 0
