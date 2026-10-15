"""Property tests of the JSON schema converter and the skill frontmatter."""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from awa.blueprints import BlueprintError, model_from_json_schema
from awa.blueprints.json_schema import MAX_SCHEMA_DEPTH
from awa.errors import SkillManifestError
from awa.skills import Skill

_PROPERTY_NAMES = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True).filter(
    lambda name: not hasattr(BaseModel, name)
)
_TEXT = st.text(max_size=20)
_ENUM_VALUES = st.one_of(_TEXT, st.integers(-5, 5), st.booleans(), st.none())


def _optional(**entries: st.SearchStrategy[Any]) -> st.SearchStrategy[dict[str, Any]]:
    return st.fixed_dictionaries({}, optional=entries)


_SCALAR_SCHEMAS = st.one_of(
    st.builds(
        lambda extra: {"type": "string", **extra},
        _optional(
            minLength=st.integers(0, 5),
            maxLength=st.integers(5, 50),
            pattern=st.sampled_from(["^[a-z]+$", "[0-9]", ".*"]),
            description=_TEXT,
            title=_TEXT,
        ),
    ),
    st.builds(
        lambda kind, extra: {"type": kind, **extra},
        st.sampled_from(["number", "integer"]),
        _optional(minimum=st.integers(-100, 0), maximum=st.integers(1, 100)),
    ),
    st.just({"type": "boolean"}),
    st.just({"type": "null"}),
    st.builds(lambda values: {"enum": values}, st.lists(_ENUM_VALUES, min_size=1, max_size=5)),
    st.sampled_from([["string", "null"], ["integer", "number"], ["boolean", "null"]]).map(
        lambda kinds: {"type": kinds}
    ),
)


@st.composite
def _object_schema(draw: st.DrawFn, children: st.SearchStrategy[dict[str, Any]]) -> dict[str, Any]:
    properties = draw(st.dictionaries(_PROPERTY_NAMES, children, min_size=1, max_size=4))
    required = draw(st.lists(st.sampled_from(sorted(properties)), unique=True))
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    if draw(st.booleans()):
        schema["additionalProperties"] = False
    return schema


def _extend(children: st.SearchStrategy[dict[str, Any]]) -> st.SearchStrategy[dict[str, Any]]:
    return st.one_of(
        _object_schema(children),
        st.builds(
            lambda items, extra: {"type": "array", "items": items, **extra},
            children,
            _optional(minItems=st.integers(0, 2), maxItems=st.integers(2, 10)),
        ),
        st.just({"type": "object"}),
        st.just({"type": "array"}),
    )


VALID_SCHEMAS = _object_schema(st.recursive(_SCALAR_SCHEMAS, _extend, max_leaves=12))
"""Root object schemas using only the supported subset."""


_KEYWORDS = st.sampled_from(
    [
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "additionalProperties",
        "default",
        "description",
        "title",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "format",
        "$ref",
        "oneOf",
    ]
)
_TYPE_NAMES = st.sampled_from(["object", "array", "string", "number", "integer", "boolean", "null"])
_LEAVES = st.one_of(
    _TYPE_NAMES,
    st.lists(_TYPE_NAMES, max_size=3),
    st.integers(-3, 3),
    st.floats(allow_nan=True, allow_infinity=True),
    _TEXT,
    st.booleans(),
    st.none(),
    st.sampled_from(["(", "[a-", "^(?=a)", "a{3,1}"]),  # invalid or unsupported regexes
)
SCHEMA_LIKE = st.recursive(
    st.dictionaries(_KEYWORDS, _LEAVES, max_size=4),
    lambda children: st.one_of(
        st.dictionaries(_KEYWORDS, st.one_of(_LEAVES, children), max_size=5),
        st.dictionaries(st.one_of(_PROPERTY_NAMES, _TEXT), children, max_size=3).map(
            lambda props: {"type": "object", "properties": props}
        ),
        st.lists(children, max_size=3).map(lambda items: {"type": "array", "items": items}),
    ),
    max_leaves=20,
)
"""Mappings that look like schemas: mostly-supported keywords with arbitrary values."""


@given(VALID_SCHEMAS)
def test_supported_schemas_always_build_a_usable_model(schema: dict[str, Any]) -> None:
    model = model_from_json_schema(schema, name="Generated")

    assert issubclass(model, BaseModel)
    model.model_json_schema()
    if schema.get("required"):
        with pytest.raises(ValidationError):
            model.model_validate({})
    else:
        assert model.model_validate({}).model_dump() == dict.fromkeys(schema["properties"])


@given(SCHEMA_LIKE, _TEXT)
def test_arbitrary_schemas_build_or_raise_blueprint_error(schema: Any, name: str) -> None:
    try:
        model = model_from_json_schema(schema, name=name)
    except BlueprintError:
        return
    assert issubclass(model, BaseModel)


@given(st.one_of(st.none(), st.integers(), _TEXT, st.lists(st.integers(), max_size=3)))
def test_non_mapping_schemas_raise_blueprint_error(schema: Any) -> None:
    with pytest.raises(BlueprintError):
        model_from_json_schema(schema)


