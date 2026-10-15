"""`ProcessWorkflow`: order, check-before-next, documents, approval pause and resume, events."""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from awa.agents import Agent
from awa.contracts import RunCompletedEvent, StepEvent, UsageLimits
from awa.contracts.events import ApprovalDecidedEvent, ApprovalRequestedEvent, RunEvent
from awa.errors import AgentRunError, StepCheckFailedError, UsageLimitExceededError
from awa.runtime import use_global_subscriber
from awa.testing import FakeChatModel, structured_output
from awa.workflows import ApprovalCheck, ProcessStep, ProcessWorkflow, RuleCheck, SchemaCheck


class Invoice(BaseModel):
    supplier: str
    total: float


class Report(BaseModel):
    ok: bool
    max_deviation_pct: float


def scripted(name: str, *responses: Any, **kwargs: Any) -> Agent:
    return Agent(
        name, description=f"The {name} agent.", model=FakeChatModel(list(responses)), **kwargs
    )


def calls(agent: Agent) -> int:
    model = agent._model
    assert isinstance(model, FakeChatModel)
    return model.call_count


def extractor(supplier: str = "ACME", total: float = 1190.0) -> Agent:
    return scripted(
        "extractor",
        structured_output(Invoice, {"supplier": supplier, "total": total}),
        output_type=Invoice,
    )


def price_checker(pct: float = 0.5) -> Agent:
    return scripted(
        "price_checker",
        structured_output(Report, {"ok": True, "max_deviation_pct": pct}),
        output_type=Report,
    )


def booker() -> Agent:
    return scripted("booker", "booked")


def rule(name: str, expression: str, message: str = "The rule fired.") -> RuleCheck:
    return RuleCheck(f"{name}.rule.1", expression, message)


class Docs:
    """Documents with a scripted state: what is wrong, and the block the agent gets."""

    def __init__(self, problems: list[str] | None = None, block: str = "DOCS BLOCK") -> None:
        self._problems = problems or []
        self._block = block
        self.checked = 0

    def problems(self) -> list[str]:
        self.checked += 1
        return list(self._problems)

    def block(self) -> str:
        return self._block


def process(*steps: ProcessStep, **options: Any) -> ProcessWorkflow:
    return ProcessWorkflow("invoice_check", list(steps), **options)


async def events_of(workflow: ProcessWorkflow, request: str, **options: Any) -> list[RunEvent]:
    seen: list[RunEvent] = []
    try:
        async for event in workflow.stream(request, **options):
            seen.append(event)
    except Exception:  # noqa: BLE001 - the tests look at the events a failing run emitted
        pass
    return seen


def statuses(events: list[RunEvent]) -> list[str]:
    return [f"{e.step}:{e.status}" for e in events if isinstance(e, StepEvent)]


# --------------------------------------------------------------------------- the happy path


async def test_steps_run_in_order_and_the_result_lists_every_step_as_checked() -> None:
    first, second, third = extractor(), price_checker(), booker()
    wf = process(
        ProcessStep(
            "extract", first, title="Read the invoice", checks=(rule("extract", "total <= 0"),)
        ),
        ProcessStep("validate", second),
        ProcessStep("approve", third),
    )

    result = await wf.run("Check invoice 1")

    assert result.output == "booked"
    assert [s["name"] for s in result.metadata["steps"]] == ["extract", "validate", "approve"]
    assert {s["status"] for s in result.metadata["steps"]} == {"checked"}
    assert result.metadata["steps"][0]["title"] == "Read the invoice"
    ids = [c["id"] for c in result.metadata["steps"][0]["checks"]]
    assert ids == ["extract.answer", "extract.rule.1"]
    assert calls(first) == calls(second) == calls(third) == 1


async def test_a_step_gets_the_previous_steps_output_as_json() -> None:
    writer = scripted("writer", "done")
    wf = process(ProcessStep("extract", extractor()), ProcessStep("write", writer))

    await wf.run("go")

    model = writer._model
    assert isinstance(model, FakeChatModel)
    assert model.calls[0].last_message.content == '{"supplier":"ACME","total":1190.0}'


async def test_carry_all_gives_the_request_and_every_earlier_step() -> None:
    writer = scripted("writer", "done")
    wf = process(
        ProcessStep("extract", extractor()),
        ProcessStep("validate", price_checker()),
        ProcessStep("write", writer),
        carry="all",
    )

    await wf.run("the request")

    model = writer._model
    assert isinstance(model, FakeChatModel)
    text = str(model.calls[0].last_message.content)
    assert text.startswith("the request") and "[extractor]" in text and "[price_checker]" in text


