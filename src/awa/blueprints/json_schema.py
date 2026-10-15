"""Convert a small, safe subset of JSON Schema into a pydantic model.

Supported: `object`, `array`, `string`, `number`, `integer`, `boolean`, `null`, type lists,
`enum`, `default`, `title`, `description` and the range, length, pattern and item-count
constraints. Anything else raises `BlueprintError`, so a schema never means less than it
says. Numbers follow JSON (no booleans, numeric text, NaN or infinity), and the size is
bounded by `MAX_SCHEMA_DEPTH` and `MAX_SCHEMA_NODES`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Union

import pydantic_core
from pydantic import AllowInfNan, BaseModel, ConfigDict, Field, Strict, create_model

from awa.blueprints.errors import BlueprintError

_SCALARS: dict[str, Any] = {
    "string": str,
    # JSON numbers: strict, so `true`, `false` and numeric text are not read as numbers, and
    # finite, as NaN and infinity are not JSON numbers and no check can decide on them.
    "number": Annotated[float, Strict(), AllowInfNan(False)],
    "integer": Annotated[int, Strict()],
    "boolean": bool,
    "null": type(None),
}
_ANNOTATIONS = frozenset({"title", "description", "default", "examples", "$schema"})
_NUMBERS = frozenset({"number", "integer"})
_STRING = frozenset({"string"})
_ARRAY = frozenset({"array"})
# Each constraint's pydantic `Field` argument and the JSON types it applies to; pydantic
# itself would fail only at validation time ("Unable to apply constraint ...").
_CONSTRAINTS = {
    "minimum": ("ge", _NUMBERS),
    "maximum": ("le", _NUMBERS),
    "minLength": ("min_length", _STRING),
    "maxLength": ("max_length", _STRING),
    "pattern": ("pattern", _STRING),
    "minItems": ("min_length", _ARRAY),
    "maxItems": ("max_length", _ARRAY),
}
_OBJECT_KEYWORDS = ("properties", "required", "additionalProperties")
_STRUCTURE = frozenset({"type", "properties", "required", "items", "enum", "additionalProperties"})
_SUPPORTED = _ANNOTATIONS | _STRUCTURE | frozenset(_CONSTRAINTS)
_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

_MODELS: dict[str, type[BaseModel]] = {}
"""Generated models by class name (the newest of a name) and by their qualified name, which is
unique per schema, so a checkpoint and a pickle both find their class again (see `__getattr__`)."""

MAX_SCHEMA_DEPTH = 32
"""Deepest nesting accepted: the root is level 1, each property or `items` schema (and each
value nested in `default` or `examples`) adds one."""

MAX_SCHEMA_NODES = 1_000
"""Most nodes accepted: every subschema, `enum` value and value inside `default` or `examples`."""


def model_from_json_schema(schema: Mapping[str, Any], *, name: str = "Output") -> type[BaseModel]:
    """Build a pydantic model from an object JSON schema.

    Generated models forbid unknown fields; properties not in `required` default to `None`
    (or their `default`). Nested objects are named `<name><Property>`.

    Raises:
        BlueprintError: If the schema uses an unsupported keyword or type, a property name
            is not an identifier, the root is not an object, or a size limit is exceeded.

    Example:
        ```python
        Invoice = model_from_json_schema(
            {"type": "object", "properties": {"total": {"type": "number"}}}, name="Invoice"
        )
        ```
    """

    if not isinstance(schema, Mapping):
        raise BlueprintError("An output schema must be a JSON schema with 'type: object'")
    _check_limits(schema)
    if _types(schema, "$") != ["object"]:
        raise BlueprintError("An output schema must be a JSON schema with 'type: object'")
    _check_applicable(schema, ["object"], "$")
    model = _object_model(schema, class_name(name), "$")
    _MODELS[model.__name__] = model
    return model


def __getattr__(name: str) -> type[BaseModel]:
    """A generated model by class name, so pickled or checkpointed instances import again."""

    try:
        return _MODELS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def _object_model(schema: Mapping[str, Any], name: str, path: str) -> type[BaseModel]:
    _check_keywords(schema, path)
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping) or not properties:
        raise BlueprintError(f"{path}: an object schema needs a non-empty 'properties' mapping")
    if schema.get("additionalProperties", False) is not False:
        raise BlueprintError(f"{path}: only 'additionalProperties: false' is supported")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise BlueprintError(f"{path}: 'required' must be a list of property names")
    unknown = sorted(set(required) - set(properties))
    if unknown:
        raise BlueprintError(f"{path}: 'required' names unknown properties: {', '.join(unknown)}")

    digest = hashlib.sha1(
        json.dumps(schema, sort_keys=True, default=repr).encode(), usedforsecurity=False
    ).hexdigest()[:12]
    unique = f"{name}_{digest}"
    if unique in _MODELS:
        return _MODELS[unique]
    fields: dict[str, Any] = {}
    for prop, sub in properties.items():
        prop_path = f"{path}.{prop}"
        if not isinstance(prop, str) or not _FIELD_NAME.fullmatch(prop):
            raise BlueprintError(
                f"{prop_path}: property names must start with a letter and contain only "
                "letters, digits and '_'"
            )
        if hasattr(BaseModel, prop):
            raise BlueprintError(f"{prop_path}: property name {prop!r} is reserved")
        if not isinstance(sub, Mapping):
            raise BlueprintError(f"{prop_path}: a property schema must be a mapping")
        annotation = _annotation(sub, f"{name}{class_name(prop)}", prop_path)
        field_kwargs = _field_kwargs(sub, prop_path)
        default = field_kwargs.pop("default", None)  # a required property has none
        if prop in required:
            fields[prop] = (annotation, Field(**field_kwargs))
        else:
            fields[prop] = (annotation | None, Field(default=default, **field_kwargs))
    try:
        model: type[BaseModel] = create_model(
            name,
            __config__=ConfigDict(extra="forbid"),
            __module__=__name__,
            __doc__=schema.get("description") or schema.get("title"),
            **fields,
        )
    except (pydantic_core.SchemaError, TypeError, ValueError) as exc:
        reason = str(exc).strip().splitlines()[-1].strip()
        raise BlueprintError(
            f"{path}: the schema cannot be turned into a model: {reason}"
        ) from None
    model.__qualname__ = unique
    _MODELS[unique] = model
    return model


def _annotation(schema: Mapping[str, Any], name: str, path: str) -> Any:
    _check_keywords(schema, path)
    kinds = _types(schema, path)
    _check_applicable(schema, kinds, path)
    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or not values:
            raise BlueprintError(f"{path}: 'enum' must be a non-empty list")
        if not all(isinstance(v, str | int | float | bool) or v is None for v in values):
            raise BlueprintError(f"{path}: 'enum' values must be strings, numbers or booleans")
        wrong = [v for v in values if kinds and not _json_types(v) & set(kinds)]
        if wrong:
            raise BlueprintError(
                f"{path}: 'enum' values {wrong!r} do not match the type {schema['type']!r}"
            )
        return Literal[tuple(values)]
    members: list[Any] = []
    for kind in kinds:
        if kind == "object":
            if "properties" in schema:
                members.append(_object_model(schema, name, path))
            else:
                if schema.get("required"):
                    raise BlueprintError(f"{path}: 'required' needs a 'properties' mapping")
                if schema.get("additionalProperties", True) is not True:
                    raise BlueprintError(
                        f"{path}: an object without 'properties' accepts any keys; "
                        "'additionalProperties' must be omitted or true"
                    )
                members.append(dict[str, Any])
        elif kind == "array":
            items = schema.get("items")
            if items is None:
                members.append(list[Any])
            elif isinstance(items, Mapping):
                members.append(list[_annotation(items, f"{name}Item", f"{path}[]")])  # type: ignore[misc]  # an annotation built at run time
            else:
                raise BlueprintError(f"{path}: 'items' must be a single schema")
        else:
            members.append(_SCALARS[kind])
    return members[0] if len(members) == 1 else Union[tuple(members)]  # noqa: UP007


def _json_types(value: Any) -> set[str]:
    """The JSON types an enum value has; an integer is also a number."""

    if value is None:
        return {"null"}
    if isinstance(value, bool):
        return {"boolean"}
    if isinstance(value, str):
        return {"string"}
    return {"integer", "number"} if isinstance(value, int) else {"number"}


def _types(schema: Mapping[str, Any], path: str) -> list[str]:
    raw = schema.get("type")
    if raw is None:
        if "enum" in schema:
            return []
        raise BlueprintError(f"{path}: every schema needs a 'type' (or an 'enum')")
    kinds = raw if isinstance(raw, list) else [raw]
    if not kinds:
        raise BlueprintError(f"{path}: 'type' must not be an empty list")
    for kind in kinds:
        if not isinstance(kind, str) or (kind not in _SCALARS and kind not in ("object", "array")):
            raise BlueprintError(f"{path}: unsupported type {kind!r}")
    return list(dict.fromkeys(kinds))


def _check_applicable(schema: Mapping[str, Any], kinds: list[str], path: str) -> None:
    """Reject keywords that cannot apply to the schema's types instead of ignoring them."""

    for keyword in ("description", "title"):
        if keyword in schema and not isinstance(schema[keyword], str):
            raise BlueprintError(f"{path}: {keyword!r} must be a string")
    concrete = [kind for kind in kinds if kind != "null"]
    for keyword, (_, applicable) in _CONSTRAINTS.items():
        if keyword in schema and ("enum" in schema or not all(k in applicable for k in concrete)):
            raise BlueprintError(
                f"{path}: {keyword!r} does not apply to "
                f"{'an enum' if 'enum' in schema else 'type ' + ' | '.join(kinds)}; "
                f"it applies to {' or '.join(sorted(applicable))} only"
            )
    if "items" in schema and "array" not in kinds:
        raise BlueprintError(f"{path}: 'items' only applies to type array")
    for keyword in _OBJECT_KEYWORDS:
        if keyword in schema and "object" not in kinds:
            raise BlueprintError(f"{path}: {keyword!r} only applies to type object")


