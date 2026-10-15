"""Loading blueprints (explicit loaders, format version, limits, safe YAML) and the JSON schema to pydantic converter with its limits."""

from __future__ import annotations

import asyncio
import os
import pickle
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from _helpers.polling import SUBPROCESS_TIMEOUT
from awa.blueprints import Blueprint, BlueprintError, load, model_from_json_schema
from awa.blueprints.json_schema import MAX_SCHEMA_DEPTH, MAX_SCHEMA_NODES
from awa.blueprints.schema import BLUEPRINT_FORMAT_VERSION, MAX_BLUEPRINT_BYTES

TEXT = "name: triage\nagents:\n  - name: classifier\n    description: Labels tickets.\n"


@pytest.fixture
def blueprint_file(tmp_path: Path) -> Path:
    path = tmp_path / "triage.yaml"
    path.write_text(TEXT, encoding="utf-8")
    return path


def test_from_yaml_file_and_text_load_the_same_blueprint(blueprint_file: Path) -> None:
    from_file = Blueprint.from_yaml_file(blueprint_file)

    assert from_file == Blueprint.from_yaml_file(str(blueprint_file))
    assert from_file == Blueprint.from_yaml_text(TEXT)
    assert from_file.format_version == BLUEPRINT_FORMAT_VERSION == "awa/v1"


