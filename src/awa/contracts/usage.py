"""Token and request accounting, and the limits a run can be held to."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator
from typing_extensions import TypedDict, Unpack

from awa.errors import ConfigurationError, MissingPriceError, UsageLimitExceededError

if TYPE_CHECKING:
    from awa.contracts.events import ModelResponseEvent

UNKNOWN_MODEL = "unknown"
"""Key used in `Usage.by_model` when neither the model nor its response names the model."""

_PROVIDER_NAME = re.compile(r"[a-z][a-z0-9_]*")
"""What a LangChain provider prefix looks like (`openai`, `google_genai`, ...)."""


class _ModelCallCounts(TypedDict, total=False):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    tool_calls: int


class Usage(BaseModel):
    """Accumulated resource usage of a run.

    Mutable on purpose: one instance is shared by every agent in a run tree, so limits apply to
    the run as a whole. Use `snapshot()` for an independent copy.

    Attributes:
        tool_calls: Number of tool calls the models requested.
        by_model: Per-model breakdown, keyed by the name the chat model is configured with
            (`my-model` for `azure_openai:my-model`), else the name the provider reported
            (`UNKNOWN_MODEL` when there is neither).
    """

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls: int = 0
    by_agent: dict[str, Usage] = Field(default_factory=dict)
    by_model: dict[str, Usage] = Field(default_factory=dict)
    # Model calls started but not yet recorded; lets `max_requests` hold exactly
    # when agents of one run tree call models concurrently.
    _pending_requests: int = PrivateAttr(default=0)
    # The usage this one also counts into (see `under`).
    _pool: Usage | None = PrivateAttr(default=None)

    @classmethod
    def under(cls, pool: Usage) -> Usage:
        """A usage that counts every model call into `pool` too.

        `UsageLimits` are checked against the outermost pool, so runs under one pool are bounded
        together (strictly for `max_requests`) while each keeps its own numbers.
        """

        usage = cls()
        usage._pool = pool
        return usage

    @property
    def pool(self) -> Usage:
        """The outermost usage this one counts into (itself when it has none)."""

        return self if self._pool is None else self._pool.pool

    @property
    def requests_started(self) -> int:
        """Model calls made or in flight, in the whole pool."""

        pool = self.pool
        return pool.requests + pool._pending_requests

    @property
    def own_requests_started(self) -> int:
        """Model calls made or in flight, counted in this usage itself (not its pool)."""

        return self.requests + self._pending_requests

    def request_started(self) -> None:
        """Note a model call in flight (until `request_ended`); counts in the pool too."""

        self._pending_requests += 1
        if self._pool is not None:
            self._pool.request_started()

    def request_ended(self) -> None:
        """Note that a call noted by `request_started` has ended (its usage is recorded apart)."""

        self._pending_requests -= 1
        if self._pool is not None:
            self._pool.request_ended()

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens."""

        return self.input_tokens + self.output_tokens

    def record_model_call(
        self, agent: str, *, model: str | None = None, **counts: Unpack[_ModelCallCounts]
    ) -> None:
        """Add one model call to the totals and to the agent and model breakdowns."""

        targets = (
            self,
            self.by_agent.setdefault(agent, Usage()),
            self.by_model.setdefault(model or UNKNOWN_MODEL, Usage()),
        )
        for target in targets:
            target.requests += 1
            for name, value in counts.items():
                setattr(target, name, getattr(target, name) + value)
        if self._pool is not None:
            self._pool.record_model_call(agent, model=model, **counts)

    def record_usage_metadata(
        self,
        agent: str,
        usage_metadata: Mapping[str, Any] | None,
        *,
        tool_calls: int = 0,
        model: str | None = None,
    ) -> None:
        """Add one model call from a LangChain `usage_metadata` mapping, even a partial one."""

        metadata = usage_metadata or {}
        cache_read, cache_write = cache_tokens(metadata)
        self.record_model_call(
            agent,
            model=model,
            input_tokens=int(metadata.get("input_tokens") or 0),
            output_tokens=int(metadata.get("output_tokens") or 0),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            tool_calls=tool_calls,
        )

    def merge(self, other: Usage) -> None:
        """Add every counter of `other` (including its breakdowns) into this one."""

        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.tool_calls += other.tool_calls
        for agent, usage in other.by_agent.items():
            self.by_agent.setdefault(agent, Usage()).merge(usage)
        for model, usage in other.by_model.items():
            self.by_model.setdefault(model, Usage()).merge(usage)

    def cost(self, prices: PriceTable) -> float:
        """Return the cost of this usage under `prices`; see `PriceTable.cost`."""

        return prices.cost(self)

    def snapshot(self) -> Usage:
        """Return an independent deep copy."""

        pool, self._pool = self._pool, None
        try:
            return self.model_copy(deep=True)
        finally:
            self._pool = pool


