"""The requested hosted models resolve with their supported, priced limits."""

from decimal import Decimal

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.config import PACKAGE_ROOT
from agent_core.domain.messages import Capability
from agent_core.model.registry import ProviderRegistry, StaticModelRouter
from tests.contract.support import NOW


@pytest.mark.parametrize(
    ("policy", "provider", "model", "context_window", "cached_input"),
    [
        ("balanced", "openai", "gpt-6-astra", 272000, "1.00"),
        ("flagship", "anthropic", "claude-fable-5-1", 1000000, "0.25"),
        ("astra", "openai", "gpt-6-astra", 272000, "1.00"),
        ("fable", "anthropic", "claude-fable-5-1", 1000000, "0.25"),
    ],
)
async def test_requested_models_resolve_with_verified_pricing_and_limits(
    policy: str,
    provider: str,
    model: str,
    context_window: int,
    cached_input: str,
) -> None:
    router = StaticModelRouter(
        ProviderRegistry.load(PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS),
        FixedClock(NOW),
    )

    resolved = await router.resolve(
        policy,
        tenant_id="tenant-a",
        required=frozenset({Capability.STRUCTURED_OUTPUT, Capability.STREAMING}),
    )

    assert (resolved.provider, resolved.model) == (provider, model)
    assert resolved.limits.context_window_tokens == context_window
    assert resolved.limits.max_output_tokens == 128000
    assert resolved.pricing.input_per_mtok == Decimal("10.00")
    assert resolved.pricing.cached_input_per_mtok == Decimal(cached_input)
    assert resolved.pricing.cache_write_per_mtok == Decimal("12.50")
    assert resolved.pricing.output_per_mtok == Decimal("50.00")
    assert resolved.pricing.reasoning_priced_separately is False
