"""Multi-step evaluation: a case's decisions resume the run at every approval pause."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from awa import Agent, Team, tool
from awa.cli import build_parser
from awa.contracts import AgentCard, RunResult
from awa.contracts.events import ApprovalDecidedEvent, ToolCallEvent
from awa.evals import (
    ApprovalDecided,
    ApprovalRequested,
    Dataset,
    EvalCase,
    EvalError,
    NoApprovalRequested,
    RequiresApproval,
    evaluate,
    isolated_run,
    parse_scorer,
)
from awa.models import ModelRegistry
from awa.runtime import new_run_id
from awa.testing import FunctionChatModel, ModelCall, tool_call

BOOKED: list[dict[str, Any]] = []


@tool(approval="amount >= 5000")
def book_invoice(invoice_id: str, amount: float) -> str:
    """Book an invoice in the ledger.

    Args:
        invoice_id: The invoice, e.g. INV-7.
        amount: Total in EUR.
    """

    BOOKED.append({"invoice_id": invoice_id, "amount": amount})
    return f"booked {invoice_id} over {amount:g} EUR"


@tool(approval=True)
def notify_supplier(invoice_id: str, text: str) -> str:
    """Tell the supplier about the booking.

    Args:
        invoice_id: The invoice.
        text: The message.
    """

    return f"supplier told about {invoice_id}: {text}"


@pytest.fixture(autouse=True)
def _clear() -> None:
    BOOKED.clear()


def clerk_model(call: ModelCall) -> Any:
    """Books the invoice (6200 EUR when the request says 'large', else 800), then notifies."""

    request = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
    results = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
    amount = 6200 if "large" in request else 800
    if not results:
        return tool_call("book_invoice", {"invoice_id": "INV-7", "amount": amount})
    if len(results) == 1:
        return tool_call("notify_supplier", {"invoice_id": "INV-7", "text": "booked"})
    return "Done. " + " | ".join(results)


def clerk() -> Agent:
    return Agent(
        "clerk",
        model=FunctionChatModel(clerk_model),
        tools=[book_invoice, notify_supplier],
        checkpointer=InMemorySaver(),
    )


def case(case_id: str, text: str, *decisions: Any, scorers: tuple[str, ...] = ()) -> EvalCase:
    return EvalCase(id=case_id, input=text, decisions=decisions, scorers=scorers)


async def test_a_process_with_two_approvals_is_evaluated_to_its_end() -> None:
    report = await evaluate(
        clerk,
        [
            case(
                "large",
                "Book the large invoice INV-7.",
                {"action": "approve"},
                {"action": "reject", "note": "Wrong wording."},
                scorers=(
                    "approval_requested:book_invoice",
                    "approval_granted:book_invoice",
                    "approval_rejected:notify_supplier",
                    "tool:notify_supplier",
                    "contains:Wrong wording",
                ),
            )
        ],
    )

    [result] = report.cases
    assert result.status == "passed", report.to_markdown()
    assert result.approvals == ["book_invoice", "notify_supplier"]
    assert result.decisions == ["approve:book_invoice", "reject:notify_supplier"]
    assert (result.segments, result.unused_decisions, result.interrupted) == (3, 0, False)
    assert result.tool_calls == ["book_invoice", "notify_supplier"]
    assert BOOKED == [{"invoice_id": "INV-7", "amount": 6200}]
    trace = result.trace
    assert trace is not None and len(trace.runs) == 3 and trace.result is trace.runs[-1]
    assert [d["action"] for d in trace.decisions] == ["approve", "reject"]
    assert len(trace.of_type(ToolCallEvent)) == 2
    assert result.usage.requests == 3  # all segments share one usage


async def test_below_the_threshold_nobody_is_asked_and_above_someone_is() -> None:
    dataset = Dataset(
        cases=(
            case(
                "small",
                "Book invoice INV-7.",
                "approve",
                scorers=(
                    "no_approval:book_invoice",
                    "approval_requested:notify_supplier",
                    "contains:booked INV-7 over 800 EUR",
                ),
            ),
            case(
                "large",
                "Book the large invoice INV-7.",
                "approve",
                "approve",
                scorers=("approval_requested:book_invoice", "contains:over 6200 EUR"),
            ),
        )
    )

    report = await evaluate(clerk, dataset)

    assert report.pass_rate == 1.0, report.to_markdown()
    assert [r.approvals for r in report.cases] == [
        ["notify_supplier"],
        ["book_invoice", "notify_supplier"],
    ]


async def test_without_decisions_the_run_ends_at_the_first_pause() -> None:
    report = await evaluate(
        clerk,
        [
            case(
                "paused",
                "Book the large invoice INV-7.",
                scorers=("approval:book_invoice", "no_tool:notify_supplier"),
            )
        ],
    )

    [result] = report.cases
    assert result.status == "passed" and result.interrupted and result.segments == 1
    assert result.approvals == ["book_invoice"] and not BOOKED


async def test_decisions_the_run_never_asks_for_are_counted() -> None:
    report = await evaluate(
        clerk, [case("extra", "Book invoice INV-7.", "approve", "approve", scorers=("tool:x",))]
    )

    [result] = report.cases
    assert result.unused_decisions == 1 and result.decisions == ["approve:notify_supplier"]
    assert "decisions applied: approve:notify_supplier; 1 never asked for" in (report.to_markdown())


async def test_an_edit_decision_grants_the_call_with_the_new_arguments() -> None:
    report = await evaluate(
        clerk,
        [
            case(
                "edit",
                "Book the large invoice INV-7.",
                {"action": "edit", "args": {"amount": 6100}},
                {"action": "approve", "reviewer": "maria"},
                scorers=("approval_granted:book_invoice", "contains:over 6100 EUR"),
            )
        ],
    )

    [result] = report.cases
    assert result.status == "passed", report.to_markdown()
    assert result.decisions == ["edit:book_invoice", "approve:notify_supplier"]
    assert result.trace is not None
    assert [e.reviewer for e in result.trace.of_type(ApprovalDecidedEvent)] == [None, "maria"]


async def test_parallel_requests_each_get_the_next_decision() -> None:
    def parallel(call: ModelCall) -> Any:
        results = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
        if not results:
            return [
                tool_call("notify_supplier", {"invoice_id": "INV-1", "text": "a"}, id="c1"),
                tool_call("notify_supplier", {"invoice_id": "INV-2", "text": "b"}, id="c2"),
            ]
        return " | ".join(results)

    def system() -> Agent:
        return Agent(
            "notifier",
            model=FunctionChatModel(parallel),
            tools=[notify_supplier],
            checkpointer=InMemorySaver(),
        )

    report = await evaluate(
        system,
        [
            case(
                "both",
                "Notify both suppliers.",
                "approve",
                {"action": "reject", "note": "not this one"},
                scorers=("approval_requested:notify_supplier", "contains:not this one"),
            )
        ],
    )

    [result] = report.cases
    assert result.status == "passed", report.to_markdown()
    assert result.segments == 2 and len(result.approvals) == 2
    assert sorted(result.decisions) == ["approve:notify_supplier", "reject:notify_supplier"]
    assert ApprovalRequested("notify_supplier", min_times=2).name == (
        "approval_requested:notify_supplier"
    )


async def test_a_team_member_s_approvals_are_resumed_through_the_team() -> None:
    def lead(call: ModelCall) -> Any:
        last = call.last_message
        if isinstance(last, ToolMessage):
            return f"Accounts says: {last.content}"
        return tool_call("delegate_task", {"agent": "accounts", "task": str(last.content)})

    def team() -> Team:
        accounts = Agent(
            "accounts",
            description="Books invoices.",
            model=FunctionChatModel(clerk_model),
            tools=[book_invoice, notify_supplier],
        )
        head = Agent("head", description="Front desk.", model=FunctionChatModel(lead))
        return Team("desk", orchestrator=head, members=[accounts], checkpointer=InMemorySaver())

    report = await evaluate(
        team,
        [
            case(
                "large",
                "Book the large invoice INV-7.",
                "approve",
                "approve",
                scorers=(
                    "delegated:accounts",
                    "approval_granted:book_invoice",
                    "approval_granted:notify_supplier",
                    "contains:Accounts says",
                ),
            )
        ],
    )

    [result] = report.cases
    assert result.status == "passed", report.to_markdown()
    assert result.approvals == ["book_invoice", "notify_supplier"]
    assert result.tool_calls.count("book_invoice") == 1
    assert BOOKED == [{"invoice_id": "INV-7", "amount": 6200}]


class Paused:
    """A system that pauses but cannot be resumed (or has no thread)."""

    def __init__(self, *, thread: str | None = "t-1") -> None:
        self._thread = thread

    @property
    def name(self) -> str:
        return "paused"

    @property
    def card(self) -> AgentCard:
        return AgentCard(name="paused")

    async def run(self, input: Any, **kwargs: Any) -> RunResult[Any]:
        return RunResult(
            run_id=new_run_id(),
            agent="paused",
            output=None,
            thread_id=self._thread,
            interrupts=[{"id": "i-1", "value": {"tool": "pay"}}],
        )


class NoThread(Paused):
    async def resume(self, thread_id: str, decision: Any) -> RunResult[Any]:
        raise AssertionError("never resumed")


async def test_a_system_that_cannot_resume_fails_the_case_clearly() -> None:
    run = await isolated_run(Paused(), "Pay.", decisions=[{"action": "approve"}])
    assert isinstance(run.error, EvalError) and "has no resume()" in str(run.error)
    assert len(run.segments) == 1

    run = await isolated_run(NoThread(thread=None), "Pay.", decisions=[{"action": "approve"}])
    assert isinstance(run.error, EvalError) and "without a thread" in str(run.error)

    report = await evaluate(Paused(), [case("pay", "Pay.", "approve", scorers=("approval",))])
    assert report.cases[0].status == "error"
    assert report.cases[0].approvals == ["pay"]


@pytest.mark.parametrize("book_first", [True, False])
async def test_a_decision_naming_its_tool_answers_that_request_in_any_order(
    book_first: bool,
) -> None:
    # Parallel calls may arrive in either order; the targeted rejection must still
    # reach its own tool and not the other pending request.
    def both(call: ModelCall) -> Any:
        results = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
        if results:
            return " | ".join(results)
        calls = [
            tool_call("book_invoice", {"invoice_id": "INV-7", "amount": 6200}, id="b"),
            tool_call("notify_supplier", {"invoice_id": "INV-7", "text": "booked"}, id="n"),
        ]
        return calls if book_first else calls[::-1]

    def system() -> Agent:
        return Agent(
            "clerk",
            model=FunctionChatModel(both),
            tools=[book_invoice, notify_supplier],
            checkpointer=InMemorySaver(),
        )

    report = await evaluate(
        system,
        [
            case(
                "targeted",
                "Book and notify.",
                {"action": "reject", "tool": "book_invoice", "note": "Budget freeze."},
                "approve",
                scorers=("approval_rejected:book_invoice", "approval_granted:notify_supplier"),
            )
        ],
    )

    [result] = report.cases
    assert result.status == "passed", report.to_markdown()
    assert not BOOKED


async def test_a_targeted_decision_waits_for_its_tool_and_ends_the_run_paused_otherwise() -> None:
    later = await isolated_run(
        clerk(),
        "Book the large invoice INV-7.",
        decisions=[
            {"action": "approve", "tool": "notify_supplier"},
            {"action": "reject", "tool": "book_invoice", "note": "No."},
        ],
    )
    assert later.error is None and later.result is not None
    assert not later.result.is_interrupted and later.unused_decisions == 0
    assert [(d["action"], d["tool"]) for d in later.decisions] == [
        ("reject", "book_invoice"),
        ("approve", "notify_supplier"),
    ]

    unmatched = await isolated_run(
        clerk(),
        "Book the large invoice INV-7.",
        decisions=[{"action": "approve", "tool": "notify_supplier"}],
    )
    assert unmatched.result is not None and unmatched.result.is_interrupted
    assert (len(unmatched.segments), unmatched.unused_decisions) == (1, 1)
    assert not BOOKED


REMOVED: list[str] = []


@tool(approval=True)
def remove_license(license: str) -> str:
    """Remove one license of the leaver.

    Args:
        license: The license, e.g. office.
    """

    REMOVED.append(license)
    return f"removed {license}"


LICENSES = ("office", "crm", "vpn")


def offboarding(*, licenses_first: bool, parallel: bool = False) -> Agent:
    """Removes every license (one call each, or all at once) and notifies, in either order."""

    removals = [tool_call("remove_license", {"license": name}, id=name) for name in LICENSES]
    notify = tool_call("notify_supplier", {"invoice_id": "-", "text": "leaver"}, id="n")
    steps: list[Any] = [removals] if parallel else list(removals)
    steps = [*steps, notify] if licenses_first else [notify, *steps]

    def respond(call: ModelCall) -> Any:
        done = sum(isinstance(m, ToolMessage) for m in call.messages)
        taken = 0
        for step in steps:
            taken += len(step) if isinstance(step, list) else 1
            if taken > done:
                return step
        return " | ".join(str(m.content) for m in call.messages if isinstance(m, ToolMessage))

    return Agent(
        "offboarder",
        model=FunctionChatModel(respond),
        tools=[remove_license, notify_supplier],
        checkpointer=InMemorySaver(),
    )


@pytest.mark.parametrize(
    ("licenses_first", "parallel"), [(True, False), (False, False), (True, True), (False, True)]
)
async def test_a_repeating_decision_answers_every_request_of_its_tool(
    licenses_first: bool, parallel: bool
) -> None:
    REMOVED.clear()
    run = await isolated_run(
        offboarding(licenses_first=licenses_first, parallel=parallel),
        "Offboard the leaver.",
        decisions=[
            {"action": "approve", "tool": "remove_license", "repeat": True},
            {"action": "reject", "tool": "notify_supplier", "note": "Not yet."},
        ],
    )

    assert run.error is None and run.result is not None
    assert not run.result.is_interrupted and run.unused_decisions == 0
    assert sorted(REMOVED) == sorted(LICENSES)
    applied = [(d["action"], d["tool"]) for d in run.decisions]
    assert applied.count(("approve", "remove_license")) == 3
    assert applied.count(("reject", "notify_supplier")) == 1
    assert "Not yet." in run.result.text


async def test_a_one_off_decision_for_a_tool_goes_before_its_repeating_one() -> None:
    REMOVED.clear()
    run = await isolated_run(
        offboarding(licenses_first=True),
        "Offboard the leaver.",
        decisions=[
            {"action": "approve", "tool": "remove_license", "repeat": True},
            {"action": "reject", "tool": "remove_license", "note": "Keep office."},
            {"action": "approve"},
        ],
    )

    assert run.result is not None and not run.result.is_interrupted
    assert REMOVED == ["crm", "vpn"]
    assert [d["action"] for d in run.decisions] == ["reject", "approve", "approve", "approve"]


async def test_a_repeating_decision_never_asked_for_is_unused() -> None:
    run = await isolated_run(
        clerk(),
        "Book invoice INV-7.",
        decisions=[
            {"action": "approve"},
            {"action": "approve", "tool": "remove_license", "repeat": True},
        ],
    )

    assert run.result is not None and not run.result.is_interrupted
    assert run.unused_decisions == 1


@pytest.mark.parametrize(
    ("decisions", "message"),
    [
        (["maybe"], "decision 1: 'maybe' is not 'approve' or 'reject'"),
        ([{"action": "approve", "repeat": True}], "repeat needs the tool"),
        ([{"action": "approve", "tool": "pay", "repeat": "yes"}], "repeat must be true or false"),
        (
            [
                {"action": "approve", "tool": "pay", "repeat": True},
                {"action": "reject", "tool": "pay", "repeat": True},
            ],
            "more than one repeating decision for tool pay",
        ),
        ([{"action": "ok"}], "decision 1 needs action approve, reject or edit"),
        ([{"action": "edit"}], "an edit needs an 'args' mapping"),
        ([{"action": "reject", "note": 3}], "note must be text"),
        ([7], "decision 1 must be a mapping"),
        ([{"action": "approve", "tool": " "}], "tool must be the name of the tool it answers"),
        ("approve", "decisions must be a list"),
    ],
)
def test_invalid_decisions_are_refused(decisions: Any, message: str) -> None:
    with pytest.raises(EvalError, match=message):
        Dataset.from_records([{"id": "x", "input": "Pay.", "decisions": decisions}])


def test_decisions_load_from_yaml_and_round_trip() -> None:
    dataset = Dataset.from_yaml(
        """
        cases:
          - id: two-steps
            input: Book and notify.
            decisions: [approve, {action: reject, tool: notify, note: later}]
        """
    )
    assert dataset["two-steps"].decisions == (
        {"action": "approve"},
        {"action": "reject", "tool": "notify", "note": "later"},
    )
    assert Dataset.from_yaml(dataset.to_yaml()) == dataset


@pytest.mark.parametrize(
    ("spec", "kind", "name"),
    [
        ("approval_requested", ApprovalRequested, "approval_requested"),
        ("approval_requested:pay", ApprovalRequested, "approval_requested:pay"),
        ("no_approval", NoApprovalRequested, "no_approval"),
        ("no_approval:pay", NoApprovalRequested, "no_approval:pay"),
        ("approval_granted", ApprovalDecided, "approval_granted"),
        ("approval_granted:pay", ApprovalDecided, "approval_granted:pay"),
        ("approval_rejected:pay", ApprovalDecided, "approval_rejected:pay"),
        ("approval:pay", RequiresApproval, "approval:pay"),
    ],
)
def test_approval_scorer_specs(spec: str, kind: type, name: str) -> None:
    scorer = parse_scorer(spec)
    assert isinstance(scorer, kind) and scorer.name == name


def test_approval_scorer_arguments_are_checked() -> None:
    with pytest.raises(ValueError, match="min_times"):
        ApprovalRequested(min_times=2, max_times=1)
    with pytest.raises(ValueError, match="min_times"):
        ApprovalDecided(min_times=0)


async def test_failing_approval_scorers_say_what_happened_after_the_run() -> None:
    report = await evaluate(
        clerk,
        [
            case(
                "large",
                "Book the large invoice INV-7.",
                "approve",
                "approve",
                scorers=("approval_rejected:book_invoice", "approval:notify_supplier"),
            )
        ],
    )

    reasons = [score.reason for score in report.cases[0].scores]
    assert reasons[0] == "0 call(s) for book_invoice rejected (decisions: approve:book_invoice)"
    assert reasons[1] == "the run did not pause for approval"


# ---------------------------------------------------------------------- awa eval


BLUEPRINT = """
name: ledger
agents:
  - name: clerk
    description: Books invoices.
    tools: [book, notify]
