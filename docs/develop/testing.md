# Testing

You test an awa system offline: a scripted chat model plays the model, and the real agent loop runs
around it. No network and no key are needed. This page shows how to set up a test suite, which test
doubles exist, and how to measure a system on a dataset of cases.

`FakeChatModel` and `FunctionChatModel` are real `BaseChatModel`s with tool calling. So tool calls,
structured output, delegation, handoffs and approval pauses all run as in production.

## A test suite without a model

Install pytest and pytest-asyncio with the extra `testing` (`[all]` does not include it):
`python -m pip install ".[testing]"` in the awa folder, or the wheel file with `[testing]` behind its
name.

Turn on async tests in your `pyproject.toml`:

```toml title="pyproject.toml"
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

Block real model requests for every test in `conftest.py`:

```python title="conftest.py"
from collections.abc import Iterator

import pytest

from awa.models import allow_model_requests


@pytest.fixture(autouse=True)
def _no_real_models() -> Iterator[None]:
    with allow_model_requests(False):  # a test can never reach a provider by accident
        yield
```

Write the tests, here in `test_support.py`:

```python title="test_support.py"
from awa import Agent, Team
from awa.automation import InMemoryEventQueue
from awa.contracts import DelegationEvent
from awa.observability import EventRecorder
from awa.testing import FakeChatModel, FakeClock, FixedIds, tool_call


def get_weather(city: str) -> str:
    """Return the current weather for a city."""
    return f"Sunny in {city}."


async def test_the_agent_uses_the_tool() -> None:
    model = FakeChatModel([tool_call("get_weather", {"city": "Berlin"}), "It is sunny in Berlin."])
    agent = Agent("assistant", tools=[get_weather], model=model)

    result = await agent.run("Weather in Berlin?")

    assert result.output == "It is sunny in Berlin."
    assert result.usage.tool_calls == 1
    assert "Sunny in Berlin." in str(model.calls[1].last_message.content)  # what the model saw


async def test_the_lead_delegates_to_the_researcher() -> None:
    researcher = Agent("researcher", description="Finds facts.", model=FakeChatModel(["Heat pumps save 60 %."]))
    lead = Agent(
        "lead",
        description="Coordinates.",
        model=FakeChatModel(
            [tool_call("delegate_task", {"agent": "researcher", "task": "Find one fact."}), "Heat pumps save 60 %."]
        ),
    )
    recorder = EventRecorder()
    team = Team("desk", orchestrator=lead, members=[researcher], hooks=[recorder])

    result = await team.run("One fact about heat pumps?")

    assert result.usage.by_agent["researcher"].requests == 1
    assert [event.target for event in recorder.of_type(DelegationEvent)] == ["researcher"]


def build_support_team() -> Team:  # your production factory, without any model
    triage = Agent("triage", description="Routes requests.")
    billing = Agent("billing", description="Answers billing questions.")
    return Team("support", orchestrator=triage, members=[billing], handoffs={"triage": ["billing"]})


async def test_triage_hands_over_to_billing() -> None:
    system = build_support_team()
    triage, billing = system.orchestrator, system.members[0]

    with triage.override(model=FakeChatModel([tool_call("transfer_to_billing", {"reason": "invoice"})])), \
            billing.override(model=FakeChatModel(["Invoice 5 is paid."])):
        result = await system.run("Is invoice 5 paid?")

    assert (result.agent, result.output) == ("billing", "Invoice 5 is paid.")


async def test_a_delayed_message_appears_when_the_clock_moves() -> None:
    clock = FakeClock()
    queue = InMemoryEventQueue(clock=clock)
    await queue.send({"id": 1}, delay="10m")
    assert await queue.receive() == []  # not yet, and the test does not wait
    clock.advance(minutes=10)
    [message] = await queue.receive()
    assert message.payload == {"id": 1}


def test_ids_are_predictable() -> None:
    ids = FixedIds()
    assert [ids.new_id("evt"), ids.new_id("evt")] == ["evt_000000000001", "evt_000000000002"]
```

Run them with `python -m pytest`. The first test checks one agent and its tool, the second a
delegation, the third a handoff in a team built by your production code, and the last two time and
ids.

### Block real requests

With the fixture in `conftest.py`, any agent that resolves a real provider model fails with
`ModelResolutionError` before a request is made. Offline models stay allowed. Mark the tests that
need a real model (an `integration` marker, for example) and run them separately.

### The doubles

| Double | Use |
| --- | --- |
| `FakeChatModel([...])` | Plays back a script: a final answer (`str`), `tool_call(name, args)`, a list of tool calls at once, `structured_output(Schema, data)`, an `AIMessage`, or a function of the call. An exhausted script raises `AssertionError`; `repeat_last=True` repeats the last item. |
| `FunctionChatModel(function)` | Computes each answer from the `ModelCall` (`messages`, `last_message`, `tool_names`, `index`); handy for routers and judges. |
| `model.calls` | Every call, so a test asserts what the model saw. `stream_chunk_size=` streams text in chunks. |
| `expect_scripts_consumed()` | A context manager that fails when a `FakeChatModel` created inside it was called but still has unreturned items at its end: the test stopped before the run it scripted. A model never called is a placeholder and is not checked. Wrap each test in it from an autouse fixture; `allow_unconsumed=True` exempts one model. |
| `agent.override(model=..., deps=...)` | Swaps the model or the deps of an agent built by your production code, also inside a team. |
| `FakeClock`, `FixedIds` | Time that moves only when told to, and predictable ids, for code that takes a `Clock` or an `IdSource`. |
| `FakeTransport` | The connector `MockTransport` under its testing name: answers connector requests from routes and records them ([Connectors](connectors.md#write-a-connector)). |
| `ToolContract(tool, deps=...)` | Runs one tool as an agent would, approval pauses included ([Tools](tools.md#a-tool-with-its-safety-contract)). |
| `replay_models(path)` | Records model answers once and replays them in later runs. |
| `assert_terminates_via_own_budget(graph, state)` | Proves that a loop of your own graph stops on its own budget. |

Script a handoff as `tool_call("transfer_to_<agent>", {"reason": "..."})` and a consultation as
`tool_call("consult_peer", {"peer": "...", "question": "..."})`. A store, queue or audit sink you
write is held to the contract suite of its backend protocol
([Extend](extend.md#write-a-store-backend)).

## Evaluate a system

Offline tests prove the wiring. An evaluation measures what the system does on realistic requests.
`awa.evals` runs a system on a dataset of cases, scores every run and returns a report:

```python title="evaluate_desk.py"
from awa import Agent
from awa.evals import Dataset, evaluate_sync
from awa.testing import FunctionChatModel

