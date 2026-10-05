"""Cost tracking — token counts and dollar costs per session.

Tracks cumulative input/output tokens and estimated dollar costs using
provider model metadata and reported response usage when available.
"""

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypedDict

from nova.model_metadata import get_model_metadata


class UsageDelta(TypedDict, total=False):
    """One response's worth of usage, ready to splat into CostTracker.add_usage.

    `total=False` because not all fields are always present — add_usage()
    supplies its own defaults.
    """

    input_tokens: int
    output_tokens: int
    input_cost: float
    output_cost: float
    total_cost: float
    cache_read_tokens: int
    cache_write_tokens: int


# Default pricing for unknown models (cheap model assumption)
_DEFAULT_PRICING = {"input": 0.10, "output": 0.30}


@dataclass
class UsageSnapshot:
    """Immutable snapshot of usage at a point in time."""

    input_tokens: int = 0
    output_tokens: int = 0
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_cost: float = 0.0
    cache_write_cost: float = 0.0
    reported_total_cost: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def total_cost(self) -> float:
        if self.reported_total_cost is not None:
            return self.reported_total_cost
        return self.input_cost + self.output_cost + self.cache_read_cost + self.cache_write_cost


@dataclass
class CostTracker:
    """Tracks cumulative token usage and dollar costs for a session.

    Usage:
        tracker = CostTracker(model="qwen/qwen3.6-flash")
        tracker.add_usage(input_tokens=1000, output_tokens=500)
        # Or with OpenRouter header costs:
        tracker.add_usage(input_tokens=1000, output_tokens=500,
                          input_cost=0.00003, output_cost=0.000045)
        print(tracker.total.total_cost)  # 0.000075
    """

    model: str = ""
    _usage: UsageSnapshot = field(default_factory=UsageSnapshot)
    _reported_total_cost: float | None = None
    # Sub-agents merge into their parent's tracker from worker threads.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        input_cost: float | None = None,
        output_cost: float | None = None,
        total_cost: float | None = None,
    ) -> None:
        """Add usage from an API response.

        If costs are not provided, they are estimated from the model's
        pricing table.
        """
        if input_tokens < 0 or output_tokens < 0 or cache_read_tokens < 0 or cache_write_tokens < 0:
            raise ValueError("token counts cannot be negative")
        for cost in (input_cost, output_cost, total_cost):
            if cost is not None and cost < 0:
                raise ValueError("costs cannot be negative")

        with self._lock:
            self._add_usage_locked(
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
                input_cost,
                output_cost,
                total_cost,
            )

    def _add_usage_locked(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        input_cost: float | None,
        output_cost: float | None,
        total_cost: float | None,
    ) -> None:
        estimated: dict[str, float] = {}
        if total_cost is not None:
            self._reported_total_cost = (self._reported_total_cost or 0.0) + total_cost
            input_cost = 0.0
            output_cost = 0.0
        elif input_cost is None or output_cost is None:
            estimated = self._estimate_cost(
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
            )
            if input_cost is None:
                input_cost = estimated["input"]
            if output_cost is None:
                output_cost = estimated["output"]

        self._usage = UsageSnapshot(
            input_tokens=self._usage.input_tokens + input_tokens,
            output_tokens=self._usage.output_tokens + output_tokens,
            input_cost=self._usage.input_cost + input_cost,
            output_cost=self._usage.output_cost + output_cost,
            cache_read_tokens=self._usage.cache_read_tokens + cache_read_tokens,
            cache_write_tokens=self._usage.cache_write_tokens + cache_write_tokens,
            cache_read_cost=self._usage.cache_read_cost + estimated.get("cache_read", 0.0)
            if total_cost is None
            else self._usage.cache_read_cost,
            cache_write_cost=self._usage.cache_write_cost + estimated.get("cache_write", 0.0)
            if total_cost is None
            else self._usage.cache_write_cost,
            reported_total_cost=self._reported_total_cost,
        )

    def merge(self, other: UsageSnapshot) -> None:
        """Add another tracker's accumulated usage, keeping its priced costs."""
        with self._lock:
            if other.reported_total_cost is not None:
                self._reported_total_cost = (
                    self._reported_total_cost or 0.0
                ) + other.reported_total_cost
            u = self._usage
            self._usage = UsageSnapshot(
                input_tokens=u.input_tokens + other.input_tokens,
                output_tokens=u.output_tokens + other.output_tokens,
                input_cost=u.input_cost + other.input_cost,
                output_cost=u.output_cost + other.output_cost,
                cache_read_tokens=u.cache_read_tokens + other.cache_read_tokens,
                cache_write_tokens=u.cache_write_tokens + other.cache_write_tokens,
                cache_read_cost=u.cache_read_cost + other.cache_read_cost,
                cache_write_cost=u.cache_write_cost + other.cache_write_cost,
                reported_total_cost=self._reported_total_cost,
            )

    def _estimate_cost(
        self, input_tokens: int, output_tokens: int, cache_read_tokens: int, cache_write_tokens: int
    ) -> dict[str, float]:
        """Estimate dollar cost from token counts using model pricing."""
        metadata = get_model_metadata(self.model)
        input_price = metadata.input_price_per_million if metadata else None
        output_price = metadata.output_price_per_million if metadata else None
        cache_read_price = metadata.cache_read_price_per_million if metadata else None
        cache_write_price = metadata.cache_write_price_per_million if metadata else None
        if input_price is None or output_price is None:
            input_price = input_price if input_price is not None else _DEFAULT_PRICING["input"]
            output_price = output_price if output_price is not None else _DEFAULT_PRICING["output"]
        cache_read_price = cache_read_price if cache_read_price is not None else input_price
        cache_write_price = cache_write_price if cache_write_price is not None else input_price
        return {
            "input": max(0, input_tokens - cache_read_tokens - cache_write_tokens)
            * input_price
            / 1_000_000,
            "output": output_tokens * output_price / 1_000_000,
            "cache_read": cache_read_tokens * cache_read_price / 1_000_000,
            "cache_write": cache_write_tokens * cache_write_price / 1_000_000,
        }

    @property
    def total(self) -> UsageSnapshot:
        """Return the current cumulative usage snapshot."""
        return self._usage

    def reset(self) -> None:
        """Reset the tracker to zero."""
        with self._lock:
            self._usage = UsageSnapshot()
            self._reported_total_cost = None

    def format_summary(self) -> str:
        """Return a human-readable usage summary."""
        t = self.total
        lines = [
            f"Tokens: {t.total_tokens:,} total ({t.input_tokens:,} in, {t.output_tokens:,} out)",
        ]
        if t.cache_read_tokens or t.cache_write_tokens:
            lines.append(f"Cache: {t.cache_read_tokens:,} read, {t.cache_write_tokens:,} written")
        total_cost = (
            self._reported_total_cost if self._reported_total_cost is not None else t.total_cost
        )
        if total_cost > 0:
            lines.append(
                f"Cost: ${total_cost:.6f} (${t.input_cost:.6f} in, ${t.output_cost:.6f} out)"
            )
        return " | ".join(lines)


