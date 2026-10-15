"""Blueprint `policies:`: run-time tool policies wired into agents, and what `check` makes of them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from awa import Agent, Blueprint, BlueprintError
from awa import tool as awa_tool
from awa.cli import main
from awa.testing import FakeChatModel, tool_call

CALLS: list[str] = []


@tool
def fetch_page(url: str) -> str:
    """Fetch a web page."""
    CALLS.append(f"fetch {url}")
    return "page"


@tool
def send_email(to: str, body: str) -> str:
    """Send an e-mail."""
    CALLS.append(f"send {to}")
    return "sent"


@awa_tool(approval=True, risk=["external_action"])
def post_update(text: str) -> str:
    """Post an update to the customer portal."""
    CALLS.append("post")
    return "posted"


@pytest.fixture(autouse=True)
def _reset() -> None:
    CALLS.clear()


def _blueprint(policies: list[dict[str, Any]], tools: list[str] | None = None) -> Blueprint:
    return Blueprint.from_dict(
        {
            "name": "desk",
            "agents": [
                {
                    "name": "desk",
                    "description": "Answers from the web.",
                    "tools": tools if tools is not None else ["fetch_page", "send_email"],
                }
            ],
            "policies": policies,
        }
    )


def _texts(result: Any) -> list[str]:
    return [str(m.content) for m in result.messages if isinstance(m, ToolMessage)]


async def test_a_deny_policy_refuses_the_call_at_run_time() -> None:
    blueprint = _blueprint([{"deny": ["send_email"], "reason": "this desk never mails"}])
    agent = blueprint.build(tools=[fetch_page, send_email])
    assert isinstance(agent, Agent) and agent.tool_policy is not None

    with agent.override(
        model=FakeChatModel([tool_call("send_email", {"to": "x", "body": "b"}), "ok"])
    ):
        result = await agent.run("mail x")

    assert CALLS == []
    assert _texts(result) == [
        "Error: the call to 'send_email' was denied by policy: this desk never mails"
    ]


async def test_an_ask_policy_pauses_and_needs_a_checkpointer() -> None:
    blueprint = _blueprint([{"agents": ["desk"], "ask": ["external_action"]}])
    assert blueprint.policy_agents() == ["desk"]
    with pytest.raises(BlueprintError, match="checkpointer"):
        blueprint.build(tools=[fetch_page, send_email])

    agent = blueprint.build(tools=[fetch_page, send_email], checkpointer=InMemorySaver())
    assert isinstance(agent, Agent)
    model = FakeChatModel([tool_call("send_email", {"to": "x", "body": "b"}), "sent it"])
    with agent.override(model=model):
        paused = await agent.run("mail x")
        assert paused.is_interrupted and CALLS == []
        assert paused.thread_id is not None
        done = await agent.resume(paused.thread_id, {"action": "approve"})

    assert done.output == "sent it" and CALLS == ["send x"]


def test_policies_gate_the_risky_tool_warning() -> None:
    ungated = _blueprint([])
    denied = _blueprint([{"deny": ["send_email"]}])
    asked = _blueprint([{"ask": ["external_action"]}])
    other_agent = Blueprint.from_dict(
        {
            **_blueprint([]).to_dict(),
            "agents": [
                _blueprint([]).agents[0].model_dump(),
                {"name": "other", "description": "x"},
            ],
            "entrypoint": "desk",
            "policies": [{"agents": ["other"], "ask": ["external_action"]}],
        }
    )

    def warns(bp: Blueprint) -> bool:
        return any(
            "reads untrusted content" in i.message for i in bp.check(tools=[fetch_page, send_email])
        )

    assert warns(ungated)
    assert not warns(denied) and not warns(asked)
    assert warns(other_agent)


def test_self_gated_tools_are_not_reported_as_ungated() -> None:
    blueprint = _blueprint([], tools=["fetch_page", "post_update"])

    issues = blueprint.check(tools=[fetch_page, post_update])

    assert not any("reads untrusted content" in i.message for i in issues)


def test_policy_mistakes_are_check_errors() -> None:
    blueprint = _blueprint([{}, {"agents": ["nobody"], "deny": ["x"]}, {"use": "not a reference"}])

    errors = {
        (i.path, i.message.split(" (")[0]) for i in blueprint.check() if i.severity == "error"
    }

    assert ("policies[0]", "a policy needs deny, ask or use") in errors
    assert ("policies[1].agents[0]", "unknown agent 'nobody'") in errors
    assert any(path == "policies[2].use" for path, _ in errors)
    with pytest.raises(BlueprintError):
        Blueprint.from_dict({"name": "x", "agents": {"x": "y"}, "policies": [{"ask": ["loud"]}]})


async def test_a_policy_function_is_imported_from_a_file(tmp_path: Path) -> None:
    (tmp_path / "desk_policies.py").write_text(
        "from awa.tools import Deny, Allow\n\n"
        "def no_mail(call, ctx):\n"
        "    return Deny('mail is off') if call.name == 'send_email' else Allow()\n",
        encoding="utf-8",
    )
    path = tmp_path / "desk.yaml"
    path.write_text(_blueprint([{"use": "./desk_policies.py:no_mail"}]).to_yaml(), encoding="utf-8")
    blueprint = Blueprint.from_yaml_file(path)

    with pytest.raises(BlueprintError, match="allow_imports"):
        blueprint.build(tools=[fetch_page, send_email])
    agent = blueprint.build(tools=[fetch_page, send_email], allow_imports=True)
    assert isinstance(agent, Agent)
    with agent.override(
        model=FakeChatModel([tool_call("send_email", {"to": "x", "body": "b"}), "ok"])
    ):
        result = await agent.run("mail")

    assert CALLS == [] and "mail is off" in _texts(result)[0]


def test_show_prints_policies_and_auto_matching(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "desk.yaml"
    data = _blueprint([{"agents": ["desk"], "deny": ["send_email"], "ask": ["external_action"]}])
    path.write_text(data.model_copy(update={"auto": True}).to_yaml(), encoding="utf-8")

    assert main(["show", str(path)]) == 0

    out = capsys.readouterr().out
    assert "policy (desk): deny send_email; ask for external_action" in out
    assert "auto-matching for desk" in out
