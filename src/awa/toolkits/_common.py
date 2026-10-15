"""Shared building blocks of the builtin toolkits: tool construction, caps, secrets."""

from __future__ import annotations

import json
from collections.abc import Callable, Container, Mapping, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from awa._internal.install import install_hint, safe_find_spec
from awa._internal.install import require_module as require_module
from awa._internal.logging import get_logger
from awa._internal.upstream.tools import BaseTool, StructuredTool, ToolException
from awa.core.config import env_value as project_value
from awa.errors import ConfigurationError, OptionalDependencyError
from awa.tools.approval import (
    APPROVAL_METADATA_KEY,
    ApprovalPredicate,
    create_tool_approval_wrapper,
)
from awa.tools.hardening import FRAMED_METADATA_KEY, cap_text, frame_tool_output
from awa.tools.risk import RISK_METADATA_KEY, ToolRisk


def toolkits_install_hint() -> str:
    """The command that installs the `files` extra (document and spreadsheet readers)."""

    return install_hint("files")


TOOLKIT_METADATA_KEY = "awa_toolkit"
"""Tool metadata key naming the toolkit a builtin tool belongs to."""

DEFAULT_MAX_OUTPUT_CHARS = 20_000
"""Default cap on the text a tool hands back to the model."""

M = TypeVar("M", bound=BaseModel)


class ToolArgs(BaseModel):
    """Base of every builtin tool's argument schema: unknown arguments are errors."""

    model_config = ConfigDict(extra="forbid")


def tool_json(value: Any) -> str:
    """Serialize a tool result as compact, readable JSON (`NaN` and infinity become `null`)."""

    plain = json.loads(json.dumps(value, default=str), parse_constant=lambda _: None)
    return json.dumps(plain, ensure_ascii=False, indent=1, allow_nan=False)


def positive(value: float | None, name: str) -> None:
    """Raise `ConfigurationError` unless `value` is `None` or positive."""

    if value is not None and value <= 0:
        raise ConfigurationError(f"{name} must be positive, got {value!r}")


def env_value(name: str, *, what: str) -> str:
    """Read a secret or setting from the environment at call time.

    Raises:
        ToolException: If the variable is unset or empty; the message names
            only the variable, never a value.
    """

    value = project_value(name)
    if not value:
        raise ToolException(f"{what} is not configured: environment variable {name} is not set")
    return value


def secret_value(value: SecretStr | str | None) -> str | None:
    """Unwrap a `SecretStr` (or plain string)."""

    if value is None:
        return None
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def extra_installed(module: str) -> bool:
    """Return whether the optional module `module` can be imported."""

    return safe_find_spec(module) is not None


def selected(names: Sequence[str], include: Sequence[str] | None, toolkit: str) -> list[str]:
    """The tool names of a toolkit that `include` keeps, in toolkit order.

    Raises:
        ConfigurationError: If `include` names a tool the toolkit does not have.
    """

    if include is None:
        return list(names)
    unknown = [name for name in include if name not in names]
    if unknown:
        raise ConfigurationError(
            f"the {toolkit} toolkit has no tool(s) {', '.join(unknown)} (has: {', '.join(names)})"
        )
    return [name for name in names if name in include]


