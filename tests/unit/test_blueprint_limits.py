"""Blueprint `limits:` and `prices:`: the schema, `check()`, and enforcement on every path.

Every path (run, chat, eval, serve, worker, the web app) gets a blueprint with `max_cost: 0.0001`
and a model whose every reply uses 1000 prompt and 200 completion tokens, which cost 0.0036 at the
test prices: the run must stop after that reply.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from awa._internal import aio as asyncio_compat
from awa.automation import SqliteRunStore
from awa.blueprints import Blueprint, BlueprintError, LimitsBlueprint
from awa.cli import ChatSession, build_parser, main
from awa.contracts import PriceTable, UsageLimits
from awa.errors import UsageLimitExceededError
from awa.evals import EvalReport
from awa.models import ModelRegistry
from awa.server._config import ServedSystem
from awa.testing import FakeChatModel

PRICES = {"gpt-test": {"input_per_mtok": 2.0, "output_per_mtok": 8.0}}
REPLY_COST = (1000 * 2.0 + 200 * 8.0) / 1_000_000  # 0.0036

DESK = """
name: desk
agents:
  - name: clerk
    description: Answers questions.
limits: {max_cost: 0.0001}
prices:
  gpt-test: {input_per_mtok: 2.0, output_per_mtok: 8.0}
