"""Package configured connectors as `Toolbox` classes."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from awa._internal.upstream.tools import BaseTool
from awa.core.config import env_value
from awa.errors import ConfigurationError, MissingConfigurationError
from awa.tools.decorator import tool

_UNSET: Any = object()


def env_field(variable: str, *, default: Any = _UNSET, description: str | None = None) -> Any:
    """A `Toolbox` field read from the environment variable `variable` when not given.

    Use it with `SecretStr` for credentials. Without `default`, a missing variable raises
    `MissingConfigurationError` when the toolbox is created.
    """

    def read() -> Any:
        value = env_value(variable)
        if value is not None:
            return value
        if default is not _UNSET:
            return default
        raise MissingConfigurationError(
            f"Set the environment variable {variable} or pass the value explicitly"
        )

    return Field(
        default_factory=read,
        validate_default=True,
        description=description,
        json_schema_extra={"env": variable},
    )


class Toolbox(BaseModel):
    """Base class of configurable connectors: config fields plus `@tool_method` methods.

    `tools()` returns the methods as tools bound to this configuration, named `<prefix>_<method>`
    when the class declares a prefix. The configuration is validated once, frozen, and never part of
    a tool schema; `SecretStr` fields never show in a repr, a log or a schema.

    Example:
        ```python
        class Tickets(Toolbox, prefix="tickets"):
            base_url: str
            token: SecretStr = env_field("TICKETS_TOKEN")

            @tool_method(risk=["private_data"])
            def search(self, query: str) -> str:
                \"\"\"Search tickets by text.\"\"\"
                ...

            @tool_method(approval=True, risk=["external_action"])
            async def close(self, ticket_id: str, note: str) -> str:
                \"\"\"Close a ticket with a note to the customer.\"\"\"
                ...

        tools = Tickets(base_url="https://tickets.example.com").tools()
        assert [t.name for t in tools] == ["tickets_search", "tickets_close"]
        ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    tool_prefix: ClassVar[str | None] = None
    """Prefix of the tool names; set with `class X(Toolbox, prefix="x")`."""

    _tools: tuple[BaseTool, ...] | None = PrivateAttr(default=None)

    def __init_subclass__(cls, prefix: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if prefix is not None:
            cls.tool_prefix = prefix

    @classmethod
    def tool_method_names(cls) -> list[str]:
        """Names of the methods marked with `tool_method`, in definition order."""

        names: list[str] = []
        for klass in reversed(cls.__mro__):
            for attribute, value in vars(klass).items():
                marked = getattr(value, "__awa_tool__", None) is not None
                if marked and attribute not in names:
                    names.append(attribute)
                elif not marked and attribute in names:
                    names.remove(attribute)
        return names

    def tools(self) -> list[BaseTool]:
        """The marked methods as tools bound to this instance (built once, then cached).

        Raises:
            ConfigurationError: If the class marks no methods, or a method's tool cannot be built.
        """

        if self._tools is None:
            names = self.tool_method_names()
            if not names:
                raise ConfigurationError(
                    f"{type(self).__name__} has no @tool_method methods; decorate the methods "
                    "the model may call"
                )
            built: list[BaseTool] = []
            for attribute in names:
                method = getattr(self, attribute)
                options: dict[str, Any] = dict(getattr(method, "__awa_tool__", {}))
                base_name = options.pop("name", None) or attribute
                full_name = f"{self.tool_prefix}_{base_name}" if self.tool_prefix else base_name
                built.append(tool(method, name=full_name, **options))
            self._tools = tuple(built)
        return list(self._tools)


__all__ = ["Toolbox", "env_field"]