def test_from_yaml_follows_a_fixed_rule(
    blueprint_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(blueprint_file.parent)

    assert Blueprint.from_yaml(blueprint_file).name == "triage"  # a Path is a file
    assert Blueprint.from_yaml("triage.yaml").name == "triage"  # a one-line string is a path
    assert Blueprint.from_yaml(TEXT).name == "triage"  # a line break makes it text
    one_line = "{name: inline, agents: [{name: a}]}"
    assert Blueprint.from_yaml(one_line).name == "inline"  # flow style is text


def test_a_mistyped_path_is_reported_as_not_found(tmp_path: Path) -> None:
    with pytest.raises(BlueprintError, match="Blueprint file not found") as caught:
        Blueprint.from_yaml(str(tmp_path / "triage.ymll"))
    assert "from_yaml_text" in str(caught.value)
    assert isinstance(caught.value.__cause__, FileNotFoundError)

    with pytest.raises(BlueprintError, match="Blueprint file not found"):
        Blueprint.from_yaml("blueprints/triage")  # no extension, not YAML text
    with pytest.raises(BlueprintError, match="resolved to"):
        Blueprint.from_yaml_file("missing.yaml")
    with pytest.raises(BlueprintError, match="Blueprint file not found"):
        load(str(tmp_path / "missing.yaml"))


def test_a_directory_is_not_a_blueprint_file(tmp_path: Path) -> None:
    with pytest.raises(BlueprintError, match="is a directory"):
        Blueprint.from_yaml_file(tmp_path)


def test_a_one_line_key_value_string_is_yaml_text() -> None:
    with pytest.raises(BlueprintError, match="Invalid blueprint") as caught:
        Blueprint.from_yaml("name: triage")
    assert {issue.path for issue in caught.value.issues} == {"agents"}


def test_non_utf8_files_fail_clearly(tmp_path: Path) -> None:
    path = tmp_path / "latin1.yaml"
    path.write_bytes("name: caf\xe9\nagents: [{name: a}]\n".encode("latin-1"))

    with pytest.raises(BlueprintError, match="not valid UTF-8"):
        Blueprint.from_yaml_file(path)


def test_a_utf8_bom_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "bom.yaml"
    path.write_bytes(b"\xef\xbb\xbf" + TEXT.encode("utf-8"))

    assert Blueprint.from_yaml_file(path).name == "triage"


def test_wrong_argument_types_are_type_errors() -> None:
    with pytest.raises(TypeError):
        Blueprint.from_yaml_text(b"name: x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Blueprint.from_yaml(42)  # type: ignore[arg-type]


def test_format_version_is_written_first_and_round_trips() -> None:
    blueprint = Blueprint.from_yaml_text(TEXT)

    assert list(blueprint.to_dict())[0] == "format_version"
    assert blueprint.to_yaml().startswith("format_version: awa/v1\nname: triage\n")
    assert Blueprint.from_yaml_text(blueprint.to_yaml()) == blueprint


def test_an_explicit_known_format_version_is_accepted() -> None:
    assert Blueprint.from_yaml_text(f"format_version: awa/v1\n{TEXT}").name == "triage"


def test_an_unknown_future_format_version_asks_for_an_upgrade() -> None:
    with pytest.raises(
        BlueprintError, match=r"Unsupported blueprint format_version 'awa/v2'.*upgrade awa"
    ):
        Blueprint.from_yaml_text(f"format_version: awa/v2\n{TEXT}")


@pytest.mark.parametrize("value", ["1", "v1", "awa/1", "awa/v0", "AWA/V1"])
def test_a_malformed_format_version_is_rejected(value: str) -> None:
    with pytest.raises(BlueprintError, match="Invalid blueprint format_version"):
        Blueprint.from_dict({"format_version": value, "name": "x", "agents": [{"name": "a"}]})


def test_a_numeric_format_version_is_rejected() -> None:
    with pytest.raises(BlueprintError, match="Invalid blueprint format_version 1"):
        Blueprint.from_yaml_text(f"format_version: 1\n{TEXT}")


def test_direct_construction_validates_the_format_version() -> None:
    with pytest.raises(ValueError, match="awa/v3"):
        Blueprint(format_version="awa/v3", name="x", agents=[{"name": "a"}])  # type: ignore[list-item]


def test_files_over_the_size_limit_are_rejected_before_parsing(tmp_path: Path) -> None:
    path = tmp_path / "huge.yaml"
    padding = "#" * (MAX_BLUEPRINT_BYTES + 1)
    path.write_text(f"{TEXT}{padding}\n", encoding="utf-8")

    with pytest.raises(BlueprintError, match="larger than MAX_BLUEPRINT_BYTES"):
        Blueprint.from_yaml_file(path)
    with pytest.raises(BlueprintError, match="larger than MAX_BLUEPRINT_BYTES"):
        Blueprint.from_yaml_text(f"{TEXT}{padding}")


def test_text_size_counts_utf8_bytes() -> None:
    text = TEXT + "# " + "é" * (MAX_BLUEPRINT_BYTES // 2) + "\n"

    assert len(text) < MAX_BLUEPRINT_BYTES < len(text.encode("utf-8"))
    with pytest.raises(BlueprintError, match="larger than MAX_BLUEPRINT_BYTES"):
        Blueprint.from_yaml_text(text)


def test_alias_expansion_bombs_are_rejected() -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    lines += [f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]" for i in range(1, 9)]
    bomb = "\n".join(lines) + "\nname: bomb\nagents: [{name: a, tags: *a8}]\n"

    assert len(bomb) < 2_000
    with pytest.raises(BlueprintError, match="more than 100,000 values"):
        Blueprint.from_yaml_text(bomb)


def test_self_referencing_aliases_are_rejected() -> None:
    with pytest.raises(BlueprintError, match="nests deeper|more than"):
        Blueprint.from_yaml_text("loop: &loop [*loop]\nname: x\nagents: [{name: a}]\n")


def test_deep_nesting_is_rejected() -> None:
    deep = "[" * 5_000 + "]" * 5_000
    with pytest.raises(BlueprintError, match="nests deeper"):
        Blueprint.from_yaml_text(f"name: x\nagents: [{{name: a}}]\nextra: {deep}\n")


def test_python_object_tags_are_not_constructed() -> None:
    text = f"{TEXT}description: !!python/object/apply:os.system [echo pwned]\n"

    with pytest.raises(BlueprintError, match="Malformed blueprint YAML"):
        Blueprint.from_yaml_text(text)


def test_anchors_and_merge_keys_still_work() -> None:
    text = (
        "name: merged\n"
        "agents:\n"
        "  - &base {name: a, description: Shared., max_steps: 3}\n"
        "  - {<<: *base, name: b}\n"
    )

    blueprint = Blueprint.from_yaml_text(text)

    assert blueprint.agent("b").description == "Shared."
    assert blueprint.agent("b").max_steps == 3


def test_the_version_text_is_kept_as_written() -> None:
    blueprint = Blueprint.from_yaml_text(f"version: 1.10\n{TEXT}")

    assert blueprint.version == "1.10"


def test_load_accepts_a_path_like(blueprint_file: Path) -> None:
    class _PathLike(os.PathLike[str]):
        def __fspath__(self) -> str:
            return str(blueprint_file)

    assert Blueprint.from_yaml(_PathLike()).name == "triage"


def test_blueprint_import_leaves_the_build_loop_unloaded() -> None:
    probe = (
        "import sys; from awa import Blueprint; "
        "print(sorted(m for m in sys.modules if m.startswith('awa.blueprints.')))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    loaded = completed.stdout.strip().splitlines()[-1]
    for heavy in ("_build_loop", "_project", "architect", "_build'", "auto"):
        assert f"awa.blueprints.{heavy}" not in loaded


def test_every_listed_name_resolves() -> None:
    import awa.blueprints as blueprints

    for name in dir(blueprints):
        assert getattr(blueprints, name) is not None or name.startswith("_")
    with pytest.raises(AttributeError):
        _ = blueprints.no_such_name


def test_unknown_build_options_are_rejected(blueprint_file: Path) -> None:
    blueprint = Blueprint.from_yaml(blueprint_file)

    with pytest.raises(BlueprintError, match="Unknown build option.*checkpoint"):
        blueprint.build(checkpoint=None)  # type: ignore[call-arg]
    with pytest.raises(BlueprintError, match="Unknown build option.*task_board"):
        blueprint.build_agent("classifier", task_board=None)  # type: ignore[call-arg]
    with pytest.raises(BlueprintError, match="Unknown build option.*tool"):
        load(blueprint_file, tool=[])  # type: ignore[call-arg]


async def test_unknown_async_build_options_are_rejected(blueprint_file: Path) -> None:
    from awa.blueprints import aload

    with pytest.raises(BlueprintError, match="Unknown build option.*models"):
        await Blueprint.from_yaml(blueprint_file).abuild(models=None)  # type: ignore[call-arg]
    with pytest.raises(BlueprintError, match="Unknown build option.*models"):
        await aload(blueprint_file, models=None)  # type: ignore[call-arg]


INVOICE: dict[str, Any] = {
    "type": "object",
    "description": "An extracted invoice.",
    "properties": {
        "vendor": {"type": "string", "description": "Vendor name.", "minLength": 1},
        "total": {"type": "number", "minimum": 0},
        "lines": {"type": "integer", "maximum": 100, "default": 1},
        "paid": {"type": "boolean"},
        "currency": {"enum": ["EUR", "USD"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "address": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "zip": {"type": "string", "pattern": "^[0-9]+$"},
            },
            "required": ["city"],
        },
        "extra": {"type": "object"},
        "note": {"type": ["string", "null"]},
    },
    "required": ["vendor", "total"],
}


def test_converts_scalars_nested_objects_arrays_and_enums() -> None:
    model = model_from_json_schema(INVOICE, name="invoice")

    value = model(
        vendor="ACME",
        total=12.5,
        currency="EUR",
        tags=["a"],
        address={"city": "Berlin", "zip": "10115"},
        extra={"k": 1},
    )

    assert model.__name__ == "Invoice"
    assert model.__doc__ == "An extracted invoice."
    dumped = value.model_dump()
    assert dumped["vendor"] == "ACME"
    assert dumped["lines"] == 1
    assert dumped["paid"] is None
    assert dumped["address"] == {"city": "Berlin", "zip": "10115"}
    assert type(value.address).__name__ == "InvoiceAddress"  # type: ignore[attr-defined]
    schema = model.model_json_schema()
    assert schema["required"] == ["vendor", "total"]
    assert schema["properties"]["vendor"]["description"] == "Vendor name."


@pytest.mark.parametrize(
    "data",
    [
        {"total": 1},
        {"vendor": "", "total": 1},
        {"vendor": "a", "total": -1},
        {"vendor": "a", "total": 1, "lines": 101},
        {"vendor": "a", "total": 1, "currency": "GBP"},
        {"vendor": "a", "total": 1, "tags": ["1", "2", "3", "4"]},
        {"vendor": "a", "total": 1, "address": {"zip": "1"}},
        {"vendor": "a", "total": 1, "address": {"city": "x", "zip": "abc"}},
        {"vendor": "a", "total": 1, "unknown": True},
    ],
)
def test_generated_model_enforces_the_schema(data: dict[str, Any]) -> None:
    model = model_from_json_schema(INVOICE)
    with pytest.raises(ValidationError):
        model(**data)


def test_nullable_type_list_accepts_none() -> None:
    model = model_from_json_schema(
        {"type": "object", "properties": {"n": {"type": ["integer", "null"]}}, "required": ["n"]}
    )
    assert model(n=None).model_dump() == {"n": None}
    assert model(n=3).model_dump() == {"n": 3}


SAME_LINE = {
    "type": "object",
    "properties": {"line": {"type": "object", "properties": {"n": {"type": "integer"}}}},
}


def test_instances_pickle_after_another_schema_takes_the_same_name() -> None:
    first = model_from_json_schema(SAME_LINE, name="same_name")
    kept = first.model_validate({"line": {"n": 1}})
    second = model_from_json_schema(
        {"type": "object", "properties": {"other": {"type": "string"}}}, name="same_name"
    )

    assert pickle.loads(pickle.dumps(kept)) == kept
    assert first.__name__ == second.__name__ == "SameName"
    assert model_from_json_schema(SAME_LINE, name="same_name") is first


def test_a_required_property_with_a_default_stays_required() -> None:
    model = model_from_json_schema(
        {
            "type": "object",
            "properties": {"n": {"type": "integer", "default": 1}},
            "required": ["n"],
        }
    )
    with pytest.raises(ValidationError):
        model()
    assert model(n=2).model_dump() == {"n": 2}


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"type": "string"}, "type: object"),
        ({"type": "object", "properties": {}}, "non-empty 'properties'"),
        ({"type": "object", "properties": {"a": {"type": "string", "format": "email"}}}, "format"),
        ({"type": "object", "properties": {"a": {"$ref": "#/x"}}}, "$ref"),
        ({"type": "object", "properties": {"a": {"type": "date"}}}, "unsupported type"),
        ({"type": "object", "properties": {"a": {}}}, "needs a 'type'"),
        ({"type": "object", "properties": {"bad-name": {"type": "string"}}}, "property names"),
        ({"type": "object", "properties": {"schema": {"type": "string"}}}, "reserved"),
        ({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]}, "unknown"),
        (
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "additionalProperties": True,
            },
            "additionalProperties",
        ),
        ({"type": "object", "properties": {"a": {"enum": []}}}, "non-empty list"),
        (
            {"type": "object", "properties": {"a": {"type": "integer", "enum": ["low", 2]}}},
            "do not match the type",
        ),
        (
            {"type": "object", "properties": {"a": {"type": "array", "items": [{}]}}},
            "single schema",
        ),
        ({"type": "object", "properties": {"a": {"type": "number", "minimum": "0"}}}, "minimum"),
    ],
)
def test_unsupported_or_invalid_schemas_raise(schema: dict[str, Any], message: str) -> None:
    with pytest.raises(BlueprintError, match=message.replace("$", r"\$")):
        model_from_json_schema(schema)


