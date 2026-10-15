"""Blueprint model for the run limits (data only)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field, field_validator

from awa.blueprints._automation import _Duration
from awa.blueprints._schema_spec import _Spec
from awa.contracts.usage import UsageLimits

if TYPE_CHECKING:
    from awa.contracts.usage import PriceTable


def duration_seconds(value: _Duration, what: str = "max_duration") -> float | None:
    from awa._internal.util import to_timedelta

    duration = to_timedelta(value, what=what)
    return None if duration is None else duration.total_seconds()


class LimitsBlueprint(_Spec):
    """What one run may use at most; a run that reaches a limit stops and fails.

    The limits cover the whole run tree and apply wherever the blueprint runs (CLI and web
    app); a command line option can only make them stricter. `max_cost` is in the currency of
    `prices` and needs a price for every model used; `max_duration` reads like `5m`.
    """

    max_requests: int | None = Field(default=None, ge=1)
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_cost: float | None = Field(default=None, gt=0)
    max_duration: _Duration = None

    @field_validator("max_duration")
    @classmethod
    def _positive(cls, value: _Duration) -> _Duration:
        seconds = duration_seconds(value)
        if seconds is not None and not seconds > 0:
            raise ValueError("max_duration must be positive")
        return value

    def usage_limits(self, prices: PriceTable | None = None) -> UsageLimits:
        """The `UsageLimits` these limits describe (`prices` price `max_cost`)."""

        return UsageLimits(
            max_requests=self.max_requests,
            max_total_tokens=self.max_total_tokens,
            max_tool_calls=self.max_tool_calls,
            max_cost=self.max_cost,
            prices=prices,
            max_duration=duration_seconds(self.max_duration),
        )
