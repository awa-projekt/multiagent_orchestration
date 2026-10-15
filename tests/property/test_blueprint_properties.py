"""Property tests of blueprints and special-case tables."""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from awa.blueprints import Blueprint, BlueprintError
from awa.models import ModelRegistry
from awa.testing import FakeChatModel
from awa.workflows import SpecialCases


@tool
def lookup(order_id: str) -> str:
    """Look up an order."""
    return order_id


@tool
def refund(order_id: str) -> str:
    """Refund an order."""
    return order_id


@tool
def notify(text: str) -> str:
    """Notify the customer."""
    return text


TOOLS = [lookup, refund, notify]
TOOL_NAMES = [t.name for t in TOOLS]


_NAMES = st.from_regex(r"[a-z][a-z0-9_]{0,10}", fullmatch=True).filter(
    lambda name: name not in ("team", "workflow")
)
# Printable text including line breaks, tabs and non-ASCII: exercises YAML quoting
# and the block style `to_yaml` uses for multi-line strings.
_TEXT = st.text(
    st.characters(codec="utf-8", exclude_categories=("Cs", "Cc"), include_characters="\n\t"),
    max_size=60,
)


@st.composite
def _agents(draw: st.DrawFn, names: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": draw(_TEXT.filter(str.strip)),
            "instructions": draw(_TEXT),
            "tools": draw(st.lists(st.sampled_from(TOOL_NAMES), unique=True, max_size=3)),
            "capabilities": draw(st.lists(_NAMES, unique=True, max_size=2)),
        }
        for name in names
    ]


def _workflow(names: list[str]) -> st.SearchStrategy[Any]:
    """A workflow tree that references every agent at least once."""

    leaves = st.sampled_from(names)
    tree = st.recursive(
        leaves,
        lambda children: st.one_of(
            st.lists(children, min_size=1, max_size=3).map(lambda steps: {"sequential": steps}),
            st.lists(leaves, min_size=1, max_size=3, unique=True).map(
                lambda branches: {"parallel": branches}
            ),
            st.builds(
                lambda step, marker: {"loop": step, "until_contains": marker, "max_iterations": 2},
                children,
                st.sampled_from(["DONE", "APPROVED"]),
            ),
        ),
        max_leaves=6,
    )
    return tree.map(lambda node: {"sequential": [*names, node]})


@st.composite
def valid_blueprints(draw: st.DrawFn) -> dict[str, Any]:
    names = draw(st.lists(_NAMES, min_size=1, max_size=4, unique=True))
    system = draw(_NAMES.filter(lambda name: name not in names))
    data: dict[str, Any] = {
        "name": system,
        "description": draw(_TEXT),
        "version": draw(st.sampled_from(["1.0.0", "2.1", "0.3.0-beta"])),
        "agents": draw(_agents(names)),
    }
    shape = draw(st.sampled_from(["agent", "team", "workflow"]))
    if shape == "team" and len(names) >= 2:
        orchestrator, *members = names
        data["team"] = {
            "orchestrator": orchestrator,
            "members": members,
            "task_board": draw(st.booleans()),
            "blackboard": draw(st.booleans()),
        }
        if len(members) >= 2:
            data["team"]["peers"] = {members[0]: members[1:]}
    elif shape == "workflow":
        data["workflow"] = draw(_workflow(names))
    else:
        data["entrypoint"] = names[0]
    used = sorted({t for agent in data["agents"] for t in agent["tools"]})
    if used and shape != "workflow":
        gated = draw(st.lists(st.sampled_from(used), unique=True, max_size=2))
        data["approvals"] = [{"tool": name} for name in gated]
    return data


