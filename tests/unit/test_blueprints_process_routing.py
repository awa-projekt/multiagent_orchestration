"""Blueprint retries, `after_last_try`, `when` and special cases bound to steps: what `check`
refuses (the bypass tests), and a built process that retries, routes and rejects."""

from __future__ import annotations

from textwrap import dedent
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from awa.blueprints import Blueprint, BlueprintError
from awa.contracts import StepEvent
from awa.contracts.events import RunEvent
from awa.errors import StepRejectedError
from awa.models import ModelRegistry
from awa.runtime import use_global_subscriber
from awa.testing import FakeChatModel, structured_output
from awa.workflows import ProcessWorkflow

AGENTS = """
name: desk
limits: {max_requests: 40}
toolkits:
  email: {sender: desk@example.com}
agents:
  - name: reader
    description: Reads an invoice.
    instructions: Read it.
    model: reader_model
    output_schema:
      type: object
      properties: {supplier: {type: string}, total: {type: number}}
      required: [supplier, total]
  - name: clerk
    description: Prepares the decision.
    instructions: Decide.
    model: clerk_model
    output_schema:
      type: object
      properties: {supplier: {type: string}, total: {type: number}}
      required: [supplier, total]
  - name: mailer
    description: Sends the confirmation.
    instructions: Send it.
    tools: [builtin:send_email]
    model: mailer_model
approvals: [{tool: builtin:send_email}]
"""


def blueprint(process: str, extra: str = "") -> Blueprint:
    return Blueprint.from_yaml_text(dedent(AGENTS) + dedent(process) + dedent(extra))


def errors(bp: Blueprint) -> dict[str, str]:
    return {issue.path: issue.message for issue in bp.check() if issue.severity == "error"}


def warnings(bp: Blueprint) -> dict[str, str]:
    return {issue.path: issue.message for issue in bp.check() if issue.severity == "warning"}


def test_retry_after_last_try_and_when_load_in_their_short_forms() -> None:
    bp = blueprint(
        """
        process:
          - {name: read, agent: reader, retry: 3, after_last_try: "Go: decide"}
          - {name: decide, agent: clerk, retry: {tries: 2, wait: 5s, on: [check]}, when: "total > 0"}
          - {name: send, agent: mailer, after_last_try: {go: send2}}
          - {name: send2, agent: clerk, after_last_try: ask}
        """
    )
    assert bp.process is not None
    read, decide, send, send2 = bp.process.steps
    assert read.retry is not None and read.retry.max_attempts == 3
    assert read.after_last_try == "go: decide" and read.last_try().to == "decide"
    retry = decide.retry.step_retry() if decide.retry else None
    assert retry is not None and retry.max_attempts == 2 and retry.initial == 5.0
    assert retry.on == frozenset({"check"}) and retry.delay(5) == 50.0  # capped at 10 x wait
    assert send.after_last_try == "go: send2" and send2.last_try().action == "ask"
    assert Blueprint.from_yaml_text(bp.to_yaml()).process == bp.process


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("retry: 6", "less than or equal to 5"),
        ("retry: {tries: 2, wait: soon}", "duration"),
        ("retry: {on: [timeout]}", "error"),
        ("after_last_try: retry", "stop, ask or go"),
        ("after_last_try: 'go: 9x'", "not a step name"),
        ("when: 'total >'", "cannot be read"),
    ],
)
def test_invalid_step_fields_fail_to_load_with_the_fix(field: str, message: str) -> None:
    with pytest.raises(BlueprintError, match=message):
        Blueprint.from_yaml_text(
            dedent(AGENTS) + f"process:\n  - name: read\n    agent: reader\n    {field}\n"
        )


def test_a_writing_agent_may_not_retry() -> None:
    bp = blueprint("process:\n  - {name: send, agent: mailer, retry: 2}\n")
    assert "writes or sends with builtin:send_email" in errors(bp)["process.steps[0].retry"]
    assert errors(blueprint("process:\n  - {name: read, agent: reader, retry: 2}\n")) == {}


def test_a_writing_agent_may_not_continue_elsewhere_after_its_last_try() -> None:
    bp = blueprint(
        """
        process:
          - {name: send, agent: mailer, after_last_try: "go: done"}
          - {name: done, agent: clerk}
        """
    )
    assert "half-done action" in errors(bp)["process.steps[0].after_last_try"]


def test_a_failed_step_may_not_continue_at_a_writing_step() -> None:
    bp = blueprint(
        """
        process:
          - {name: read, agent: reader, after_last_try: "go: send"}
          - {name: send, agent: mailer}
        """
    )
    assert "must not lead to an action" in errors(bp)["process.steps[0].after_last_try"]


def test_go_backwards_or_to_an_unknown_step_is_an_error() -> None:
    bp = blueprint(
        """
        process:
          - {name: read, agent: reader}
          - {name: decide, agent: clerk, after_last_try: "go: read"}
          - {name: last, agent: clerk, after_last_try: "go: nowhere"}
        """
    )
    found = errors(bp)
    assert "not later in the process" in found["process.steps[1].after_last_try"]
    assert "not a step of the process" in found["process.steps[2].after_last_try"]