def build_tool(
    impl: Callable[[M], str],
    *,
    name: str,
    description: str,
    args_schema: type[M],
    toolkit: str,
    risk: ToolRisk,
    frame: bool = False,
    max_output_chars: int | None = DEFAULT_MAX_OUTPUT_CHARS,
    requires_approval: bool | ApprovalPredicate = False,
    source: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    approval_payload: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> BaseTool:
    """Turn `impl(args) -> str` into a builtin tool.

    Failures come back to the model as `"Error: ..."` results: a `ToolException` with its message,
    any other exception with its type only (details go to the log). The result is capped at
    `max_output_chars`, then framed when `frame` is set; `requires_approval` wraps the tool with
    `create_tool_approval_wrapper`, showing the reviewer `approval_payload`.

    Raises:
        ConfigurationError: If `max_output_chars` is not positive.
    """

    positive(max_output_chars, "max_output_chars")

    def run(**kwargs: Any) -> str:
        try:
            args = args_schema.model_validate(kwargs)
            text = impl(args)
        except ToolException:
            raise
        except ValidationError as exc:
            raise ToolException(_validation_text(exc)) from None
        except OptionalDependencyError as exc:
            raise ToolException(str(exc)) from None
        except Exception as exc:
            get_logger("tools").error("Tool %s failed", name, exc_info=True)
            raise ToolException(f"{type(exc).__name__}: the tool failed; see the log") from None
        _refuse_cut_page(text, max_output_chars)
        text = cap_text(text, max_output_chars)
        return frame_tool_output(text, tool=name, source=source) if frame else text

    tool_metadata: dict[str, Any] = {
        **(metadata or {}),
        TOOLKIT_METADATA_KEY: toolkit,
        RISK_METADATA_KEY: risk.model_dump(exclude_none=True),
    }
    if frame:
        tool_metadata[FRAMED_METADATA_KEY] = True
    tool: BaseTool = StructuredTool.from_function(
        func=run,
        name=name,
        description=description,
        args_schema=args_schema,
        handle_tool_error=lambda exc: f"Error: {exc}",
        handle_validation_error=_invalid_arguments_text,
        metadata=tool_metadata,
        tags=[f"awa:toolkit:{toolkit}"],
    )
    if requires_approval is False:
        return tool
    gated = create_tool_approval_wrapper(
        tool,
        requires_approval=_always if requires_approval is True else requires_approval,
        build_interrupt_payload=approval_payload,
    )
    return gated.model_copy(
        update={
            "metadata": {**tool_metadata, APPROVAL_METADATA_KEY: True},
            "tags": list(tool.tags or ()),
        }
    )


ToolSpecs = Mapping[str, tuple[Callable[[Any], str], type[ToolArgs], str]]
"""Tools of a toolkit by name: the function, its arguments model and its description."""


def toolkit_tools(
    toolkit: str,
    names: Sequence[str],
    specs: ToolSpecs,
    *,
    requires_approval: bool = True,
    framed: Container[str] = (),
    limits: Mapping[str, int] | None = None,
    **options: Any,
) -> list[BaseTool]:
    """Build the catalog tools `names` of `toolkit` from `specs`.

    Risk and approval come from each tool's catalog entry; with `requires_approval` false no
    tool waits and the descriptions drop "Needs human approval.". Tools in `framed` are framed
    as data, `limits` raise `max_output_chars` per tool, `options` go to `build_tool`.
    """

    from awa.toolkits._catalog import catalog

    tools: list[BaseTool] = []
    for name in names:
        function, schema, description = specs[name]
        entry = catalog().get(name)
        gated = requires_approval and entry.requires_approval
        own = (
            {**options, "max_output_chars": limits[name]} if limits and name in limits else options
        )
        tools.append(
            build_tool(
                function,
                name=name,
                description=description
                if gated
                else description.replace(" Needs human approval.", ""),
                args_schema=schema,
                toolkit=toolkit,
                risk=entry.risk,
                frame=name in framed,
                requires_approval=gated,
                **own,
            )
        )
    return tools


def _refuse_cut_page(text: str, limit: int | None) -> None:
    """Refuse a JSON page of `items` longer than `limit`, naming a `limit` that fits.

    Cutting would lose the last items and the `next_page` token; a smaller page loses nothing.
    """

    if limit is None or len(text) <= limit or not text.startswith("{"):
        return
    try:
        data = json.loads(text)
    except ValueError:
        return
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) < 2:
        return
    fits = max(1, int(len(items) * limit / len(text) * 0.9))
    raise ToolException(
        f"the page of {len(items)} items has {len(text):,} characters, more than the {limit:,} "
        f"a result may have; call again with limit={fits} and the same page_token"
    )


def _always(_args: dict[str, Any]) -> bool:
    return True


def _invalid_arguments_text(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return f"Error: {_validation_text(exc)}"
    return f"Error: invalid arguments: {exc}"


def _validation_text(exc: ValidationError) -> str:
    return "invalid arguments: " + "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
        for error in exc.errors(include_input=False, include_url=False)
    )


def dig(data: Any, path: str) -> Any:
    """The value at a dotted `path` of JSON data (`"data.items"`, `"results.-1.id"`), or `None`.

    List positions are integers (negative counts from the end); `""` is `data` itself.
    Keys that contain dots (`"@odata.nextLink"`) match as a whole, longest first.
    """

    parts = [part for part in path.split(".") if part]
    position = 0
    while position < len(parts):
        if isinstance(data, dict):
            for end in range(len(parts), position, -1):
                key = ".".join(parts[position:end])
                if key in data:
                    data, position = data[key], end
                    break
            else:
                return None
        elif isinstance(data, list) and parts[position].removeprefix("-").isdecimal():
            index = int(parts[position])
            if not -len(data) <= index < len(data):
                return None
            data, position = data[index], position + 1
        else:
            return None
    return data


def header_values(
    headers: Mapping[str, str] | None, headers_env: Mapping[str, str] | None
) -> tuple[dict[str, str], frozenset[str]]:
    """Resolve configured headers; returns them and the lower-cased secret header names.

    `headers` are literal values; `headers_env` maps a header name to the
    environment variable holding its value, read at call time.
    """

    resolved = dict(headers or {})
    for header, variable in (headers_env or {}).items():
        resolved[header] = env_value(variable, what=f"header {header}")
    return resolved, frozenset(name.lower() for name in (headers_env or {}))