# Building compiles LangGraph graphs, so build properties use a fifth of the profile's examples.
_BUILD_SETTINGS = settings(max_examples=max(10, settings().max_examples // 5))


def _registry() -> ModelRegistry:
    registry = ModelRegistry()
    registry.register("default", FakeChatModel(["ok"]))
    return registry


@given(valid_blueprints())
def test_dict_round_trip_is_lossless(data: dict[str, Any]) -> None:
    blueprint = Blueprint.from_dict(data)

    assert Blueprint.from_dict(blueprint.to_dict()) == blueprint


@given(valid_blueprints())
def test_yaml_round_trip_is_lossless(data: dict[str, Any]) -> None:
    blueprint = Blueprint.from_dict(data)

    assert Blueprint.from_yaml(blueprint.to_yaml()) == blueprint


def _build(blueprint: Blueprint) -> Any:
    # Workflows do not persist state and refuse a checkpointer; everything else may pause.
    checkpointer = None if blueprint.resolved_entrypoint() == "workflow" else InMemorySaver()
    return blueprint.build(tools=TOOLS, model_registry=_registry(), checkpointer=checkpointer)


@pytest.mark.slow
@_BUILD_SETTINGS
@given(valid_blueprints())
def test_valid_blueprints_check_without_errors_and_build(data: dict[str, Any]) -> None:
    blueprint = Blueprint.from_dict(data)

    errors = [issue for issue in blueprint.check(tools=TOOLS) if issue.severity == "error"]
    assert errors == []
    system = _build(blueprint)
    assert system.name in (blueprint.name, *blueprint.active_agents())


# Names that are valid, reserved, invalid or colliding, so references dangle and clash.
_ANY_NAME = st.one_of(
    st.sampled_from(["a", "b", "c", "team", "workflow", "", "1bad", "x" * 49, "has space"]),
    st.text(max_size=8),
)
_ANY_TOOL = st.one_of(
    st.sampled_from(
        [*TOOL_NAMES, "mcp:srv", "mcp:srv/t", "mcp:", "mcp:srv/", "mcp:x/y/z", "pkg.mod:attr", ":"]
    ),
    st.text(max_size=8),
)
_SCALARS = st.one_of(
    st.none(), st.booleans(), st.integers(-2, 30), st.floats(), st.text(max_size=8)
)
_EDGES = st.dictionaries(_ANY_NAME, st.lists(_ANY_NAME, max_size=3), max_size=3)


def _any_workflow() -> st.SearchStrategy[Any]:
    return st.recursive(
        _ANY_NAME,
        lambda children: st.one_of(
            st.fixed_dictionaries(
                {"sequential": st.lists(children, max_size=3)},
                optional={
                    "carry": st.sampled_from(["previous", "all", "bogus"]),
                    "name": _ANY_NAME,
                },
            ),
            st.fixed_dictionaries(
                {"parallel": st.lists(children, max_size=3)},
                optional={"max_concurrency": _SCALARS, "name": _ANY_NAME},
            ),
            st.fixed_dictionaries(
                {"loop": children},
                optional={
                    "until": st.sampled_from(["pkg.mod:done", "not a ref", ""]),
                    "until_contains": st.text(max_size=5),
                    "max_iterations": _SCALARS,
                },
            ),
        ),
        max_leaves=8,
    )


_ANY_AGENT = st.fixed_dictionaries(
    {"name": _ANY_NAME},
    optional={
        "description": st.text(max_size=10),
        "tools": st.lists(_ANY_TOOL, max_size=4),
        "skills": st.lists(st.sampled_from(["prompt-injection-awareness", "nope"]), max_size=3),
        "max_steps": _SCALARS,
        "output_schema": st.one_of(
            st.just({"type": "object", "properties": {"a": {"type": "string"}}}),
            st.dictionaries(st.text(max_size=5), _SCALARS, max_size=3),
        ),
        "memory": st.fixed_dictionaries(
            {}, optional={"scope": st.sampled_from(["agent", "team", "bogus"])}
        ),
        "unknown_key": _SCALARS,
    },
)
ARBITRARY_BLUEPRINT_DATA = st.fixed_dictionaries(
    {"name": _ANY_NAME, "agents": st.lists(_ANY_AGENT, max_size=4)},
    optional={
        "version": _SCALARS,
        "entrypoint": _ANY_NAME,
        "team": st.fixed_dictionaries(
            {"orchestrator": _ANY_NAME, "members": st.lists(_ANY_NAME, max_size=4)},
            optional={
                "name": _ANY_NAME,
                "delegates": _EDGES,
                "peers": _EDGES,
                "handoffs": _EDGES,
                "limits": st.dictionaries(
                    st.sampled_from(["max_handoffs", "delegation_timeout", "bogus"]), _SCALARS
                ),
            },
        ),
        "workflow": _any_workflow(),
        "approvals": st.lists(
            st.fixed_dictionaries(
                {"tool": _ANY_TOOL}, optional={"when": st.sampled_from(["pkg.mod:f", "bad"])}
            ),
            max_size=3,
        ),
        "mcp_servers": st.lists(
            st.fixed_dictionaries(
                {"name": _ANY_NAME, "command": st.just("server")},
                optional={"include": st.lists(st.sampled_from(["t", "x*"]), max_size=2)},
            ),
            max_size=2,
        ),
    },
)
"""Blueprint-shaped data with random names, dangling references and wrong value types."""


@pytest.mark.slow
@given(ARBITRARY_BLUEPRINT_DATA)
def test_check_never_crashes_on_arbitrary_data(data: dict[str, Any]) -> None:
    try:
        blueprint = Blueprint.from_dict(data)
    except BlueprintError as exc:
        assert exc.issues
        return
    issues = blueprint.check(tools=TOOLS)
    assert all(issue.severity in ("error", "warning") for issue in issues)
    blueprint.check()  # without a tool catalog
    blueprint.to_mermaid()
    assert Blueprint.from_dict(blueprint.to_dict()) == blueprint


@_BUILD_SETTINGS
@given(ARBITRARY_BLUEPRINT_DATA)
def test_build_raises_only_blueprint_error(data: dict[str, Any]) -> None:
    try:
        blueprint = Blueprint.from_dict(data)
    except BlueprintError:
        return
    errors = [issue for issue in blueprint.check(tools=TOOLS) if issue.severity == "error"]
    try:
        _build(blueprint)
    except BlueprintError:
        return
    assert errors == [], "build() accepted a blueprint that check() rejects"


@given(st.text(max_size=300))
def test_from_yaml_raises_only_blueprint_error_on_arbitrary_text(text: str) -> None:
    with contextlib.suppress(BlueprintError):
        Blueprint.from_yaml("\n" + text)  # the newline keeps it from being read as a path


@given(st.dictionaries(st.text(max_size=5), _SCALARS, max_size=5) | _SCALARS)
def test_from_dict_rejects_anything_but_blueprints_with_blueprint_error(data: Any) -> None:
    with pytest.raises(BlueprintError):
        Blueprint.from_dict(data)


def test_impossible_yaml_timestamp_is_a_blueprint_error() -> None:
    with pytest.raises(BlueprintError):
        Blueprint.from_yaml("name: x\nagents: [{name: a}]\ndescription: 2001-13-01\n")


FIELDS = ("amount", "supplier", "count")
LIMITS = st.sampled_from([0, 1, 100, 5000, 20000, 4999.99, -3])
TEXTS = st.sampled_from(["Acme", "acme", "Bolt", "paid", ""])

CLEAR_VALUES = st.one_of(
    st.integers(-10, 30000), st.floats(-1e6, 1e6, allow_nan=False), TEXTS, st.booleans()
)
ALWAYS_UNCLEAR = st.sampled_from([None, float("nan"), float("inf"), -float("inf")])
"""Unclear in every comparison: missing, empty, not a finite number."""
UNCLEAR_NUMBERS = st.one_of(
    ALWAYS_UNCLEAR, st.sampled_from(["5.000", "5,000", "n/a", [1], {"a": 1}, True, False])
)
"""Unclear where a row compares the value with numbers only."""
CLEAR_NUMBERS = st.one_of(
    st.integers(-10, 30000), st.floats(-1e6, 1e6, allow_nan=False), st.sampled_from(["4999"])
)
ANY_VALUE = st.one_of(CLEAR_VALUES, UNCLEAR_NUMBERS, st.sampled_from(["4999", "20.000,5"]))


@st.composite
def conditions(draw: st.DrawFn, numbers_only: bool) -> dict[str, Any]:
    field = draw(st.sampled_from(FIELDS))
    op = draw(st.sampled_from(["=", "!=", "<", "<=", ">", ">=", "one of", "not one of"]))
    other = LIMITS if numbers_only else st.one_of(LIMITS, TEXTS)
    if op in ("<", "<=", ">", ">="):
        value: Any = draw(LIMITS)
    elif op in ("one of", "not one of"):
        value = draw(st.lists(other, min_size=1, max_size=3))
    else:
        value = draw(other if numbers_only else st.one_of(other, st.booleans()))
    return {"field": field, "op": op, "value": value}


@st.composite
def expressions(draw: st.DrawFn) -> str:
    parts = [
        f"{draw(st.sampled_from(FIELDS))} {draw(st.sampled_from(['<', '>=', '==']))} "
        f"{draw(LIMITS)!r}"
        for _ in range(draw(st.integers(1, 3)))
    ]
    joiners = [draw(st.sampled_from([" and ", " or "])) for _ in parts[1:]]
    text = parts[0]
    for joiner, part in zip(joiners, parts[1:], strict=True):
        text += joiner + part
    return text


@st.composite
def tables(draw: st.DrawFn, numbers_only: bool = False) -> SpecialCases:
    rows = []
    for _ in range(draw(st.integers(1, 6))):
        condition: Any = draw(
            st.one_of(st.lists(conditions(numbers_only), min_size=1, max_size=3), expressions())
        )
        rows.append({"if": condition, "then": draw(st.sampled_from(["approve", "ask", "reject"]))})
    return SpecialCases.model_validate(
        {
            "cases": rows,
            "else": draw(st.sampled_from(["approve", "ask", "reject"])),
            "when_unclear": draw(st.sampled_from(["ask", "reject"])),
        }
    )


records = st.dictionaries(st.sampled_from(FIELDS), ANY_VALUE)


@given(tables(), records)
def test_unknown_never_falls_through(table: SpecialCases, values: dict[str, Any]) -> None:
    """The outcome is the first row that is not false: true decides, unknown goes to
    `when_unclear`; `else` only when every row is false."""

    outcome = table.decide(values)
    for number in range(1, len(table.cases) + 1):
        holds, reason = table.check_row(number, values)
        if holds is False:
            continue
        if holds is None:
            assert (outcome.source, outcome.row) == ("unclear", number)
            assert outcome.action == table.when_unclear and reason
        else:
            assert (outcome.source, outcome.row) == ("row", number)
            assert outcome.action == table.cases[number - 1]
        break
    else:
        assert outcome.source == "else" and outcome.action == table.else_


@given(tables(), records)
def test_an_unclear_value_never_approves_on_its_own(
    table: SpecialCases, values: dict[str, Any]
) -> None:
    outcome = table.decide(values)
    if outcome.source == "unclear":
        assert outcome.then in ("ask", "reject")


def _same_when_clear(
    table: SpecialCases, values: dict[str, Any], field: str, unclear: Any, clear: Any
) -> None:
    with_unclear = {**values, field: unclear}
    for number in range(1, len(table.cases) + 1):
        holds, _ = table.check_row(number, with_unclear)
        if holds is not None:
            assert table.check_row(number, {**values, field: clear})[0] is holds


@given(tables(), records, st.sampled_from(FIELDS), ALWAYS_UNCLEAR, CLEAR_VALUES)
def test_a_decided_row_did_not_depend_on_an_unclear_value(
    table: SpecialCases, values: dict[str, Any], field: str, unclear: Any, clear: Any
) -> None:
    """A row that is true or false with a missing or non-finite value in `field` is the same with
    any clear value there: falling past a false row never skips a case the value could have hit."""

    _same_when_clear(table, values, field, unclear, clear)


@given(tables(numbers_only=True), records, st.sampled_from(FIELDS), UNCLEAR_NUMBERS, CLEAR_NUMBERS)
def test_where_only_numbers_are_compared_text_lists_and_true_false_are_unclear(
    table: SpecialCases, values: dict[str, Any], field: str, unclear: Any, clear: Any
) -> None:
    """With numbers only in the rows, `5.000`, `n/a`, a list, an object or true/false in `field`
    never decides a row differently from a clear number."""

    _same_when_clear(table, values, field, unclear, clear)