def extract_usage_from_response(response_data: dict) -> UsageDelta:
    """Extract token usage from an OpenRouter API response.

    OpenRouter returns usage in the response body under 'usage' key.
    Also checks for cost headers.

    Returns a UsageDelta with input_tokens, output_tokens, and optionally
    input_cost, output_cost from headers — splat-safe into add_usage().
    """
    usage = response_data.get("usage") or {}
    cache_read_tokens = usage.get("cache_read_input_tokens") or usage.get(
        "prompt_cache_hit_tokens", 0
    )
    prompt_details = usage.get("prompt_tokens_details")
    if not cache_read_tokens and isinstance(prompt_details, Mapping):
        cached_tokens = prompt_details.get("cached_tokens")
        if type(cached_tokens) is int and cached_tokens >= 0:
            cache_read_tokens = cached_tokens
    cache_read_tokens = cache_read_tokens or 0
    cache_write_tokens = usage.get(
        "cache_creation_input_tokens", usage.get("prompt_cache_miss_tokens", 0)
    )
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    if not input_tokens:
        input_tokens = cache_read_tokens + cache_write_tokens
    result: UsageDelta = {
        "input_tokens": input_tokens,
        "output_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
    }

    if "cost" in usage:
        # OpenRouter reports `cost` for the complete request.
        result["input_cost"] = usage["cost"]
        result["output_cost"] = 0.0
        result["total_cost"] = usage["cost"]

    return result
