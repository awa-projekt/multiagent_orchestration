"""Request bodies of the run and resume endpoints."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from awa._internal.upstream.messages import convert_to_messages
from awa._internal.util import accepts_keyword
from awa.contracts.protocols import AgentInput
from awa.contracts.usage import UsageLimits
from awa.observability.audit import ACTOR_METADATA_KEY, REQUEST_ID_METADATA_KEY, TENANT_METADATA_KEY
from awa.server._auth import Principal
from awa.server._config import THREAD_ID_PATTERN
from awa.server._logging import current_request_id


def _check_messages(value: list[dict[str, Any]], field: str) -> None:
    try:
        convert_to_messages(value)
    except Exception as exc:
        raise ValueError(f"{field} is not a valid list of messages: {exc}") from None


class RunRequest(BaseModel):
    """Body of `POST /v1/systems/{name}/runs`.

    Attributes:
        input: A prompt, or a list of `{"role": ..., "content": ...}` messages.
        deps: Run dependencies; only keys the system allows (see `ServedSystem`).
        usage_limits: Limits for this run, capped by the system's ceiling.
    """

    model_config = ConfigDict(extra="forbid")

    input: str | list[dict[str, Any]]
    history: list[dict[str, Any]] | None = None
    thread_id: str | None = Field(default=None, pattern=THREAD_ID_PATTERN)
    deps: dict[str, Any] | None = None
    usage_limits: UsageLimits | None = None

    @field_validator("input")
    @classmethod
    def _check_input(cls, value: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        if not value:
            raise ValueError("input must not be empty")
        if isinstance(value, list):
            _check_messages(value, "input")
        return value

    @field_validator("history")
    @classmethod
    def _check_history(cls, value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if value:
            _check_messages(value, "history")
        return value


class ResumeRequest(BaseModel):
    """Body of `POST /v1/systems/{name}/threads/{thread_id}/resume`.

    Attributes:
        decision: The human decision, e.g. `{"action": "approve"}`; with several pending
            approvals, a mapping of interrupt id to decision.
        deps: Run dependencies; only keys the system allows (see `ServedSystem`).
        usage_limits: Limits for the resumed run, capped by the system's ceiling.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Any = Field(...)
    deps: dict[str, Any] | None = None
    usage_limits: UsageLimits | None = None


@dataclass(frozen=True)
class RunCall:
    input: AgentInput
    deps: Any
    thread_id: str | None
    usage_limits: UsageLimits | None
    history: list[dict[str, Any]] | None = None
    metadata: Mapping[str, Any] | None = None
    timeout: float | None = None

    def options(self, method: Callable[..., Any]) -> dict[str, Any]:
        """Keyword arguments for `method` (`run` or `stream`) of the system."""

        history = {"history": self.history} if self.history else {}
        return call_options(
            method,
            deps=self.deps,
            usage_limits=self.usage_limits,
            metadata=self.metadata,
            timeout=self.timeout,
            thread_id=self.thread_id,
            **history,
        )


def call_options(
    method: Callable[..., Any],
    *,
    deps: Any,
    usage_limits: UsageLimits | None,
    metadata: Mapping[str, Any] | None,
    timeout: float | None,
    **fixed: Any,
) -> dict[str, Any]:
    """Keyword arguments for `method` (`run`, `stream` or `resume`) of a served system: `fixed`,
    `deps` and `usage_limits` when set, `metadata=` and `timeout=` when `method` takes them."""

    options = dict(fixed)
    if deps is not None:
        options["deps"] = deps
    if usage_limits is not None:
        options["usage_limits"] = usage_limits
    if metadata and accepts_keyword(method, "metadata"):
        options["metadata"] = dict(metadata)
    if timeout is not None and accepts_keyword(method, "timeout"):
        options["timeout"] = timeout
    return options


def run_metadata(actor: Principal) -> dict[str, Any]:
    """Request context for the run: read by `AuditLog` and available in `RunContext.metadata`."""

    return {
        REQUEST_ID_METADATA_KEY: current_request_id(),
        TENANT_METADATA_KEY: actor.tenant,
        ACTOR_METADATA_KEY: actor.id,
    }