_CACHE_TIERS = ("", "priority_", "flex_")
"""Prefixes of the cache keys of `input_token_details`: LangChain's OpenAI integration reports the
`priority` and `flex` service tiers as `priority_cache_read`, `flex_cache_creation`, ..."""


def cache_tokens(usage_metadata: Mapping[str, Any] | None) -> tuple[int, int]:
    """The cache read and cache write tokens of a LangChain `usage_metadata` mapping.

    Adds up every service tier (`cache_read`, `priority_cache_read`, `flex_cache_read`, and the
    same for `cache_creation`). Both are part of the mapping's `input_tokens`.
    """

    details = (usage_metadata or {}).get("input_token_details") or {}
    read = sum(int(details.get(f"{tier}cache_read") or 0) for tier in _CACHE_TIERS)
    write = sum(int(details.get(f"{tier}cache_creation") or 0) for tier in _CACHE_TIERS)
    return read, write


class ModelPrice(BaseModel):
    """Prices of one model, per million tokens, in a single currency of your choice.

    The cache prices default to `input_per_mtok`.
    """

    model_config = ConfigDict(frozen=True)

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    cache_read_per_mtok: float | None = Field(default=None, ge=0)
    cache_write_per_mtok: float | None = Field(default=None, ge=0)

    def cost(self, usage: Usage) -> float:
        """Return the cost of `usage`; cache tokens (part of `input_tokens`) at their own price."""

        cache_read = (
            self.input_per_mtok if self.cache_read_per_mtok is None else self.cache_read_per_mtok
        )
        cache_write = (
            self.input_per_mtok if self.cache_write_per_mtok is None else self.cache_write_per_mtok
        )
        plain_input = max(
            0, usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens
        )
        return (
            plain_input * self.input_per_mtok
            + usage.cache_read_tokens * cache_read
            + usage.cache_write_tokens * cache_write
            + usage.output_tokens * self.output_per_mtok
        ) / 1_000_000