async def test_step_events_report_started_checking_checked_for_every_step_in_order() -> None:
    wf = process(
        ProcessStep("extract", extractor(), checks=(rule("extract", "total <= 0"),)),
        ProcessStep("approve", booker()),
    )

    events = await events_of(wf, "go")

    assert statuses(events) == [
        "extract:started",
        "extract:checking",
        "extract:checked",
        "approve:started",
        "approve:checking",
        "approve:checked",
    ]
    checked = [e for e in events if isinstance(e, StepEvent) and e.status == "checked"]
    assert checked[0].agent == "extractor" and checked[0].total == 2 and checked[1].index == 1
    assert [c["id"] for c in checked[0].checks] == ["extract.answer", "extract.rule.1"]
    assert isinstance(events[-1], RunCompletedEvent)


def test_step_names_must_be_unique() -> None:
    with pytest.raises(ValueError, match="more than once"):
        process(ProcessStep("a", booker()), ProcessStep("a", booker()))
    with pytest.raises(ValueError, match="at least one"):
        process()


def test_process_workflow_has_no_outside_steps_parameter() -> None:
    with pytest.raises(TypeError, match="outside_steps"):
        ProcessWorkflow("p", [ProcessStep("a", booker())], outside_steps=["b"])  # type: ignore[call-arg]


# --------------------------------------------------------------------------- check before next


async def test_the_next_agent_never_runs_after_a_failed_rule() -> None:
    first, second = extractor(total=0.0), price_checker()
    wf = process(
        ProcessStep(
            "extract",
            first,
            checks=(rule("extract", "total <= 0", "The invoice has a total of zero."),),
        ),
        ProcessStep("validate", second),
    )

    events: list[RunEvent] = []
    with pytest.raises(StepCheckFailedError) as raised:
        async for event in wf.stream("go"):
            events.append(event)

    error = raised.value
    assert calls(first) == 1 and calls(second) == 0
    assert error.step == "extract" and "The invoice has a total of zero." in str(error)
    assert "Later steps were not run" in str(error) and "total = 0.0" in str(error)
    assert [s["status"] for s in error.steps] == ["failed", "waiting"]
    assert error.checks[-1]["passed"] is False and error.code == "awa.step_check_failed"
    assert statuses(events) == ["extract:started", "extract:checking", "extract:failed"]
    failed = [e for e in events if isinstance(e, StepEvent) and e.status == "failed"][0]
    assert "The invoice has a total of zero." in failed.message


async def test_a_failure_in_step_three_reports_the_first_two_as_checked() -> None:
    wf = process(
        ProcessStep("extract", extractor()),
        ProcessStep(
            "validate",
            price_checker(pct=40.0),
            checks=(rule("validate", "max_deviation_pct > 25"),),
        ),
        ProcessStep("approve", booker()),
    )

    with pytest.raises(StepCheckFailedError) as raised:
        await wf.run("go")

    assert [s["status"] for s in raised.value.steps] == ["checked", "failed", "waiting"]


async def test_a_schema_check_fails_when_a_field_is_missing_and_stops_the_process() -> None:
    nameless = scripted("loose", '{"supplier": "ACME"}')
    follower = booker()

    def problems(data: Any) -> list[str]:
        return [f"missing: {n}" for n in ("supplier", "total") if n not in data]

    wf = process(
        ProcessStep("extract", nameless, checks=(SchemaCheck("extract.schema.1", problems),)),
        ProcessStep("approve", follower),
    )

    with pytest.raises(StepCheckFailedError) as raised:
        await wf.run("go")

    assert "missing: total" in str(raised.value) and calls(follower) == 0


async def test_an_empty_answer_fails_the_step_with_the_fix() -> None:
    silent = scripted("silent", "  ")
    follower = booker()
    wf = process(ProcessStep("read", silent), ProcessStep("approve", follower))

    with pytest.raises(StepCheckFailedError) as raised:
        await wf.run("go")

    assert "'silent' returned nothing" in str(raised.value) and calls(follower) == 0


async def test_an_agent_that_raises_marks_the_step_failed_and_the_error_propagates() -> None:
    class Boom(RuntimeError):
        pass

    def explode(_: Any) -> Any:
        raise Boom("model down")

    from awa.testing import FunctionChatModel

    broken = Agent("broken", model=FunctionChatModel(explode))
    follower = booker()
    wf = process(ProcessStep("read", broken), ProcessStep("approve", follower))

    events: list[RunEvent] = []
    with pytest.raises(Boom, match="model down"):
        async for event in wf.stream("go"):
            events.append(event)

    assert "read:failed" in statuses(events) and calls(follower) == 0


async def test_usage_limits_apply_across_the_steps() -> None:
    wf = process(ProcessStep("extract", extractor()), ProcessStep("validate", price_checker()))

    with pytest.raises(UsageLimitExceededError):
        await wf.run("go", usage_limits=UsageLimits(max_requests=1))


# --------------------------------------------------------------------------- documents