def _field_kwargs(schema: Mapping[str, Any], path: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if "description" in schema:
        kwargs["description"] = str(schema["description"])
    if "default" in schema:
        kwargs["default"] = schema["default"]
    for keyword, (argument, _) in _CONSTRAINTS.items():
        if keyword in schema:
            value = schema[keyword]
            valid = _is_pattern(value) if keyword == "pattern" else _is_number(value)
            if not valid:
                hint = (
                    " (not a regular expression pydantic can compile; lookaround and "
                    "backreferences are unsupported)"
                    if keyword == "pattern" and isinstance(value, str)
                    else ""
                )
                raise BlueprintError(f"{path}: invalid value for {keyword!r}: {value!r}{hint}")
            kwargs[argument] = value
    return kwargs


def _check_limits(schema: Mapping[str, Any]) -> None:
    """Enforce `MAX_SCHEMA_DEPTH` and `MAX_SCHEMA_NODES` iteratively; terminates on self-containing input."""

    size = 0
    stack: list[tuple[Mapping[str, Any], str, int]] = [(schema, "$", 1)]
    while stack:
        node, path, depth = stack.pop()
        if depth > MAX_SCHEMA_DEPTH:
            raise BlueprintError(
                f"{path}: the schema is nested deeper than {MAX_SCHEMA_DEPTH} levels "
                "(MAX_SCHEMA_DEPTH)"
            )
        size += 1
        enum = node.get("enum")
        if isinstance(enum, list):
            size += len(enum)
        for keyword in ("default", "examples"):
            if keyword in node:
                size += _value_size(node[keyword], f"{path}.{keyword}")
        if size > MAX_SCHEMA_NODES:
            raise BlueprintError(
                f"{path}: the schema has more than {MAX_SCHEMA_NODES} nodes (subschemas, "
                "enum values and default/example values; MAX_SCHEMA_NODES)"
            )
        properties = node.get("properties")
        if isinstance(properties, Mapping):
            stack.extend(
                (sub, f"{path}.{prop}", depth + 1)
                for prop, sub in properties.items()
                if isinstance(sub, Mapping)
            )
        items = node.get("items")
        if isinstance(items, Mapping):
            stack.append((items, f"{path}[]", depth + 1))


def _value_size(value: Any, path: str) -> int:
    """Count the lists, mappings and scalars in a `default` or `examples` value."""

    size = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack and size <= MAX_SCHEMA_NODES:  # beyond that, the caller reports the size
        item, depth = stack.pop()
        size += 1
        if isinstance(item, Mapping | list | tuple):
            if depth > MAX_SCHEMA_DEPTH:
                raise BlueprintError(
                    f"{path}: the value is nested deeper than {MAX_SCHEMA_DEPTH} levels "
                    "(MAX_SCHEMA_DEPTH)"
                )
            children = item.values() if isinstance(item, Mapping) else item
            stack.extend((child, depth + 1) for child in children)
    return size


def _check_keywords(schema: Mapping[str, Any], path: str) -> None:
    unsupported = sorted(str(key) for key in schema if key not in _SUPPORTED)
    if unsupported:
        raise BlueprintError(
            f"{path}: unsupported JSON schema keyword(s): {', '.join(unsupported)} "
            f"(supported: {', '.join(sorted(_SUPPORTED))})"
        )


def _is_pattern(value: Any) -> bool:
    """Whether `value` is a regular expression pydantic can compile."""

    if not isinstance(value, str):
        return False
    try:
        pydantic_core.SchemaValidator(pydantic_core.core_schema.str_schema(pattern=value))
    except pydantic_core.SchemaError:
        return False
    return True


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def class_name(text: str) -> str:
    """A PascalCase class name for `text`: `order line` -> `OrderLine`."""

    parts = re.split(r"[^A-Za-z0-9]+", text)
    joined = "".join(part[:1].upper() + part[1:] for part in parts if part)
    return joined if joined[:1].isalpha() else f"Model{joined}"


__all__ = ["MAX_SCHEMA_DEPTH", "MAX_SCHEMA_NODES", "model_from_json_schema"]