class PriceTable(BaseModel):
    """Model prices used to compute what a run cost; awa ships none, supply the ones you pay.

    Keys are model names as `Usage.by_model` holds them. A key also matches longer names that
    start with it (`"my-model"` prices `"my-model-20260101"`), and a `provider:` prefix is ignored
    when the full name has no entry; a key never matches only the start of a provider name.
    Without `default`, pricing a model that has no entry raises `MissingPriceError`.

    Example:
        ```python
        prices = PriceTable(prices={"my-model": ModelPrice(input_per_mtok=3, output_per_mtok=15)})
        result = await team.run("...", usage_limits=UsageLimits(max_cost=0.50, prices=prices))
        print(result.usage.cost(prices))
        ```
    """

    model_config = ConfigDict(frozen=True)

    prices: dict[str, ModelPrice] = Field(default_factory=dict)
    default: ModelPrice | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, source: str = "prices") -> PriceTable:
        """Build a table from `{model: {input_per_mtok, output_per_mtok, ...}}`.

        The shape of a prices file (`awa eval --prices`) and of a blueprint's `prices:`. Unknown
        keys are refused, so a typo cannot silently price a model at the default.

        Raises:
            ConfigurationError: If `data` is not a mapping of models to valid entries.
        """

        if not isinstance(data, Mapping):
            raise ConfigurationError(f"{source}: expected a mapping of model names to prices")
        known = set(ModelPrice.model_fields)
        prices: dict[str, ModelPrice] = {}
        for model, entry in data.items():
            if not isinstance(entry, Mapping):
                raise ConfigurationError(
                    f"{source}: model {model!r} needs input_per_mtok and output_per_mtok"
                )
            unknown = sorted(str(key) for key in entry if key not in known)
            if unknown:
                raise ConfigurationError(
                    f"{source}: model {model!r} has unknown key(s) {', '.join(unknown)}; "
                    f"use {', '.join(sorted(known))}"
                )
            try:
                prices[str(model)] = ModelPrice.model_validate(dict(entry))
            except ValidationError as exc:
                problem = "; ".join(
                    f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors()
                )
                raise ConfigurationError(f"{source}: model {model!r}: {problem}") from None
        return cls(prices=prices)

    @classmethod
    def from_yaml(cls, path: str | Path) -> PriceTable:
        """Load a prices file (YAML or JSON) in the shape `from_mapping` takes.

        Raises:
            ConfigurationError: If the file cannot be read or does not hold valid prices.
        """

        import yaml

        target = Path(path)
        try:
            data = yaml.safe_load(target.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"Cannot read prices file {str(target)!r}: {exc}") from None
        if not isinstance(data, Mapping) or not data:
            raise ConfigurationError(
                f"Prices file {str(target)!r} must map model names to "
                "{input_per_mtok: ..., output_per_mtok: ...}"
            )
        return cls.from_mapping(data, source=f"Prices file {str(target)!r}")

    def price_for(self, model: str) -> ModelPrice:
        """Return the price that applies to `model`.

        Raises:
            MissingPriceError: If no entry matches and there is no `default`.
        """

        # The full name first: model ids may contain ':' themselves (Bedrock's
        # "...-v1:0", Ollama's "name:tag"), so it is not always a provider prefix.
        # But a key never matches just the start of a provider name: "p" or
        # "llama" must not price "provider:my-model" or "llamacpp:qwen3".
        head, colon, rest = model.partition(":")
        provider = len(head) if colon and _PROVIDER_NAME.fullmatch(head) else 0
        candidates = ((model, provider), (rest, 0)) if colon else ((model, 0),)
        for name, min_key_length in candidates:
            if name in self.prices:
                return self.prices[name]
            matches = [
                key for key in self.prices if len(key) >= min_key_length and name.startswith(key)
            ]
            if matches:
                return self.prices[max(matches, key=len)]
        if self.default is not None:
            return self.default
        raise MissingPriceError(
            f"No price for model {model!r}; add it to the PriceTable or set a default"
        )

    def cost(self, usage: Usage) -> float:
        """Return the total cost of `usage`, summed over its per-model breakdown.

        Raises:
            MissingPriceError: If a model that consumed tokens has no price.
        """

        return sum(
            self.price_for(model).cost(model_usage)
            for model, model_usage in usage.by_model.items()
            if model_usage.input_tokens or model_usage.output_tokens
        )


def cost_or_none(usage: Usage, prices: PriceTable | None) -> float | None:
    """What `usage` cost under `prices`; `None` without prices or when a model used has no price."""

    if prices is None:
        return None
    try:
        return prices.cost(usage)
    except MissingPriceError:
        return None


def model_call_cost(event: ModelResponseEvent, prices: PriceTable | None) -> float | None:
    """What the model call of `event` cost under `prices`, or `None` as in `cost_or_none`."""

    usage = Usage()
    usage.record_model_call(
        event.agent,
        model=event.model,
        input_tokens=event.input_tokens,
        output_tokens=event.output_tokens,
        cache_read_tokens=event.cache_read_tokens,
        cache_write_tokens=event.cache_write_tokens,
    )
    return cost_or_none(usage, prices)


