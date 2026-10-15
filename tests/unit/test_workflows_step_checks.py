"""The checks of a process step: every check passes and fails, fails closed on non-finite values, and is cached by LangGraph."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from awa.agents import Agent
from awa.blueprints import Blueprint
from awa.blueprints._process_documents import ProjectDocuments
from awa.contracts.results import RunResult
from awa.errors import ConfigurationError
from awa.persistence.cache import InMemoryCacheStore
from awa.runtime.cache import Cache, CacheSettings
from awa.testing import FakeChatModel
from awa.tools.approval import ApprovalCondition
from awa.workflows import ProcessStep, ProcessWorkflow
from awa.workflows._step_checks import (
    ApprovalCheck,
    RuleCheck,
    SchemaCheck,
    answer_check,
    approval_result,
    documents_check,
    json_in_text,
    output_values,
    run_checks,
)


class Invoice(BaseModel):
    supplier: str
    total: float


def result(output: Any = None, text: str = "") -> RunResult[Any]:
    return RunResult(run_id="r", agent="a", output=output, text=text)


def schema_check(required: tuple[str, ...] = ("supplier", "total")) -> SchemaCheck:
    def problems(data: Any) -> list[str]:
        missing = [name for name in required if name not in data]
        return [f"missing: {', '.join(missing)}"] if missing else []

    return SchemaCheck("s.schema.1", problems, required)


RULE = RuleCheck(
    "s.rule.1", "empty(supplier) or total <= 0", "The invoice has no supplier or a zero total."
)


# ------------------------------------------------------------------ reading the output


def test_the_names_of_an_expression_are_the_fields_of_a_model_output_plus_text() -> None:
    values = output_values(result(Invoice(supplier="ACME", total=5.0), text="done"))

    assert values == {"supplier": "ACME", "total": 5.0, "text": "done"}


def test_json_in_the_answer_text_gives_the_fields_whole_or_fenced() -> None:
    plain = output_values(result('{"total": 7}', text='{"total": 7}'))
    fenced = output_values(result(None, text='Here:\n```json\n{"total": 9}\n```\nbye'))

    assert plain["total"] == 7
    assert fenced["total"] == 9
    assert json_in_text("not json") is None
    assert json_in_text("{broken") is None
    assert json_in_text("[1, 2]") is None


def test_a_text_answer_without_json_has_only_the_text_name() -> None:
    assert output_values(result("hello", text="hello")) == {"text": "hello"}


def test_a_field_called_text_wins_over_the_answer_text() -> None:
    assert output_values(result({"text": "field"}, text="raw"))["text"] == "field"


# ------------------------------------------------------------------ implicit checks


def test_an_empty_answer_fails_the_answer_check_with_the_fix() -> None:
    bad = answer_check("extract", "extractor", result(None, text="  "))
    good = answer_check("extract", "extractor", result(None, text="ok"))
    structured = answer_check("extract", "extractor", result(Invoice(supplier="a", total=1)))

    assert not bad.passed and "'extractor' returned nothing" in bad.message
    assert "run this step again" in bad.message
    assert good.passed and structured.passed
    assert bad.id == "extract.answer" and bad.kind == "answer"


def test_the_documents_check_passes_without_problems_and_joins_them_otherwise() -> None:
    assert documents_check("s", []).passed
    failed = documents_check("s", ["Document 'a' is missing.", "Document 'b' changed."])

    assert not failed.passed and failed.message == "Document 'a' is missing. Document 'b' changed."


# ------------------------------------------------------------------ schema and rule


def test_a_schema_check_passes_a_complete_output_and_lists_what_is_missing() -> None:
    check = schema_check()

    ok = run_checks([check], result(Invoice(supplier="a", total=1)))
    bad = run_checks([check], result({"supplier": "a"}))

    assert ok.passed
    assert not bad.passed and bad.results[0].message == "missing: total"


def test_a_schema_check_on_an_answer_without_fields_fails_with_the_fix() -> None:
    verdict = run_checks([schema_check()], result("hello", text="hello"))

    assert not verdict.passed
    assert "no output fields (supplier, total)" in verdict.results[0].message


def test_a_rule_passes_when_its_expression_is_false() -> None:
    assert run_checks([RULE], result(Invoice(supplier="ACME", total=5))).passed


def test_a_rule_that_fires_fails_with_its_message_and_the_values_it_read() -> None:
    verdict = run_checks([RULE], result({"supplier": "ACME", "total": 0}))

    message = verdict.failures[0].message
    assert message.startswith("The invoice has no supplier or a zero total.")
    assert "supplier = ACME" in message and "total = 0" in message


def test_a_rule_on_a_missing_field_fails_closed_and_says_the_field_is_missing() -> None:
    verdict = run_checks([RULE], result({"supplier": "ACME"}))

    assert not verdict.passed
    assert "The output has no field 'total'" in verdict.failures[0].message


def test_a_rule_that_cannot_be_evaluated_fails_instead_of_passing() -> None:
    rule = RuleCheck("s.rule.1", "total > 5", "Too small.")

    verdict = run_checks([rule], result({"total": "a lot"}))

    assert not verdict.passed


def test_a_rule_with_a_malformed_expression_is_refused_when_it_is_made() -> None:
    with pytest.raises(ConfigurationError):
        RuleCheck("s.rule.1", "total <=", "m")


def test_inverting_a_rule_is_detected_by_the_pass_and_fail_cases_together() -> None:
    # Mutation guard: `fail_when` is true -> failed. Both directions must hold.
    rule = RuleCheck("s.rule.1", "total > 100", "Too large.")

    assert run_checks([rule], result({"total": 5})).passed
    assert not run_checks([rule], result({"total": 500})).passed


# ------------------------------------------------------------------ approval


def test_an_approval_without_a_condition_always_asks() -> None:
    ask = ApprovalCheck("s.approval.1", reason="Always.", by="manager")

    verdict = run_checks([ask], result({"total": 1}))

    assert verdict.passed and verdict.approval is not None
    assert verdict.approval.reasons == ("Always.",) and verdict.approval.by == ("manager",)


def test_an_approval_with_a_condition_asks_only_when_it_is_true() -> None:
    ask = ApprovalCheck("s.approval.1", when="total >= 5000", reason="Large.")

    assert run_checks([ask], result({"total": 6200})).approval is not None
    assert run_checks([ask], result({"total": 100})).approval is None


def test_an_approval_whose_condition_cannot_be_evaluated_asks() -> None:
    ask = ApprovalCheck("s.approval.1", when="total >= 5000", reason="Large.")

    assert run_checks([ask], result({"supplier": "ACME"})).approval is not None
    assert run_checks([ask], result({"total": "5.000"})).approval is not None
    assert run_checks([ask], result("no fields", text="no fields")).approval is not None


def test_no_person_is_asked_about_a_result_that_failed_a_rule() -> None:
    ask = ApprovalCheck("s.approval.1", reason="Always.")

    verdict = run_checks([ask, RULE], result({"supplier": "", "total": 0}))

    assert not verdict.passed and verdict.approval is None


def test_several_approvals_that_apply_are_asked_once_with_every_reason() -> None:
    first = ApprovalCheck("a.1", when="total > 1", reason="Large.", by="manager")
    second = ApprovalCheck("a.2", reason="New supplier.", by="finance")
    third = ApprovalCheck("a.3", when="total > 1000", reason="Huge.")

    verdict = run_checks([first, second, third], result({"total": 5}))

    assert verdict.approval is not None
    assert verdict.approval.reasons == ("Large.", "New supplier.")
    assert verdict.approval.by == ("manager", "finance")
    assert verdict.approval.check_ids == ("a.1", "a.2")


def test_a_rejection_names_the_role_and_the_note() -> None:
    ask = ApprovalCheck("s.approval.1", reason="r", by="manager")
    request = run_checks([ask], result({})).approval
    assert request is not None

    rejected = approval_result(request, approved=False, note="Wrong supplier")
    approved = approval_result(request, approved=True, note="fine")

    assert not rejected.passed and rejected.message == "Rejected by manager: Wrong supplier"
    assert approved.passed and approved.message == ""


NOT_FINITE_NUMBERS: list[Any] = [
    math.nan,
    math.inf,
    -math.inf,
    [1],
    [],
    {"amount": 1},
    True,
    False,
    "abc",
    "nan",
    "inf",
    "5.000",
]
IDS = [
    "nan",
    "inf",
    "minus-inf",
    "list",
    "empty-list",
    "object",
    "true",
    "false",
    "text",
    "text-nan",
    "text-inf",
    "ambiguous",
]

FAIL_WHEN = [
    "total <= 0",
    "total > 1000",
    "not (total > 0)",
    "total * 2 > 10",
    "abs(total) > 10",
    "round(total) > 10",
    "max(total, 1) > 10",
    "min(total, 1) < 0",
    "number(total) > 10",
    "total == 0",
]


def total_result(total: Any) -> RunResult[Any]:
    return RunResult(run_id="r", agent="a", output={"total": total, "supplier": "ACME"}, text="")


@pytest.mark.parametrize("fail_when", FAIL_WHEN)
@pytest.mark.parametrize("total", NOT_FINITE_NUMBERS, ids=IDS)
def test_a_rule_fails_the_step_for_a_value_that_is_not_a_finite_number(
    fail_when: str, total: Any
) -> None:
    verdict = run_checks(
        [RuleCheck("s.rule.1", fail_when, "The total is wrong.")], total_result(total)
    )

    assert not verdict.passed, f"{fail_when!r} passed for total = {total!r}"


@pytest.mark.parametrize("when", ["total >= 5000", "abs(total) >= 5000", "max(total, 0) > 100"])
@pytest.mark.parametrize("total", NOT_FINITE_NUMBERS, ids=IDS)
def test_an_approval_asks_for_a_value_that_is_not_a_finite_number(when: str, total: Any) -> None:
    verdict = run_checks(
        [ApprovalCheck("s.approval.1", when, "Large totals.")], total_result(total)
    )

    assert verdict.approval is not None, f"{when!r} did not ask for total = {total!r}"


@pytest.mark.parametrize("total", [0, 0.0, 12, 12.5, "12.5", "1,234.50", -3])
def test_finite_numbers_still_decide_as_before(total: Any) -> None:
    small = run_checks(
        [RuleCheck("s.rule.1", "abs(total) > 5000", "Too large.")], total_result(total)
    )
    zero = run_checks([RuleCheck("s.rule.2", "total == 0", "Zero.")], total_result(total))

    assert small.passed
    assert zero.passed is (float(str(total).replace(",", "")) != 0)


def test_a_list_of_finite_numbers_still_works_in_max_and_min() -> None:
    rule = RuleCheck("s.rule.1", "max(prices) > 100 or min(prices) < 0", "A price is off.")

    ok = run_checks([rule], RunResult(run_id="r", agent="a", output={"prices": [1, 2.5]}, text=""))
    bad = run_checks(
        [rule], RunResult(run_id="r", agent="a", output={"prices": [1, math.nan]}, text="")
    )

    assert ok.passed
    assert not bad.passed


def test_text_fields_and_flags_still_compare_as_values() -> None:
    rule = RuleCheck("s.rule.1", "status == 'error' or ok == false", "The step reported an error.")

    ok = run_checks(
        [rule], RunResult(run_id="r", agent="a", output={"status": "done", "ok": True}, text="")
    )
    bad = run_checks(
        [rule], RunResult(run_id="r", agent="a", output={"status": "done", "ok": False}, text="")
    )

    assert ok.passed
    assert not bad.passed


@pytest.mark.parametrize(
    "amount", [math.nan, math.inf, True, "5.000"], ids=["nan", "inf", "true", "ambiguous"]
)
def test_a_tool_approval_condition_asks_for_the_same_values(amount: Any) -> None:
    assert ApprovalCondition("abs(amount) >= 5000")({"amount": amount})
    assert ApprovalCondition("amount == 0 or amount >= 5000")({"amount": amount})


class Documents:
    """Step documents that count how often they are read."""

    def __init__(self, *, fingerprint: bool = True) -> None:
        self.reads = 0
        self.version = "v1"
        if fingerprint:
            self.fingerprint = lambda: self.version

    def problems(self) -> list[str]:
        self.reads += 1
        return []

    def block(self) -> str:
        return f"## Documents\n- invoice ({self.version})"


def process(documents: Any, cache: Cache | None) -> ProcessWorkflow:
    agent = Agent("clerk", model=FakeChatModel(["Booked."], repeat_last=True))
    return ProcessWorkflow("intake", [ProcessStep("read", agent, documents=documents)], cache=cache)


def steps_cache(**settings: Any) -> Cache:
    return Cache(CacheSettings(**settings), InMemoryCacheStore(), project="intake")


async def test_an_unchanged_documents_check_runs_once() -> None:
    documents = Documents()
    flow = process(documents, steps_cache())

    for _ in range(3):
        result = await flow.run("Book it.")

    assert documents.reads == 1
    assert result.output == "Booked."


async def test_the_step_still_sees_its_documents_block_when_the_check_is_reused() -> None:
    documents = Documents()
    model = FakeChatModel(["Booked."], repeat_last=True)
    flow = ProcessWorkflow(
        "intake",
        [ProcessStep("read", Agent("clerk", model=model), documents=documents)],
        cache=steps_cache(),
    )

    await flow.run("Book it.")
    await flow.run("Book it.")

    assert all("invoice (v1)" in str(call.messages[-1].content) for call in model.calls)


async def test_changed_documents_are_checked_again() -> None:
    documents = Documents()
    flow = process(documents, steps_cache())

    await flow.run("Book it.")
    documents.version = "v2"
    await flow.run("Book it.")

    assert documents.reads == 2


async def test_two_tenants_never_share_a_documents_check() -> None:
    documents = Documents()
    flow = process(documents, steps_cache())

    await flow.run("Book it.", metadata={"awa.tenant": "acme"})
    await flow.run("Book it.", metadata={"awa.tenant": "globex"})
    await flow.run("Book it.", metadata={"awa.tenant": "acme"})

    assert documents.reads == 2


async def test_without_the_switch_a_fingerprint_or_a_cache_the_check_runs_every_time() -> None:
    for documents, cache in (
        (Documents(), steps_cache(steps=False)),
        (Documents(), steps_cache(enabled=False)),
        (Documents(fingerprint=False), steps_cache()),
        (Documents(), None),
    ):
        flow = process(documents, cache)
        await flow.run("Book it.")
        await flow.run("Book it.")
        assert documents.reads == 2


def test_the_fingerprint_of_project_documents_follows_their_files(tmp_path: Path) -> None:
    bp = Blueprint.from_yaml_text(
        """