"""


def reply(text: str = "ok") -> AIMessage:
    return AIMessage(
        content=text,
        usage_metadata={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
    )


def priced_model() -> FakeChatModel:
    return FakeChatModel([reply()], repeat_last=True, model_name="gpt-test")


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> ModelRegistry:
    registry = ModelRegistry()
    registry.register("default", priced_model())
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    return registry


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "desk.yaml").write_text(DESK, encoding="utf-8")
    return tmp_path


def blueprint(text: str) -> Blueprint:
    return Blueprint.from_yaml_text(text)


def messages(issues: list[Any], severity: str) -> list[str]:
    return [f"{i.path}: {i.message}" for i in issues if i.severity == severity]


def test_limits_and_prices_load_build_limits_and_round_trip() -> None:
    spec = blueprint(DESK.replace("{max_cost: 0.0001}", "{max_cost: 0.5, max_duration: 5m}"))

    assert isinstance(spec.limits, LimitsBlueprint)
    limits = spec.usage_limits()
    assert limits is not None
    assert (limits.max_cost, limits.max_duration, limits.max_requests) == (0.5, 300.0, None)
    assert limits.prices == PriceTable.from_mapping(PRICES) == spec.price_table()
    assert Blueprint.from_yaml_text(spec.to_yaml()) == spec
    assert spec.to_dict()["prices"] == PRICES


def test_a_blueprint_without_limits_or_prices_is_unchanged() -> None:
    spec = blueprint("name: x\nagents: {a: do it}\n")

    assert spec.limits is None and spec.prices == {}
    assert spec.usage_limits() is None and spec.price_table() is None
    assert "limits" not in spec.to_dict() and "prices" not in spec.to_dict()


def test_all_five_limits_become_usage_limits() -> None:
    spec = blueprint(
        "name: x\nagents: {a: do it}\n"
        "limits: {max_requests: 40, max_total_tokens: 200000, max_tool_calls: 60, "
        "max_cost: 0.5, max_duration: 90}\n"
        "prices: {m: {input_per_mtok: 1, output_per_mtok: 2}}\n"
    )

    limits = spec.usage_limits()

    assert limits is not None
    assert (limits.max_requests, limits.max_total_tokens, limits.max_tool_calls) == (40, 200000, 60)
    assert (limits.max_cost, limits.max_duration) == (0.5, 90.0)


@pytest.mark.parametrize(
    ("fragment", "message"),
    [
        ("limits: {max_requests: 0}", "limits.max_requests"),
        ("limits: {max_cost: -1}", "limits.max_cost"),
        ("limits: {speed: 3}", "limits.speed"),
        ("limits: {max_duration: soon}", "limits.max_duration"),
        ("limits: {max_duration: 0}", "max_duration must be positive"),
        ("prices: {m: {input_per_mtok: 1}}", "output_per_mtok"),
        ("prices: {m: {input_per_mtok: 1, output_per_mtok: 2, typo: 3}}", "unknown key(s) typo"),
        ("prices: [gpt]", "prices"),
    ],
)
def test_invalid_limits_and_prices_are_refused(fragment: str, message: str) -> None:
    with pytest.raises(BlueprintError, match=message.replace("(", r"\(").replace(")", r"\)")):
        blueprint(f"name: x\nagents: {{a: do it}}\n{fragment}\n")


def test_max_cost_without_prices_cannot_become_limits() -> None:
    spec = blueprint("name: x\nagents: {a: do it}\nlimits: {max_cost: 1}\n")

    with pytest.raises(BlueprintError, match="needs `prices:`"):
        spec.usage_limits()


def test_check_errors_when_a_used_model_has_no_price() -> None:
    spec = blueprint(
        "name: x\nagents:\n  - {name: a, description: d, model: 'azure_openai:gpt-4.1'}\n"
        "limits: {max_cost: 1}\nprices: {gpt-4.1-mini: {input_per_mtok: 1, output_per_mtok: 2}}\n"
    )

    [error] = messages(spec.check(), "error")

    assert error.startswith("agents[0].model: max_cost is set but agent 'a' uses the model")
    assert "'azure_openai:gpt-4.1'" in error and "no entry under `prices:`" in error
    with pytest.raises(BlueprintError, match="no entry under `prices:`"):
        spec.build()


def test_check_errors_when_max_cost_has_no_prices_at_all() -> None:
    spec = blueprint("name: x\nagents: {a: do it}\nlimits: {max_cost: 1}\n")

    assert messages(spec.check(), "error") == [
        "limits.max_cost: max_cost needs `prices:` with the price of every model the blueprint uses"
    ]


@pytest.mark.parametrize("model", ["azure_openai:gpt-4.1", "openai:gpt-4.1-2026-01-01", "gpt-4.1"])
def test_check_accepts_a_price_by_name_provider_prefix_or_version(model: str) -> None:
    spec = blueprint(
        f"name: x\nagents:\n  - {{name: a, description: d, model: '{model}'}}\n"
        "limits: {max_cost: 1}\nprices: {gpt-4.1: {input_per_mtok: 1, output_per_mtok: 2}}\n"
    )

    assert not [i for i in spec.check() if i.severity == "error"]


def test_check_only_warns_about_a_profile_it_cannot_resolve() -> None:
    spec = blueprint(
        "name: x\nagents:\n  - {name: a, description: d, model: my_profile}\n"
        "limits: {max_cost: 1}\nprices: {gpt-4.1: {input_per_mtok: 1, output_per_mtok: 2}}\n"
    )

    issues = spec.check()

    assert not messages(issues, "error")
    assert any("profile 'my_profile'" in w for w in messages(issues, "warning"))


def test_check_ignores_prices_without_max_cost_and_unused_agents() -> None:
    spec = blueprint(
        "name: x\nentrypoint: a\nagents:\n  - {name: a, description: d, model: 'x:priced'}\n"
        "  - {name: b, description: d, model: 'x:unpriced'}\n"
        "limits: {max_cost: 1}\nprices: {priced: {input_per_mtok: 1, output_per_mtok: 2}}\n"
    )

    assert not messages(spec.check(), "error")
    cheap = spec.model_copy(update={"limits": LimitsBlueprint(max_requests=3)})
    assert not [i for i in cheap.check() if "prices" in i.message]


TRIGGERED = (
    "name: x\nagents: {a: do it}\ntriggers: [{name: hook, webhook: {allow_unsigned: true}}]\n"
)


def test_check_warns_when_a_blueprint_with_triggers_has_no_limits() -> None:
    [warning] = [w for w in messages(blueprint(TRIGGERED).check(), "warning") if "limits" in w]

    assert warning.startswith("limits: the blueprint has triggers but no `limits:`")


def test_check_is_quiet_with_limits_or_without_triggers() -> None:
    limited = blueprint(TRIGGERED + "limits: {max_requests: 50}\n")

    assert not [w for w in messages(limited.check(), "warning") if "limits" in w]
    assert not messages(blueprint("name: x\nagents: {a: do it}\n").check(), "warning")


def test_a_built_process_that_starts_by_itself_gets_limits() -> None:
    from awa.blueprints._project import _TRIGGERED_MAX_REQUESTS, TriggerDesign, _finish

    schedule = TriggerDesign(
        kind="schedule", name="monday", cron="0 7 * * MON", input="Report.", sample_payload="{}"
    )
    plain = blueprint("name: x\nagents: {a: do it}\n")

    triggered = _finish(plain, [], None, triggers=[schedule])

    assert triggered.limits is not None
    assert triggered.limits.max_requests == _TRIGGERED_MAX_REQUESTS == 200
    assert not [w for w in messages(triggered.check(), "warning") if "limits" in w]
    assert _finish(plain, [], None).limits is None  # an on-demand process is left alone


def test_awa_run_stops_at_the_cost_limit(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "desk.yaml", "Hello"]) == 1

    err = capsys.readouterr().err
    assert f"error: Usage limit exceeded: cost={REPLY_COST:.4f} > max_cost=0.0001" in err
    assert models.get("default").call_count == 1  # type: ignore[attr-defined]


def test_awa_run_without_limits_prints_the_cost(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "free.yaml").write_text(
        DESK.replace("limits: {max_cost: 0.0001}\n", ""), encoding="utf-8"
    )

    assert main(["run", "free.yaml", "Hello"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "ok"
    assert f"cost {REPLY_COST:.4f}" in captured.err

    assert main(["run", "free.yaml", "Hello", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["cost"] == REPLY_COST


def test_cli_flags_can_only_tighten_the_blueprint_limits(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "wide.yaml").write_text(
        DESK.replace("{max_cost: 0.0001}", "{max_total_tokens: 1000, max_cost: 1}"),
        encoding="utf-8",
    )

    # Looser than the blueprint: the blueprint's 1000 tokens still hold (the reply uses 1200).
    assert main(["run", "wide.yaml", "Hi", "--max-total-tokens", "100000", "--max-cost", "5"]) == 1
    assert "total_tokens=1200 > max_total_tokens=1000" in capsys.readouterr().err
    # Tighter than the blueprint: the cost limit of the flag holds.
    (workspace / "loose.yaml").write_text(
        (workspace / "wide.yaml").read_text().replace("max_total_tokens: 1000, ", ""),
        encoding="utf-8",
    )
    assert main(["run", "loose.yaml", "Hi", "--max-cost", "0.001"]) == 1
    assert "max_cost=0.0010" in capsys.readouterr().err
    # A flag alone limits a blueprint that has no limits.
    assert main(["run", "loose.yaml", "Hi", "--max-requests", "5"]) == 0
    capsys.readouterr()


def test_flags_are_checked(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    free = DESK.replace("limits: {max_cost: 0.0001}\n", "").replace("prices:", "other:")
    (workspace / "bare.yaml").write_text(free.split("other:")[0], encoding="utf-8")

    assert main(["run", "bare.yaml", "Hi", "--max-cost", "1"]) == 1
    assert "--max-cost needs prices" in capsys.readouterr().err
    assert main(["run", "desk.yaml", "Hi", "--max-requests", "0"]) == 1
    assert "--max-requests must be greater than 0" in capsys.readouterr().err
    assert main(["run", "desk.yaml", "Hi", "--max-duration", "soon"]) == 1
    assert "--max-duration" in capsys.readouterr().err


def test_chat_shows_the_limit_error_and_goes_on(workspace: Path, models: ModelRegistry) -> None:
    spec = Blueprint.from_yaml_file(workspace / "desk.yaml")
    system = spec.build(checkpointer=InMemorySaver())
    out, err = io.StringIO(), io.StringIO()
    lines = iter(["first", "second"])

    def read(prompt: str) -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    session = ChatSession(
        system, stream=False, usage_limits=spec.usage_limits(), input=read, out=out, err=err
    )

    assert session.run() == 0
    assert err.getvalue().count("error: UsageLimitExceededError: Usage limit exceeded") == 2
    assert out.getvalue().count("ok") == 0


def test_awa_chat_applies_the_blueprint_limits(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))

    assert main(["chat", "desk.yaml", "--no-stream"]) == 0

    assert "UsageLimitExceededError: Usage limit exceeded: cost=0.0036" in capsys.readouterr().err


CASES = """
name: desk
cases:
  - id: hello
    input: Hello
    expected: ok
    scorers: [contains]