@given(st.integers(MAX_SCHEMA_DEPTH + 1, 5 * MAX_SCHEMA_DEPTH), st.booleans())
def test_deep_schemas_raise_blueprint_error_not_recursion_error(depth: int, arrays: bool) -> None:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(depth - 1):
        schema = (
            {"type": "array", "items": schema}
            if arrays
            else {"type": "object", "properties": {"inner": schema}}
        )
    schema = {"type": "object", "properties": {"inner": schema}}

    with pytest.raises(BlueprintError, match="MAX_SCHEMA_DEPTH"):
        model_from_json_schema(schema)


_NAMES = st.from_regex(r"[a-z0-9]{1,10}(-[a-z0-9]{1,10}){0,3}", fullmatch=True)
# Descriptions: no XML tags, not blank; leading/trailing whitespace is stripped.
_DESCRIPTIONS = st.text(
    st.characters(codec="utf-8", exclude_categories=("Cs", "Cc"), exclude_characters="<>"),
    min_size=1,
    max_size=200,
).filter(str.strip)
# Body lines without exotic line separators (`str.splitlines` splits on those too).
_BODY_LINES = st.lists(
    st.text(st.characters(codec="utf-8", categories=("L", "N", "P", "Zs", "S")), max_size=40),
    min_size=1,
    max_size=8,
).filter(lambda lines: "\n".join(lines).strip())
# PyYAML writes U+0085 (NEL) unescaped inside a quoted scalar and reads it back as a folded
# line break (a space), so that one character cannot round-trip through YAML at all.
_METADATA_VALUES = st.text(st.characters(exclude_characters="\x85"), max_size=10)
_LABELS = st.lists(
    st.from_regex(r"[a-z][a-z0-9_.-]{0,10}", fullmatch=True), unique=True, max_size=4
)


@st.composite
def skill_files(draw: st.DrawFn) -> tuple[str, dict[str, object], list[str]]:
    fields: dict[str, object] = {"name": draw(_NAMES), "description": draw(_DESCRIPTIONS)}
    for key, strategy in (
        ("version", st.sampled_from(["1.0.0", "2.3", "0.1.0-rc1"])),
        ("category", st.sampled_from(["support", "finance"])),
        ("tags", _LABELS),
        ("allowed-tools", _LABELS),
        ("risk", st.sampled_from(["low", "medium", "high"])),
        ("license", st.sampled_from(["MIT", "LICENSE.txt"])),
        ("metadata", st.dictionaries(st.sampled_from(["owner", "team"]), _METADATA_VALUES)),
    ):
        if draw(st.booleans()):
            fields[key] = draw(strategy)
    body = draw(_BODY_LINES)
    frontmatter = yaml.safe_dump(fields, allow_unicode=True, sort_keys=False)
    bom = draw(st.sampled_from(["", "﻿"]))
    newline = draw(st.sampled_from(["\n", "\r\n"]))
    # Split like the parser: only at line feeds, so Unicode line separators stay inside values.
    text = newline.join([f"{bom}---", *frontmatter.rstrip("\n").split("\n"), "---", *body])
    return text, fields, body


@given(skill_files())
def test_well_formed_files_parse_to_their_fields(
    case: tuple[str, dict[str, object], list[str]],
) -> None:
    text, fields, body = case

    skill = Skill.from_markdown(text)

    assert skill.name == fields["name"]
    assert skill.description == str(fields["description"]).strip()
    assert skill.instructions == "\n".join(body).strip()
    assert skill.version == fields.get("version", "1.0.0")
    assert skill.tags == tuple(fields.get("tags", ()))  # type: ignore[arg-type]
    assert skill.allowed_tools == tuple(fields.get("allowed-tools", ()))  # type: ignore[arg-type]
    assert skill.metadata == fields.get("metadata", {})


_FRONTMATTER_VALUES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(),
    st.text(max_size=20),
    st.lists(st.one_of(st.text(max_size=5), st.integers()), max_size=3),
    st.dictionaries(st.text(max_size=5), st.one_of(st.text(max_size=5), st.integers()), max_size=3),
)


@given(
    st.dictionaries(
        st.sampled_from(
            ["name", "description", "version", "tags", "allowed-tools", "risk", "metadata", "x"]
        ),
        _FRONTMATTER_VALUES,
    ),
    st.text(max_size=100),
)
def test_arbitrary_frontmatter_parses_or_raises_manifest_error(
    fields: dict[str, object], body: str
) -> None:
    text = f"---\n{yaml.safe_dump(fields, allow_unicode=True)}---\n{body}"
    with contextlib.suppress(SkillManifestError):
        Skill.from_markdown(text)


@given(st.text(max_size=400))
def test_arbitrary_text_parses_or_raises_manifest_error(text: str) -> None:
    for candidate in (text, f"---\n{text}", f"---\n{text}\n---\nbody"):
        with contextlib.suppress(SkillManifestError):
            Skill.from_markdown(candidate)


def test_impossible_yaml_timestamp_is_a_manifest_error() -> None:
    with pytest.raises(SkillManifestError):
        Skill.from_markdown("---\nname: a\ndescription: 2001-13-01\n---\nbody")