def test_go_past_a_rule_or_approval_check_is_refused() -> None:
    bp = blueprint(
        """
        process:
          - {name: read, agent: reader, after_last_try: "go: last"}
          - name: decide
            agent: clerk
            checks: [{kind: rule, fail_when: "total > 100", message: Too large.}]
          - {name: last, agent: clerk}
        """,
        """
        special_cases:
          read:
            - {if: "total < 10", then: go, to: last}
        """,
    )
    found = errors(bp)
    assert "jump over decide" in found["process.steps[0].after_last_try"]
    assert "jump over decide" in found["special_cases.read.cases[0].to"]


def test_special_cases_for_an_unknown_step_are_an_error() -> None:
    bp = blueprint(
        "process:\n  - {name: read, agent: reader}\n",
        "special_cases:\n  decide:\n    - {if: 'total > 1', then: approve}\n",
    )
    assert "which the process does not have (read)" in errors(bp)["special_cases.decide"]


def test_when_on_a_step_with_checks_warns_that_they_are_skipped_too() -> None:
    bp = blueprint(
        """
        process:
          - {name: read, agent: reader}
          - name: decide
            agent: clerk
            when: "total > 100"
            checks: [{kind: approval, reason: Large.}]
        """
    )
    assert "skipped with its checks (approval)" in warnings(bp)["process.steps[1].when"]


PROCESS = """
process:
  - {name: read, agent: reader, retry: {tries: 3, wait: 0}, checks: [{kind: rule, fail_when: "total <= 0", message: No total.}]}
  - name: decide
    agent: clerk
    checks: [{kind: approval, when: "total >= 5000", reason: Large invoices need a person.}]
  - {name: archive, agent: clerk}
special_cases:
  decide:
    cases:
      - {if: "supplier in ['Blocked Ltd']", then: reject, message: Blocked supplier.}
      - {if: [{field: total, op: "<", value: 100}], then: go, to: archive}
      - {if: [{field: total, op: "<", value: 10000}], then: approve}
    else: ask
"""


def models(supplier: str, *totals: float) -> dict[str, FakeChatModel]:
    def answers(name: str) -> list[Any]:
        return [structured_output(name, {"supplier": supplier, "total": t}) for t in totals]

    return {
        "reader_model": FakeChatModel(answers("ReaderOutput"), repeat_last=True),
        "clerk_model": FakeChatModel(answers("ClerkOutput")[-1:], repeat_last=True),
        "mailer_model": FakeChatModel(["sent"]),
    }


def build(scripted: dict[str, FakeChatModel]) -> ProcessWorkflow:
    registry = ModelRegistry()
    for name, model in scripted.items():
        registry.register(name, model)
    bp = blueprint(PROCESS)
    assert errors(bp) == {}
    system = bp.build(model_registry=registry, checkpointer=InMemorySaver())
    assert isinstance(system, ProcessWorkflow)
    return system


async def run(system: ProcessWorkflow) -> tuple[Any, list[RunEvent]]:
    seen: list[RunEvent] = []
    with use_global_subscriber(seen.append):
        try:
            result: Any = await system.run("Check invoice 7")
        except Exception as exc:  # noqa: BLE001 - the tests look at the error and the events
            result = exc
    return result, seen


async def test_the_built_process_retries_and_a_go_row_skips_to_archive() -> None:
    scripted = models("ACME", 0, 50)
    result, events = await run(build(scripted))

    assert scripted["reader_model"].call_count == 2
    assert [s["status"] for s in result.metadata["steps"]] == ["checked", "checked", "checked"]
    decide = result.metadata["steps"][1]
    assert decide["case"]["id"] == "decide.case.2" and decide["case"]["then"] == "go"
    cases = [(e.step, e.status, e.case) for e in events if isinstance(e, StepEvent) and e.case]
    assert cases == [("decide", "checked", "decide.case.2")]


async def test_a_reject_row_ends_the_built_run_as_rejected() -> None:
    result, events = await run(build(models("Blocked Ltd", 500)))

    assert isinstance(result, StepRejectedError)
    assert result.details["case"] == "decide.case.1"
    assert ("decide", "rejected") in [
        (e.step, e.status) for e in events if isinstance(e, StepEvent)
    ]


async def test_an_approve_row_does_not_skip_a_matching_approval_check() -> None:
    system = build(models("ACME", 6000))
    paused = await system.run("Check invoice 7")

    assert paused.is_interrupted
    assert paused.interrupts[0]["value"]["reason"] == "Large invoices need a person."
    done = await system.resume(paused.thread_id, {"action": "approve"})
    assert done.metadata["steps"][1]["case"]["then"] == "approve"


async def test_else_ask_merges_into_one_question_with_the_approval_check() -> None:
    paused = await build(models("ACME", 20000)).run("Check invoice 7")

    reason = paused.interrupts[0]["value"]["reason"]
    assert "Large invoices need a person." in reason and "Special case: else" in reason