class UsageLimits(BaseModel):
    """Upper bounds for a run.

    A field left `None` takes the run-tree ceiling (`AWA_RUN_MAX_*`); a limit can be set lower or
    higher than the ceiling, but not switched off. `max_requests` is enforced strictly, also for
    concurrent calls; token, tool-call and cost limits are checked after each response, so calls
    already in flight can overshoot by their own usage.

    Attributes:
        max_cost: Maximum cost, in the currency of `prices` (required with it).
        max_duration: Maximum wall-clock seconds of the whole run tree; when it expires the tree
            is cancelled and `RunTimeoutError` raised, like the `timeout` argument of `run`.
        max_depth: Maximum nesting depth (the top-level run has depth 0); a deeper nested run
            is refused with `UsageLimitExceededError`.
        per_tool_limits: Maximum calls per tool name over the run tree, for example
            `{"consult_peer": 6}`; a call beyond it gets an error result instead.
    """

    max_requests: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_tool_calls: int | None = None
    max_cost: float | None = Field(default=None, ge=0)
    prices: PriceTable | None = None
    max_duration: float | None = Field(default=None, gt=0)
    max_depth: int | None = Field(default=None, ge=1)
    per_tool_limits: dict[str, int] | None = None

    @model_validator(mode="after")
    def _require_prices(self) -> UsageLimits:
        if self.max_cost is not None and self.prices is None:
            raise ValueError("max_cost requires a PriceTable in `prices`")
        return self

    def check(self, usage: Usage, *, pooled: bool = True) -> None:
        """Raise `UsageLimitExceededError` if `usage` (its whole pool) exceeds any limit.

        With `pooled=False` only `usage` itself is checked (a delegation's own budget).
        """

        usage = usage.pool if pooled else usage
        checks = (
            ("requests", usage.requests, self.max_requests),
            ("input_tokens", usage.input_tokens, self.max_input_tokens),
            ("output_tokens", usage.output_tokens, self.max_output_tokens),
            ("total_tokens", usage.total_tokens, self.max_total_tokens),
            ("tool_calls", usage.tool_calls, self.max_tool_calls),
        )
        for label, value, limit in checks:
            if limit is not None and value > limit:
                raise UsageLimitExceededError(
                    f"Usage limit exceeded: {label}={value} > max_{label}={limit}",
                    details={"limit": f"max_{label}", "value": value, "maximum": limit},
                )
        if self.max_cost is not None and self.prices is not None:
            cost = self.prices.cost(usage)
            if cost > self.max_cost:
                raise UsageLimitExceededError(
                    f"Usage limit exceeded: cost={cost:.4f} > max_cost={self.max_cost:.4f}",
                    details={"limit": "max_cost", "value": cost, "maximum": self.max_cost},
                )


def merge_limits(requested: UsageLimits | None, ceiling: UsageLimits | None) -> UsageLimits | None:
    """Return limits no looser than `ceiling`: the smaller value of every field.

    Per-tool limits merge key by key, the smaller count winning. A cost limit under different
    price tables comes from the ceiling when it sets one.
    """

    if ceiling is None:
        return requested
    if requested is None:
        return ceiling
    values: dict[str, Any] = {}
    for name in (
        "max_requests",
        "max_input_tokens",
        "max_output_tokens",
        "max_total_tokens",
        "max_tool_calls",
        "max_duration",
        "max_depth",
    ):
        bounds = [v for v in (getattr(requested, name), getattr(ceiling, name)) if v is not None]
        values[name] = min(bounds, default=None)
    values["per_tool_limits"] = _merge_tool_limits(
        requested.per_tool_limits, ceiling.per_tool_limits
    )
    if (
        requested.max_cost is not None
        and ceiling.max_cost is not None
        and requested.prices == ceiling.prices
    ):
        values["max_cost"] = min(requested.max_cost, ceiling.max_cost)
        values["prices"] = ceiling.prices
    else:
        source = ceiling if ceiling.max_cost is not None else requested
        values["max_cost"] = source.max_cost
        values["prices"] = source.prices
    return UsageLimits(**values)


def _merge_tool_limits(
    requested: Mapping[str, int] | None, ceiling: Mapping[str, int] | None
) -> dict[str, int] | None:
    if requested is None or ceiling is None:
        return dict(requested or ceiling or {}) or None
    return {
        tool: min(v for v in (requested.get(tool), ceiling.get(tool)) if v is not None)
        for tool in requested.keys() | ceiling.keys()
    }


__all__ = ["UNKNOWN_MODEL", "ModelPrice", "PriceTable", "Usage", "UsageLimits", "merge_limits"]