def nested_objects(levels: int) -> dict[str, Any]:
    """An object schema `levels` deep (the root counts as one level)."""

    schema: dict[str, Any] = {"type": "object", "properties": {"leaf": {"type": "string"}}}
    for _ in range(levels - 2):
        schema = {"type": "object", "properties": {"inner": schema}}
    return schema


def nested_arrays(levels: int) -> dict[str, Any]:
    items: dict[str, Any] = {"type": "string"}
    for _ in range(levels - 2):
        items = {"type": "array", "items": items}
    return {"type": "object", "properties": {"values": items}}


@pytest.mark.parametrize("build", [nested_objects, nested_arrays])
def test_the_deepest_allowed_schema_builds(build: Any) -> None:
    model = model_from_json_schema(build(MAX_SCHEMA_DEPTH))
    assert model.model_json_schema()["type"] == "object"


@pytest.mark.parametrize("build", [nested_objects, nested_arrays])
def test_one_level_deeper_raises_blueprint_error(build: Any) -> None:
    with pytest.raises(BlueprintError, match=r"nested deeper than 32 levels \(MAX_SCHEMA_DEPTH\)"):
        model_from_json_schema(build(MAX_SCHEMA_DEPTH + 1))


def test_very_deep_schemas_fail_fast_without_recursion_error() -> None:
    with pytest.raises(BlueprintError, match="MAX_SCHEMA_DEPTH"):
        model_from_json_schema(nested_objects(50_000))


