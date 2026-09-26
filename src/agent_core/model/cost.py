"""Per-attempt cost calculation from immutable model pricing."""

from __future__ import annotations

from decimal import Decimal

from agent_core.domain.messages import CacheHints, ModelPricing, ModelUsage

MILLION = Decimal(1_000_000)


def price_usage(usage: ModelUsage, pricing: ModelPricing) -> ModelUsage:
    """Return usage with exact Decimal cost and the catalog's typed source."""

    classified_input = usage.cached_input_tokens + usage.cache_write_input_tokens
    if classified_input > usage.input_tokens:
        raise ValueError("cached and cache-write tokens exceed total input tokens")
    if usage.cache_write_1h_input_tokens > usage.cache_write_input_tokens:
        raise ValueError("one-hour cache-write tokens exceed cache-write tokens")
    ordinary_input = usage.input_tokens - classified_input
    cost = Decimal(ordinary_input) * pricing.input_per_mtok
    cost += Decimal(usage.cached_input_tokens) * pricing.cached_input_per_mtok
    cache_write_price = (
        pricing.input_per_mtok
        if pricing.cache_write_per_mtok is None
        else pricing.cache_write_per_mtok
    )
    five_minute_writes = usage.cache_write_input_tokens - usage.cache_write_1h_input_tokens
    cost += Decimal(five_minute_writes) * cache_write_price
    if usage.cache_write_1h_input_tokens:
        # Adapters request the one-hour TTL only when it is priced (ADR-0132).
        if pricing.cache_write_1h_per_mtok is None:
            raise ValueError("one-hour cache writes have no price")
        cost += Decimal(usage.cache_write_1h_input_tokens) * pricing.cache_write_1h_per_mtok

    priced_output = usage.output_tokens
    if pricing.reasoning_priced_separately and usage.reasoning_tokens is not None:
        if usage.reasoning_tokens > usage.output_tokens:
            raise ValueError("reasoning tokens exceed total output tokens")
        priced_output -= usage.reasoning_tokens
        reasoning_price = (
            pricing.output_per_mtok
            if pricing.reasoning_per_mtok is None
            else pricing.reasoning_per_mtok
        )
        cost += Decimal(usage.reasoning_tokens) * reasoning_price
    cost += Decimal(priced_output) * pricing.output_per_mtok
    return usage.model_copy(
        update={"cost": cost / MILLION, "cost_source": pricing.source},
        deep=True,
    )


def highest_input_rate(pricing: ModelPricing, cache_hints: CacheHints | None = None) -> Decimal:
    """The most one input token of a request can cost, for worst-case reservations.

    A one-hour cache write is the dearest input token, and a request incurs it
    only when a breakpoint asks for that TTL and the model prices it.
    """

    rates = [
        pricing.input_per_mtok,
        pricing.cached_input_per_mtok,
        pricing.cache_write_per_mtok or Decimal(0),
    ]
    if pricing.cache_write_1h_per_mtok is not None and any(
        hint.ttl == "1h" for hint in (cache_hints.breakpoints if cache_hints else ())
    ):
        rates.append(pricing.cache_write_1h_per_mtok)
    return max(rates)
