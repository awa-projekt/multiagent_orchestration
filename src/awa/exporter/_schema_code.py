"""The structured output of an agent as readable pydantic source.

`model_source` writes the model `awa.blueprints.model_from_json_schema` builds for an
`output_schema`, as Python classes with the same field types, constraints and strictness, so
the exported agent accepts and rejects the same answers. `sample` makes one valid answer, which
the generated tests use as the model's scripted output.
"""

from __future__ import annotations

import keyword
from collections.abc import Mapping
from typing import Any

from awa.blueprints.json_schema import class_name

_SCALARS = {
    "string": "str",
    "number": "Annotated[float, Strict(), AllowInfNan(False)]",
    "integer": "Annotated[int, Strict()]",
    "boolean": "bool",
    "null": "None",
}
_CONSTRAINTS = {
    "minimum": "ge",
    "maximum": "le",
    "minLength": "min_length",
    "maxLength": "max_length",
    "pattern": "pattern",
    "minItems": "min_length",
    "maxItems": "max_length",
}


def _types(schema: Mapping[str, Any]) -> list[str]:
    raw = schema.get("type")
    return list(dict.fromkeys(raw if isinstance(raw, list) else [raw]))


def keywords(schema: Mapping[str, Any]) -> list[str]:
    """The property names in `schema` that are Python keywords (they cannot become fields)."""

    found: list[str] = []
    for sub in (schema.get("properties") or {}).values():
        found += keywords(sub)
    items = schema.get("items")
    if isinstance(items, Mapping):
        found += keywords(items)
    return sorted({*found, *(p for p in schema.get("properties") or {} if keyword.iskeyword(p))})


def model_source(schema: Mapping[str, Any], name: str) -> str:
    """Python source of the pydantic classes for the object `schema`; the root class is `name`.

    Args:
        schema: A valid `output_schema` (the blueprint check has accepted it).
        name: Class name of the root model; nested objects are named `<name><Property>`.
    """

    classes: list[str] = []
    _object(schema, name, classes)
    return "\n\n\n".join(classes)


def _object(schema: Mapping[str, Any], name: str, classes: list[str]) -> str:
    required = set(schema.get("required", []))
    lines: list[str] = []
    for prop, sub in schema["properties"].items():
        annotation = _annotation(sub, f"{name}{class_name(prop)}", classes)
        arguments = _field_arguments(sub)
        if prop in required:
            default = "" if not arguments else f" = Field({', '.join(arguments)})"
        else:
            kept = [a for a in arguments if not a.startswith("default=")]
            default_value = next(
                (a.removeprefix("default=") for a in arguments if a.startswith("default=")), "None"
            )
            annotation = annotation if annotation == "None" else f"{annotation} | None"
            default = f" = Field({', '.join([f'default={default_value}', *kept])})"
        lines.append(f"    {prop}: {annotation}{default}")
    doc = schema.get("description") or schema.get("title")
    head = f'    """{doc}"""\n\n' if doc and '"' not in doc and "\\" not in doc else ""
    config = '    model_config = ConfigDict(extra="forbid")\n\n' if head or lines else ""
    classes.append(f"class {name}(BaseModel):\n{head}{config}" + "\n".join(lines))
    return name


def _annotation(schema: Mapping[str, Any], name: str, classes: list[str]) -> str:
    if "enum" in schema:
        return f"Literal[{', '.join(repr(value) for value in schema['enum'])}]"
    members: list[str] = []
    for kind in _types(schema):
        if kind == "object":
            members.append(
                _object(schema, name, classes) if "properties" in schema else "dict[str, Any]"
            )
        elif kind == "array":
            items = schema.get("items")
            members.append(
                "list[Any]"
                if items is None
                else f"list[{_annotation(items, f'{name}Item', classes)}]"
            )
        else:
            members.append(_SCALARS[kind])
    return " | ".join(members)


def _field_arguments(schema: Mapping[str, Any]) -> list[str]:
    arguments = []
    if "description" in schema:
        arguments.append(f"description={str(schema['description'])!r}")
    if "default" in schema:
        arguments.append(f"default={schema['default']!r}")
    arguments += [
        f"{argument}={schema[keyword_]!r}"
        for keyword_, argument in _CONSTRAINTS.items()
        if keyword_ in schema
    ]
    return arguments


def sample(schema: Mapping[str, Any]) -> Any:
    """One value that satisfies `schema` as far as the keywords allow (text, numbers, lists)."""

    if "enum" in schema:
        return schema["enum"][0]
    kinds = [kind for kind in _types(schema) if kind != "null"] or ["null"]
    kind = kinds[0]
    if kind == "object":
        required = schema.get("required", [])
        return {
            prop: sample(sub)
            for prop, sub in (schema.get("properties") or {}).items()
            if prop in required
        }
    if kind == "array":
        items = schema.get("items")
        return [sample(items)] * schema.get("minItems", 0) if items else []
    if kind == "string":
        return "x" * max(schema.get("minLength", 1), 1)
    if kind == "integer":
        return int(schema.get("minimum", 0))
    if kind == "number":
        return float(schema.get("minimum", 0))
    if kind == "boolean":
        return True
    return None