def test_a_schema_that_contains_itself_raises_blueprint_error() -> None:
    schema: dict[str, Any] = {"type": "object", "properties": {}}
    schema["properties"]["self"] = schema

    with pytest.raises(BlueprintError, match="MAX_SCHEMA_DEPTH"):
        model_from_json_schema(schema)


def test_too_many_subschemas_raise_blueprint_error() -> None:
    wide = {
        "type": "object",
        "properties": {f"p{index}": {"type": "string"} for index in range(MAX_SCHEMA_NODES)},
    }

    with pytest.raises(BlueprintError, match=r"more than 1000 nodes .*MAX_SCHEMA_NODES"):
        model_from_json_schema(wide)


def test_a_schema_at_the_size_limit_builds() -> None:
    at_limit = {
        "type": "object",
        "properties": {f"p{index}": {"type": "string"} for index in range(MAX_SCHEMA_NODES - 1)},
    }

    assert len(model_from_json_schema(at_limit).model_fields) == MAX_SCHEMA_NODES - 1


def test_enum_values_count_towards_the_size() -> None:
    schema = {"type": "object", "properties": {"code": {"enum": list(range(MAX_SCHEMA_NODES))}}}

    with pytest.raises(BlueprintError, match="MAX_SCHEMA_NODES"):
        model_from_json_schema(schema)


