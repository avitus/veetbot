"""Per-attempt cost calculation from immutable model pricing."""

from __future__ import annotations

from decimal import Decimal

from agent_core.domain.messages import ModelPricing, ModelUsage

MILLION = Decimal(1_000_000)


def price_usage(usage: ModelUsage, pricing: ModelPricing) -> ModelUsage:
    """Return usage with exact Decimal cost and the catalog's typed source."""

    classified_input = usage.cached_input_tokens + usage.cache_write_input_tokens
    if classified_input > usage.input_tokens:
        raise ValueError("cached and cache-write tokens exceed total input tokens")
    ordinary_input = usage.input_tokens - classified_input
    cost = Decimal(ordinary_input) * pricing.input_per_mtok
    cost += Decimal(usage.cached_input_tokens) * pricing.cached_input_per_mtok
    cache_write_price = (
        pricing.input_per_mtok
        if pricing.cache_write_per_mtok is None
        else pricing.cache_write_per_mtok
    )
    cost += Decimal(usage.cache_write_input_tokens) * cache_write_price

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