approvals:
  - tool: book
    when: "amount >= 5000"
  - tool: notify
"""

TOOLS = '''
from langchain_core.tools import tool


@tool
def book(invoice_id: str, amount: float) -> str:
    """Book an invoice."""
    return f"booked {invoice_id} over {amount:g} EUR"


@tool
def notify(invoice_id: str) -> str:
    """Notify the supplier."""
    return f"notified about {invoice_id}"
'''

CASES = """
name: ledger-cases
cases:
  - id: small
    input: Book invoice INV-1.
    decisions: [approve]
    scorers: ["no_approval:book", "approval_granted:notify", "contains:notified"]
  - id: large
    input: Book the large invoice INV-2.
    decisions: [approve, {action: reject, note: Not yet.}]
    scorers: ["approval_granted:book", "approval_rejected:notify", "contains:Not yet"]
"""


def ledger_model(call: ModelCall) -> Any:
    request = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
    results = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
    invoice = "INV-2" if "large" in request else "INV-1"
    if not results:
        amount = 7000 if "large" in request else 300
        return tool_call("book", {"invoice_id": invoice, "amount": amount})
    if len(results) == 1:
        return tool_call("notify", {"invoice_id": invoice})
    return " | ".join(results)


def test_awa_eval_resumes_with_the_decisions_of_each_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(ledger_model))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "ledger_tools", raising=False)
    for name, text in {
        "ledger_tools.py": TOOLS,
        "ledger.yaml": BLUEPRINT,
        "cases.yaml": CASES,
    }.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(
        ["eval", "ledger.yaml", "cases.yaml", "--tools", "ledger_tools.py", "--json", "r.json"]
    )

    code = args.handler(args)

    out, err = capsys.readouterr()
    assert code == 0, out + err
    assert "- Pass rate: 100.0% (2/2 runs)" in out
    saved = (tmp_path / "r.json").read_text(encoding="utf-8")
    assert '"decisions": [\n' in saved and '"approve:book"' in saved