def test_default_values_are_bounded_in_depth_and_size() -> None:
    deep: list[Any] = []
    for _ in range(MAX_SCHEMA_DEPTH + 1):
        deep = [deep]
    too_deep = {"type": "object", "properties": {"x": {"type": "array", "default": deep}}}
    too_big = {
        "type": "object",
        "properties": {"x": {"type": "array", "default": list(range(MAX_SCHEMA_NODES))}},
    }

    with pytest.raises(BlueprintError, match=r"\$\.x\.default: the value is nested deeper"):
        model_from_json_schema(too_deep)
    with pytest.raises(BlueprintError, match="MAX_SCHEMA_NODES"):
        model_from_json_schema(too_big)


def test_blueprint_check_reports_an_oversized_output_schema() -> None:
    blueprint = Blueprint.from_dict(
        {"name": "solo", "agents": [{"name": "clerk", "output_schema": nested_objects(40)}]}
    )

    [issue] = [issue for issue in blueprint.check() if issue.severity == "error"]
    assert issue.path == "agents[0].output_schema"
    assert "MAX_SCHEMA_DEPTH" in issue.message


PROJECT_BLUEPRINT = """\
name: desk
agents:
  - name: clerk
    description: Answers order questions.
    instructions: Look the order up.
    tools: [lookup_order]
    skills: [house-style]
"""

PROJECT_TOOLS = '''\
from langchain_core.tools import tool


@tool
def lookup_order(order_id: str) -> str:
    """Look an order up."""
    return f"order {order_id} shipped"
'''

PROJECT_SKILL = """\
---
name: house-style
description: Writes answers in the house style. Use for every customer answer.
---
Answer in one sentence.
"""


@pytest.fixture
def project_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "desk"
    (folder / "skills" / "house-style").mkdir(parents=True)
    (folder / "blueprint.yaml").write_text(PROJECT_BLUEPRINT, encoding="utf-8")
    (folder / "tools.py").write_text(PROJECT_TOOLS, encoding="utf-8")
    (folder / "skills" / "house-style" / "SKILL.md").write_text(PROJECT_SKILL, encoding="utf-8")
    return folder


@pytest.mark.parametrize("source", ["folder", "blueprint file"])
def test_load_reads_a_project_folder_like_the_commands(
    project_folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    from awa.models import ModelRegistry
    from awa.testing import FakeChatModel, tool_call

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "tools", raising=False)
    model = FakeChatModel([tool_call("lookup_order", {"order_id": "1002"}), "It shipped."])
    models = ModelRegistry()
    models.register("default", model)
    target = project_folder if source == "folder" else project_folder / "blueprint.yaml"

    system = load(str(target), model_registry=models)
    result = asyncio.run(system.run("Where is order 1002?"))

    assert result.output == "It shipped."
    assert "order 1002 shipped" in str(model.calls[1].messages[-1].content)


def test_given_tools_and_skills_replace_those_of_the_folder(project_folder: Path) -> None:
    from awa.skills import SkillRegistry

    with pytest.raises(BlueprintError, match="house-style"):
        load(project_folder, tools=[], skill_registry=SkillRegistry.builtin())


def test_a_folder_without_a_blueprint_names_the_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BlueprintError, match="blueprint.yaml"):
        load(tmp_path)


def test_the_commands_read_the_skills_folder_beside_the_blueprint(
    project_folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from awa.cli import main

    assert main(["validate", str(project_folder / "blueprint.yaml")]) == 0
    assert "house-style" not in capsys.readouterr().out