CASES = """
cases:
  - id: refund-status
    input: Where is my refund?
    scorers: ["contains:refund"]
  - id: delivery
    input: When does my parcel arrive?
    scorers: ["contains:Tuesday"]
"""


def build_support_desk() -> Agent:  # a factory: a fresh system for every run
    return Agent("desk", model=FunctionChatModel(lambda call: "Your refund is on its way."))


report = evaluate_sync(build_support_desk, Dataset.from_yaml(CASES), repetitions=2)
print("pass rate", report.pass_rate, "| pass^k", report.pass_hat_k)
for run in report.runs:
    print(run.case_id, run.repetition, run.status)
print(report.passed(min_pass_rate=0.9))
```

```text title="evaluate_desk.out"
pass rate 0.5 | pass^k 0.5
refund-status 0 passed
refund-status 1 passed
delivery 0 failed
delivery 1 failed
False
```

`await evaluate(...)` is the async form. For a blueprint the factory is
`lambda: awa.load("blueprint.yaml", checkpointer=StateInMemorySaver())`. Writing cases, the scorers
and `awa eval` on the command line are in [Test and improve](../guide/test.md); this is the Python
side.

### Scorers

- **Deterministic scorers** decide the same way every time and cost nothing: the answer contains a
  text, a tool was called with arguments (`tool_args_match:<tool>:<mapping>`), a tool never took
  effect without approval (`approval_present:<tool>`), at most `n` tool calls.
- `Custom(function)` wraps a function of the case and the `RunTrace`.
- `ModelJudge(criteria, model=..., threshold=...)` grades what code cannot check. It runs only when
  every deterministic scorer passed, and its usage is reported apart from the system's. Its score
  can vary between calls, so keep the criteria concrete, pin the judge model and use
  `repetitions`.

### Options of a run

- **Runs.** Each run gets a run tree of its own: its usage limits, usage and duration are its own.
  Runs overlap up to `concurrency`; results come in dataset order. A run that raises becomes an
  `"error"` result, and every scorer counts as failed for it. `usage_limits=`, `timeout=`, `deps=`
  and `fail_fast=True` apply to every run.
- **Approvals.** A run that pauses on a gated tool is resumed with the case's `decisions`. Without
  them it is scored as paused.
- **Each case in a folder of its own.** `sandbox=` copies the subfolders of a folder for every run,
  so a process that writes files never sees another case's output. Your tools find the copy with
  `awa.runtime.case_dir()`. A `setup:` function on the dataset or a case prepares the folder;
  evaluating runs that code, so evaluate only datasets you trust.
- **Recorded web search.** `awa eval --search-fixture search.json --record-search` records the
  searches once and replays them in CI.

### Reading the report

- **Reliability and cost.** With `repetitions` of 2 or more, the report has `pass_hat_k` (the share
  of cases that passed every repetition), `pass_at_k` and `consistency()`. It always has
  `latency_p50_ms`, `latency_p95_ms` and `cache_hit_rate`; with `prices=`, a cost per run.
- **Gates.** `report.gate(min_pass_rate=..., min_pass_hat_k=..., max_cost=...)` returns `passed`
  and the readable `reasons` it failed for. `report.passed(...)` is its `passed`.
- **Baselines.** `report.save("baseline.json")`, then
  `report.compare(EvalReport.from_json(...))` lists the cases and scorers that regressed or improved
  (with a `tolerance`, default 0.1).

## What can go wrong

| You see | Cause and fix |
| --- | --- |
| `AssertionError` from `FakeChatModel` about an exhausted script | The agent called the model more often than the script has items. Add items, or pass `repeat_last=True`. |
| `AssertionError` about a script `not fully consumed` | The run ended before the script did, so its last steps were never checked. Assert on them, shorten the script, or pass `allow_unconsumed=True`. |
| `ModelResolutionError: Real model requests are blocked ...` | A test reached an agent without a fake model. Give it `FakeChatModel` or use `override`. |
| `async def functions are not natively supported` | Install `pytest-asyncio` and set `asyncio_mode = auto`. |
| A handoff or delegation tool is not offered to the scripted model | The team does not allow it: add the edge ([Teams](teams.md#a-team-and-its-edges)). |
| `EvalError: Cannot read dataset file 'cases.yaml'` | The path is relative to the working directory of the test run. |

Every script in `examples/` has offline tests in `tests/examples/` built this way.
`examples/07_evaluate.py` shows an evaluation that catches a regression.
