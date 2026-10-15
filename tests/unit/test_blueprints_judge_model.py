"""The build's judges run on the fast profile with low reasoning effort (a fixed pass/fail
rubric needs no deep reasoning)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from awa.blueprints._build_loop import judge_model, low_effort
from awa.models import ModelRegistry
from awa.testing import FakeChatModel

langchain_openai = pytest.importorskip("langchain_openai")


@pytest.fixture
def registry() -> Iterator[ModelRegistry]:
    saved = ModelRegistry._default
    ModelRegistry._default = ModelRegistry()
    try:
        yield ModelRegistry._default
    finally:
        ModelRegistry._default = saved


def openai(name: str, **options: object) -> object:
    return langchain_openai.ChatOpenAI(model=name, api_key="test-placeholder", **options)


def test_a_reasoning_model_judges_with_low_effort() -> None:
    judged = low_effort(openai("o4-mini"))

    assert judged.reasoning_effort == "low"  # type: ignore[attr-defined]


def test_other_models_and_a_chosen_effort_stay_as_they_are() -> None:
    plain = openai("gpt-4.1-mini")
    chosen = openai("gpt-5", reasoning_effort="high")
    fake = FakeChatModel(["ok"])

    assert low_effort(plain) is plain and low_effort(chosen) is chosen
    assert low_effort(fake) is fake


def test_the_fast_profile_judges_when_the_build_names_no_model(registry: ModelRegistry) -> None:
    fast = FakeChatModel(["fast"])
    registry.register("fast", fast)
    own = FakeChatModel(["own"])

    assert judge_model(None) is fast
    assert judge_model("default") is fast
    assert judge_model(own) is own  # the build's own model judges its own builds


def test_without_a_fast_profile_the_build_model_judges(registry: ModelRegistry) -> None:
    default = FakeChatModel(["default"])
    registry.register("default", default)

    assert judge_model(None) is default
