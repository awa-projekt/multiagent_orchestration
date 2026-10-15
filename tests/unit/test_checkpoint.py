"""Checkpointers, and structured outputs that survive a checkpoint round trip."""

from __future__ import annotations

import dataclasses
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import ormsgpack
import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde import _msgpack
from pydantic import BaseModel, ValidationError

from awa._internal.upstream.checkpoint import (
    BaseCheckpointSaver,
    StateInMemorySaver,
    register_state_type,
    state_serde,
)
from awa.agents import Agent
from awa.blueprints import model_from_json_schema
from awa.core.settings import Settings
from awa.errors import MissingConfigurationError, OptionalDependencyError
from awa.persistence import SqliteCheckpointer
from awa.persistence.checkpointer import open_checkpointer
from awa.testing import FunctionChatModel, ModelCall, structured_output, tool_call
from awa.tools import create_tool_approval_wrapper
from awa.workflows import Sequential

SCHEMA = {
    "type": "object",
    "properties": {"events": {"type": "array", "items": {"type": "string"}}},
    "required": ["events"],
}
SENT: list[str] = []


class Verdict(BaseModel):
    """A hand-written output type (importable, like any user model)."""

    ok: bool


class Unlisted(BaseModel):
    """Never an agent's output type, so nothing registers it."""

    n: int


class Late(BaseModel):
    """Becomes an agent's output type only inside a test."""

    n: int


@tool
def send(text: str) -> str:
    """Send a text."""

    SENT.append(text)
    return f"sent {text!r}"


@pytest.fixture(autouse=True)
def _clear() -> None:
    SENT.clear()


