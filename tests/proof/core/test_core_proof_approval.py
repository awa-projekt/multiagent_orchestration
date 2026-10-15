"""Core proof, scenario 4: approval pause and resume with a decision and a restart in between.

The checkpointer is a SQLite file. "Restart" means: every object of the first run (agent, model,
checkpointer) is dropped and rebuilt from the file; one test does it in a fresh Python process.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool

from _helpers.polling import SUBPROCESS_TIMEOUT
from _helpers.proof_helpers import delegate, model_of, of_type, scripted
from awa.agents import Agent
from awa.contracts import ApprovalDecidedEvent, ApprovalRequestedEvent, RunEvent
from awa.errors import AgentRunError
from awa.persistence import SqliteCheckpointer
from awa.team import Team
from awa.testing import FakeChatModel, tool_call
from awa.tools import create_tool_approval_wrapper

PAYMENTS: list[tuple[str, float]] = []


@tool
def pay_invoice(invoice: str, amount: float) -> str:
    """Pay an invoice. Irreversible."""

    PAYMENTS.append((invoice, amount))
    return f"paid {amount} for {invoice}"


@pytest.fixture(autouse=True)
def _clean_payments() -> None:
    PAYMENTS.clear()


def clerk(db: Path, *script: Any, events: list[RunEvent] | None = None) -> Agent:
    """A new agent object on a new checkpointer object over the same file: a restart."""

    return Agent(
        "clerk",
        description="Pays invoices.",
        model=FakeChatModel(list(script)),
        tools=[create_tool_approval_wrapper(pay_invoice)],
        checkpointer=SqliteCheckpointer(db),
        hooks=[events.append] if events is not None else [],
    )


CALL = tool_call("pay_invoice", {"invoice": "INV-7", "amount": 80.0})


async def test_s4_run_pauses_before_the_tool_and_names_the_pending_call(tmp_path: Path) -> None:
    """Claim: a gated tool does not run before a decision; the result says it is interrupted and
    carries the tool, its arguments and the thread id; one ApprovalRequestedEvent is emitted and
    the model was called once."""

    events: list[RunEvent] = []
    agent = clerk(tmp_path / "c.db", CALL, events=events)

    paused = await agent.run("Pay INV-7.")

    assert paused.is_interrupted and paused.thread_id
    assert PAYMENTS == []
    pending = paused.interrupts[0]["value"]
    assert pending["tool"] == "pay_invoice"
    assert pending["args"] == {"invoice": "INV-7", "amount": 80.0}
    [requested] = of_type(events, ApprovalRequestedEvent)
    assert requested.tool == "pay_invoice" and requested.thread_id == paused.thread_id
    assert model_of(agent).call_count == 1


@pytest.mark.parametrize(
    ("decision", "payments", "final"),
    [
        ({"action": "approve"}, [("INV-7", 80.0)], "paid"),
        ({"action": "edit", "args": {"amount": 40.0}}, [("INV-7", 40.0)], "paid 40"),
        ({"action": "reject", "note": "over budget"}, [], "not paid"),
    ],
    ids=["approve", "edit", "reject"],
)
async def test_s4_decision_after_a_restart_is_applied_exactly_once(
    tmp_path: Path, decision: dict[str, Any], payments: list[tuple[str, float]], final: str
) -> None:
    """Claim: after the agent, its model and the checkpointer object are rebuilt from the file, the
    decision resumes the same thread: approve runs the call with the original arguments, edit with
    the edited ones, reject never runs the tool and tells the model why; one decision event."""

    db = tmp_path / "c.db"
    paused = await clerk(db, CALL).run("Pay INV-7.")
    assert paused.is_interrupted and PAYMENTS == [] and paused.thread_id
    saved = paused.thread_id  # only this string and the file survive the "restart"
    del paused

    events: list[RunEvent] = []
    restarted = clerk(db, final, events=events)

    done = await restarted.resume(saved, decision)

    assert not done.is_interrupted and done.output == final
    assert payments == PAYMENTS
    [decided] = of_type(events, ApprovalDecidedEvent)
    assert decided.decision == decision["action"] and decided.tool == "pay_invoice"
    if decision["action"] == "reject":
        followup = model_of(restarted).calls[0].messages[-1]
        assert "over budget" in str(followup.content)
    assert model_of(restarted).call_count == 1


async def test_s4_a_decision_is_not_applied_twice(tmp_path: Path) -> None:
    """Claim: once a paused run was resumed and finished, a second resume of the same thread does
    not run the tool again."""

    db = tmp_path / "c.db"
    paused = await clerk(db, CALL).run("Pay INV-7.")
    thread = paused.thread_id
    assert thread
    await clerk(db, "paid").resume(thread, {"action": "approve"})
    assert PAYMENTS == [("INV-7", 80.0)]

    with pytest.raises(AgentRunError):  # a finished thread has nothing left to decide
        await clerk(db, "paid again").resume(thread, {"action": "approve"})

    assert PAYMENTS == [("INV-7", 80.0)]


async def test_s4_malformed_decision_counts_as_a_rejection(tmp_path: Path) -> None:
    """Claim: a decision that is not approve, reject or edit never runs the tool."""

    db = tmp_path / "c.db"
    paused = await clerk(db, CALL).run("Pay INV-7.")
    assert paused.thread_id

    await clerk(db, "not paid").resume(paused.thread_id, {"action": "yes please"})

    assert PAYMENTS == []


async def test_s4_edit_with_invalid_arguments_rejects_the_call(tmp_path: Path) -> None:
    """Claim: an edit whose arguments do not fit the tool's schema is a rejection; nothing runs."""

    db = tmp_path / "c.db"
    paused = await clerk(db, CALL).run("Pay INV-7.")
    assert paused.thread_id

    await clerk(db, "not paid").resume(
        paused.thread_id, {"action": "edit", "args": {"amount": "a lot"}}
    )

    assert PAYMENTS == []