async def test_documents_are_checked_before_the_agent_and_a_problem_stops_the_step() -> None:
    agent = price_checker()
    docs = Docs(
        problems=["Document 'price_list' is missing. Drop the file on the Data page again."]
    )
    wf = process(ProcessStep("validate", agent, documents=docs))

    with pytest.raises(StepCheckFailedError) as raised:
        await wf.run("go")

    assert calls(agent) == 0 and docs.checked == 1
    assert "Document 'price_list' is missing" in str(raised.value)
    assert raised.value.checks[0]["kind"] == "documents"


async def test_the_document_block_is_appended_to_the_step_input_not_the_instructions() -> None:
    agent = scripted("validator", "fine", instructions="Check things.")
    wf = process(ProcessStep("validate", agent, documents=Docs(block="## Documents for this step")))

    await wf.run("the request")

    model = agent._model
    assert isinstance(model, FakeChatModel)
    sent = str(model.calls[0].last_message.content)
    assert sent == "the request\n\n## Documents for this step"
    assert all("Documents for this step" not in str(m.content) for m in model.calls[0].messages[:1])


async def test_the_documents_of_a_later_step_are_not_given_to_an_earlier_one() -> None:
    first, second = scripted("first", "one"), scripted("second", "two")
    wf = process(ProcessStep("a", first), ProcessStep("b", second, documents=Docs(block="B-DOCS")))

    await wf.run("go")

    one, two = first._model, second._model
    assert isinstance(one, FakeChatModel) and isinstance(two, FakeChatModel)
    assert "B-DOCS" not in str(one.calls[0].last_message.content)
    assert "B-DOCS" in str(two.calls[0].last_message.content)


# --------------------------------------------------------------------------- approval


def approval_process(
    when: str | None = None, *, checkpointer: bool = True
) -> tuple[ProcessWorkflow, dict[str, Agent]]:
    agents = {"extract": extractor(total=6200.0), "approve": booker()}
    wf = process(
        ProcessStep(
            "extract",
            agents["extract"],
            title="Read the invoice",
            checks=(
                ApprovalCheck(
                    "extract.approval.1",
                    when=when,
                    reason="Large invoices are decided by a person.",
                    by="accounts_manager",
                ),
            ),
        ),
        ProcessStep("approve", agents["approve"]),
        checkpointer=InMemorySaver() if checkpointer else None,
    )
    return wf, agents


async def test_an_approval_pauses_the_process_with_a_payload_naming_step_and_agent() -> None:
    wf, agents = approval_process("total >= 5000")

    paused = await wf.run("go")

    assert paused.is_interrupted and paused.thread_id is not None
    [pending] = paused.interrupts
    value = pending["value"]
    assert value["kind"] == "step_approval" and value["step"] == "extract"
    assert value["title"] == "Read the invoice" and value["agent"] == "extractor"
    assert value["reason"] == "Large invoices are decided by a person."
    assert value["output"] == {"supplier": "ACME", "total": 6200.0}
    assert value["by"] == ["accounts_manager"] and value["tool"] == "step:extract"
    assert calls(agents["approve"]) == 0


async def test_approving_continues_without_running_the_first_step_again() -> None:
    wf, agents = approval_process("total >= 5000")
    paused = await wf.run("go")
    assert paused.thread_id is not None

    done = await wf.resume(paused.thread_id, {"action": "approve", "reviewer": "ana"})

    assert done.output == "booked" and not done.is_interrupted
    assert calls(agents["extract"]) == 1 and calls(agents["approve"]) == 1
    first = done.metadata["steps"][0]
    assert first["status"] == "checked"
    assert first["checks"][-1]["kind"] == "approval" and first["checks"][-1]["passed"] is True


async def test_rejecting_fails_the_step_and_the_next_agent_never_runs() -> None:
    wf, agents = approval_process("total >= 5000")
    paused = await wf.run("go")
    assert paused.thread_id is not None

    with pytest.raises(StepCheckFailedError) as raised:
        await wf.resume(paused.thread_id, {"action": "reject", "note": "Wrong supplier"})

    assert "Rejected by accounts_manager: Wrong supplier" in str(raised.value)
    assert calls(agents["approve"]) == 0
    assert raised.value.details.get("rejected") is True


@pytest.mark.parametrize(
    "answer",
    ["approve", True, None, {"action": "edit", "args": {"total": 1}}, {"decision": "approve"}, {}],
)
async def test_anything_but_an_explicit_approval_counts_as_a_rejection(answer: Any) -> None:
    wf, agents = approval_process()
    paused = await wf.run("go")
    assert paused.thread_id is not None

    with pytest.raises(StepCheckFailedError):
        await wf.resume(paused.thread_id, answer)

    assert calls(agents["approve"]) == 0