name: demo
agents:
  - {name: a, description: Does a.}
data:
  - {name: prices, path: data/prices.csv, role: reference}
process:
  - {name: one, agent: a, documents: [prices]}
"""
    )
    (tmp_path / "data").mkdir()
    file = tmp_path / "data" / "prices.csv"
    file.write_text("Artikel;Preis\nA;1\n", encoding="utf-8")
    documents = ProjectDocuments(bp, ["prices"], base=tmp_path)

    first = documents.fingerprint()
    assert documents.fingerprint() == first
    file.write_text("Artikel;Preis\nA;2\n", encoding="utf-8")  # same size, other content
    assert documents.fingerprint() != first


def retried(documents: Any, cache: Cache | None) -> tuple[ProcessWorkflow, FakeChatModel]:
    from awa.workflows import RuleCheck, StepRetry

    # The first answer fails the rule, the second passes: two tries of one step.
    model = FakeChatModel(["unknown", "Booked."])
    flow = ProcessWorkflow(
        "intake",
        [
            ProcessStep(
                "read",
                Agent("clerk", model=model),
                documents=documents,
                checks=(RuleCheck("read.rule.1", "'unknown' in text", "No answer."),),
                retry=StepRetry(max_attempts=2, initial=0.0, jitter=0.0),
            )
        ],
        cache=cache,
        sleep=_no_wait,
    )
    return flow, model


async def _no_wait(seconds: float) -> None:
    return None


async def test_a_retry_does_not_check_unchanged_documents_again() -> None:
    documents = Documents()
    flow, model = retried(documents, steps_cache())

    result = await flow.run("Book it.")

    assert result.output == "Booked." and len(model.calls) == 2
    assert documents.reads == 1
    assert all("invoice (v1)" in str(call.messages[-1].content) for call in model.calls)


async def test_a_retry_checks_documents_again_when_they_changed() -> None:
    documents = Documents()
    flow, model = retried(documents, steps_cache())
    original = documents.problems

    def changing() -> list[str]:
        documents.version = f"v{documents.reads + 2}"  # the file changes after each read
        return original()

    documents.problems = changing  # type: ignore[method-assign]

    await flow.run("Book it.")

    assert documents.reads == 2
    assert "invoice (v3)" in str(model.calls[1].messages[-1].content)