"""


def test_awa_eval_stops_each_run_at_the_limit(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "cases.yaml").write_text(CASES, encoding="utf-8")

    assert main(["eval", "desk.yaml", "cases.yaml", "--json", "report.json"]) == 1

    report = EvalReport.from_json(workspace / "report.json")
    [case] = report.cases
    assert case.status == "error"
    assert case.error is not None and "Usage limit exceeded: cost=0.0036" in case.error
    assert case.usage.requests == 1
    # The blueprint's prices record the cost although no --prices file was given.
    assert case.cost == REPLY_COST
    capsys.readouterr()


def test_awa_eval_flags_tighten_the_blueprint_limits(
    workspace: Path, models: ModelRegistry
) -> None:
    (workspace / "cases.yaml").write_text(CASES, encoding="utf-8")
    (workspace / "free.yaml").write_text(
        DESK.replace("limits: {max_cost: 0.0001}\n", ""), encoding="utf-8"
    )

    assert main(["eval", "free.yaml", "cases.yaml", "--json", "ok.json"]) == 0
    assert (
        main(["eval", "free.yaml", "cases.yaml", "--max-cost", "0.0001", "--json", "x.json"]) == 1
    )
    [case] = EvalReport.from_json(workspace / "x.json").cases
    assert case.error is not None and "max_cost=0.0001" in case.error


def test_the_served_system_stops_at_the_limit_and_ignores_looser_requests(
    workspace: Path, models: ModelRegistry
) -> None:
    from fastapi.testclient import TestClient

    from awa.server import build_blueprint_app

    app = build_blueprint_app("desk.yaml")
    with TestClient(app) as client:
        plain = client.post("/v1/systems/clerk/runs", json={"input": "hi"})
        asked_more = client.post(
            "/v1/systems/clerk/runs", json={"input": "hi", "usage_limits": {"max_requests": 1000}}
        )

    for response in (plain, asked_more):
        assert response.status_code == 429
        assert "max_cost=0.0001" in json.dumps(response.json())


def test_a_request_cannot_loosen_the_blueprint_ceiling() -> None:
    spec = blueprint(DESK.replace("{max_cost: 0.0001}", "{max_total_tokens: 500, max_cost: 0.1}"))
    served = ServedSystem(spec.build(), usage_limits=spec.usage_limits())

    merged = served.limits_for(UsageLimits(max_total_tokens=100000, max_requests=7))

    assert merged is not None
    assert (merged.max_total_tokens, merged.max_requests, merged.max_cost) == (500, 7, 0.1)
    assert merged.prices == spec.price_table()


def test_serve_flags_tighten_the_ceiling(workspace: Path, models: ModelRegistry) -> None:
    from awa.cli._common import limits_from_args

    args = build_parser(["serve", "desk.yaml", "--max-cost", "0.00005"]).parse_args(
        ["serve", "desk.yaml", "--max-cost", "0.00005"]
    )
    limits = limits_from_args(args, Blueprint.from_yaml_file(workspace / "desk.yaml"))

    assert limits is not None and limits.max_cost == 0.00005
    assert UsageLimits.model_validate(limits.model_dump(mode="json")) == limits


TRIGGERED_DESK = DESK.replace(
    "limits: {max_cost: 0.0001}\n",
    "limits: {max_cost: 0.0001}\n"
    "triggers:\n  - name: drop\n    file_drop: {directory: inbox, stable_for: 0}\n"
    "automation:\n  retry: {max_attempts: 3, initial: 1s}\n",
)


def test_awa_worker_fails_a_run_at_the_limit_without_a_retry_and_records_the_cost(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "desk.yaml").write_text(TRIGGERED_DESK, encoding="utf-8")
    (workspace / "inbox").mkdir()
    (workspace / "inbox" / "a.txt").write_text("hello", encoding="utf-8")

    args = build_parser(["worker", "desk.yaml", "--once"]).parse_args(
        ["worker", "desk.yaml", "--once"]
    )
    assert args.handler(args) == 1  # `worker --once` exits 1 when a run failed

    out = capsys.readouterr().out
    assert "failed attempts=1" in out and "tokens=1200" in out and "cost=0.0036" in out
    store = SqliteRunStore(workspace / ".awa" / "awa.db")
    [run] = asyncio_compat.run(store.list())
    asyncio_compat.run(store.close())
    assert (run.status, run.attempts) == ("failed", 1)
    assert run.error_code == UsageLimitExceededError.code
    assert run.usage.requests == 1 and run.usage.total_tokens == 1200
    assert run.cost == REPLY_COST == PriceTable.from_mapping(PRICES).cost(run.usage)
    assert models.get("default").call_count == 1  # type: ignore[attr-defined]


def test_worker_flags_tighten_the_blueprint_limits(workspace: Path, models: ModelRegistry) -> None:
    from awa.cli._automation import _from_blueprint

    (workspace / "desk.yaml").write_text(TRIGGERED_DESK, encoding="utf-8")
    argv = ["worker", "desk.yaml", "--once", "--max-requests", "3", "--max-cost", "5"]
    args = build_parser(argv).parse_args(argv)

    async def build() -> Any:
        from contextlib import AsyncExitStack

        from awa.automation import InMemoryRunStore

        async with AsyncExitStack() as stack:
            return await _from_blueprint(
                workspace / "desk.yaml", args, store=InMemoryRunStore(), stack=stack
            )

    automation = asyncio_compat.run(build())

    assert automation.usage_limits.max_requests == 3
    assert automation.usage_limits.max_cost == 0.0001  # looser than the blueprint: not taken
    assert automation.prices == PriceTable.from_mapping(PRICES)


def test_the_web_app_stops_a_conversation_at_the_limit_and_explains_it(
    workspace: Path, models: ModelRegistry
) -> None:
    from fastapi.testclient import TestClient

    from awa.ui import create_ui_app

    (workspace / "blueprint.yaml").write_text(DESK, encoding="utf-8")
    token, base = "limits-token", "http://127.0.0.1:8765"
    safe = {"Origin": base}
    app = create_ui_app(workspace, token=token, port=8765, environ={}, model_registry=models)

    with TestClient(app, base_url=base) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        body = client.post("/api/run", json={"message": "Hi"}, headers=safe).text

    assert "event: error" in body
    assert "Usage limit exceeded" in body and "limits" in body.split("event: error")[-1]


def test_the_web_app_applies_the_limits_to_its_tests(
    workspace: Path, models: ModelRegistry
) -> None:
    from fastapi.testclient import TestClient

    from awa.ui import create_ui_app

    (workspace / "blueprint.yaml").write_text(DESK, encoding="utf-8")
    (workspace / "evals").mkdir()
    (workspace / "evals" / "cases.yaml").write_text(CASES, encoding="utf-8")
    token, base = "limits-token", "http://127.0.0.1:8765"
    safe = {"Origin": base}
    app = create_ui_app(workspace, token=token, port=8765, environ={}, model_registry=models)

    with TestClient(app, base_url=base) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        body = client.post("/api/test", json={}, headers=safe).text

    assert "Usage limit exceeded" in body and '"passed":0' in body


def test_blueprint_limits_apply_to_runs_made_from_code(models: ModelRegistry) -> None:
    spec = blueprint(DESK)
    system = spec.build()

    with pytest.raises(UsageLimitExceededError, match="max_cost=0.0001"):
        asyncio_compat.run(system.run("Hi", usage_limits=spec.usage_limits()))


def test_a_blueprint_served_as_mcp_tools_keeps_its_limits_as_the_ceiling(
    workspace: Path, models: ModelRegistry
) -> None:
    pytest.importorskip("mcp")
    from awa.server.mcp import build_blueprint_system

    served, _, spec = build_blueprint_system("desk.yaml")

    assert served.usage_limits == spec.usage_limits() is not None


def test_awa_serve_flags_are_honoured_by_the_served_app(
    workspace: Path, models: ModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    from test_server_cli import FakeUvicorn

    import awa.server._app as server_app

    fake = FakeUvicorn()
    monkeypatch.setattr(server_app, "import_uvicorn", lambda: fake)
    (workspace / "free.yaml").write_text(
        DESK.replace("limits: {max_cost: 0.0001}\n", ""), encoding="utf-8"
    )

    assert main(["serve", "free.yaml"]) == 0
    assert main(["serve", "free.yaml", "--max-total-tokens", "100"]) == 0

    [(plain, _), (limited, _)] = fake.calls
    with TestClient(plain, base_url="http://127.0.0.1:8000") as client:
        assert client.post("/v1/systems/clerk/runs", json={"input": "hi"}).status_code == 200
    with TestClient(limited, base_url="http://127.0.0.1:8000") as client:
        refused = client.post("/v1/systems/clerk/runs", json={"input": "hi"})
        looser = client.post(
            "/v1/systems/clerk/runs",
            json={"input": "hi", "usage_limits": {"max_total_tokens": 100000}},
        )
    for response in (refused, looser):
        assert response.status_code == 429
        assert "max_total_tokens=100" in json.dumps(response.json())


async def test_a_job_that_asks_for_more_than_the_ceiling_stops_at_the_ceiling(
    models: ModelRegistry,
) -> None:
    from awa.server.jobs import JobRegistry, execute

    spec = blueprint(DESK)
    registry = JobRegistry({"clerk": ServedSystem(spec.build(), usage_limits=spec.usage_limits())})

    for asked in (None, {"max_requests": 1000, "max_cost": 100}):
        job: dict[str, Any] = {"system": "clerk", "input": "hi"}
        if asked is not None:
            job["usage_limits"] = {**asked, "prices": {"prices": PRICES}}
        result = await execute(job, registry)

        assert result.status == "failed" and result.error is not None
        assert "max_cost=0.0001" in json.dumps(result.error)


async def test_a_blueprint_served_as_mcp_tools_stops_every_run_at_its_limits(
    workspace: Path, models: ModelRegistry
) -> None:
    pytest.importorskip("mcp")
    from test_server_mcp import connect

    from awa.server.mcp import build_blueprint_system, create_mcp_server

    served, _, _ = build_blueprint_system("desk.yaml")
    async with connect(create_mcp_server({"clerk": served})) as session:
        result = await session.call_tool("run_clerk", {"input": "hi"})

    assert result.isError
    assert "max_cost=0.0001" in result.content[0].text  # type: ignore[union-attr]
    assert models.get("default").call_count == 1  # type: ignore[attr-defined]


EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


@pytest.mark.slow
def test_no_example_blueprint_with_triggers_lacks_limits() -> None:
    paths = [*EXAMPLES.rglob("blueprint.yaml"), *(EXAMPLES / "blueprints").glob("*.yaml")]
    assert len(paths) > 10

    unbounded = [
        str(path.relative_to(EXAMPLES))
        for path in paths
        if any(
            "no `limits:`" in w for w in messages(Blueprint.from_yaml_file(path).check(), "warning")
        )
    ]

    assert unbounded == []