async def test_an_approval_with_a_false_condition_does_not_pause() -> None:
    wf, agents = approval_process("total >= 99999")

    done = await wf.run("go")

    assert done.output == "booked" and not done.is_interrupted
    assert calls(agents["approve"]) == 1


async def test_an_approval_whose_condition_cannot_be_evaluated_asks() -> None:
    wf, _ = approval_process("amount >= 5000")  # the output has no 'amount'

    paused = await wf.run("go")

    assert paused.is_interrupted


async def test_without_a_checkpointer_an_approval_fails_the_run_instead_of_passing() -> None:
    wf, agents = approval_process(checkpointer=False)

    with pytest.raises(AgentRunError, match="checkpointer"):
        await wf.run("go")

    assert calls(agents["approve"]) == 0


async def test_no_person_is_asked_about_a_result_that_failed_a_rule() -> None:
    agents = {"extract": extractor(total=0.0), "approve": booker()}
    wf = process(
        ProcessStep(
            "extract",
            agents["extract"],
            checks=(ApprovalCheck("extract.approval.1"), rule("extract", "total <= 0")),
        ),
        ProcessStep("approve", agents["approve"]),
        checkpointer=InMemorySaver(),
    )

    events: list[RunEvent] = []
    with pytest.raises(StepCheckFailedError):
        async for event in wf.stream("go"):
            events.append(event)

    assert not [e for e in events if isinstance(e, ApprovalRequestedEvent)]
    assert statuses(events)[-1] == "extract:failed"


async def test_approval_events_name_the_step_and_record_the_reviewer() -> None:
    wf, _ = approval_process("total >= 5000")
    first: list[RunEvent] = []
    async for event in wf.stream("go"):
        first.append(event)
    paused = [e for e in first if isinstance(e, RunCompletedEvent)][0].result
    assert paused.thread_id is not None

    requested = [e for e in first if isinstance(e, ApprovalRequestedEvent)]
    assert [(e.tool, e.reason, e.agent) for e in requested] == [
        ("step:extract", "step_check_requires_approval", "extractor")
    ]
    assert requested[0].interrupt_id
    assert "extract:needs_approval" in statuses(first)

    second: list[RunEvent] = []
    with use_global_subscriber(second.append):
        await wf.resume(paused.thread_id, {"action": "approve", "reviewer": "ana"})
    decided = [e for e in second if isinstance(e, ApprovalDecidedEvent)]
    assert [(e.tool, e.decision, e.reviewer) for e in decided] == [
        ("step:extract", "approve", "ana")
    ]
    assert "extract:checked" in statuses(second) and "approve:checked" in statuses(second)
    assert "extract:started" not in statuses(second)  # answering does not start the step again


async def test_a_decision_keyed_by_the_interrupt_id_approves_and_a_bare_word_does_not() -> None:
    wf, agents = approval_process()
    paused = await wf.run("go")
    assert paused.thread_id is not None
    interrupt_id = paused.interrupts[0]["id"]

    done = await wf.resume(paused.thread_id, {interrupt_id: {"action": "approve"}})

    assert done.output == "booked" and calls(agents["approve"]) == 1
    wf2, agents2 = approval_process()
    paused2 = await wf2.run("go")
    assert paused2.thread_id is not None
    with pytest.raises(StepCheckFailedError):
        await wf2.resume(paused2.thread_id, {paused2.interrupts[0]["id"]: "approve"})
    assert calls(agents2["approve"]) == 0


async def test_an_approved_step_cannot_be_approved_a_second_time() -> None:
    from awa.errors import NoPendingInterruptError

    wf, _ = approval_process()
    paused = await wf.run("go")
    assert paused.thread_id is not None
    await wf.resume(paused.thread_id, {"action": "approve"})

    with pytest.raises(NoPendingInterruptError):
        await wf.resume(paused.thread_id, {"action": "approve"})


async def test_the_approval_preview_of_a_step_redacts_secrets_like_a_tool_approval() -> None:
    class Leaky(BaseModel):
        supplier: str
        total: float
        api_key: str
        note: str

    secret = "sk-ant-api03-" + "A" * 40
    leaky = scripted(
        "extractor",
        structured_output(
            Leaky, {"supplier": "ACME", "total": 6200.0, "api_key": secret, "note": f"key {secret}"}
        ),
        output_type=Leaky,
    )
    wf = process(
        ProcessStep(
            "extract", leaky, checks=(ApprovalCheck("extract.approval.1", when="total >= 5000"),)
        ),
        ProcessStep("approve", booker()),
        checkpointer=InMemorySaver(),
    )

    events = await events_of(wf, "go")

    [requested] = [e for e in events if isinstance(e, ApprovalRequestedEvent)]
    assert secret not in requested.args_preview
    assert "sk-ant-api03" not in requested.args_preview
    assert "ACME" in requested.args_preview