async def test_s4_team_pauses_as_a_whole_and_resumes_after_a_restart(tmp_path: Path) -> None:
    """Claim: an approval inside a delegated member pauses the whole team; after the team is
    rebuilt from the checkpoint file the decision resumes it, the tool runs once and the lead
    gives the final answer."""

    db = tmp_path / "t.db"

    def build(*lead_script: Any, member_script: Any) -> Team:
        member = Agent(
            "payer",
            description="Pays.",
            model=FakeChatModel(list(member_script)),
            tools=[create_tool_approval_wrapper(pay_invoice)],
        )
        lead = scripted("lead", *lead_script)
        return Team(
            "desk", orchestrator=lead, members=[member], checkpointer=SqliteCheckpointer(db)
        )

    first = build(delegate("payer"), member_script=[CALL])
    paused = await first.run("Pay INV-7.")
    assert paused.is_interrupted and PAYMENTS == []
    assert paused.interrupts[0]["value"]["tool"] == "pay_invoice"
    assert paused.thread_id

    second = build("All paid.", member_script=["paid"])
    done = await second.resume(paused.thread_id, {"action": "approve"})

    assert not done.is_interrupted and PAYMENTS == [("INV-7", 80.0)]
    assert done.output == "All paid."


@pytest.mark.slow
async def test_s4_resume_in_a_fresh_process(tmp_path: Path) -> None:
    """Claim: the pause survives a real process restart: process A pauses and exits, process B
    (a new interpreter) opens the same file and resumes; the tool runs in B only, once."""

    db = tmp_path / "p.db"
    marker = tmp_path / "payments.txt"
    program = textwrap.dedent(
        """
        import asyncio, sys
        from langchain_core.tools import tool
        from awa.agents import Agent
        from awa.persistence import SqliteCheckpointer
        from awa.testing import FakeChatModel, tool_call
        from awa.tools import create_tool_approval_wrapper

        db, marker, mode, thread = sys.argv[1:5]

        @tool
        def pay_invoice(invoice: str, amount: float) -> str:
            \"\"\"Pay an invoice.\"\"\"
            with open(marker, "a") as handle:
                handle.write(f"{invoice},{amount}\\n")
            return "paid"

        script = (
            [tool_call("pay_invoice", {"invoice": "INV-7", "amount": 80.0})]
            if mode == "pause" else ["done"]
        )
        agent = Agent("clerk", model=FakeChatModel(script), checkpointer=SqliteCheckpointer(db),
                      tools=[create_tool_approval_wrapper(pay_invoice)])

        async def main():
            if mode == "pause":
                result = await agent.run("Pay INV-7.")
                print("THREAD", result.thread_id, result.is_interrupted)
            else:
                result = await agent.resume(thread, {"action": "approve"})
                print("DONE", result.output, result.is_interrupted)

        asyncio.run(main())
        """
    )
    script = tmp_path / "proc.py"
    script.write_text(program, encoding="utf-8")

    def run(mode: str, thread: str = "-") -> str:
        done = subprocess.run(
            [sys.executable, str(script), str(db), str(marker), mode, thread],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
        assert done.returncode == 0, done.stderr[-800:]
        return done.stdout

    first = run("pause").split()
    assert first[0] == "THREAD" and first[2] == "True"
    assert not marker.exists()
    second = run("resume", first[1]).split()

    assert second[:2] == ["DONE", "done"] and second[2] == "False"
    assert marker.read_text(encoding="utf-8").splitlines() == ["INV-7,80.0"]