@pytest.fixture(params=[False, True], ids=["permissive", "strict"])
def strict(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(_msgpack, "STRICT_MSGPACK_ENABLED", request.param)
    return bool(request.param)


def build(saver: BaseCheckpointSaver[Any], output_type: type[BaseModel]) -> Sequential:
    """The workflow of a blueprint: a structured stage, then a stage that needs approval."""

    def draft(call: ModelCall) -> Any:
        return structured_output(output_type, _data(output_type))

    def mail(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "mailer done"
        return tool_call("send", {"text": "hello"})

    builder = Agent("builder", model=FunctionChatModel(draft), output_type=output_type)
    mailer = Agent(
        "mailer", model=FunctionChatModel(mail), tools=[create_tool_approval_wrapper(send)]
    )
    return Sequential("pipeline", [builder, mailer], checkpointer=saver)


def _data(output_type: type[BaseModel]) -> dict[str, Any]:
    return {"ok": True} if output_type is Verdict else {"events": ["start", "stop"]}


def stored_output(saver: BaseCheckpointSaver[Any], step: str) -> Any:
    state = saver.get_tuple({"configurable": {"thread_id": "t"}})
    assert state is not None
    return state.checkpoint["channel_values"]["_steps"][step]["output"]


@pytest.fixture(params=["sqlite", "memory"])
def make_saver(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    """A factory of checkpointers on the same storage (a file, or one object for memory)."""

    memory = StateInMemorySaver()
    opened: list[SqliteCheckpointer] = []

    def make() -> BaseCheckpointSaver[Any]:
        if request.param == "memory":
            return memory
        saver = SqliteCheckpointer(tmp_path / "checkpoints.db")
        opened.append(saver)
        return saver

    yield make
    for saver in opened:
        saver.close()


@pytest.mark.parametrize("generated", [True, False], ids=["generated model", "own model"])
async def test_structured_output_is_revived_as_its_model_after_a_restart(
    make_saver: Any, strict: bool, generated: bool, caplog: pytest.LogCaptureFixture
) -> None:
    def model() -> type[BaseModel]:
        return model_from_json_schema(SCHEMA, name="builder_output") if generated else Verdict

    paused = await build(make_saver(), model()).run("Go.", thread_id="t")
    assert paused.interrupts and SENT == []

    # The restart: a new model class of the same name, a new checkpointer, a new workflow.
    restarted = model()
    saver = make_saver()
    with caplog.at_level(logging.WARNING):
        revived = stored_output(saver, "builder")
        done = await build(saver, restarted).resume("t", {"action": "approve"})

    assert type(revived) is restarted
    assert revived.model_dump() == _data(restarted)
    assert done.output == "mailer done" and SENT == ["hello"]
    assert not [
        r for r in caplog.records if "msgpack" in r.getMessage() or "unregistered" in r.getMessage()
    ]


def test_a_class_nobody_registered_is_blocked_whatever_the_environment_says(strict: bool) -> None:
    serde = state_serde()
    revived = serde.loads_typed(serde.dumps_typed(Unlisted(n=1)))
    assert revived == {"n": 1}


def test_an_agent_built_after_the_serializer_still_registers_its_output_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_msgpack, "STRICT_MSGPACK_ENABLED", True)
    serde = state_serde()
    blob = serde.dumps_typed(Late(n=2))
    assert serde.loads_typed(blob) == {"n": 2}

    Agent("late", model=FunctionChatModel(lambda call: "x"), output_type=Late)

    assert serde.loads_typed(blob) == Late(n=2)


def _crafted_blob(directory: Path) -> tuple[str, bytes]:
    """What a writer of the checkpoint store could plant: a call of `tempfile.mkdtemp`."""

    call = ormsgpack.packb(("tempfile", "mkdtemp", ("", "planted_", str(directory))))
    return "msgpack", ormsgpack.packb(ormsgpack.Ext(1, call))


@pytest.mark.parametrize("env_strict", [False, True], ids=["permissive env", "strict env"])
@pytest.mark.parametrize("kind", ["serde", "memory", "sqlite"])
def test_a_crafted_checkpoint_blob_cannot_call_a_function(
    kind: str, env_strict: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_msgpack, "STRICT_MSGPACK_ENABLED", env_strict)
    planted = tmp_path / "planted"
    planted.mkdir()
    if kind == "sqlite":
        saver = SqliteCheckpointer(tmp_path / "c.db")
        serde = saver.serde
    else:
        serde = state_serde() if kind == "serde" else StateInMemorySaver().serde
    revived = serde.loads_typed(_crafted_blob(planted))

    assert list(planted.iterdir()) == []  # mkdtemp never ran
    assert list(revived) == ["", "planted_", str(planted)]  # the arguments come back as plain data
    if kind == "sqlite":
        saver.close()


def test_the_serializer_of_a_saver_with_a_registered_type_stays_closed_to_other_callables(
    tmp_path: Path,
) -> None:
    register_state_type(Late)
    serde = state_serde()
    assert serde.loads_typed(serde.dumps_typed(Late(n=3))) == Late(n=3)
    planted = tmp_path / "planted"
    planted.mkdir()
    serde.loads_typed(_crafted_blob(planted))
    assert list(planted.iterdir()) == []


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def test_settings_rejects_an_unknown_checkpointer_backend() -> None:
    with pytest.raises(ValidationError, match="literal_error"):
        _settings(checkpointer_backend="redis")


async def test_open_checkpointer_memory_backend_is_usable_as_a_saver() -> None:
    async with open_checkpointer(_settings(checkpointer_backend="memory")) as checkpointer:
        assert isinstance(checkpointer, InMemorySaver)


async def test_open_checkpointer_postgres_without_dsn_fails_clearly() -> None:
    with pytest.raises(MissingConfigurationError, match="AWA_POSTGRES_DSN"):
        async with open_checkpointer(_settings(checkpointer_backend="postgres", postgres_dsn=None)):
            pass


async def test_open_checkpointer_postgres_without_extra_installed_fails_clearly() -> None:
    with (
        patch.dict(sys.modules, {"langgraph.checkpoint.postgres.aio": None}),
        pytest.raises(OptionalDependencyError, match="optional extra"),
    ):
        async with open_checkpointer(
            _settings(checkpointer_backend="postgres", postgres_dsn="postgresql://localhost/db")
        ):
            pass


@dataclasses.dataclass
class _Planted:
    value: int


def test_node_cache_does_not_revive_unregistered_classes() -> None:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from awa._internal.upstream.caches import NodeCache

    store: dict[str, bytes] = {}
    encoding, payload = JsonPlusSerializer().dumps_typed(_Planted(1))
    store["ns/key"] = encoding.encode("ascii") + b"\n" + payload
    cache = NodeCache(store.get, lambda key, value, ttl: store.__setitem__(key, value))

    found = cache.get([(("ns",), "key")])

    assert not any(isinstance(value, _Planted) for value in found.values())
